"""把进程归到 harness 名下。

匹配优先级（按规则列表顺序，先命中先得）：
    1. 可执行文件路径命中安装目录 / 仓库目录
    2. exe 名精确匹配
    3. 命令行特征
    4. 进程树继承

三个必须处理好的现实：

**dev 版本**。开发版跑的是仓库里的代码，进程命令行可能经过 npm 的 junction
或 symlink，只出现中转路径。所以匹配前先做一次路径真实化（core.pathreal），
再用仓库根目录判定。例：DSH 的入口
    C:\\...\\npm\\node_modules\\@deepseek-ai\\dsh  ->  G:\\projects\\deepseek-harness\\apps\\cli
不解析就会漏掉整个 dev 树。

**shell 不是边界**。agent 常常经由多层 bash 启动子进程（Claude Code 里
跑 `node dist/server.js` 会经过 3 层 bash）。把 bash/sh/cmd 当边界会让父链
提前断掉，整棵子树掉进「未归因」。只有 GUI 启动器才该是边界。

**规则顺序即优先级**。Antigravity 的 sidecar 路径含 .gemini，必须排在 Gemini 前；
ChatGPT 的 MSIX 包名是 OpenAI.Codex，必须排在 Codex 前；Claude Code 装在
Claude Desktop 的 claude-code 子目录里，必须排在 Claude Desktop 前。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .pathreal import resolve_paths
from .winproc import ProcInfo

# 只有 GUI 启动器是边界。shell / 解释器一律允许向上穿透——
# agent 就是这么起子进程的，把 bash 当边界等于放弃整棵子树。
TREE_BOUNDARIES = {
    "explorer",
    "sihost",
    "userinit",
    "wininit",
    "services",
    "svchost",
    "dllhost",
    "runtimebroker",
    "startmenuexperiencehost",
    "shellexperiencehost",
    "windowsterminal",
    "wt",
    "openconsole",
}

EXCLUDED_EXES = {
    "system", "idle", "registry", "memory compression",
    "msmpeng", "searchindexer",
}

MAX_TREE_DEPTH = 24


def _norm(p: str) -> str:
    return p.replace("/", "\\").lower()


@dataclass(slots=True)
class HarnessRule:
    id: str
    display: str
    color: str
    exe: list[str] = field(default_factory=list)
    path_re: list[str] = field(default_factory=list)
    cmd_re: list[str] = field(default_factory=list)
    # 安装根 / 代码仓库根（真实路径）。dev 版本靠这个认。
    repo_paths: list[str] = field(default_factory=list)
    data_dirs: list[str] = field(default_factory=list)
    inherit_tree: bool = True
    show_by_default: bool = True
    is_ide: bool = False
    enabled: bool = True
    _path_pats: list = field(default_factory=list, init=False, repr=False)
    _cmd_pats: list = field(default_factory=list, init=False, repr=False)
    _exe_set: set = field(default_factory=set, init=False, repr=False)
    _repo_norm: list = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.compile()

    def compile(self) -> None:
        self._path_pats = [re.compile(p, re.I) for p in self.path_re]
        self._cmd_pats = [re.compile(p, re.I) for p in self.cmd_re]
        self._exe_set = {e.lower() for e in self.exe}
        self._repo_norm = [
            _norm(str(Path(p).expanduser())).rstrip("\\")
            for p in self.repo_paths if p
        ]

    def match(self, proc: ProcInfo, resolved: str) -> str | None:
        """判进程自身。resolved 是路径真实化后的 path+cmdline 文本。"""
        path = _norm(proc.path or "")
        for pat in self._path_pats:
            if path and pat.search(path):
                return "path"
        if proc.exe in self._exe_set:
            return "exe"
        if resolved:
            low = _norm(resolved)
            for repo in self._repo_norm:
                if repo and repo in low:
                    return "repo"
            for pat in self._cmd_pats:
                if pat.search(resolved):
                    return "cmd"
        return None


@dataclass(slots=True)
class Classified:
    proc: ProcInfo
    harness: str | None
    root_pid: int
    via: str


def default_rules() -> list[HarnessRule]:
    home = Path.home()
    return [
        # ================= 桌面应用 =================
        HarnessRule(
            id="z-code",
            display="Z Code",
            color="#4A90D9",
            exe=["zcode", "z-code"],
            path_re=[r"\\Programs\\ZCode\\"],
            cmd_re=[r'"ZCode\.exe"', r"ZCode\\resources\\"],
            data_dirs=[str(home / ".zcode")],
        ),
        HarnessRule(
            # 实测 exe 是 "Qoder CN.exe"，装在 D 盘
            id="qoder",
            display="Qoder",
            color="#7B61FF",
            exe=["qoder cn", "qoder", "qoder-cn"],
            path_re=[r"\\Programs\\Qoder( CN)?\\", r"\\Roaming\\com\.qodercn"],
            cmd_re=[r"Qoder( CN)?\.exe", r"com\.qodercn"],
            data_dirs=[str(home / ".qoder")],
        ),
        HarnessRule(
            # MSIX 包名 OpenAI.Codex_*，但可执行文件叫 ChatGPT.exe
            id="chatgpt",
            display="ChatGPT",
            color="#10A37F",
            exe=["chatgpt"],
            path_re=[r"\\OpenAI\.Codex_", r"\\Programs\\ChatGPT\\"],
            cmd_re=[r"OpenAI\.Codex_", r"\\Roaming\\Codex\\web"],
            data_dirs=[
                str(home / "AppData/Roaming/Codex"),
                str(home / "AppData/Roaming/ChatGPT"),
            ],
        ),
        HarnessRule(
            # Claude Desktop 自带的 Claude Code，装在 Roaming\Claude\claude-code\<ver>\<hash>\
            # 必须排在 Claude Desktop 之前
            id="claude-code",
            display="Claude Code",
            color="#C15F3C",
            exe=[],
            path_re=[
                r"\\Roaming\\Claude\\claude-code\\",
                r"\\npm\\node_modules\\@anthropic-ai\\claude-code",
                r"\\.local\\share\\claude",
            ],
            cmd_re=[
                r"claude-code[/\\]cli\.js",
                r"@anthropic-ai/claude-code",
                r"claude\.exe\s+--output-format\s+stream-json",
            ],
            data_dirs=[str(home / ".claude")],
        ),
        HarnessRule(
            id="claude-desktop",
            display="Claude Desktop",
            color="#D97757",
            exe=[],
            path_re=[
                r"\\WindowsApps\\Claude_",
                r"\\AnthropicClaude\\",
                r"\\Programs\\Claude\\",
            ],
            cmd_re=[r"WindowsApps\\Claude_", r"AnthropicClaude"],
            data_dirs=[
                str(home / "AppData/Roaming/Claude"),
                str(home / "AppData/Local/AnthropicClaude"),
            ],
        ),
        HarnessRule(
            id="opencode",
            display="OpenCode",
            color="#E8B44A",
            exe=["opencode", "opencode-cli", "opencode-desktop"],
            path_re=[r"@opencode-aidesktop", r"ai\.opencode\.desktop"],
            cmd_re=[r"opencode-cli", r"ai\.opencode\.desktop"],
            data_dirs=[
                str(home / ".local/share/opencode"),
                str(home / ".config/opencode"),
            ],
        ),
        HarnessRule(
            # dev 版、打包版、全局安装三种形态都要覆盖。
            # dev 版命中靠 repo_paths —— 前提是 junction 已解析。
            id="dsh",
            display="DeepSeek Harness",
            color="#5B8DEF",
            exe=["dsh", "deepseek harness", "deepseek-harness"],
            path_re=[
                r"@deepseek-ai\\dsh",
                r"\\node_modules\\@deepseek-ai\\",
                r"\\apps\\deepseek-harness\\",
                r"\\apps\\desktop\\release\\",
                r"\\DeepSeek Harness\\",
                r"\\.dsh\\profiles\\",
            ],
            cmd_re=[
                r"@deepseek-ai/dsh",
                r"\bdsh\b.*--profile",
                r"dsh-desktop",
                r"deepseek-harness[/\\]packages",
            ],
            repo_paths=[],   # 本地 dev 仓库写进 harnesses.json，不要进源码
            data_dirs=[str(home / ".dsh")],
        ),
        HarnessRule(
            # 必须排在 gemini 前：sidecar 路径含 .gemini
            id="antigravity",
            display="Antigravity",
            color="#8E7CC3",
            exe=["antigravity", "agy"],
            path_re=[r"\\Programs\\Antigravity\\", r"\.gemini\\antigravity"],
            cmd_re=[r"Antigravity\.exe", r"antigravity/lib/node_modules"],
            data_dirs=[
                str(home / ".gemini/antigravity"),
                str(home / ".gemini/antigravity-cli"),
            ],
        ),
        # ================= CLI =================
        HarnessRule(
            id="codex-cli",
            display="Codex CLI",
            color="#6E6E6E",
            exe=[],
            path_re=[r"\\codex-runtimes\\"],
            cmd_re=[r"codex-runtimes", r"@openai\\codex", r"@openai/codex"],
            data_dirs=[str(home / ".codex")],
        ),
        HarnessRule(
            id="gemini-cli",
            display="Gemini CLI",
            color="#4285F4",
            exe=[],
            path_re=[r"\\node_modules\\@google\\gemini-cli"],
            cmd_re=[r"@google/gemini-cli", r"gemini-cli[/\\]"],
            data_dirs=[str(home / ".gemini")],
        ),
        HarnessRule(
            id="qwen-code",
            display="Qwen Code",
            color="#615CED",
            exe=[],
            path_re=[r"\\node_modules\\@qwen-code"],
            cmd_re=[r"@qwen-code", r"qwen-code[/\\]"],
            data_dirs=[str(home / ".qwen")],
        ),
        HarnessRule(
            id="kimi-code",
            display="Kimi Code",
            color="#1F1F1F",
            exe=["kimi"],
            path_re=[r"\\MoonshotAI\\", r"\\.kimi-code\\"],
            cmd_re=[r"kimi-code", r"MoonshotAI"],
            data_dirs=[str(home / ".kimi-code")],
        ),
        HarnessRule(
            id="aider",
            display="Aider",
            color="#22B573",
            exe=["aider"],
            cmd_re=[r"aider[/\\](main|__main__)\.py", r"-m\s+aider"],
            data_dirs=[],
        ),
        HarnessRule(
            id="goose",
            display="Goose",
            color="#FF6B35",
            exe=["goose"],
            cmd_re=[r"block/goose", r"\\goose\\"],
            data_dirs=[str(home / ".local/share/goose")],
        ),
        # ================= IDE 整体（默认隐藏）=================
        HarnessRule(
            id="cursor",
            display="Cursor",
            color="#000000",
            exe=["cursor"],
            path_re=[r"\\Programs\\Cursor\\"],
            data_dirs=[str(home / ".cursor"), str(home / "AppData/Roaming/Cursor")],
            show_by_default=False,
            is_ide=True,
        ),
        HarnessRule(
            id="windsurf",
            display="Windsurf",
            color="#09B6A2",
            exe=["windsurf"],
            path_re=[r"\\Programs\\Windsurf\\"],
            data_dirs=[str(home / ".codeium/windsurf")],
            show_by_default=False,
            is_ide=True,
        ),
        HarnessRule(
            id="vscode",
            display="VS Code",
            color="#007ACC",
            exe=[],
            path_re=[r"\\Programs\\Microsoft VS Code\\"],
            data_dirs=[str(home / ".vscode")],
            show_by_default=False,
            is_ide=True,
        ),
        HarnessRule(
            id="zed",
            display="Zed",
            color="#084CCF",
            exe=["zed"],
            path_re=[r"\\Programs\\Zed\\"],
            data_dirs=[str(home / ".local/share/zed")],
            show_by_default=False,
            is_ide=True,
        ),
    ]


class Classifier:
    def __init__(self, rules: list[HarnessRule]) -> None:
        self.rules = [r for r in rules if r.enabled]
        self.by_id = {r.id: r for r in self.rules}
        self._resolved_cache: dict[tuple[int, int], str] = {}

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> "Classifier":
        rules = default_rules()
        if path and Path(path).exists():
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                rules = _merge_rules(rules, raw)
            except Exception as exc:
                print(f"[classify] 读取 {path} 失败，使用内置规则: {exc}")
        return cls(rules)

    def _resolved_text(self, proc: ProcInfo) -> str:
        """路径真实化后的文本，带缓存。junction 解析有系统调用开销。"""
        key = proc.key
        hit = self._resolved_cache.get(key)
        if hit is not None:
            return hit
        raw = f"{proc.path}\n{proc.cmdline}"
        text = resolve_paths(raw)
        self._resolved_cache[key] = text
        return text

    def _match_self(self, proc: ProcInfo) -> tuple[str, str] | None:
        resolved = self._resolved_text(proc)
        for rule in self.rules:
            how = rule.match(proc, resolved)
            if how:
                return rule.id, how
        return None

    def classify(self, procs: list[ProcInfo]) -> list[Classified]:
        by_pid = {p.pid: p for p in procs}
        out: list[Classified] = []
        for proc in procs:
            if proc.exe in EXCLUDED_EXES:
                continue

            self_hit = self._match_self(proc)
            if self_hit:
                out.append(Classified(proc, self_hit[0], proc.pid, self_hit[1]))
                continue

            cur = by_pid.get(proc.ppid)
            hops = 0
            found: tuple[str, str] | None = None
            root_pid = proc.ppid
            while cur is not None and hops < MAX_TREE_DEPTH:
                if cur.exe in TREE_BOUNDARIES:
                    break
                hit = self._match_self(cur)
                if hit:
                    found = hit
                    root_pid = cur.pid
                    break
                root_pid = cur.ppid
                cur = by_pid.get(cur.ppid)
                hops += 1

            if found and self.by_id[found[0]].inherit_tree:
                out.append(Classified(proc, found[0], root_pid, "tree"))
            else:
                out.append(Classified(proc, None, proc.pid, "none"))

        self._prune_cache({p.key for p in procs})
        return out

    def _prune_cache(self, live: set[tuple[int, int]]) -> None:
        if len(self._resolved_cache) > 2000:
            self._resolved_cache = {
                k: v for k, v in self._resolved_cache.items() if k in live
            }


def _merge_rules(base: list[HarnessRule], raw: dict) -> list[HarnessRule]:
    by_id = {r.id: r for r in base}
    ordered = list(base)
    new_rules: list[HarnessRule] = []
    for hid, patch in raw.items():
        if hid.startswith("_"):
            continue
        if hid in by_id:
            cur = by_id[hid]
            for k, v in patch.items():
                if k == "__order__":
                    continue
                if hasattr(cur, k) and not k.startswith("_"):
                    setattr(cur, k, v)
            cur.compile()
        else:
            clean = {
                k: v for k, v in patch.items()
                if k != "__order__" and hasattr(HarnessRule, k)
            }
            clean.setdefault("display", hid)
            clean.setdefault("color", "#888888")
            new_rules.append(HarnessRule(id=hid, **clean))
    ordered.extend(new_rules)
    for hid, patch in raw.items():
        if isinstance(patch, dict) and "__order__" in patch:
            rule = next((r for r in ordered if r.id == hid), None)
            if rule:
                ordered.remove(rule)
                ordered.insert(max(0, min(len(ordered), int(patch["__order__"]))), rule)
    return ordered


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main() -> None:
    import argparse

    from .winproc import Prober

    ap = argparse.ArgumentParser(description="归因自检")
    ap.add_argument("--config", default=None)
    ap.add_argument("--unmatched", action="store_true")
    ap.add_argument("--min-mb", type=float, default=10.0)
    ap.add_argument("--trace", type=int, default=0, help="追踪某个 pid 的归因过程")
    args = ap.parse_args()

    cfg = args.config or Path(__file__).parent.parent / "harnesses.json"
    clf = Classifier.from_config(cfg)
    procs = Prober().sample()
    by_pid = {p.pid: p for p in procs}

    if args.trace:
        cur = by_pid.get(args.trace)
        while cur:
            resolved = clf._resolved_text(cur)
            hit = clf._match_self(cur)
            print(f"pid={cur.pid} {cur.exe}")
            print(f"  path: {cur.path}")
            print(f"  cmd : {(cur.cmdline or '')[:140]}")
            if resolved != f"{cur.path}\n{cur.cmdline}":
                extra = resolved.split("\n", 1)[1]
                print(f"  解析: {extra[:140]}")
            print(f"  命中: {hit}")
            if cur.exe in TREE_BOUNDARIES:
                print("  <- 边界，停止")
                break
            cur = by_pid.get(cur.ppid)
            print()
        return

    res = clf.classify(procs)

    if args.unmatched:
        rows = [
            c for c in res
            if c.harness is None and c.proc.write_bytes >= args.min_mb * 1048576
        ]
        rows.sort(key=lambda c: -c.proc.write_bytes)
        print(f"未归因且写入 ≥ {args.min_mb}MB：{len(rows)} 个\n")
        for c in rows:
            p = c.proc
            parent = by_pid.get(p.ppid)
            ptxt = parent.exe if parent else f"<DEAD {p.ppid}>"
            print(f"{_human(p.write_bytes):>9}  {p.exe:<18} pid={p.pid:<7} parent={ptxt}")
            if p.path:
                print(f"{'':10}{p.path[:150]}")
            if p.cmdline:
                print(f"{'':10}{p.cmdline[:150]}")
        if not rows:
            print("  (无)")
        return

    agg: dict[str, list[Classified]] = {}
    for c in res:
        if c.harness:
            agg.setdefault(c.harness, []).append(c)
    total = sum(c.proc.write_bytes for c in res if c.harness)
    print(f"归因 {sum(len(v) for v in agg.values())}/{len(res)} 进程   合计 {_human(total)}\n")
    for hid, items in sorted(agg.items(), key=lambda kv: -sum(i.proc.write_bytes for i in kv[1])):
        w = sum(i.proc.write_bytes for i in items)
        via: dict[str, int] = {}
        for i in items:
            via[i.via] = via.get(i.via, 0) + 1
        detail = " ".join(f"{k}×{v}" for k, v in sorted(via.items()))
        print(f"  {clf.by_id[hid].display:<18} {_human(w):>9}  {len(items):>4} 进程  [{detail}]")
    print()


if __name__ == "__main__":
    main()
