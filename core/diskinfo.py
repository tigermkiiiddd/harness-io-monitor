"""纯 ctypes 物理盘枚举，替代 PowerShell 的 Get-PhysicalDisk。

为什么不用 PowerShell：程序用 pythonw 运行，每次起 PowerShell 进程都会
弹出一个空白控制台窗口；而且 Get-PhysicalDisk 实测经常返回空（界面显示「0 块盘」）。

做法：依次尝试打开 \\\\.\\PhysicalDrive0 … PhysicalDrive31（中间可能有空号，
打不开就跳过，不因一个失败而停），对每块盘发 IOCTL 取：
    型号      IOCTL_STORAGE_QUERY_PROPERTY + StorageDeviceProperty   (STORAGE_DEVICE_DESCRIPTOR)
    总线类型  同上，BusType 字段（NVMe=17、SATA=11、USB=7 …）
    容量      IOCTL_DISK_GET_LENGTH_INFO
    介质      IOCTL_STORAGE_QUERY_PROPERTY + StorageDeviceSeekPenaltyProperty
              IncursSeekPenalty=True → HDD，False → SSD；BusType=NVMe → NVMe

所有 IOCTL 失败都容错（字段留空/为 0），list_disks() 不抛异常。
解析逻辑拆成接收 bytes 的纯函数，便于不接触硬件的单元测试。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import struct
from typing import Callable

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
]
kernel32.DeviceIoControl.restype = w.BOOL
kernel32.DeviceIoControl.argtypes = [
    w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
    ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p,
]
kernel32.CloseHandle.restype = w.BOOL
kernel32.CloseHandle.argtypes = [w.HANDLE]

INVALID = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
PROPERTY_STANDARD_QUERY = 0
STORAGE_DEVICE_PROPERTY = 0            # StorageDeviceProperty
STORAGE_SEEK_PENALTY_PROPERTY = 7      # StorageDeviceSeekPenaltyProperty

MAX_PHYSICAL_DRIVES = 32               # PhysicalDrive0 … PhysicalDrive31
BUS_TYPE_NVME = 17                     # STORAGE_BUS_TYPE: BusTypeNvme

_DESCRIPTOR_HEADER = 36                # STORAGE_DEVICE_DESCRIPTOR 固定头长度（RawDeviceProperties 之前）


# ---------------------------------------------------------------- 纯函数（可单测）

def _cstr_at(buf: bytes, off: int) -> str:
    """从 buf[off] 读一个以 NUL 结尾的字符串；偏移为 0 或越界返回空串。"""
    if not off or off >= len(buf):
        return ""
    end = buf.find(b"\x00", off)
    if end < 0:
        end = len(buf)
    return buf[off:end].decode("ascii", errors="replace").strip()


def parse_storage_descriptor(buf: bytes) -> tuple[str, str, int]:
    """解析 STORAGE_DEVICE_DESCRIPTOR，返回 (vendor, product, bus_type)。

    布局（均为小端 DWORD）：
        12  VendorIdOffset
        16  ProductIdOffset
        28  BusType
    字符串位于 buf 内、由偏移指向。拿不到返回 ("", "", 0)。
    """
    if len(buf) < _DESCRIPTOR_HEADER:
        return "", "", 0
    vendor_off, product_off = struct.unpack_from("<II", buf, 12)
    bus_type = struct.unpack_from("<I", buf, 28)[0]
    return _cstr_at(buf, vendor_off), _cstr_at(buf, product_off), bus_type


def compose_name(vendor: str, product: str) -> str:
    """拼盘名：vendor 为空，或 product 已含 vendor（如 'INTEL' + 'INTEL SSD…'），只用 product。"""
    vendor = (vendor or "").strip()
    product = (product or "").strip()
    if not product:
        return vendor
    if not vendor:
        return product
    v, p = vendor.lower(), product.lower()
    if v == p or v in p:
        return product
    return f"{vendor} {product}"


def parse_seek_penalty(buf: bytes) -> bool | None:
    """解析 DEVICE_SEEK_PENALTY_DESCRIPTOR：偏移 8 处的 IncursSeekPenalty 字节。读不到返回 None。"""
    if len(buf) < 9:
        return None
    return buf[8] != 0


def parse_length(buf: bytes) -> int | None:
    """解析 GET_LENGTH_INFO 返回的 LARGE_INTEGER（8 字节小端）。读不到返回 None。"""
    if len(buf) < 8:
        return None
    return struct.unpack_from("<q", buf, 0)[0]


def media_type(bus_type: int, seek_penalty: bool | None) -> str:
    """NVMe 总线记 'NVMe'；否则按寻道惩罚判 HDD/SSD；拿不到记 ''。

    注意前端只把 media == 'HDD' 画成机械盘图标，其余都画 SSD 图标。
    """
    if bus_type == BUS_TYPE_NVME:
        return "NVMe"
    if seek_penalty is True:
        return "HDD"
    if seek_penalty is False:
        return "SSD"
    return ""


def collect_disks(
    probe: Callable[[int], dict | None],
    max_index: int = MAX_PHYSICAL_DRIVES,
) -> list[dict]:
    """依次探测 0..max_index-1，跳过 probe 返回 None 的空号（不因空号而停）。

    probe 抛异常也视作空号。probe 作为参数注入，便于用假盘测试枚举逻辑。
    """
    out: list[dict] = []
    for i in range(max_index):
        try:
            d = probe(i)
        except Exception:
            d = None
        if d:
            out.append(d)
    return out


# ---------------------------------------------------------------- 硬件访问（容错）

def _open(index: int, access: int) -> int | None:
    """打开 \\\\.\\PhysicalDriveN。打不开（空号/无权限）返回 None。"""
    try:
        h = kernel32.CreateFileW(
            rf"\\.\PhysicalDrive{index}", access,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
        )
    except Exception:
        return None
    if not h or h == INVALID:
        return None
    return h


def _close(h: int | None) -> None:
    if h:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass


def _ioctl(h: int, code: int, in_bytes: bytes, out_size: int) -> bytes | None:
    """发一个 IOCTL，返回实际返回的字节；失败返回 None。"""
    try:
        inbuf = ctypes.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
        outbuf = ctypes.create_string_buffer(out_size)
        returned = w.DWORD(0)
        ok = kernel32.DeviceIoControl(
            h, code, inbuf, len(in_bytes), outbuf, out_size,
            ctypes.byref(returned), None,
        )
        if not ok:
            return None
        return outbuf.raw[:returned.value]
    except Exception:
        return None


def _query_property(h: int, prop_id: int, out_size: int) -> bytes | None:
    """STORAGE_PROPERTY_QUERY：PropertyId(4) + QueryType(4) + AdditionalParameters(1，补齐到 12 字节)。"""
    query = struct.pack("<III", prop_id, PROPERTY_STANDARD_QUERY, 0)
    return _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, query, out_size)


def _length_of(h: int) -> int | None:
    buf = _ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, b"", 8)
    return parse_length(buf) if buf else None


def _probe(index: int) -> dict | None:
    """探测单块物理盘。打不开返回 None（空号）；打开了则尽量填字段，IOCTL 失败的字段留空。"""
    h = _open(index, 0)
    if h is None:
        return None
    vendor = product = ""
    bus = 0
    seek: bool | None = None
    size: int | None = None
    try:
        desc = _query_property(h, STORAGE_DEVICE_PROPERTY, 1024)
        if desc:
            vendor, product, bus = parse_storage_descriptor(desc)
        sp = _query_property(h, STORAGE_SEEK_PENALTY_PROPERTY, 16)
        if sp:
            seek = parse_seek_penalty(sp)
        size = _length_of(h)
    finally:
        _close(h)

    if size is None:
        # GET_LENGTH_INFO 需要读权限，access=0 的句柄可能被拒；降级用 GENERIC_READ 句柄再试一次
        h2 = _open(index, GENERIC_READ)
        if h2 is not None:
            try:
                size = _length_of(h2)
            finally:
                _close(h2)

    return {
        "name": compose_name(vendor, product) or f"PhysicalDrive{index}",
        "device_id": str(index),
        "media_type": media_type(bus, seek),
        "size_bytes": int(size or 0),
        "health": "",
    }


def list_disks() -> list[dict]:
    """枚举本机物理盘，返回 list[dict]，键与原 PowerShell 输出兼容：
    name / device_id(str 数字) / media_type / size_bytes / health（拿不到为 ""）。

    普通权限下 desired access=0 一般能查属性；拿不到时返回空列表，不抛异常。
    """
    try:
        return collect_disks(_probe)
    except Exception:
        return []
