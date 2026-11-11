"""直接读 NVMe SMART/Health Information Log（Log Page 0x02）。

为什么必须这么做：
    Windows 的 Get-StorageReliabilityCounter 对大多数 NVMe 盘不填 Wear 字段，
    直接返回 0 —— 那是「没数据」，不是「零磨损」。
    实测：GIGABYTE 工具显示 Intel SSDPEKNW010T8 剩余寿命 63%（Percentage Used 字段 = 37），
    而 Get-StorageReliabilityCounter 报 0%。差 37 个百分点。

正确途径是 IOCTL_STORAGE_QUERY_PROPERTY 走协议特定查询，
把 NVMe 的 SMART/Health 日志页原样读出来。

日志页布局（NVMe Base Spec 5.2.1，512 字节）：
    偏移 0-1    关键警告标志
    偏移 2-3    复合温度（开尔文）
    偏移 4      可用备用空间百分比
    偏移 5      备用空间阈值
    偏移 6      **Percentage Used（100 − 剩余寿命）**  ← 这就是磨损度
    偏移 24-39  已写入数据单元（1 单元 = 512000 字节）
    偏移 128-143 介质与数据完整性错误
    偏移 144-159 错误信息日志条目数
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

BS = chr(92)
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
STORAGE_DEVICE_PROTOCOL_SPECIFIC_PROPERTY = 50
PROPERTY_STANDARD_QUERY = 0
PROTOCOL_TYPE_NVME = 3
NVME_DATA_TYPE_LOG_PAGE = 2
NVME_SMART_HEALTH_LOG = 0x02
SMART_LOG_SIZE = 512


class STORAGE_PROTOCOL_SPECIFIC_DATA(ctypes.Structure):
    _fields_ = [
        ("ProtocolType", ctypes.c_uint32),
        ("DataType", ctypes.c_uint32),
        ("ProtocolDataRequestValue", ctypes.c_uint32),
        ("ProtocolDataRequestSubValue", ctypes.c_uint32),
        ("ProtocolDataOffset", ctypes.c_uint32),
        ("ProtocolDataLength", ctypes.c_uint32),
        ("FixedProtocolReturnData", ctypes.c_uint32),
        ("ProtocolDataRequestSubValue2", ctypes.c_uint32),
        ("ProtocolDataRequestSubValue3", ctypes.c_uint32),
        ("ProtocolDataRequestSubValue4", ctypes.c_uint32),
    ]


SPD_SIZE = ctypes.sizeof(STORAGE_PROTOCOL_SPECIFIC_DATA)   # 40
QUERY_HEADER = 8                                            # PropertyId(4) + QueryType(4)

kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
]
kernel32.DeviceIoControl.argtypes = [
    w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
    ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p,
]
kernel32.CloseHandle.argtypes = [w.HANDLE]

INVALID = w.HANDLE(-1).value
GENERIC_READ = 0x80000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3


def _device_path(device_id: str) -> str:
    return BS + BS + "." + BS + "PhysicalDrive" + str(device_id)


def read_nvme_smart(device_id: str) -> dict | None:
    """读一块 NVMe 盘的 SMART/Health 日志。

    返回 dict（键见下方），不是 NVMe 或读取失败返回 None。
    """
    path = _device_path(device_id)
    # 必须 GENERIC_READ，无权限打开在普通用户下会失败（err=5）
    h = kernel32.CreateFileW(path, GENERIC_READ, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID:
        return None

    try:
        in_size = QUERY_HEADER + SPD_SIZE
        out_size = QUERY_HEADER + SPD_SIZE + SMART_LOG_SIZE

        in_buf = ctypes.create_string_buffer(in_size)
        out_buf = ctypes.create_string_buffer(out_size)

        # STORAGE_PROPERTY_QUERY: PropertyId, QueryType
        ctypes.memmove(in_buf, ctypes.byref(ctypes.c_uint32(STORAGE_DEVICE_PROTOCOL_SPECIFIC_PROPERTY)), 4)
        ctypes.memmove(ctypes.byref(in_buf, 4), ctypes.byref(ctypes.c_uint32(PROPERTY_STANDARD_QUERY)), 4)

        spd = STORAGE_PROTOCOL_SPECIFIC_DATA()
        spd.ProtocolType = PROTOCOL_TYPE_NVME
        spd.DataType = NVME_DATA_TYPE_LOG_PAGE
        spd.ProtocolDataRequestValue = NVME_SMART_HEALTH_LOG
        spd.ProtocolDataRequestSubValue = 0
        spd.ProtocolDataOffset = SPD_SIZE
        spd.ProtocolDataLength = SMART_LOG_SIZE
        ctypes.memmove(ctypes.byref(in_buf, QUERY_HEADER), ctypes.byref(spd), SPD_SIZE)

        returned = w.DWORD(0)
        ok = kernel32.DeviceIoControl(
            h, IOCTL_STORAGE_QUERY_PROPERTY,
            in_buf, in_size,
            out_buf, out_size,
            ctypes.byref(returned), None,
        )
        if not ok:
            return None

        # 输出里的协议头。ProtocolDataOffset=40 是相对输出缓冲区起始的偏移，
        # 实测数据从 raw[QUERY_HEADER + 40] = raw[48] 开始。
        out_spd = STORAGE_PROTOCOL_SPECIFIC_DATA.from_buffer_copy(
            out_buf.raw[QUERY_HEADER:QUERY_HEADER + SPD_SIZE]
        )
        data_off = QUERY_HEADER + out_spd.ProtocolDataOffset
        if data_off + SMART_LOG_SIZE > len(out_buf.raw):
            data_off = QUERY_HEADER + SPD_SIZE
        raw = out_buf.raw[data_off:data_off + SMART_LOG_SIZE]
        if len(raw) < SMART_LOG_SIZE:
            return None

        def u16(off: int) -> int:
            return raw[off] | (raw[off + 1] << 8)

        def u64(off: int) -> int:
            return int.from_bytes(raw[off:off + 8], "little")

        # NVMe Base Spec 5.2.1 字段偏移（相对日志页起点）：
        #   0        Critical Warning
        #   1-2      Composite Temperature (Kelvin)
        #   3        Available Spare %
        #   4        Available Spare Threshold %
        #   5        Percentage Used %      <- 磨损度
        #   32-47    Data Units Read
        #   48-63    Data Units Written     (1 unit = 512000 bytes)
        #   128-143  Power On Hours
        #   160-175  Media and Data Integrity Errors
        #   176-191  Error Information Log Entries
        temp_k = u16(1)
        temp_c = round(temp_k - 273.15) if temp_k else None

        return {
            "wear": raw[5],                     # Percentage Used（剩余寿命 = 100 − 它），可超过 100
            "temperature": temp_c,
            "available_spare": raw[3],
            "spare_threshold": raw[4],
            "data_written_bytes": u64(48) * 512000,
            "power_on_hours": u64(128),
            "media_errors": u64(160),
            "error_log_entries": u64(176),
            "critical_warning": u16(0),
        }
    finally:
        kernel32.CloseHandle(h)


def is_nvme(device_id: str) -> bool:
    """通过读 SMART 日志是否成功来判断是不是 NVMe。"""
    return read_nvme_smart(device_id) is not None


if __name__ == "__main__":
    import json
    import sys

    ids = sys.argv[1:] or [str(i) for i in range(8)]
    for did in ids:
        r = read_nvme_smart(did)
        if r is None:
            print(f"dev{did}: not NVMe or access denied")
        else:
            print(f"dev{did}: {json.dumps(r, ensure_ascii=False)}")
