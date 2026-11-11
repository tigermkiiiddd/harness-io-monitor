"""「免授权启动」用的按需计划任务（任务计划程序 COM，纯 ctypes，不起子进程、不依赖 pywin32）。

机制（见 docs/rebuild-plan.md「免授权启动」）：
    · 主程序已提权时登记任务 HarnessIOMonitor：**无任何触发器**、最高权限、仅用户登录时交互运行，
      动作 = pythonw.exe "<repo>\\app.py" --from-task。只能被手动 Run，不会开机/登录自启。
    · launcher.pyw（普通权限）触发 Run → 任务计划服务以最高权限启动主程序，全程无 UAC。

COM 走 IDispatch 晚绑定（ITaskService / ITaskFolder / IRegisteredTask 都是双接口），
只用到 Connect / GetFolder / GetTask / RegisterTask / DeleteTask / Run / Xml 这几个成员。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "HarnessIOMonitor"
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
FROM_TASK_FLAG = "--from-task"

RUN_HIGHEST = "HighestAvailable"
RUN_LEAST = "LeastPrivilege"          # 只给单测用（普通权限也能登记）

# ---------------- 任务定义（纯逻辑，可单测）----------------


@dataclass(frozen=True)
class TaskAction:
    command: str
    arguments: str
    working_dir: str


def expected_action(pythonw: str | os.PathLike, app_py: str | os.PathLike) -> TaskAction:
    app = Path(app_py).resolve()
    return TaskAction(
        command=str(Path(pythonw)),
        arguments=f'"{app}" {FROM_TASK_FLAG}',
        working_dir=str(app.parent),
    )


def build_xml(action: TaskAction, user_id: str, run_level: str = RUN_HIGHEST,
              description: str = "Harness I/O Monitor 免授权启动（无触发器，仅供启动器按需运行）") -> str:
    """任务 XML。要点：<Triggers/> 为空；不限执行时长；电池照常；已在运行则不启动新实例；普通优先级。"""
    e = escape
    return f"""<Task version="1.2" xmlns="{TASK_NS}">
  <RegistrationInfo>
    <Description>{e(description)}</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{e(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{e(run_level)}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(action.command)}</Command>
      <Arguments>{e(action.arguments)}</Arguments>
      <WorkingDirectory>{e(action.working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


@dataclass(frozen=True)
class TaskInfo:
    action: TaskAction | None
    trigger_count: int
    run_level: str
    enabled: bool


def parse_xml(xml_text: str) -> TaskInfo:
    """从任务 XML 抽出关心的字段。任务计划程序返回的 XML 带 UTF-16 声明，先去掉再解析。"""
    text = xml_text.strip()
    if text.startswith("<?xml"):
        text = text[text.index("?>") + 2:]
    root = ET.fromstring(text)
    ns = {"t": TASK_NS}

    def _t(path: str) -> str:
        el = root.find(path, ns)
        return (el.text or "").strip() if el is not None else ""

    triggers = root.find("t:Triggers", ns)
    exec_el = root.find("t:Actions/t:Exec", ns)
    action = None
    if exec_el is not None:
        action = TaskAction(
            command=_t("t:Actions/t:Exec/t:Command"),
            arguments=_t("t:Actions/t:Exec/t:Arguments"),
            working_dir=_t("t:Actions/t:Exec/t:WorkingDirectory"),
        )
    enabled_text = _t("t:Settings/t:Enabled")
    return TaskInfo(
        action=action,
        trigger_count=len(list(triggers)) if triggers is not None else 0,
        run_level=_t("t:Principals/t:Principal/t:RunLevel") or RUN_LEAST,
        enabled=enabled_text.lower() != "false",
    )


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.strip().strip('"'))) if p.strip() else ""


def points_to_app(info: TaskInfo | None, want: TaskAction, check_command: bool = True) -> bool:
    """任务是否仍是「我们的」：无触发器、最高权限、已启用、动作指向当前 app.py 与仓库根。

    check_command=False 时不比较解释器路径（启动器用：只要求解释器文件还在）。
    """
    if info is None or info.action is None:
        return False
    a = info.action
    if info.trigger_count != 0 or info.run_level != RUN_HIGHEST or not info.enabled:
        return False
    if a.arguments.strip() != want.arguments.strip():
        return False
    if _norm(a.working_dir) != _norm(want.working_dir):
        return False
    if check_command:
        return _norm(a.command) == _norm(want.command)
    return bool(a.command) and Path(a.command.strip('"')).exists()


def gui_python(exe: str | os.PathLike) -> str:
    """与解释器同目录的 pythonw.exe（存在时），否则原样返回。"""
    p = Path(exe)
    if p.name.lower() == "python.exe":
        pw = p.with_name("pythonw.exe")
        if pw.exists():
            return str(pw)
    return str(p)


def current_user() -> str:
    """DOMAIN\\user（NameSamCompatible）。拿不到就退回环境变量。"""
    try:
        secur32 = ctypes.WinDLL("secur32")
        size = w.ULONG(256)
        buf = ctypes.create_unicode_buffer(size.value)
        if secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    dom = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{dom}\\{user}" if dom else user


# ---------------- 最小 IDispatch 晚绑定（ctypes）----------------

VT_EMPTY, VT_I2, VT_I4, VT_BSTR, VT_DISPATCH, VT_BOOL = 0, 2, 3, 8, 9, 11
DISPATCH_METHOD, DISPATCH_PROPERTYGET = 1, 2
DISP_E_EXCEPTION = -2147352567            # 0x80020009
RPC_E_CHANGED_MODE = -2147417850          # 0x80010106
HRESULT_FILE_NOT_FOUND = -2147024894      # 0x80070002
HRESULT_PATH_NOT_FOUND = -2147024893      # 0x80070003
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_INTERACTIVE_TOKEN = 3

CLSID_TASK_SCHEDULER = "{0F87369F-A4E5-4CFC-BD3E-73E6154572DD}"
IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _VU(ctypes.Union):
    _fields_ = [
        ("llVal", ctypes.c_longlong),
        ("lVal", ctypes.c_long),
        ("iVal", ctypes.c_short),
        ("boolVal", ctypes.c_short),
        ("ptr", ctypes.c_void_p),
        ("_pad", ctypes.c_byte * (2 * ctypes.sizeof(ctypes.c_void_p))),
    ]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", w.WORD), ("r2", w.WORD), ("r3", w.WORD), ("u", _VU)]


class DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", ctypes.POINTER(VARIANT)),
        ("rgdispidNamedArgs", ctypes.POINTER(ctypes.c_long)),
        ("cArgs", ctypes.c_uint),
        ("cNamedArgs", ctypes.c_uint),
    ]


