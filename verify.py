"""交叉验证：证明监控报出的数字是真的。

一个子进程写 N MB，用几条互相独立的路径同时测：

    A. 本程序 ctypes        GetProcessIoCounters
    B. WMI                  Win32_Process.WriteTransferCount
    D. 文件实际大小          os.stat().st_size

**关键教训（这个脚本踩过的坑）**

用 `sys.executable` 起子进程时，它可能是个 venv shim：
    G:\\projects\\...\\venv\\Scripts\\python.exe   <- 写 0
      └─ C:\\...\\Python312\\python.exe            <- 真正在写
只盯着 Popen 返回的 pid 会读到 0，然后误判成「监控不准」。
所以这里必须**按进程树归集**：把目标 pid 及其所有子孙的写入加起来。
这也正是监控程序在真实场景里做的事。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.winproc import Prober  # noqa: E402

SIZE_MB = 64
HOLD_SEC = 3.0

WRITER = r"""
import os, sys, time
path, mb, hold = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
chunk = b'x' * (1024 * 1024)
with open(path, 'wb') as f:
    for _ in range(mb):
        f.write(chunk)
    f.flush()
    os.fsync(f.fileno())
time.sleep(hold)
"""


def tree_write(procs, root_pid: int) -> int:
    """root_pid 及其所有子孙的累计写入之和。"""
    by_pid = {p.pid: p for p in procs}
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)
    total = 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        p = by_pid.get(pid)
        if p:
            total += p.write_bytes
        stack.extend(children.get(pid, ()))
    return total


class WmiReader(threading.Thread):
    """后台读 WMI，取最大值。绝不阻塞采样。"""

    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.value: int | None = None
        self._halt = threading.Event()

    def run(self) -> None:
        ps = (
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={self.pid}' "
            "-ErrorAction SilentlyContinue; "
            "if ($p) { [int64]$p.WriteTransferCount } else { -1 }"
        )
        while not self._halt.is_set():
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, text=True, timeout=20,
                )
                v = r.stdout.strip()
                if v and int(v) >= 0:
                    self.value = max(self.value or 0, int(v))
            except Exception:
                pass
            self._halt.wait(0.5)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=6)


def mb(n: int | None) -> str:
    return "n/a" if n is None else f"{n / 1048576:.2f} MB"


def main() -> int:
    sink = Path(tempfile.gettempdir()) / "hio-verify.bin"
    try:
        sink.unlink()
    except OSError:
        pass

    prober = Prober()
    self_pid = os.getpid()

    def self_write() -> int:
        for p in prober.sample():
            if p.pid == self_pid:
                return p.write_bytes
        return 0

    self_before = self_write()

    print("=" * 70)
    print(f"交叉验证   目标：子进程写 {SIZE_MB} MB")
    print("=" * 70)

    proc = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(sink), str(SIZE_MB), str(HOLD_SEC)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    target = proc.pid
    wmi = WmiReader(target)
    wmi.start()

    a_val: int | None = None
    sample_count = 0
    while proc.poll() is None:
        procs = prober.sample()
        sample_count += 1
        a_val = tree_write(procs, target)
        time.sleep(0.15)

    proc.wait()
    time.sleep(0.3)
    wmi.stop()

    file_size = sink.stat().st_size if sink.exists() else 0
    self_delta = self_write() - self_before

    print(f"\n  采样次数                          {sample_count}")
    print(f"  A. GetProcessIoCounters (整棵树)  {mb(a_val)}")
    print(f"  B. WMI 单进程 WriteTransferCount  {mb(wmi.value)}")
    print(f"  D. 文件实际大小                    {file_size / 1048576:.2f} MB")
    print(f"\n  监控程序自身写入增量              {self_delta / 1024:.1f} KB")

    ok_a = a_val is not None and abs(a_val - file_size) <= max(file_size * 0.05, 2 * 1048576)
    ok_self = self_delta < 5 * 1048576

    print("\n" + "-" * 70)
    print(f"  [{'PASS' if ok_a else 'FAIL'}] A 与文件大小一致 "
          f"(差 {abs((a_val or 0) - file_size) / 1048576:.2f} MB)")
    print(f"  [{'PASS' if ok_self else 'FAIL'}] 监控自身写入远小于被测值")
    if wmi.value is not None:
        print(f"  [INFO] WMI 只测单进程，若子进程是 shim 会偏小，仅供参考")
    print("-" * 70)

    try:
        sink.unlink()
    except OSError:
        pass
    return 0 if (ok_a and ok_self) else 1


if __name__ == "__main__":
    sys.exit(main())
