"""Harness I/O Monitor 入口（pywebview 外壳，替代旧 desktop.py）。

双击 launch.bat（pythonw）即可运行，不依赖终端、IDE 或 agent。

启动顺序（沿用旧 desktop.py 的 main()）：
    单实例锁 → 非管理员自动提权一次（用 pythonw.exe，不冒控制台）
    → 起 Sampler → 起磁盘后台线程（每 60 秒读一次 SMART）→ 开窗口

运行期约束：
    · 不启动任何子进程（不调 PowerShell / cmd / wmic）。
    · 关闭按钮 = 收进托盘继续采样；托盘「退出」= flush + 停采样 + 关库 + 退出。
    · 每秒由后台线程算好快照，经 window.evaluate_js 推给前端 window.hio.onSnapshot。

免授权启动（core/schtask.py）：
    · 已提权时检查一次计划任务 HarnessIOMonitor（无触发器、最高权限），没有或路径变了就静默登记；
      用户在菜单里取消过就不再自动登记。
    · launcher.pyw 触发该任务拉起本程序（带 --from-task，跳过自提权），全程无 UAC。
"""

from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as w
import json
import math
import os
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import schtask, smart                           # noqa: E402
from core.classify import Classifier                      # noqa: E402
from core.dataguard import backup_db                      # noqa: E402
from core.sampler import Sampler                          # noqa: E402
from core.single import ShowSignal, SingleInstance, activate_existing_window, signal_show  # noqa: E402
from core.snapshot import RANGE_SPANS, DEFAULT_RANGE, SnapshotBuilder, View  # noqa: E402
from core.store import Store                              # noqa: E402
from core.tray import TrayIcon                            # noqa: E402

APP_NAME = "Harness I/O Monitor"
WINDOW_TITLE = "Harness I/O"          # 窗口标题（单实例激活时按它找窗口）
DB_PATH = ROOT / "data" / "monitor.db"
CONFIG_PATH = ROOT / "harnesses.json"
UI_INDEX = ROOT / "ui" / "index.html"
BG_COLOR = "#0e0f11"                  # 与设计稿 --bg 一致，避免白屏闪烁
DEFAULT_SIZE = (1180, 720)
MIN_SIZE = (360, 160)
MINI_WIDTH = 360
PUSH_PERIOD = 1.0                     # 快照推送周期（秒）
DISK_PERIOD = 60.0                    # SMART 读取与落库周期（秒）
TASK_OPTOUT_KEY = "task_launch_optout"  # meta 表：用户在菜单里关掉了「免授权启动」→ 启动时不再自动登记

# ---------------- Win32 辅助（ctypes，不起子进程）----------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetDpiForWindow.argtypes = [w.HWND]
user32.GetDpiForWindow.restype = w.UINT
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [w.HWND]
dwmapi.DwmSetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]

MB_OK = 0x0
MB_ICONINFORMATION = 0x40
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def _find_own_window(title: str) -> int:
    """在本进程的顶层窗口里按标题找句柄。找不到返回 0。"""
    found: list[int] = []
    my_pid = os.getpid()
    EnumProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def _cb(hwnd, _lparam):
        pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != my_pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    return found[0] if found else 0


def _frame_height(hwnd: int) -> int:
    """窗口外框高度 - 客户区高度（标题栏 + 边框），与 window.resize 同一单位。"""
    wr = w.RECT()
    cr = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    return max(0, (wr.bottom - wr.top) - (cr.bottom - cr.top))


def _dpi_scale(hwnd: int) -> float:
    """CSS 像素 → 窗口单位的倍率。DPI 虚拟化的窗口返回 96，倍率为 1。"""
    dpi = user32.GetDpiForWindow(hwnd) or 96
    return dpi / 96.0


def _dark_titlebar(hwnd: int) -> None:
    val = ctypes.c_int(1)
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
        if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
            return


def _message_box(text: str, owner: int = 0) -> None:
    user32.MessageBoxW(owner or None, text, APP_NAME, MB_OK | MB_ICONINFORMATION)


