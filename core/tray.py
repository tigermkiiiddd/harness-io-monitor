"""系统托盘图标（纯 ctypes，不依赖 pystray / pywin32 / tkinter）。

用途：程序关闭主窗口后驻留托盘继续采样。
点击托盘图标恢复窗口，右键菜单可退出。

线程模型（为 pywebview 外壳而改）：
    - create() 启动一个专属的后台线程。窗口类注册、消息窗口创建、Shell_NotifyIcon 加图标
      都在这个线程里完成，然后该线程自己跑 GetMessageW / TranslateMessage / DispatchMessageW 循环。
      不再依赖任何 GUI 主循环泵消息。
    - 托盘消息（点击、菜单）在托盘线程里收到，on_show / on_exit 也在托盘线程里执行。
      回调里只做「切回窗口」的动作；pywebview 的 show / hide / destroy 内部会自行 Invoke 到 GUI 线程。
    - destroy() 可从任意线程调用：PostMessage(WM_CLOSE) → 托盘线程里 DestroyWindow →
      WM_DESTROY 里删图标并 PostQuitMessage → 消息循环退出，线程结束。

实现要点：
    - Shell_NotifyIcon 注册托盘图标
    - 隐藏的消息窗口接收回调
    - 菜单用 CreatePopupMenu + TrackPopupMenu
    - 图标用系统内置图标（IDI_APPLICATION），避免打包资源文件
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 20

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTALIGN = 0x0000

IDI_APPLICATION = 32512

MENU_SHOW = 1001
MENU_EXIT = 1002

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM
)

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]

# 64 位 Python 下 ctypes 默认返回 c_int（32 位），句柄会被截断成负数，
# 导致 RegisterClass/CreateWindow/Shell_NotifyIcon 拿到错误的句柄。必须显式声明。
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.LoadIconW.restype = w.HICON
user32.LoadIconW.argtypes = [w.HINSTANCE, w.LPCWSTR]
user32.CreatePopupMenu.restype = w.HMENU
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID,
]
user32.DestroyMenu.argtypes = [w.HMENU]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DestroyWindow.argtypes = [w.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.RegisterWindowMessageW.restype = w.UINT
user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
shell32.Shell_NotifyIconW.restype = w.BOOL


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("hWnd", w.HWND),
        ("uID", w.UINT),
        ("uFlags", w.UINT),
        ("uCallbackMessage", w.UINT),
        ("hIcon", w.HICON),
        ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD),
        ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256),
        ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64),
        ("dwInfoFlags", w.DWORD),
    ]


class TrayIcon:
    """托盘图标。on_show / on_exit 是回调，在托盘线程里执行。"""

    def __init__(self, tooltip: str, on_show, on_exit) -> None:
        self.tooltip = tooltip[:127]
        self.on_show = on_show
        self.on_exit = on_exit
        self.hwnd = None
        self._added = False
        self._wndproc = None
        self._hicon = None
        self._msg_taskbar_created = 0
        self._class_name = f"HioTray{id(self)}"
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False

    # ---------- 创建 ----------

    def create(self, timeout: float = 5.0) -> bool:
        """启动托盘线程并等待图标加好。成功返回 True。"""
        if sys.platform != "win32":
            return False
        if self._thread is not None:
            return self._ok
        self._thread = threading.Thread(target=self._run, name="hio-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def _run(self) -> None:
        try:
            ok = self._setup()
        except Exception as exc:
            print(f"[tray] 创建失败: {exc}")
            ok = False
        self._ok = ok
        self._ready.set()
        if not ok:
            # 窗口可能已建但图标没加上：自己销毁，线程结束
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            return

        # 本线程自己的消息循环。GetMessage 返回 0 表示收到 WM_QUIT，循环结束。
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _setup(self) -> bool:
        hinst = kernel32.GetModuleHandleW(None)

        self._wndproc = WNDPROC(self._handle_message)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):   # 1410 = class already exists
                return False

        self.hwnd = user32.CreateWindowExW(
            0, self._class_name, "HIO Tray", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not self.hwnd:
            return False

        self._hicon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        # 资源管理器重启后托盘会清空，系统广播 TaskbarCreated，需要重新注册图标
        self._msg_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        nid = self._make_nid()
        self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        return self._added

    def _make_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip
        return nid

    # ---------- 消息处理（托盘线程）----------

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if self._msg_taskbar_created and msg == self._msg_taskbar_created:
            try:
                nid = self._make_nid()
                self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            except Exception:
                pass
            return 0
        if msg == WM_TRAYICON:
            low = lparam & 0xFFFF
            if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._safe(self.on_show)
            elif low == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == MENU_SHOW:
                self._safe(self.on_show)
            elif cmd == MENU_EXIT:
                self._safe(self.on_exit)
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            # 在拥有窗口的线程里删图标，然后退出消息循环
            if self._added:
                try:
                    nid = self._make_nid()
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
                self._added = False
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[tray] 回调失败: {exc}")

    def _show_menu(self) -> None:
        try:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, "显示主窗口")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "退出")

            pt = w.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须有前台窗口，否则菜单不消失
            user32.SetForegroundWindow(self.hwnd)
            user32.TrackPopupMenu(
                menu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON,
                pt.x, pt.y, 0, self.hwnd, None,
            )
            user32.PostMessageW(self.hwnd, 0, 0, 0)
            user32.DestroyMenu(menu)
        except Exception as exc:
            print(f"[tray] 菜单失败: {exc}")

    def update_tooltip(self, text: str) -> None:
        if not self._added:
            return
        try:
            nid = self._make_nid()
            nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    # ---------- 销毁 ----------

    def destroy(self, timeout: float = 3.0) -> None:
        """可从任意线程调用。投递 WM_CLOSE，等托盘线程自己清理完退出。"""
        if self.hwnd and self._thread is not None and self._thread.is_alive():
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout)
        self.hwnd = None
