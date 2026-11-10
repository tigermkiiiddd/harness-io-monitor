"""路径真实化：把 junction / symlink 还原成真实磁盘路径。

为什么需要：DSH 的开发版入口是
    C:\\Users\\<u>\\AppData\\Roaming\\npm\\node_modules\\@deepseek-ai\\dsh
指向
    G:\\projects\\deepseek-harness\\apps\\cli

进程命令行里只出现前者。如果不还原，按「仓库目录」匹配就永远命中不了 dev 版，
只能靠包名硬猜——而包名路径是 npm 的副产品，不该当作身份依据。

实现用 ctypes 读 reparse point，不依赖 pywin32。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C
MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024


class REPARSE_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("ReparseTag", ctypes.c_ulong),
        ("ReparseDataLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
    ]


def _read_junction(path: str) -> str | None:
    """读 junction / symlink 的目标路径。失败返回 None。"""
    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x7
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FSCTL_GET_REPARSE_POINT = 0x000900A8

    kernel32.CreateFileW.restype = w.HANDLE
    kernel32.CreateFileW.argtypes = [
        w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE
    ]
    kernel32.DeviceIoControl.argtypes = [
        w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
        ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p,
    ]

    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == w.HANDLE(-1).value or not handle:
        return None

    try:
        buf = ctypes.create_string_buffer(MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = w.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buf,
            len(buf),
            ctypes.byref(returned),
            None,
        )
        if not ok or returned.value < 8:
            return None

        tag = ctypes.cast(buf, ctypes.POINTER(REPARSE_DATA_BUFFER)).contents.ReparseTag
        if tag not in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
            return None

        # 结构：ReparseTag(4) ReparseDataLength(2) Reserved(2)
        #       SubstituteNameOffset(2) SubstituteNameLength(2)
        #       PrintNameOffset(2) PrintNameLength(2) PathBuffer[...]
        base = 8
        sub_off = ctypes.c_ushort.from_buffer(buf, base).value
        sub_len = ctypes.c_ushort.from_buffer(buf, base + 2).value
        if sub_len == 0:
            return None
        raw = buf.raw[base + 8 + sub_off: base + 8 + sub_off + sub_len]
        target = raw.decode("utf-16-le", errors="ignore")
        # junction 目标可能带 \??\ 前缀
        for prefix in ("\\??\\", "\\\\?\\"):
            if target.startswith(prefix):
                target = target[len(prefix):]
                break
        return target or None
    finally:
        kernel32.CloseHandle(handle)


def resolve_paths(text: str) -> str:
    """把一段文本里出现的路径逐个尝试真实化，返回「原文 + 所有解析结果」。

    只做一次尝试性解析：找到存在的目录前缀，读它的 junction 目标。
    这样代价可控，不需要遍历所有可能的子路径。
    """
    if not text or "\\\\" not in text:
        return text
    extra: list[str] = []
    for token in _candidate_dirs(text):
        real = _resolve_one(token)
        if real and real.lower() != token.lower():
            extra.append(real)
    if not extra:
        return text
    return text + "\n" + "\n".join(extra)


def _candidate_dirs(text: str) -> list[str]:
    """从文本里挑出看起来像绝对路径的目录。"""
    out: list[str] = []
    for raw in text.replace('"', " ").split():
        tok = raw.strip().rstrip(",;")
        if len(tok) < 8:
            continue
        low = tok.lower()
        if ":" not in tok:
            continue
        # Windows 盘符路径，或 git-bash 的 /c/ 形式
        if not (
            (len(tok) > 2 and tok[1] == ":")
            or low.startswith("/c/")
            or low.startswith("/g/")
            or low.startswith("/d/")
        ):
            continue
        out.append(tok)
    return out


def _resolve_one(token: str) -> str | None:
    """对一个路径逐级向上找最近的 reparse point。"""
    try:
        p = Path(token)
        if not p.is_absolute():
            return None
        cur = p
        for _ in range(12):
            if cur.exists() or cur.parent == cur:
                break
            cur = cur.parent
        # 从最近的已存在节点开始向上找 reparse point
        probe = cur
        for _ in range(12):
            if probe.parent == probe:
                break
            try:
                st = os.lstat(probe)
            except OSError:
                probe = probe.parent
                continue
            if getattr(st, "st_reparse_tag", 0) or (st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT if hasattr(st, "st_file_attributes") else False):
                target = _read_junction(str(probe))
                if target:
                    rel = p.relative_to(probe) if p != probe else None
                    real = Path(target)
                    if rel:
                        real = real / rel
                    return str(real)
            probe = probe.parent
    except Exception:
        return None
    return None


def resolve_dir(path: str) -> str:
    """解析一个已知目录（如数据目录）的真实路径。"""
    try:
        p = Path(path)
        if not p.exists():
            return path
        st = os.lstat(p)
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            target = _read_junction(str(p))
            if target:
                return target
    except Exception:
        pass
    return path


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(f"{arg}\n  -> {resolve_dir(arg)}")
    print("\n示例：")
    print(resolve_paths(
        r'"C:\Program Files\nodejs\node.exe" C:\Users\s\AppData\Roaming\npm/node_modules/@deepseek-ai/dsh/lib/bin.js --profile web'
    ))
