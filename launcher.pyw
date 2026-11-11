"""Harness I/O Monitor 启动器（pythonw 运行，快捷方式和 launch.bat 都指向它）。

    1. 主程序已在运行 → 请它显示窗口，退出。
    2. 免授权启动任务存在且指向本仓库的 app.py → 触发任务运行，退出。全程无窗口、无 UAC。
    3. 否则（任务不存在 / 已失效 / 触发失败）→ 直接启动 app.py：它会自提权（弹一次 UAC），
       提权后再登记任务，下次就走第 2 步。

启动器本身是一次性的，不常驻。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import schtask                                                       # noqa: E402
from core.single import activate_existing_window, allow_any_foreground, is_running, signal_show  # noqa: E402

APP = ROOT / "app.py"
DATA = ROOT / "data"
LOG = DATA / "launcher.log"


def _log(msg: str) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def _start_app_directly() -> None:
    pyw = schtask.gui_python(sys.executable)
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", pyw, f'"{APP}"', str(ROOT), 1)
    if int(r) <= 32:
        _log(f"直接启动 app.py 失败: ShellExecute={int(r)}")
        ctypes.windll.user32.MessageBoxW(None, f"无法启动 {APP}", "Harness I/O Monitor", 0x10)


def main() -> None:
    allow_any_foreground()          # 先让出前台权，被唤醒/拉起的主程序才能把窗口提到最前

    if is_running(DATA):
        if not signal_show():
            activate_existing_window()
        return

    want = schtask.expected_action(schtask.gui_python(sys.executable), APP)
    try:
        info = schtask.query()
        if schtask.points_to_app(info, want, check_command=False):
            schtask.run()
            return
        if info is not None:
            _log("计划任务已失效（路径变了或被改动），改为直接启动")
    except Exception as exc:
        _log(f"触发计划任务失败，改为直接启动: {exc}")

    _start_app_directly()


if __name__ == "__main__":
    main()