class EXCEPINFO(ctypes.Structure):
    _fields_ = [
        ("wCode", w.WORD), ("wReserved", w.WORD),
        ("bstrSource", ctypes.c_void_p), ("bstrDescription", ctypes.c_void_p), ("bstrHelpFile", ctypes.c_void_p),
        ("dwHelpContext", w.DWORD), ("pvReserved", ctypes.c_void_p), ("pfnDeferredFillIn", ctypes.c_void_p),
        ("scode", ctypes.c_long),
    ]


_ole32 = ctypes.WinDLL("ole32")
_oleaut32 = ctypes.WinDLL("oleaut32")
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, w.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.CLSIDFromString.argtypes = [w.LPCWSTR, ctypes.POINTER(GUID)]
_ole32.CLSIDFromString.restype = ctypes.c_long
_oleaut32.SysAllocString.argtypes = [w.LPCWSTR]
_oleaut32.SysAllocString.restype = ctypes.c_void_p
_oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
_oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]

_FN_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_FN_GETIDS = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
                                ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint, w.DWORD,
                                ctypes.POINTER(ctypes.c_long))
_FN_INVOKE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(GUID),
                                w.DWORD, w.WORD, ctypes.POINTER(DISPPARAMS), ctypes.POINTER(VARIANT),
                                ctypes.POINTER(EXCEPINFO), ctypes.POINTER(ctypes.c_uint))


