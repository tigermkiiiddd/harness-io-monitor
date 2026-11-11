"""单实例锁。

为什么必须做：多个实例同时写同一个 SQLite 会互相踩，
WAL 锁竞争、数据覆盖、甚至崩溃。而且每个实例都在采样，
会把同一进程的 I/O 重复计（各自维护独立的内存状态）。

实现用「独占占用的锁文件」而不是 PID 猜谜：
    - 用 msvcrt.locking 对锁文件加独占锁，进程崩溃时由 OS 自动释放
    - 锁文件里写 PID 和启动时间，方便诊断
    - 第二个实例启动时抢锁失败 → 激活已有窗口并退出

不依赖第三方库。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LOCK_NAME = "monitor.lock"


class SingleInstance:
    def __init__(self, lock_dir: Path) -> None:
        self.path = Path(lock_dir) / LOCK_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._acquired = False

    def acquire(self) -> bool:
        """尝试获取单实例锁。成功返回 True。"""
        try:
            self._fh = open(self.path, "a+", encoding="utf-8")
        except OSError:
            # 锁文件都建不了，退化成允许运行（不要把用户挡在门外）
            return True

        try:
            import msvcrt
            self._fh.seek(0)
            # 非阻塞独占锁
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._acquired = True
        except (OSError, ImportError):
            self._acquired = False

        if self._acquired:
            try:
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(f"{os.getpid()}\n{time.time():.0f}\n")
                self._fh.flush()
            except OSError:
                pass
        else:
            # 抢锁失败，读一下对方是谁
            try:
                self._fh.seek(0)
                content = self._fh.read().split()
                self._other_pid = int(content[0]) if content else 0
            except (OSError, ValueError):
                self._other_pid = 0
        return self._acquired

    @property
    def other_pid(self) -> int:
        return getattr(self, "_other_pid", 0)

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if self._acquired:
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, ImportError):
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self._acquired = False


# ---------------- 「显示主窗口」信号 ----------------
#
# 启动器（普通权限）要唤醒已提权的主程序：跨完整性级别发窗口消息会被 UIPI 拦掉，
# 窗口收在托盘里时按标题激活也不会让主程序恢复推送。所以主程序建一个命名事件，
# 启动器 SetEvent，主程序的等待线程收到后自己 show。
# 安全描述：交互用户可 SetEvent/等待；完整性标签降到 Medium，普通权限进程才写得进去。

SHOW_EVENT_NAME = "Local\\HarnessIOMonitor.Show"
_SHOW_EVENT_SDDL = "D:(A;;0x00100002;;;IU)(A;;GA;;;SY)(A;;GA;;;BA)S:(ML;;NW;;;ME)"
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0


def _kernel32():
    import ctypes
    import ctypes.wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k.CreateEventW.restype = w.HANDLE
    k.OpenEventW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    k.OpenEventW.restype = w.HANDLE
    k.SetEvent.argtypes = [w.HANDLE]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.WaitForSingleObject.restype = w.DWORD
    k.CloseHandle.argtypes = [w.HANDLE]
    k.LocalFree.argtypes = [ctypes.c_void_p]
    return k


class ShowSignal:
    """主程序侧：持有命名事件，wait() 阻塞到有人请求显示窗口（或超时）。"""

    def __init__(self) -> None:
        self._h = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            import ctypes.wintypes as w

            class SECURITY_ATTRIBUTES(ctypes.Structure):
                _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                            ("bInheritHandle", w.BOOL)]

            k = _kernel32()
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            psd = ctypes.c_void_p(0)
            sa = None
            if adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(_SHOW_EVENT_SDDL, 1, ctypes.byref(psd), None):
                sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), psd, False)
            self._h = k.CreateEventW(ctypes.byref(sa) if sa else None, False, False, SHOW_EVENT_NAME)
            if psd:
                k.LocalFree(psd)
        except Exception:
            self._h = None

    @property
    def ok(self) -> bool:
        return bool(self._h)

    def wait(self, timeout_ms: int) -> bool:
        if not self._h:
            return False
        return _kernel32().WaitForSingleObject(self._h, timeout_ms) == WAIT_OBJECT_0

    def close(self) -> None:
        if self._h:
            _kernel32().CloseHandle(self._h)
            self._h = None


def signal_show() -> bool:
    """启动器侧：请求已在运行的主程序显示窗口。事件不存在（主程序没在跑或是旧版本）返回 False。"""
    if sys.platform != "win32":
        return False
    try:
        k = _kernel32()
        h = k.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
        if not h:
            return False
        try:
            return bool(k.SetEvent(h))
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def allow_any_foreground() -> None:
    """把本进程的前台权转让出去（ASFW_ANY），让随后启动/唤醒的主程序能把窗口提到最前。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)
    except Exception:
        pass


def is_running(lock_dir: Path) -> bool:
    """主程序是否在跑：试抢一下单实例锁，抢到就立刻放掉。"""
    probe = SingleInstance(lock_dir)
    got = probe.acquire()
    probe.release()
    return not got


def activate_existing_window() -> bool:
    """把已在运行的窗口提到前台。

    Windows 上跨会话找窗口不可靠（提权进程的窗口普通进程枚举不到），
    所以这里尽力而为：
      1. 按窗口标题找
      2. 找到就 ShowWindow + SetForegroundWindow
    找不到返回 False，调用方可退回「提示已在运行」。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes as w

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[int] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def _cb(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == "Harness I/O":      # 与 app.py 的 WINDOW_TITLE 一致
                    found.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        if not found:
            return False

        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
