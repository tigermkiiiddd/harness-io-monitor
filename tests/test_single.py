"""单实例锁探测与「显示主窗口」信号。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import single  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_is_running_reflects_lock(tmp_path):
    assert single.is_running(tmp_path) is False
    lock = single.SingleInstance(tmp_path)
    assert lock.acquire()
    try:
        assert single.is_running(tmp_path) is True
    finally:
        lock.release()
    assert single.is_running(tmp_path) is False


def test_show_signal_roundtrip(monkeypatch):
    monkeypatch.setattr(single, "SHOW_EVENT_NAME", "Local\\HarnessIOMonitor.Show.pytest")
    assert single.signal_show() is False          # 没人建事件：主程序不在
    sig = single.ShowSignal()
    try:
        assert sig.ok
        assert sig.wait(0) is False
        assert single.signal_show() is True
        assert sig.wait(1000) is True
        assert sig.wait(0) is False                # 自动复位：一次信号只唤醒一次
    finally:
        sig.close()
    assert single.signal_show() is False
