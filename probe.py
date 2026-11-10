"""一次性环境探测：找出本机装了哪些 harness、它们的进程签名和写入量。

用法：
    python probe.py            # 人读的报告
    python probe.py --json     # 机器读，用于生成 harnesses.json

输出重点是「签名」——exe 名、安装路径特征、命令行特征，
这些直接决定 classify.py 的匹配规则，不用猜。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.winproc import ProcInfo, Prober, is_admin

HOME = Path.home()

# 已知 harness 的「线索」——只用来在探测时把进程归堆，不是最终规则
SIGNATURES: dict[str, dict] = {
    "z-code": {
        "display": "Z Code",
        "exe": ["zcode", "z-code", "zcode-desktop"],
        "path_re": [r"\\ZCode\\", r"\\zcode\\"],
        "cmd_re": [r"\.zcode\\"],
        "data_dirs": [".zcode"],
    },
    "qoder": {
        "display": "Qoder",
        "exe": ["qoder", "qoder-desktop"],
        "path_re": [r"\\Qoder\\", r"\\qoder\\"],
        "cmd_re": [r"qoder"],
        "data_dirs": [".qoder"],
    },
    "deepseek-harness": {
        "display": "DeepSeek Harness",
        "exe": ["dsh", "deepseek-harness", "deepseek"],
        "path_re": [r"\\DSH\\", r"\\dsh\\", r"\\DeepSeek"],
        "cmd_re": [r"\bdsh\b", r"deepseek[-_]harness"],
        "data_dirs": [".dsh"],
    },
    "opencode": {
        "display": "OpenCode",
        "exe": ["opencode", "opencode-desktop"],
        "path_re": [r"\\opencode", r"\\OpenCode"],
        "cmd_re": [r"opencode"],
        "data_dirs": [".config/opencode", ".local/share/opencode", "AppData/Local/opencode", "AppData/Roaming/opencode"],
    },
    "claude-desktop": {
        "display": "Claude",
        "exe": ["claude"],
        "path_re": [r"\\AnthropicClaude\\", r"\\Claude\\"],
        "cmd_re": [r"AnthropicClaude"],
        "data_dirs": ["AppData/Roaming/Claude", "AppData/Local/AnthropicClaude"],
    },
    "chatgpt-desktop": {
        "display": "ChatGPT",
        "exe": ["chatgpt"],
        "path_re": [r"\\ChatGPT\\", r"\\OpenAI"],
        "cmd_re": [r"chatgpt", r"OpenAI"],
        "data_dirs": ["AppData/Roaming/ChatGPT", "AppData/Local/Programs/ChatGPT"],
    },
    "claude-code": {
        "display": "Claude Code",
        "exe": ["claude"],
        "path_re": [],
        "cmd_re": [r"claude-code", r"\.claude\\"],
        "data_dirs": [".claude"],
    },
    "codex": {
        "display": "Codex",
        "exe": ["codex"],
        "path_re": [r"\\codex"],
        "cmd_re": [r"codex"],
        "data_dirs": [".codex"],
    },
    "gemini-cli": {
        "display": "Gemini CLI",
        "exe": ["gemini"],
        "path_re": [],
        "cmd_re": [r"gemini"],
        "data_dirs": [".gemini"],
    },
    "qwen-code": {
        "display": "Qwen Code",
        "exe": ["qwen"],
        "path_re": [],
        "cmd_re": [r"qwen"],
        "data_dirs": [".qwen"],
    },
    "kimi-code": {
        "display": "Kimi Code",
        "exe": ["kimi"],
        "path_re": [],
        "cmd_re": [r"kimi"],
        "data_dirs": [".kimi-code"],
    },
    "aider": {
        "display": "Aider",
        "exe": ["aider"],
        "path_re": [],
        "cmd_re": [r"aider"],
        "data_dirs": [],
    },
    "cursor": {
        "display": "Cursor",
        "exe": ["cursor"],
        "path_re": [r"\\Cursor\\"],
        "cmd_re": [r"Cursor"],
        "data_dirs": [".cursor", "AppData/Roaming/Cursor"],
        "ide": True,
    },
}

GENERIC_SHELLS = {
    "explorer", "windowsterminal", "conhost", "openconsole", "pwsh",
    "powershell", "cmd", "bash", "wsl", "wt", "code", "sihost",
}

INTERESTING = {
    "node", "python", "python3", "deno", "bun", "msedgewebview2",
    "chrome", "electron", "rg", "ripgrep", "git", "esbuild", "tsserver",
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def dir_size_bounded(root: Path, max_files: int = 200_000, max_seconds: float = 4.0) -> tuple[int, bool]:
    """有界目录体积统计，返回 (字节数, 是否被截断)。

    ~/.zcode 这类目录动辄几十万文件，不能无脑 rglob。
    """
    import time

    start = time.monotonic()
    total = 0
    count = 0
    truncated = False
    stack = [root]
    while stack:
        if count >= max_files or time.monotonic() - start > max_seconds:
            truncated = True
            break
        cur = stack.pop()
        try:
            entries = os.scandir(cur)
        except OSError:
            continue
        with entries:
            for e in entries:
                count += 1
                if count >= max_files:
                    truncated = True
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total, truncated


def match_hint(proc: ProcInfo) -> str | None:
    """宽松匹配，只用于探测归堆。"""
    exe = proc.exe
    path = proc.path.lower()
    cmd = proc.cmdline.lower()
    for hid, sig in SIGNATURES.items():
        if exe in sig["exe"]:
            return hid
        for pat in sig["path_re"]:
            if __import__("re").search(pat, path, __import__("re").I):
                return hid
        for pat in sig["cmd_re"]:
            if __import__("re").search(pat, cmd, __import__("re").I):
                return hid
    return None


def build_tree(procs: list[ProcInfo]) -> dict[int, list[ProcInfo]]:
    tree: dict[int, list[ProcInfo]] = defaultdict(list)
    for p in procs:
        tree[p.ppid].append(p)
    return tree


def attribute(procs: list[ProcInfo]) -> dict[str, list[ProcInfo]]:
    """沿父链向上找最近的命中，遇 shell 边界停。"""
    by_pid = {p.pid: p for p in procs}
    out: dict[str, list[ProcInfo]] = defaultdict(list)
    for p in procs:
        cur = p
        hops = 0
        while cur and hops < 12:
            if cur.exe in GENERIC_SHELLS:
                break
            hit = match_hint(cur)
            if hit:
                out[hit].append(p)
                break
            cur = by_pid.get(cur.ppid)
            hops += 1
    return out


def probe_dirs() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for hid, sig in SIGNATURES.items():
        hits = []
        for rel in sig["data_dirs"]:
            p = HOME / rel
            if p.exists():
                hits.append(str(p))
        if hits:
            found[hid] = {"dirs": hits, "exists": True}
    return found


def probe_procs() -> tuple[dict[str, dict], list[ProcInfo]]:
    procs = Prober().sample()
    groups = attribute(procs)
    summary: dict[str, dict] = {}
    for hid, members in groups.items():
        if not members:
            continue
        write_total = sum(m.write_bytes for m in members)
        read_total = sum(m.read_bytes for m in members)
        exes = sorted({m.exe for m in members})
        summary[hid] = {
            "display": SIGNATURES[hid]["display"],
            "processes": len(members),
            "write_bytes": write_total,
            "read_bytes": read_total,
            "exe_names": exes,
            "sample_paths": sorted({m.path for m in members if m.path})[:4],
            "sample_cmdlines": [
                m.cmdline[:180] for m in members if m.cmdline
            ][:3],
        }
    return summary, procs


def report(procs: list[ProcInfo], groups: dict, dirs: dict) -> None:
    print("=" * 78)
    print(f"AI Harness 环境探测   管理员={is_admin()}   进程总数={len(procs)}")
    print("=" * 78)

    if groups:
        print("\n【已识别的 harness】（按累计写入排序）\n")
        for hid, g in sorted(groups.items(), key=lambda kv: -kv[1]["write_bytes"]):
            print(f"  {g['display']}  ({hid})")
            print(f"    进程 {g['processes']} 个   写 {human(g['write_bytes'])}   读 {human(g['read_bytes'])}")
            print(f"    exe: {', '.join(g['exe_names'])}")
            for p in g["sample_paths"][:2]:
                print(f"    路径: {p}")
            for c in g["sample_cmdlines"][:2]:
                print(f"    命令行: {c}")
            print()
    else:
        print("\n【已识别的 harness】无——当前没有 harness 在运行\n")

    print("【数据目录】\n")
    if dirs:
        for hid, d in sorted(dirs.items()):
            sizes = []
            for path in d["dirs"]:
                total, truncated = dir_size_bounded(Path(path))
                mark = " (部分)" if truncated else ""
                sizes.append(f"{path}  ({human(total)}{mark})")
            for s in sizes:
                print(f"  {SIGNATURES[hid]['display']:<20} {s}")
    else:
        print("  未发现已知 harness 数据目录")
    print()

    # 没被归到任何 harness、但可能是 node/python 的可疑进程
    orphans = [p for p in procs if p.exe in INTERESTING and p.write_bytes > 5 * 1024 * 1024]
    if orphans:
        orphans.sort(key=lambda x: -x.write_bytes)
        print("【未归类的高写入进程】（可能漏配 harness）\n")
        for p in orphans[:15]:
            print(f"  {human(p.write_bytes):>10}  {p.exe:<18} pid={p.pid:<7} {p.path[:60]}")
            if p.cmdline:
                print(f"{'':14}{p.cmdline[:110]}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    groups, procs = probe_procs()
    dirs = probe_dirs()

    if args.json:
        print(json.dumps(
            {"admin": is_admin(), "groups": groups, "dirs": dirs},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        report(procs, groups, dirs)


if __name__ == "__main__":
    main()
