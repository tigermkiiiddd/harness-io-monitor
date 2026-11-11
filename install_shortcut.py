"""创建桌面快捷方式，指向本目录的 launcher.pyw（经 pythonw.exe 启动，无控制台窗口）。

启动器负责：激活已在运行的实例 / 触发免授权启动任务（无 UAC）/ 退回直接启动 app.py。

用 PowerShell 的 WScript.Shell，不依赖 pywin32。
所有输出强制 UTF-8，避免中文 Windows 上 GBK/UTF-8 混用导致路径错误。
这是安装期的一次性操作，运行期程序本身不起任何子进程。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "launcher.pyw"
PYW = Path(sys.executable).with_name("pythonw.exe")   # 与当前解释器同目录的无控制台版本
LNK_NAME = "Harness IO Monitor.lnk"

PS = f"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '{LNK_NAME}'
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = '{PYW}'
$s.Arguments = '"{APP}"'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = 'shell32.dll,15'
$s.Description = 'AI Harness disk I/O monitor'
$s.Save()
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Output $lnk
"""


def main() -> int:
    if not APP.exists():
        print(f"missing: {APP}")
        return 1
    if not PYW.exists():
        print(f"pythonw.exe 不存在: {PYW}（请用安装了 Python 的解释器运行本脚本）")
        return 1
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        print("stderr:", r.stderr.strip())
        return r.returncode
    target = Path(r.stdout.strip())
    if target.exists():
        print(f"OK  {target}")
        return 0
    print(f"shortcut not created: {r.stdout.strip()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
