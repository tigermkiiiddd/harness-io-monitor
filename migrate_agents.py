"""把 agent 数据目录从 C: 迁到 D:，原位置留 Junction。

流程（与已完成的 Z Code 迁移一致）：
    1. 检查源存在、且不是已有的 reparse point
    2. 检查目标不存在
    3. robocopy /MOVE 搬过去（同盘符才瞬移，跨盘是真实拷贝）
    4. 源位置建 Junction 指回 D:
    5. 验证：路径可达、大小一致

失败时不动源目录。只输出 ASCII，避免 PowerShell/cmd 的编码问题。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ["USERPROFILE"])
APPDATA = Path(os.environ["APPDATA"])

# 迁移目标根目录。默认 D:\Users\<用户名>，保持与 C: 盘相同的层级结构，
# 这样 junction 后的路径结构完全可预测。可用环境变量 HIO_MIGRATE_ROOT 覆盖。
_default_root = Path("D:/Users") / Path.home().name
ROOT_DEST = Path(os.environ.get("HIO_MIGRATE_ROOT", _default_root))

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_junction(p: Path) -> bool:
    try:
        st = os.lstat(p)
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def dir_bytes(p: Path, limit_seconds: float = 20.0) -> tuple[int, bool]:
    """有界统计目录大小，返回 (字节, 是否截断)。"""
    import time

    start = time.monotonic()
    total, n, truncated, stack = 0, 0, False, [p]
    while stack:
        if time.monotonic() - start > limit_seconds or n > 300_000:
            truncated = True
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total, truncated


def lock_check(p: Path) -> str:
    """尝试独占打开目录里的第一个锁标记文件，判断是否被占用。"""
    probes = ["Local State", "opencode.db", "LOG", "lockfile", "storage", "projects"]
    for name in probes:
        f = p / name
        if not f.exists():
            continue
        if f.is_dir():
            # 目录：尝试在里头建删一个临时文件
            try:
                t = f / ".hio_probe"
                t.write_bytes(b"")
                t.unlink()
                return "FREE"
            except OSError:
                return "LOCKED"
        try:
            with open(f, "rb+"):
                pass
            return "FREE"
        except OSError:
            return "LOCKED"
    return "UNKNOWN"


def move_one(src: Path, dest: Path, label: str) -> tuple[bool, str]:
    print(f"\n{'=' * 70}")
    print(f"[{label}]")
    print(f"  from: {src}")
    print(f"  to  : {dest}")

    if not src.exists():
        return False, "source missing"
    if is_junction(src):
        return False, f"already a junction -> {os.readlink(src)}"
    if dest.exists():
        return False, "target already exists"

    size_src, trunc = dir_bytes(src)
    print(f"  size: {size_src / 1048576:.1f} MB{' (partial scan)' if trunc else ''}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        # /MOVE 与 /MT 互斥，所以先多线程 COPY，验证通过后再删源。
        # 2 万个小文件时 /MT:32 能显著缩短耗时。
        "robocopy", str(src), str(dest),
        "/E", "/MT:32", "/R:1", "/W:1",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    ]
    print("  copying (multi-thread)...")
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    # robocopy: 0-7 视为成功，>=8 是错误
    if r.returncode >= 8:
        print(f"  robocopy rc={r.returncode}")
        print(r.stdout[-1500:])
        return False, f"robocopy failed rc={r.returncode}"

    # COPY 成功后，确认目标完整再删源（而不是让 /MOVE 自己删）
    size_after, _ = dir_bytes(dest)
    if size_src and size_after / size_src < 0.98:
        return False, f"copy incomplete: src={size_src} dest={size_after}"
    print("  removing source...")
    r2 = subprocess.run(
        ["cmd", "/c", "rmdir", "/S", "/Q", str(src)],
        capture_output=True, text=True, errors="replace",
    )
    if src.exists():
        leftovers = [x.name for x in list(src.iterdir())[:5]] if src.exists() else []
        return False, f"source not removed: {leftovers}"

    # /MOVE 之后源目录可能还留空壳
    if src.exists():
        try:
            src.rmdir()
        except OSError:
            leftovers = list(src.iterdir())[:5]
            return False, f"source not empty: {[x.name for x in leftovers]}"

    # 建 junction
    m = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(src), str(dest)],
        capture_output=True, text=True, errors="replace",
    )
    if m.returncode != 0:
        return False, f"mklink failed: {m.stdout or m.stderr}"

    if not src.exists():
        return False, "junction created but path not reachable"

    size_dest, _ = dir_bytes(dest)
    ratio = (size_dest / size_src) if size_src else 1.0
    if ratio < 0.98:
        return False, f"size mismatch: src={size_src} dest={size_dest}"

    print(f"  OK  moved {size_dest / 1048576:.1f} MB, junction in place")
    return True, "ok"


TASKS = [
    ("Claude .claude", HOME / ".claude", ROOT_DEST / ".claude"),
    ("Claude Roaming", APPDATA / "Claude", ROOT_DEST / "AppData/Roaming/Claude"),
    ("Qoder .qoder", HOME / ".qoder", ROOT_DEST / ".qoder"),
    ("Qoder Roaming", APPDATA / "com.qodercn.app.stable",
     ROOT_DEST / "AppData/Roaming/com.qodercn.app.stable"),
    ("ChatGPT/Codex Roaming", APPDATA / "Codex", ROOT_DEST / "AppData/Roaming/Codex"),
    ("Codex .codex", HOME / ".codex", ROOT_DEST / ".codex"),
    ("OpenCode config", HOME / ".config/opencode", ROOT_DEST / ".config/opencode"),
    ("OpenCode share", HOME / ".local/share/opencode",
     ROOT_DEST / ".local/share/opencode"),
    ("OpenCode Roaming", APPDATA / "ai.opencode.desktop",
     ROOT_DEST / "AppData/Roaming/ai.opencode.desktop"),
    ("DeepSeek Harness .dsh", HOME / ".dsh", ROOT_DEST / ".dsh"),
    # Antigravity 的数据全在 .gemini 下（antigravity / backup / browser-profile / cli / ide）。
    # 注意：%LOCALAPPDATA%\Programs\Antigravity 是程序本体，不迁移。
    ("Antigravity .gemini", HOME / ".gemini", ROOT_DEST / ".gemini"),
    ("Antigravity Roaming", APPDATA / "Antigravity",
     ROOT_DEST / "AppData/Roaming/Antigravity"),
]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = args[0] if args else None
    dry = "--dry" in sys.argv

    print("=" * 70)
    print("Agent data migration: C: -> D: (junction)")
    print("=" * 70)

    if dry:
        for label, src, dest in TASKS:
            if only and only.lower() not in label.lower():
                continue
            if not src.exists():
                print(f"  {label:<24} MISSING")
                continue
            if is_junction(src):
                print(f"  {label:<24} ALREADY JUNCTION")
                continue
            size, trunc = dir_bytes(src)
            mark = "?" if trunc else " "
            print(f"  {label:<24} {size/1048576:9.1f} MB{mark}   lock={lock_check(src)}")
        return 0

    ok_all = True
    for label, src, dest in TASKS:
        if only and only.lower() not in label.lower():
            continue
        if not src.exists():
            print(f"\n[{label}] skip: missing")
            continue
        if is_junction(src):
            print(f"\n[{label}] skip: already junction")
            continue
        state = lock_check(src)
        if state == "LOCKED":
            print(f"\n[{label}] SKIP: locked (app is running)")
            ok_all = False
            continue
        ok, msg = move_one(src, dest, label)
        if not ok:
            print(f"  FAILED: {msg}")
            ok_all = False

    print("\n" + "=" * 70)
    print("RESULT:", "ALL OK" if ok_all else "SOME SKIPPED/FAILED (see above)")
    print("=" * 70)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