# ---------------- 应用本体 ----------------


class HarnessApp:
    """持有采样器、快照构建器、窗口状态。所有后台工作都在线程里，窗口回调只做轻量切换。"""

    def __init__(self) -> None:
        self.store = Store(DB_PATH)
        self.clf = Classifier.from_config(CONFIG_PATH)
        self.sampler = self._new_sampler()
        self.builder = SnapshotBuilder(self.store, self.clf, log=lambda m: print(m, flush=True))
        self.admin = smart.is_admin()

        self.view = View(admin=self.admin)
        self._view_lock = threading.Lock()
        self._push_lock = threading.Lock()
        self._stop = threading.Event()
        self._baseline_done = False
        self._disk_thread: threading.Thread | None = None
        self._push_thread: threading.Thread | None = None

        self.window = None
        self._hwnd = 0
        self._dark_done = False
        self.visible = False
        self.loaded = threading.Event()
        self.quitting = False
        self.tray: TrayIcon | None = None
        self.tray_ok = False
        self._mode = "full"
        self._saved_geom: tuple[int, int, int, int] | None = None

        # 免授权启动：True/False = 任务是否存在；None = 非管理员，无法管理（菜单项置灰）
        self.task_state: bool | None = None
        self._task_lock = threading.Lock()
        self._show_signal = ShowSignal()

    # ---------- 生命周期 ----------

    def _new_sampler(self) -> Sampler:
        return Sampler(self.store, self.clf, interval=1.0, exclude_pids={os.getpid()})

    def start(self) -> None:
        self.sampler.start()
        if self.admin:
            self.sampler.read_disks = True
            self._disk_thread = threading.Thread(target=self._disk_loop, name="hio-disk", daemon=True)
            self._disk_thread.start()
        else:
            print("[app] 非管理员：磁盘寿命不读取", flush=True)
        self._push_thread = threading.Thread(target=self._push_loop, name="hio-push", daemon=True)
        self._push_thread.start()
        if self._show_signal.ok:
            threading.Thread(target=self._show_loop, name="hio-show", daemon=True).start()
        if self.admin:
            # 启动时检查一次计划任务（只这一次，不周期性做）
            threading.Thread(target=self._task_startup_check, name="hio-task", daemon=True).start()

    def attach(self, window) -> None:
        """窗口创建后调用：挂上事件。"""
        self.window = window
        window.events.shown += self._on_shown
        window.events.loaded += self._on_loaded
        window.events.closing += self._on_closing

    def shutdown(self) -> None:
        """托盘「退出」或窗口真正关闭后调用：停线程 → 托盘 → flush 采样 → 关库。"""
        self._stop.set()
        for t in (self._push_thread, self._disk_thread):
            if t is not None:
                t.join(timeout=2.0)
        if self.tray is not None:
            self.tray.destroy()
            self.tray = None
        self._show_signal.close()
        try:
            self.sampler.stop()          # 内部会 flush 内存中的数据
        except Exception as exc:
            print(f"[app] 停采样失败: {exc}", flush=True)
        finally:
            self.store.close()
        print("[app] 已退出", flush=True)

    # ---------- 后台线程 ----------

    def _disk_loop(self) -> None:
        """每 60 秒读一次 SMART：注入快照构建器，并按旧逻辑每 60 秒落库一次。"""
        while not self._stop.is_set():
            try:
                disks = smart.read_disks()
                self.builder.set_disks(disks)
                now = time.time()
                rows = [asdict(d) for d in disks if d.device_id]
                if rows:
                    self.store.add_disk_health(now, rows)
            except Exception as exc:
                print(f"[disk] 读盘或落库失败: {exc}", flush=True)
                self.builder.set_disks([], error=str(exc))
            self._stop.wait(DISK_PERIOD)

    def _show_loop(self) -> None:
        """启动器发来「显示主窗口」→ 从托盘恢复并提到前台。"""
        while not self._stop.is_set():
            if self._show_signal.wait(500) and not self._stop.is_set():
                self.show_window()

    # ---------- 免授权启动（计划任务）----------

    def _task_action(self) -> schtask.TaskAction:
        return schtask.expected_action(schtask.gui_python(sys.executable), Path(__file__).resolve())

    def _task_startup_check(self) -> None:
        """已提权：任务没有或路径变了就静默（重新）登记；用户在菜单里关过就只读状态不登记。"""
        with self._task_lock:
            try:
                optout = self.store.get_meta(TASK_OPTOUT_KEY) == "1"
                if optout:
                    self.task_state = schtask.query() is not None
                else:
                    if schtask.ensure(self._task_action()):
                        print("[task] 已登记免授权启动任务", flush=True)
                    self.task_state = True
            except Exception as exc:
                print(f"[task] 检查/登记计划任务失败: {exc}", flush=True)
                self.task_state = False
        self._push()

    def set_task_launch(self, on) -> bool | None:
        """菜单开关：勾选 = 登记任务；取消 = 删除任务并记住，之后启动不再自动登记。"""
        on = bool(on)
        if not self.admin:
            self._push()
            return self.task_state
        with self._task_lock:
            try:
                if on:
                    schtask.register(self._task_action())
                    self.store.set_meta(TASK_OPTOUT_KEY, "0")
                else:
                    schtask.delete()
                    self.store.set_meta(TASK_OPTOUT_KEY, "1")
                self.task_state = on
            except Exception as exc:
                print(f"[task] 切换免授权启动失败: {exc}", flush=True)
                _message_box(f"{'开启' if on else '关闭'}免授权启动失败：\n{exc}", self._hwnd)
        self._push()
        return self.task_state

    def _push_loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._tick()
            except Exception as exc:
                print(f"[push] 推送失败: {exc}", flush=True)
            self._stop.wait(max(0.05, PUSH_PERIOD - (time.time() - started)))

    def _tick(self) -> None:
        snap = self.sampler.latest()
        if snap is None:
            return
        if not self._baseline_done:
            self._baseline_done = True
            try:
                n = self.sampler.write_baseline()
                if n:
                    print(f"[baseline] 已落基线 {n} 个进程", flush=True)
            except Exception as exc:
                print(f"[baseline] 失败: {exc}", flush=True)
        self._push(snap)

    def _push(self, snap=None) -> None:
        """构建快照；窗口可见时推给前端。隐藏时照常构建，历史才不断档。"""
        with self._push_lock:
            if snap is None:
                snap = self.sampler.latest()
            if snap is None:
                return
            data = self.builder.build(snap, self._view_now())
            data["taskLaunch"] = self.task_state
            if not (self.visible and self.loaded.is_set() and self.window is not None):
                return
            js = "window.hio && window.hio.onSnapshot(" + json.dumps(data) + ")"
            try:
                self.window.evaluate_js(js)
            except Exception as exc:
                print(f"[push] evaluate_js 失败: {exc}", flush=True)

    # ---------- 窗口事件 ----------

    def _on_shown(self) -> None:
        self.visible = True
        if not self._dark_done:
            hwnd = self._native_hwnd()
            if hwnd:
                self._hwnd = hwnd
                _dark_titlebar(hwnd)
                self._dark_done = True
                # 由计划任务启动的进程没有前台权；启动器已 AllowSetForegroundWindow，这里提到最前
                user32.SetForegroundWindow(hwnd)

    def _on_loaded(self) -> None:
        self.loaded.set()
        self._push()

    def _on_closing(self):
        """关闭 = 收进托盘。托盘不可用、或正在真退出时，放行关闭。"""
        if self.quitting or not self.tray_ok:
            return True
        self.visible = False
        self.window.hide()
        return False

    def _native_hwnd(self) -> int:
        return self._hwnd or _find_own_window(WINDOW_TITLE)

    # ---------- 托盘回调（托盘线程）----------

    def show_window(self) -> None:
        if self.window is None:
            return
        self.visible = True
        self.window.show()
        hwnd = self._native_hwnd()
        if hwnd:
            user32.SetForegroundWindow(hwnd)
        self._push()

    def request_quit(self) -> None:
        """托盘「退出」：标记真退出后关窗口，webview.start() 随之返回，再由 main 做 shutdown。"""
        self.quitting = True
        if self.window is not None:
            self.window.destroy()

    # ---------- 视图与动作（js_api 调用，运行在 pywebview 的工作线程）----------

    def _view_now(self) -> View:
        with self._view_lock:
            v = self.view
            return View(
                range_key=v.range_key,
                expanded=set(v.expanded),
                show_ide=v.show_ide,
                paused=self.sampler.is_paused(),
                admin=self.admin,
            )

    def set_view(self, range_key, expanded_ids, show_ide) -> None:
        with self._view_lock:
            self.view.range_key = range_key if range_key in RANGE_SPANS else DEFAULT_RANGE
            self.view.expanded = {str(x) for x in (expanded_ids or [])}
            self.view.show_ide = bool(show_ide)
        self._push()

    def toggle_pause(self) -> bool:
        if self.sampler.is_paused():
            self.sampler.resume()
        else:
            self.sampler.pause()
        self._push()
        return self.sampler.is_paused()

    def set_interval(self, seconds) -> None:
        self.sampler.set_interval(float(seconds))

    def set_on_top(self, on) -> None:
        if self.window is not None:
            self.window.on_top = bool(on)

    def set_mode(self, mode, content_h=0) -> None:
        """mini：记住当前尺寸位置，缩到 360 宽、高度 = 前端报的内容高 + 标题栏。full：恢复。"""
        win = self.window
        if win is None:
            return
        if mode == "mini":
            if self._mode != "mini":
                self._saved_geom = (win.width, win.height, win.x, win.y)
            hwnd = self._native_hwnd()
            scale = _dpi_scale(hwnd) if hwnd else 1.0
            frame = _frame_height(hwnd) if hwnd else 0
            h = max(MIN_SIZE[1], math.ceil(float(content_h or 0) * scale + frame))
            win.resize(MINI_WIDTH, h)
            self._mode = "mini"
        else:
            if self._mode == "mini" and self._saved_geom is not None:
                cw, ch, x, y = self._saved_geom
                win.resize(cw, ch)
                win.move(x, y)
            self._mode = "full"

    def export_csv(self, range_key=None) -> None:
        import webview

        snap = self.sampler.latest()
        win = self.window
        if snap is None or win is None:
            _message_box("还没有采样数据", self._hwnd)
            return
        default_name = f"harness-io-{datetime.now():%Y%m%d-%H%M%S}.csv"
        picked = win.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(ROOT),
            save_filename=default_name,
            file_types=("CSV (*.csv)",),
        )
        if not picked:
            return
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        if not str(path).lower().endswith(".csv"):
            path = f"{path}.csv"
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                cw = csv.writer(f)
                cw.writerow([
                    "harness", "display", "pid", "exe", "write_bps",
                    "write_total_bytes", "read_total_bytes", "path", "cmdline",
                ])
                for c in snap.processes:
                    if not c.harness:
                        continue
                    a = snap.aggs.get(c.harness)
                    cw.writerow([
                        c.harness,
                        self.clf.by_id[c.harness].display,
                        c.proc.pid,
                        c.proc.exe,
                        f"{a.write_bps:.1f}" if a else "",
                        c.proc.write_bytes,
                        c.proc.read_bytes,
                        c.proc.path,
                        c.proc.cmdline[:300],
                    ])
            _message_box(f"已导出到\n{path}", self._hwnd)
        except Exception as exc:
            _message_box(f"导出失败: {exc}", self._hwnd)

    def reset(self) -> None:
        """清除历史：确认 → 备份 → 停旧采样（flush）→ 清库 → 起新采样。"""
        win = self.window
        if win is None:
            return
        if not win.create_confirmation_dialog(
            APP_NAME,
            "确定清除所有历史数据？\n\n会先自动备份到 data/backups/，确认无误后可自行删除。",
        ):
            return
        backup = None
        try:
            backup = backup_db()
        except Exception as exc:
            if not win.create_confirmation_dialog(APP_NAME, f"备份失败（{exc}）。\n仍要继续清除吗？"):
                return

        interval = self.sampler.interval
        self.sampler.stop()
        self.store.reset(confirm_token="user-confirmed-reset")
        self.sampler = self._new_sampler()
        self.sampler.set_interval(interval)
        if self.admin:
            self.sampler.read_disks = True
        self.sampler.start()
        self.builder.reset_memory()
        self._push()
        if backup:
            _message_box(f"已清除。备份保存在：\n{backup}", self._hwnd)


