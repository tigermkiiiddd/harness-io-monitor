"""Windows 进程枚举与累计 I/O 计数（纯 ctypes，无第三方依赖）。

数据来源：
    CreateToolhelp32Snapshot   pid / ppid / exe 名
    QueryFullProcessImageNameW 完整 exe 路径
    NtQueryInformationProcess  命令行
    GetProcessTimes            进程创建时间（身份键，防 PID 复用串账）
    GetProcessIoCounters       进程生命周期累计读/写字节

注意：GetProcessIoCounters 返回的是进程启动至今的真实累计量，不是速率，
所以本模块不做积分，采样间隔只影响「采样粒度」，不影响总量精度。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
from dataclasses import dataclass

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260

_STAT_INFO_LENGTH_MISMATCH = 0xC0000004
_ProcessCommandLineInformation = 60


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * MAX_PATH),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", w.DWORD), ("dwHighDateTime", w.DWORD)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", w.USHORT),
        ("MaximumLength", w.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = w.BOOL
kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = w.BOOL
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.GetProcessIoCounters.argtypes = [w.HANDLE, ctypes.POINTER(IO_COUNTERS)]
kernel32.GetProcessIoCounters.restype = w.BOOL
kernel32.GetProcessTimes.argtypes = [
    w.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE,
    w.DWORD,
    w.LPWSTR,
    ctypes.POINTER(w.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
ntdll.NtQueryInformationProcess.argtypes = [
    w.HANDLE,
    w.ULONG,
    ctypes.c_void_p,
    w.ULONG,
    ctypes.POINTER(w.ULONG),
]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long


@dataclass(slots=True)
class ProcInfo:
    """一次采样里看到的单个进程。"""

    pid: int
    ppid: int
    exe: str          # 小写 basename，不含 .exe
    path: str         # 完整路径，取不到时为空串
    cmdline: str      # 命令行，取不到时为空串
    create_time: int  # FILETIME（100ns），0 表示取不到
    read_bytes: int   # 生命周期累计
    write_bytes: int  # 生命周期累计

    @property
    def key(self) -> tuple[int, int]:
        """身份键：PID 会被系统复用，必须带上创建时间。"""
        return (self.pid, self.create_time)


def _open(pid: int) -> int | None:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return h or None


def _read_io(h) -> tuple[int, int] | None:
    c = IO_COUNTERS()
    if not kernel32.GetProcessIoCounters(h, ctypes.byref(c)):
        return None
    return int(c.ReadTransferCount), int(c.WriteTransferCount)


def _read_create_time(h) -> int:
    created = FILETIME()
    exited = FILETIME()
    kern = FILETIME()
    user = FILETIME()
    ok = kernel32.GetProcessTimes(
        h,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kern),
        ctypes.byref(user),
    )
    if not ok:
        return 0
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def _read_path(h) -> str:
    buf = ctypes.create_unicode_buffer(32768)
    size = w.DWORD(len(buf))
    if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
        return ""
    return buf.value


def _read_cmdline(h) -> str:
    needed = w.ULONG(0)
    status = ntdll.NtQueryInformationProcess(
        h, _ProcessCommandLineInformation, None, 0, ctypes.byref(needed)
    )
    if (status & 0xFFFFFFFF) != _STAT_INFO_LENGTH_MISMATCH or not needed.value:
        return ""
    size = max(needed.value, 4096)
    buf = ctypes.create_string_buffer(size)
    status = ntdll.NtQueryInformationProcess(
        h, _ProcessCommandLineInformation, buf, size, ctypes.byref(needed)
    )
    if status != 0:
        return ""
    us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
    if not us.Buffer or us.Length == 0:
        return ""
    return ctypes.wstring_at(us.Buffer, us.Length // 2)


def _iter_pids() -> list[tuple[int, int, str]]:
    """返回 [(pid, ppid, exe_stem)]，exe_stem 已转小写、去掉 .exe。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot 失败")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out: list[tuple[int, int, str]] = []
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile or ""
            if name.lower().endswith(".exe"):
                name = name[:-4]
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), name.lower()))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return out
    finally:
        kernel32.CloseHandle(snap)


class Prober:
    """枚举 + 采计数。路径与命令行按 (pid, create_time) 缓存，进程消失后回收。"""

    def __init__(self, with_cmdline: bool = True) -> None:
        self.with_cmdline = with_cmdline
        self._path_cache: dict[tuple[int, int], str] = {}
        self._cmd_cache: dict[tuple[int, int], str] = {}

    def sample(self) -> list[ProcInfo]:
        result: list[ProcInfo] = []
        seen: set[tuple[int, int]] = set()
        for pid, ppid, exe in _iter_pids():
            if pid == 0:
                continue
            h = _open(pid)
            if not h:
                continue
            try:
                io = _read_io(h)
                if io is None:
                    continue
                ctime = _read_create_time(h)
                key = (pid, ctime)
                seen.add(key)

                path = self._path_cache.get(key)
                if path is None:
                    path = _read_path(h)
                    self._path_cache[key] = path

                if self.with_cmdline:
                    cmd = self._cmd_cache.get(key)
                    if cmd is None:
                        cmd = _read_cmdline(h)
                        self._cmd_cache[key] = cmd
                else:
                    cmd = ""

                result.append(
                    ProcInfo(
                        pid=pid,
                        ppid=ppid,
                        exe=exe,
                        path=path,
                        cmdline=cmd,
                        create_time=ctime,
                        read_bytes=io[0],
                        write_bytes=io[1],
                    )
                )
            except Exception:
                # 单个进程取不到就跳过，不能让一次采样整体失败
                continue
            finally:
                kernel32.CloseHandle(h)
        self._prune(seen)
        return result

    def final_read(self, pid: int) -> tuple[int, int] | None:
        """进程刚退出时的最后一次读数。

        Windows 在进程对象被回收前仍允许 OpenProcess + GetProcessIoCounters，
        所以进程从快照里消失后的短窗口内还能读到它一生累计的最终值。
        采样循环发现某个身份键消失时应该先调这个，再决定是否结算——
        否则「写完立刻退出」的进程会被记成 0。
        """
        h = _open(pid)
        if not h:
            return None
        try:
            return _read_io(h)
        finally:
            kernel32.CloseHandle(h)

    def _prune(self, seen: set[tuple[int, int]]) -> None:
        for cache in (self._path_cache, self._cmd_cache):
            for k in [k for k in cache if k not in seen]:
                del cache[k]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if __name__ == "__main__":
    p = Prober()
    procs = p.sample()
    print(f"进程数: {len(procs)}  管理员: {is_admin()}")
    procs.sort(key=lambda x: x.write_bytes, reverse=True)
    for x in procs[:15]:
        print(
            f"{x.write_bytes / 1048576:9.1f} MB 写 {x.read_bytes / 1048576:9.1f} MB 读  "
            f"{x.exe:<24} pid={x.pid:<7} ppid={x.ppid}"
        )
