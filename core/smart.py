"""读取 SSD/HDD 的健康度。

数据来源（全部 ctypes，不启动任何子进程，pythonw 下不会弹控制台窗口）：
    静态信息（盘名/型号/容量/介质）  core/diskinfo.py（IOCTL 枚举物理盘）
    动态寿命（NVMe）                 core/nvme.py（SMART/Health 日志页）
    HDD 的 ATA SMART                  暂不实现，保持 None

读 NVMe 的 SMART 日志需要管理员权限（GENERIC_READ），非管理员时 read_disks() 直接返回提示。

单位说明：
    wear            磨损百分比。NVMe 是 Percentage Used 字段（剩余寿命 = 100 − wear），0=全新，100=耗尽
    temperature     摄氏度
    power_on_hours  累计通电小时
"""

from __future__ import annotations

import ctypes
import json
import sys
from dataclasses import asdict, dataclass, field

from . import diskinfo


@dataclass
class DiskHealth:
    name: str = ""
    device_id: str = ""
    media_type: str = ""
    size_bytes: int = 0
    health_status: str = ""
    wear: int | None = None            # 磨损百分比，None = 读不到
    temperature: int | None = None
    temperature_max: int | None = None
    power_on_hours: int | None = None
    read_errors: int | None = None
    write_errors: int | None = None
    start_stop_cycles: int | None = None
    data_written_bytes: int | None = None   # 出厂至今物理写入量（SMART 记录）
    available_spare: int | None = None
    media_errors: int | None = None
    source: str = ""                   # 数据从哪来：nvme / reliability / none
    error: str = ""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# 静态元信息缓存（盘名/型号/容量/介质）。diskinfo.list_disks() 是 IOCTL 枚举，
# 单次约毫秒级，但盘符和型号不会变，仍缓存 10 分钟；动态指标（写入量/温度）走 NVMe IOCTL，约 2ms。
_STATIC_CACHE: list[dict] | None = None
_STATIC_CACHE_AT: float = 0.0
STATIC_TTL = 600.0            # 10 分钟刷新一次元信息


def _load_static() -> list[dict]:
    """用 diskinfo 枚举一次物理盘（型号/容量/介质）。

    枚举失败（返回空）时保留旧缓存，不把界面刷成「0 块盘」。
    """
    global _STATIC_CACHE, _STATIC_CACHE_AT
    import time as _t

    if _STATIC_CACHE and (_t.time() - _STATIC_CACHE_AT) < STATIC_TTL:
        return _STATIC_CACHE

    data = diskinfo.list_disks()
    if data:
        _STATIC_CACHE = data
        _STATIC_CACHE_AT = _t.time()
        return data
    return _STATIC_CACHE or []


def read_disks(fast: bool = False) -> list[DiskHealth]:
    """读所有物理盘的健康度。非管理员时会填 error 字段。

    fast=True 时只走 NVMe IOCTL（~2ms），不调 PowerShell。
    用于每秒轮询画曲线；完整信息（含 HDD 的 ATA SMART）用 fast=False。
    """
    if not is_admin():
        return [
            DiskHealth(
                name="(需要管理员权限)", source="none",
                error="读取 SMART 需要管理员权限",
            )
        ]

    if fast:
        return _read_disks_fast()

    out = [_from_static(d) for d in diskinfo.list_disks()]
    if not out:
        return [DiskHealth(error="没有读到物理盘")]
    for dh in out:
        _overlay_nvme(dh)
    out.sort(key=lambda x: x.device_id)
    return out


def _from_static(d: dict) -> DiskHealth:
    """diskinfo 的一条枚举结果 -> DiskHealth（动态字段留空，由 _overlay_nvme 填）。"""
    return DiskHealth(
        name=d.get("name") or "",
        device_id=str(d.get("device_id") or ""),
        media_type=d.get("media_type") or "",
        size_bytes=int(d.get("size_bytes") or 0),
        health_status=d.get("health") or "",
    )


def _overlay_nvme(dh: DiskHealth) -> None:
    """用 NVMe SMART/Health 日志填动态寿命字段。非 NVMe 或读不到则不动（保持 None）。

    wear 以 NVMe Percentage Used 字段为准（剩余寿命 = 100 − wear）；data_written_bytes 是出厂至今物理写入量，
    是寿命估算的基础（见 docs/rebuild-plan.md）。
    """
    try:
        from . import nvme as _nvme_mod
        nv = _nvme_mod.read_nvme_smart(dh.device_id)
    except Exception as exc:
        print(f"[smart] NVMe 读取失败 disk={dh.device_id}: {exc}")
        return
    if nv is None:
        return
    dh.wear = nv["wear"]
    if nv["temperature"] is not None:
        dh.temperature = nv["temperature"]
    dh.available_spare = nv["available_spare"]
    dh.media_errors = nv["media_errors"]
    dh.data_written_bytes = nv["data_written_bytes"]
    dh.power_on_hours = nv["power_on_hours"] or None
    dh.source = "nvme"


def _read_disks_fast() -> list[DiskHealth]:
    """只走 NVMe IOCTL 的快速路径，约 2ms；静态信息走缓存。HDD 会没有动态数据。"""
    out: list[DiskHealth] = []
    for d in _load_static():
        dh = _from_static(d)
        _overlay_nvme(dh)
        out.append(dh)
    return out


def _gui_python(exe: str) -> str:
    """若 exe 是 python.exe 且同目录有 pythonw.exe，返回 pythonw.exe（不冒控制台窗口）；否则原样返回。"""
    import os
    if os.path.basename(exe).lower() == "python.exe":
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pw):
            return pw
    return exe


def elevate_and_run(args: list[str]) -> bool:
    """以管理员身份重新启动自己。返回是否成功发起提权。

    提权重启优先用 pythonw.exe，避免提权后冒出控制台窗口。
    """
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = " ".join(args)
        else:
            exe = _gui_python(sys.executable)
            params = " ".join([f'"{sys.argv[0]}"', *args])
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1
        )
        return int(r) > 32
    except Exception:
        return False


if __name__ == "__main__":
    print(json.dumps(
        [asdict(d) for d in read_disks()],
        indent=2, ensure_ascii=False,
    ))
