"""免授权启动计划任务：XML 生成/解析/匹配（纯逻辑），以及可选的真机 COM 往返。

真机往返默认跳过；设 HIO_TASK_SELFTEST=1 时用临时任务名登记一个 LeastPrivilege 任务
（普通权限也能登记），读回校验后删除。不会 Run 它。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import schtask as s  # noqa: E402

PYW = r"C:\Python\pythonw.exe"
APP = ROOT / "app.py"


def _act():
    return s.expected_action(PYW, APP)


def test_expected_action_points_to_repo():
    a = _act()
    assert a.command == PYW
    assert a.arguments == f'"{APP.resolve()}" --from-task'
    assert a.working_dir == str(ROOT)


def test_xml_has_no_triggers_and_required_settings():
    xml = s.build_xml(_act(), r"PC\me")
    assert "<Triggers />" in xml
    for frag in (
        "<RunLevel>HighestAvailable</RunLevel>",
        "<LogonType>InteractiveToken</LogonType>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "<AllowStartOnDemand>true</AllowStartOnDemand>",
    ):
        assert frag in xml
    for banned in ("LogonTrigger", "BootTrigger", "TimeTrigger", "CalendarTrigger"):
        assert banned not in xml


def test_xml_escapes_paths():
    a = s.TaskAction(command=r"C:\a&b\pythonw.exe", arguments='"C:\\x<y>\\app.py" --from-task', working_dir=r"C:\x<y>")
    info = s.parse_xml(s.build_xml(a, r"PC\me"))
    assert info.action == a


def test_roundtrip_matches():
    info = s.parse_xml(s.build_xml(_act(), r"PC\me"))
    assert info.trigger_count == 0
    assert info.run_level == s.RUN_HIGHEST
    assert s.points_to_app(info, _act())


def test_parse_strips_utf16_declaration():
    xml = '<?xml version="1.0" encoding="UTF-16"?>\r\n' + s.build_xml(_act(), r"PC\me")
    assert s.points_to_app(s.parse_xml(xml), _act())


def test_mismatch_cases():
    want = _act()
    moved = s.expected_action(PYW, Path(r"D:\elsewhere\app.py"))
    assert not s.points_to_app(s.parse_xml(s.build_xml(moved, "u")), want)
    other_py = s.expected_action(r"C:\Other\pythonw.exe", APP)
    assert not s.points_to_app(s.parse_xml(s.build_xml(other_py, "u")), want)
    least = s.parse_xml(s.build_xml(want, "u", run_level=s.RUN_LEAST))
    assert not s.points_to_app(least, want)
    assert not s.points_to_app(None, want)


def test_trigger_makes_mismatch():
    xml = s.build_xml(_act(), "u").replace(
        "<Triggers />", "<Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>")
    info = s.parse_xml(xml)
    assert info.trigger_count == 1
    assert not s.points_to_app(info, _act())


def test_disabled_makes_mismatch():
    xml = s.build_xml(_act(), "u").replace("<Enabled>true</Enabled>", "<Enabled>false</Enabled>")
    assert not s.points_to_app(s.parse_xml(xml), _act())


def test_launcher_mode_ignores_interpreter_but_requires_it_exists():
    want = s.expected_action(sys.executable, APP)
    other = s.parse_xml(s.build_xml(s.expected_action(sys.executable, APP), "u"))
    assert s.points_to_app(other, s.expected_action(r"C:\nope\pythonw.exe", APP), check_command=False)
    gone = s.parse_xml(s.build_xml(s.expected_action(r"C:\nope\pythonw.exe", APP), "u"))
    assert not s.points_to_app(gone, want, check_command=False)


def test_gui_python(tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert s.gui_python(tmp_path / "python.exe") == str(tmp_path / "pythonw.exe")
    assert s.gui_python(tmp_path / "pythonw.exe") == str(tmp_path / "pythonw.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_query_missing_returns_none():
    assert s.query("HarnessIOMonitor-does-not-exist-7f3a") is None


@pytest.mark.skipif(os.environ.get("HIO_TASK_SELFTEST") != "1", reason="设 HIO_TASK_SELFTEST=1 才跑真机登记")
def test_live_register_query_delete():
    name = "HarnessIOMonitor-selftest"
    act = s.expected_action(sys.executable, APP)
    try:
        s.register(act, name=name, run_level=s.RUN_LEAST)
        info = s.query(name)
        assert info is not None
        assert info.action is not None
        assert info.action.arguments == act.arguments
        assert info.trigger_count == 0
    finally:
        s.delete(name)
    assert s.query(name) is None
    assert s.delete(name) is False