class ComError(OSError):
    def __init__(self, hresult: int, where: str, desc: str = "") -> None:
        self.hresult = hresult
        msg = f"{where}: 0x{hresult & 0xFFFFFFFF:08X}"
        if desc:
            msg += f" {desc}"
        super().__init__(msg)


def _guid(s: str) -> GUID:
    g = GUID()
    hr = _ole32.CLSIDFromString(s, ctypes.byref(g))
    if hr < 0:
        raise ComError(hr, "CLSIDFromString")
    return g


def _bstr_text(p) -> str:
    return ctypes.wstring_at(p) if p else ""


class Dispatch:
    """一个 IDispatch 指针的薄封装。持有一份引用，释放时 Release。"""

    def __init__(self, ptr: int) -> None:
        self._p = ctypes.c_void_p(ptr)
        self._vt = ctypes.cast(self._p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

    def release(self) -> None:
        if self._p:
            _FN_RELEASE(self._vt[2])(self._p)
            self._p = ctypes.c_void_p(0)

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass

    def _dispid(self, name: str) -> int:
        names = (ctypes.c_wchar_p * 1)(name)
        did = ctypes.c_long(0)
        hr = _FN_GETIDS(self._vt[5])(self._p, ctypes.byref(GUID()), names, 1, 0x0400, ctypes.byref(did))
        if hr < 0:
            raise ComError(hr, f"GetIDsOfNames({name})")
        return did.value

    def invoke(self, name: str, *args, flags: int = DISPATCH_METHOD | DISPATCH_PROPERTYGET):
        did = self._dispid(name)
        n = len(args)
        argv = (VARIANT * max(1, n))()
        bstrs: list[int] = []
        # IDispatch 约定：参数倒序放
        for i, a in enumerate(reversed(args)):
            v = argv[i]
            if a is None:
                v.vt = VT_EMPTY
            elif isinstance(a, bool):
                v.vt = VT_BOOL
                v.u.boolVal = -1 if a else 0
            elif isinstance(a, int):
                v.vt = VT_I4
                v.u.lVal = a
            elif isinstance(a, str):
                b = _oleaut32.SysAllocString(a)
                bstrs.append(b)
                v.vt = VT_BSTR
                v.u.ptr = b
            else:
                raise TypeError(f"不支持的 COM 参数类型: {type(a)!r}")
        dp = DISPPARAMS(argv if n else None, None, n, 0)
        result = VARIANT()
        exc = EXCEPINFO()
        argerr = ctypes.c_uint(0)
        try:
            hr = _FN_INVOKE(self._vt[6])(self._p, did, ctypes.byref(GUID()), 0x0400, flags,
                                         ctypes.byref(dp), ctypes.byref(result), ctypes.byref(exc),
                                         ctypes.byref(argerr))
        finally:
            for b in bstrs:
                _oleaut32.SysFreeString(b)
        if hr < 0:
            desc = ""
            if hr == DISP_E_EXCEPTION:
                desc = _bstr_text(exc.bstrDescription).strip()
                for b in (exc.bstrSource, exc.bstrDescription, exc.bstrHelpFile):
                    if b:
                        _oleaut32.SysFreeString(b)
                if exc.scode:
                    hr = exc.scode
            raise ComError(hr, name, desc)
        return _take(result)


def _take(v: VARIANT):
    """把返回 VARIANT 转成 Python 值并接管其资源。"""
    vt = v.vt
    if vt == VT_DISPATCH:
        return Dispatch(v.u.ptr) if v.u.ptr else None      # 引用归我们，不 VariantClear
    if vt == VT_BSTR:
        s = _bstr_text(v.u.ptr)
        _oleaut32.VariantClear(ctypes.byref(v))
        return s
    if vt == VT_I4:
        return v.u.lVal
    if vt == VT_I2:
        return v.u.iVal
    if vt == VT_BOOL:
        return v.u.boolVal != 0
    if vt != VT_EMPTY:
        _oleaut32.VariantClear(ctypes.byref(v))
    return None


class _Com:
    """当前线程的 COM 初始化。线程已按别的模式初始化过（RPC_E_CHANGED_MODE）也能用，只是不负责反初始化。"""

    def __enter__(self):
        hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self._owned = hr >= 0
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            raise ComError(hr, "CoInitializeEx")
        return self

    def __exit__(self, *exc):
        if self._owned:
            _ole32.CoUninitialize()
        return False


def _root_folder() -> tuple[Dispatch, Dispatch]:
    ptr = ctypes.c_void_p(0)
    hr = _ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_TASK_SCHEDULER)), None, CLSCTX_INPROC_SERVER,
                                 ctypes.byref(_guid(IID_IDISPATCH)), ctypes.byref(ptr))
    if hr < 0:
        raise ComError(hr, "CoCreateInstance(Schedule.Service)")
    svc = Dispatch(ptr.value)
    svc.invoke("Connect", flags=DISPATCH_METHOD)
    folder = svc.invoke("GetFolder", "\\", flags=DISPATCH_METHOD)
    return svc, folder


