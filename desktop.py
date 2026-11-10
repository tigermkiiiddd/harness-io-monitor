"""独立的桌面监控程序。

双击 `launch.bat` 即可运行，不依赖任何终端、IDE 或 agent。
关掉窗口程序就结束（可选最小化到托盘继续统计）。

界面：
    · 顶部：总写入速率、今日增量、采样耗时、落库倒计时
    · 中部左：harness 排行榜（可排序、点击展开进程明细）
    · 中部右：实时速率曲线（自绘 canvas）
    · 底部：暂停/继续、采样间隔、时间范围、清除数据、导出 CSV
"""

from __future__ import annotations

import csv
import os
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.classify import Classifier          # noqa: E402
from core.sampler import HarnessAgg, Sampler, Snapshot    # noqa: E402
from core.store import Store                  # noqa: E402
from core.winproc import is_admin             # noqa: E402

APP_NAME = "Harness I/O Monitor"
DB_PATH = ROOT / "data" / "monitor.db"
CONFIG_PATH = ROOT / "harnesses.json"

COLORS = {
    "bg": "#1e1f22",
    "panel": "#2b2d31",
    "panel2": "#313338",
    "fg": "#e3e5e8",
    "fg_dim": "#8b8f96",
    "accent": "#4A90D9",
    "border": "#3f4147",
    "warn": "#e8b44a",
    "hot": "#e05252",
}

RANGES = {
    "本次运行": 0,
    "最近 1 小时": 3600,
    "最近 24 小时": 86400,
    "最近 7 天": 7 * 86400,
}
SAMPLE_HISTORY = 3600  # 内存中保留的采样点数，只影响实时曲线窗口，不影响数据库历史