class Api:
    """挂在 window.pywebview.api 上的动作表。方法名与 docs/rebuild-plan.md 的表一致。"""

    def __init__(self, app: HarnessApp) -> None:
        self._app = app

    def set_view(self, range_key, expanded_ids, show_ide):
        self._app.set_view(range_key, expanded_ids, show_ide)

    def toggle_pause(self):
        return self._app.toggle_pause()

    def set_interval(self, seconds):
        self._app.set_interval(seconds)

    def export_csv(self, range_key=None):
        self._app.export_csv(range_key)

    def reset(self):
        self._app.reset()

    def set_mode(self, mode, content_h=0):
        self._app.set_mode(mode, content_h)

    def set_on_top(self, on):
        self._app.set_on_top(on)

    def set_task_launch(self, on):
        return self._app.set_task_launch(on)


# ---------------- 提权 ----------------


def _elevate_self() -> bool:
    """以管理员身份重新启动自己，优先用 pythonw.exe（不冒控制台窗口）。返回是否已发起提权。"""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    target = str(pyw if pyw.exists() else exe)
    params = f'"{Path(__file__).resolve()}"'
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, str(ROOT), 1)
        return int(r) > 32
    except Exception as exc:
        print(f"[elevate] 提权失败，以普通权限继续: {exc}", flush=True)
        return False