def _not_found(e: ComError) -> bool:
    return e.hresult in (HRESULT_FILE_NOT_FOUND, HRESULT_PATH_NOT_FOUND)


# ---------------- 对外 API ----------------


def query(name: str = TASK_NAME) -> TaskInfo | None:
    """读任务定义。不存在返回 None；其他 COM 错误抛 ComError。"""
    with _Com():
        svc, folder = _root_folder()
        try:
            try:
                task = folder.invoke("GetTask", name, flags=DISPATCH_METHOD)
            except ComError as e:
                if _not_found(e):
                    return None
                raise
            try:
                return parse_xml(task.invoke("Xml", flags=DISPATCH_PROPERTYGET) or "")
            finally:
                task.release()
        finally:
            folder.release()
            svc.release()


def register(action: TaskAction, name: str = TASK_NAME, run_level: str = RUN_HIGHEST) -> None:
    """登记（或覆盖）任务。最高权限需要调用方已提权。"""
    user = current_user()
    xml_text = build_xml(action, user, run_level)
    with _Com():
        svc, folder = _root_folder()
        try:
            task = folder.invoke("RegisterTask", name, xml_text, TASK_CREATE_OR_UPDATE,
                                 user, None, TASK_LOGON_INTERACTIVE_TOKEN, None, flags=DISPATCH_METHOD)
            if task is not None:
                task.release()
        finally:
            folder.release()
            svc.release()


def delete(name: str = TASK_NAME) -> bool:
    """删除任务。不存在返回 False。"""
    with _Com():
        svc, folder = _root_folder()
        try:
            folder.invoke("DeleteTask", name, 0, flags=DISPATCH_METHOD)
            return True
        except ComError as e:
            if _not_found(e):
                return False
            raise
        finally:
            folder.release()
            svc.release()


def run(name: str = TASK_NAME) -> None:
    """按需运行任务（普通权限即可，只要当前用户是任务作者）。"""
    with _Com():
        svc, folder = _root_folder()
        try:
            task = folder.invoke("GetTask", name, flags=DISPATCH_METHOD)
            try:
                running = task.invoke("Run", None, flags=DISPATCH_METHOD)
                if running is not None:
                    running.release()
            finally:
                task.release()
        finally:
            folder.release()
            svc.release()


def ensure(action: TaskAction, name: str = TASK_NAME) -> bool:
    """任务不存在或已不指向当前程序 → 重新登记。返回是否做了登记。"""
    if points_to_app(query(name), action):
        return False
    register(action, name)
    return True