def human(n: float) -> str:
    if n < 0:
        return "-" + human(-n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_rate(n: float) -> str:
    return human(n) + "/s"


class HarnessApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = Store(DB_PATH)
        self.clf = Classifier.from_config(CONFIG_PATH)
        self.sampler = Sampler(
            self.store,
            self.clf,
            interval=1.0,
            exclude_pids={os.getpid()},
        )
        self.session_start = time.time()
        self.history: list[tuple[float, dict[str, float]]] = []
        self.selected_harness: str | None = None
        self.expanded: set[str] = set()
        self.sort_key = "total_write"
        self.sort_desc = True
        self.show_ide = False
        self._last_ui = 0.0
        self._last_db_read = 0.0
        self._refresh_job = None
        self._today_cache: dict[str, int] = {}
        self._lifetime_cache: dict[str, int] = {}

        self._build_ui()
        self.sampler.start()
        # 等第一次采样有了数据，立刻落一次基线，
        # 这样「历史累计」从启动那一刻就有数，不用等 20 分钟
        self.root.after(1500, self._write_baseline)
        self._refresh_job = self.root.after(400, self._refresh)

    def _write_baseline(self) -> None:
        try:
            n = self.sampler.write_baseline()
            if n:
                print(f"[baseline] 已落基线 {n} 个进程")
        except Exception as exc:
            print(f"[baseline] 失败: {exc}")

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.geometry("1180x720")
        r.minsize(900, 560)
        r.configure(bg=COLORS["bg"])

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["fg"],
            rowheight=26,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["panel2"],
            foreground=COLORS["fg_dim"],
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["accent"])],
            foreground=[("selected", "white")],
        )
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["fg"])
        style.configure("TButton", background=COLORS["panel2"], foreground=COLORS["fg"])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["fg"])

        # ---- 顶栏 ----
        top = tk.Frame(r, bg=COLORS["panel"], height=76)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        self.lbl_rate = tk.Label(
            top, text="0 B/s", font=("Segoe UI", 22, "bold"),
            bg=COLORS["panel"], fg=COLORS["accent"],
        )
        self.lbl_rate.pack(side="left", padx=(20, 6), pady=10)
        tk.Label(
            top, text="当前写入", font=("Segoe UI", 9),
            bg=COLORS["panel"], fg=COLORS["fg_dim"],
        ).pack(side="left", pady=(28, 0))

        info = tk.Frame(top, bg=COLORS["panel"])
        info.pack(side="left", padx=32)
        self.lbl_total = self._stat(info, "今日增量写入")
        self.lbl_read = self._stat(info, "本次累计读取")
        self.lbl_read.pack_forget()
        self.lbl_count = self._stat(info, "活跃 harness")

        right = tk.Frame(top, bg=COLORS["panel"])
        right.pack(side="right", padx=16)
        self.lbl_status = tk.Label(
            right, text="", font=("Segoe UI", 9),
            bg=COLORS["panel"], fg=COLORS["fg_dim"],
        )
        self.lbl_status.pack(anchor="e")

        admin = "管理员" if is_admin() else "普通权限（部分系统进程不可见）"
        tk.Label(
            right, text=admin, font=("Segoe UI", 8),
            bg=COLORS["panel"], fg=COLORS["fg_dim"],
        ).pack(anchor="e")

        # ---- 主区 ----
        mid = tk.Frame(r, bg=COLORS["bg"])
        mid.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        # 左：排行榜
        left = tk.Frame(mid, bg=COLORS["panel"])
        left.pack(side="left", fill="both", expand=True)

        head = tk.Frame(left, bg=COLORS["panel2"], height=32)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(
            head, text="  Harness", bg=COLORS["panel2"], fg=COLORS["fg_dim"],
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(side="left", fill="y")

        cols = ("rate", "session", "range", "procs")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", height=18)
        self.tree.heading("#0", text="Harness")
        self.tree.column("#0", width=200, minwidth=140)
        self.tree.heading("rate", text="当前速率")
        self.tree.heading("session", text="生命周期累计")
        self.tree.heading("range", text="区间增量")
        self.tree.heading("procs", text="进程")
        self.tree.column("rate", width=100, anchor="e")
        self.tree.column("session", width=100, anchor="e")
        self.tree.column("range", width=100, anchor="e")
        self.tree.column("procs", width=56, anchor="e")

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        for hid, rule in self.clf.by_id.items():
            try:
                self.tree.tag_configure(hid, foreground=rule.color)
            except tk.TclError:
                pass
        # 已退出进程用灰色，一眼区分
        self.tree.tag_configure("exited", foreground=COLORS["fg_dim"])
        self.tree.tag_configure("alive", foreground=COLORS["fg"])
        # 已停止的 harness（本次运行没有活跃进程，但有历史）
        self.tree.tag_configure("stopped", foreground=COLORS["fg_dim"])
        self.tree.bind("<Double-1>", self._toggle_expand)
        self.tree.bind("<Button-1>", self._on_click)

        # 右：曲线
        rightp = tk.Frame(mid, bg=COLORS["panel"], width=430)
        rightp.pack(side="right", fill="both", padx=(12, 0))
        rightp.pack_propagate(False)

        ch = tk.Frame(rightp, bg=COLORS["panel2"], height=32)
        ch.pack(fill="x")
        ch.pack_propagate(False)
        tk.Label(
            ch, text="  实时写入速率", bg=COLORS["panel2"], fg=COLORS["fg_dim"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", fill="y")
        self.lbl_hover = tk.Label(
            ch, text="", bg=COLORS["panel2"], fg=COLORS["fg_dim"],
            font=("Segoe UI", 8),
        )
        self.lbl_hover.pack(side="right", padx=8)

        self.canvas = tk.Canvas(
            rightp, bg=COLORS["panel"], highlightthickness=0, height=260
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)

        legend = tk.Frame(rightp, bg=COLORS["panel"])
        legend.pack(fill="x", padx=8, pady=(0, 8))
        self.legend = legend

        # ---- 底部工具条 ----
        bar = tk.Frame(r, bg=COLORS["bg"])
        bar.pack(fill="x", padx=12, pady=10)

        self.btn_pause = ttk.Button(bar, text="暂停", width=8, command=self._toggle_pause)
        self.btn_pause.pack(side="left")

        tk.Label(bar, text="采样间隔", bg=COLORS["bg"], fg=COLORS["fg_dim"]).pack(
            side="left", padx=(16, 4)
        )
        self.var_interval = tk.StringVar(value="1 秒")
        ttk.Combobox(
            bar, textvariable=self.var_interval, width=8, state="readonly",
            values=["0.5 秒", "1 秒", "2 秒", "5 秒"],
        ).pack(side="left")
        self.var_interval.trace_add("write", lambda *_: self._apply_interval())

        tk.Label(bar, text="区间", bg=COLORS["bg"], fg=COLORS["fg_dim"]).pack(
            side="left", padx=(16, 4)
        )
        self.var_range = tk.StringVar(value="本次运行")
        cb = ttk.Combobox(
            bar, textvariable=self.var_range, width=11, state="readonly",
            values=list(RANGES.keys()),
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh(force=True))

        self.var_ide = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar, text="显示 IDE 进程", variable=self.var_ide,
            command=self._toggle_ide,
        ).pack(side="left", padx=(16, 0))

        ttk.Button(bar, text="导出 CSV", width=10, command=self._export).pack(side="right")
        ttk.Button(bar, text="清除数据", width=10, command=self._reset).pack(
            side="right", padx=8
        )

        self.lbl_foot = tk.Label(
            bar, text="", bg=COLORS["bg"], fg=COLORS["fg_dim"],
            font=("Segoe UI", 8),
        )
        self.lbl_foot.pack(side="right", padx=16)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _stat(self, parent: tk.Frame, caption: str) -> tk.Label:
        box = tk.Frame(parent, bg=COLORS["panel"])
        box.pack(side="left", padx=(0, 22))
        tk.Label(
            box, text=caption, font=("Segoe UI", 8),
            bg=COLORS["panel"], fg=COLORS["fg_dim"],
        ).pack(anchor="w")
        val = tk.Label(
            box, text="—", font=("Segoe UI", 14, "bold"),
            bg=COLORS["panel"], fg=COLORS["fg"],
        )
        val.pack(anchor="w")
        return val

    # ---------------- 交互 ----------------

    def _on_click(self, event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "tree":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item in self.clf.by_id:
            self.selected_harness = item if self.selected_harness != item else None
            self._refresh_tree(self.sampler.latest(), force=True)

    def _toggle_expand(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item or item not in self.clf.by_id:
            return
        if item in self.expanded:
            self.expanded.discard(item)
        else:
            self.expanded.add(item)
        self._refresh_tree(self.sampler.latest(), force=True)

    def _toggle_ide(self) -> None:
        self.show_ide = bool(self.var_ide.get())
        self._refresh(force=True)

    def _toggle_pause(self) -> None:
        if self.sampler.is_paused():
            self.sampler.resume()
            self.btn_pause.configure(text="暂停")
        else:
            self.sampler.pause()
            self.btn_pause.configure(text="继续")

    def _apply_interval(self) -> None:
        raw = self.var_interval.get().split()[0]
        try:
            self.sampler.set_interval(float(raw))
        except ValueError:
            pass

    def _range_since(self) -> float:
        secs = RANGES.get(self.var_range.get(), 0)
        return self.session_start if secs == 0 else time.time() - secs

    def _export(self) -> None:
        snap = self.sampler.latest()
        if not snap:
            messagebox.showinfo(APP_NAME, "还没有采样数据")
            return
        path = filedialog.asksaveasfilename(
            title="导出 CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"harness-io-{datetime.now():%Y%m%d-%H%M%S}.csv",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(
                    ["harness", "display", "pid", "exe", "write_bps",
                     "write_total_bytes", "read_total_bytes", "path", "cmdline"]
                )
                for c in snap.processes:
                    if not c.harness:
                        continue
                    a = snap.aggs.get(c.harness)
                    w.writerow([
                        c.harness,
                        self.clf.by_id[c.harness].display,
                        c.proc.pid,
                        c.proc.exe,
                        f"{a.write_bps:.1f}" if a else "",
                        c.proc.write_bytes,
                        c.proc.read_bytes,
                        c.proc.path,
                        c.proc.cmdline[:300],
                    ])
            messagebox.showinfo(APP_NAME, f"已导出到\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败: {exc}")

    def _reset(self) -> None:
        if not messagebox.askyesno(
            APP_NAME, "确定清除所有历史数据？\n（当前存活进程的累计值会从下一次采样重新计）"
        ):
            return
        self.store.reset(confirm_token="user-confirmed-reset")
        self.sampler = Sampler(
            self.store, self.clf,
            interval=self.sampler.interval,
            exclude_pids={os.getpid()},
        )
        self.sampler.start()
        self.session_start = time.time()
        self.history.clear()

    def _on_close(self) -> None:
        # 先停刷新定时器：窗口销毁后 after 回调仍会触发，报 invalid command name
        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
        try:
            self.sampler.stop()
        finally:
            self.store.close()
        self.root.destroy()

    # ---------------- 刷新 ----------------

    def _refresh(self, force: bool = False) -> None:
        try:
            snap = self.sampler.latest()
            now = time.time()
            if snap and (force or now - self._last_ui > 0.4):
                self._last_ui = now
                self._push_history(snap)
                self._refresh_top(snap)
                self._refresh_tree(snap)
                self._draw_chart()
        except Exception as exc:
            print(f"[ui] 刷新失败: {exc}")
        finally:
            self._refresh_job = self.root.after(400, self._refresh)

    def _push_history(self, snap: Snapshot) -> None:
        rates = {
            hid: a.write_bps
            for hid, a in snap.aggs.items()
            if self.clf.by_id.get(hid) and not self.clf.by_id[hid].is_ide
        }
        self.history.append((snap.ts, rates))
        if len(self.history) > SAMPLE_HISTORY:
            self.history = self.history[-SAMPLE_HISTORY:]

    def _refresh_top(self, snap: Snapshot) -> None:
        bps = snap.total_bps()
        self.lbl_rate.configure(text=human_rate(bps))
        if bps > 50 * 1048576:
            self.lbl_rate.configure(fg=COLORS["hot"])
        elif bps > 10 * 1048576:
            self.lbl_rate.configure(fg=COLORS["warn"])
        else:
            self.lbl_rate.configure(fg=COLORS["accent"])

        # 从库里读今日增量 + lifetime 累计，这样关掉程序再开也接得上
        if time.time() - self._last_db_read > 5.0:
            try:
                midnight = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                self._today_cache = self.store.sum_since(midnight)
                self._lifetime_cache = self.store.lifetime_totals()
            except Exception:
                pass
            self._last_db_read = time.time()

        today = sum(self._today_cache.values())
        lifetime_db = sum(self._lifetime_cache.values())
        lifetime_live = snap.total_lifetime_write()
        # 内存里的实时值可能比库里新（还没到 20 分钟落库点），取较大者
        lifetime = max(lifetime_db, lifetime_live)

        self.lbl_total.configure(text=human(today))
        self.lbl_count.configure(text=str(len(snap.aggs)))
        wait = self.sampler.time_to_next_persist()
        mm, ss = divmod(int(wait), 60)
        self.lbl_status.configure(
            text=f"采样 {snap.sample_ms:.0f} ms   "
                 f"下次落库 {mm:02d}:{ss:02d}   "
                 f"更新于 {datetime.fromtimestamp(snap.ts):%H:%M:%S}"
        )
        self.lbl_foot.configure(
            text=f"今日增量 {human(today)}   "
                 f"生命周期累计 {human(lifetime)}（所有被观测进程）   "
                 f"{len(snap.processes)} 进程"
        )

    def _refresh_tree(self, snap: Snapshot | None, force: bool = False) -> None:
        """渲染 harness 列表。

        数据来源是两个集合的并集：
          1. 本次运行实时采到的（snap.aggs，含速率）
          2. 数据库里的历史汇总（store.harness_summary）

        只显示集合 1 是错的——应用一关，它就从界面上消失，用户会以为数据丢了。
        实际上进程退出前的读数已经在 proc_runs 里，必须读出来展示。
        """
        if snap is None:
            return
        since = self._range_since()
        range_sum = self.store.sum_since(since) if since > 0 else {}
        try:
            history = self.store.harness_summary()
        except Exception:
            history = {}

        rows = []   # (hid, rule, agg|None, range_bytes, lifetime, alive_n, exited_n)
        seen_hids: set[str] = set()

        # --- 1. 本次运行有活跃进程的 ---
        for hid, a in snap.aggs.items():
            rule = self.clf.by_id.get(hid)
            if rule is None:
                continue
            if rule.is_ide and not self.show_ide:
                continue
            h = history.get(hid, {})
            life = max(a.lifetime_write, h.get("write_bytes", 0))
            rows.append((
                hid, rule, a,
                range_sum.get(hid, a.dw),
                life,
                a.procs,
                h.get("exited", 0),
            ))
            seen_hids.add(hid)

        # --- 2. 数据库里有历史、但本次运行没有活跃进程的 ---
        # 这些是「已经关掉的 agent」，仍然要显示，否则用户以为历史没了
        for hid, h in history.items():
            if hid in seen_hids:
                continue
            rule = self.clf.by_id.get(hid)
            if rule is None:
                continue
            if rule.is_ide and not self.show_ide:
                continue
            if h.get("write_bytes", 0) <= 0 and not h.get("exited"):
                continue
            rows.append((
                hid, rule, None,
                range_sum.get(hid, 0),
                h.get("write_bytes", 0),
                h.get("alive", 0),
                h.get("exited", 0),
            ))

        rows.sort(key=lambda r: -r[4])   # 按生命周期累计排序

        for item in self.tree.get_children(""):
            self.tree.delete(item)

        for hid, rule, a, rng, life, alive_n, exited_n in rows:
            is_live = a is not None and alive_n > 0
            label = rule.display + ("  [IDE]" if rule.is_ide else "")
            if not is_live:
                label += "  · 已停止"

            rate = human_rate(a.write_bps) if (a and is_live) else "—"
            proc_n = str(alive_n)
            if exited_n:
                proc_n += f" (+{exited_n} 退出)"

            self.tree.insert(
                "", "end", iid=hid, text=label,
                values=(rate, human(life), human(rng), proc_n),
                tags=(hid,) if is_live else ("stopped",),
            )

            if hid not in self.expanded:
                continue

            # 展开：活跃进程在前，已退出在后
            live_procs = [c for c in snap.processes if c.harness == hid]
            live_procs.sort(key=lambda c: -c.proc.write_bytes)
            for c in live_procs[:40]:
                self.tree.insert(
                    hid, "end",
                    text=f"    ● {c.proc.exe}  #{c.proc.pid}",
                    values=("", human(c.proc.write_bytes), "", c.via),
                    tags=("alive",),
                )

            # 已退出：先取本次运行内存里的，再从数据库补历史
            gone_mem = [e for e in snap.exited if e.harness == hid]
            gone_mem.sort(key=lambda e: -e.write_bytes)
            shown_pids = set()
            for e in gone_mem[:40]:
                when = datetime.fromtimestamp(e.exited_at).strftime("%H:%M:%S")
                self.tree.insert(
                    hid, "end",
                    text=f"    ○ {e.exe}  #{e.pid}   已退出 {when}",
                    values=("已退出", human(e.write_bytes), "", ""),
                    tags=("exited",),
                )
                shown_pids.add((e.pid, e.create_time))

            if force or len(gone_mem) < 40:
                try:
                    db_procs = self.store.processes_of(hid, limit=200)
                except Exception:
                    db_procs = []
                added = 0
                for p in db_procs:
                    if added >= 40:
                        break
                    if p["alive"]:
                        continue
                    if (p["pid"], p["create_time"]) in shown_pids:
                        continue
                    when = datetime.fromtimestamp(p["last_seen"]).strftime("%m-%d %H:%M")
                    self.tree.insert(
                        hid, "end",
                        text=f"    ○ {p['exe']}  #{p['pid']}   最后 {when}",
                        values=("已退出", human(p["write_bytes"]), "", ""),
                        tags=("exited",),
                    )
                    added += 1

    def _draw_chart(self) -> None:
        cv = self.canvas
        cv.delete("all")
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w < 50 or h < 50 or not self.history:
            return

        pad_l, pad_r, pad_t, pad_b = 44, 10, 10, 22
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        series: dict[str, list[float]] = {}
        for _ts, rates in self.history:
            for hid, v in rates.items():
                series.setdefault(hid, [])
        n = len(self.history)
        for hid in series:
            series[hid] = [
                self.history[i][1].get(hid, 0.0) if i < n else 0.0 for i in range(n)
            ]

        peak = max((max(v) for v in series.values() if v), default=0.0)
        peak = max(peak, 1024.0)

        cv.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill=COLORS["border"])
        cv.create_line(
            pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill=COLORS["border"]
        )
        for frac in (0.0, 0.5, 1.0):
            y = pad_t + plot_h * (1 - frac)
            cv.create_line(pad_l, y, pad_l + plot_w, y, fill=COLORS["border"], dash=(2, 4))
            cv.create_text(
                pad_l - 6, y, text=human_rate(peak * frac), anchor="e",
                fill=COLORS["fg_dim"], font=("Segoe UI", 7),
            )

        if n < 2:
            cv.create_text(
                w / 2, h / 2, text="正在采集…", fill=COLORS["fg_dim"],
                font=("Segoe UI", 9),
            )
            self._draw_legend(series.keys())
            return

        for hid, vals in series.items():
            rule = self.clf.by_id.get(hid)
            if rule is None or rule.is_ide:
                continue
            color = rule.color
            pts = []
            for i, v in enumerate(vals):
                x = pad_l + plot_w * i / (n - 1)
                y = pad_t + plot_h * (1 - min(1.0, v / peak))
                pts.extend([x, y])
            if len(pts) >= 4:
                cv.create_line(*pts, fill=color, width=2, smooth=True)

        self._draw_legend([h for h in series if self.clf.by_id.get(h)
                           and not self.clf.by_id[h].is_ide])

    def _draw_legend(self, hids) -> None:
        for w in self.legend.winfo_children():
            w.destroy()
        shown = [h for h in hids if self.clf.by_id.get(h)][:6]
        for hid in shown:
            rule = self.clf.by_id[hid]
            box = tk.Frame(self.legend, bg=COLORS["panel"])
            box.pack(side="left", padx=(0, 10))
            tk.Label(
                box, text="■", bg=COLORS["panel"], fg=rule.color,
                font=("Segoe UI", 9),
            ).pack(side="left")
            tk.Label(
                box, text=rule.display, bg=COLORS["panel"], fg=COLORS["fg_dim"],
                font=("Segoe UI", 8),
            ).pack(side="left")


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except tk.TclError:
        pass
    HarnessApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
