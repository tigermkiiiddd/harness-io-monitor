"""创建桌面快捷方式，指向本目录的 launch.bat。

用 PowerShell 的 WScript.Shell，不依赖 pywin32。
所有输出强制 UTF-8，避免中文 Windows 上 GBK/UTF-8 混用导致路径错误。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BAT = ROOT / "launch.bat"
LNK_NAME = "Harness IO Monitor.lnk"

PS = f"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '{LNK_NAME}'
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = '{BAT}'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = 'shell32.dll,15'
$s.Description = 'AI Harness disk I/O monitor'
$s.Save()
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Output $lnk
"""


def main() -> int:
    if not BAT.exists():
        print(f"missing: {BAT}")
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