# ---------------- 入口 ----------------


def run() -> None:
    import webview

    app = HarnessApp()
    app.start()

    tray = TrayIcon(
        "Harness I/O Monitor - sampling",
        on_show=app.show_window,
        on_exit=app.request_quit,
    )
    app.tray = tray
    app.tray_ok = tray.create()
    if not app.tray_ok:
        print("[tray] 托盘不可用：关闭窗口将直接退出", flush=True)

    window = webview.create_window(
        WINDOW_TITLE,
        url=str(UI_INDEX),
        js_api=Api(app),
        width=DEFAULT_SIZE[0],
        height=DEFAULT_SIZE[1],
        min_size=MIN_SIZE,
        background_color=BG_COLOR,
    )
    app.attach(window)
    try:
        webview.start()
    finally:
        app.shutdown()


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 单实例检查必须在提权之前做，否则每次启动都会先弹 UAC 再发现已有实例。
    lock = SingleInstance(DB_PATH.parent)
    if not lock.acquire():
        if not signal_show() and not activate_existing_window():
            _message_box(
                "Harness I/O Monitor 已在运行。\n\n如果没看到窗口，请检查屏幕右下角托盘区。"
            )
        return

    # 非管理员时自动请求提权一次（SMART 读取需要管理员）。
    # --from-task：由免授权启动的计划任务拉起，本来就是最高权限，跳过提权。
    # HIO_NO_ELEVATE=1 时跳过，用于开发冒烟。
    from_task = schtask.FROM_TASK_FLAG in sys.argv[1:]
    if not from_task and not os.environ.get("HIO_NO_ELEVATE") and not smart.is_admin():
        lock.release()                 # 交给提权后的新实例拿锁
        if _elevate_self():
            return
        if not lock.acquire():         # 提权失败：重新拿锁，继续以普通权限跑
            return

    try:
        run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
