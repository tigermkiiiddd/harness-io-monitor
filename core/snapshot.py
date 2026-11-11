"""快照构建：Sampler 原始数据 + SQLite 历史 + 磁盘健康 → 前端 Snapshot 契约字典。

口径照搬旧 desktop.py（_refresh_top / _refresh_tree / _push_history / _load_history_from_db /
_load_disk_history / _render_disk_*），字段结构以 docs/rebuild-plan.md 的 Snapshot 契约为准。

约束：
    · 本模块不 import webview / tkinter，也不 import core.smart。
      磁盘健康由 app.py 的后台线程调 smart.read_disks() 后经 set_disks() 注入。
    · store / classifier / snap 都按鸭子类型使用，单测可以直接用替身对象。
"""

from __future__ import annotations

import bisect
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime

RANGE_SPANS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}
DEFAULT_RANGE = "5m"
SERIES_POINTS = 240        # 堆叠图时间序列最多点数
CURVE_POINTS = 120         # 单盘寿命下降曲线最多点数
PROC_LIMIT = 40            # 展开某个 agent 时最多给多少条进程
MEM_CAP = 20000            # 内存里每条历史序列最多保留的点数（约 5.5 小时 @1Hz）
TODAY_TTL = 5.0            # 今日增量读库节流（同旧 _refresh_top 的 5 秒）
SUMMARY_TTL = 2.0          # harness_summary 缓存秒数
DB_SERIES_TTL = 60.0       # 区间历史从库里重读的周期
DISK_DB_TTL = 60.0         # 磁盘历史从库里重读的周期（与落库周期一致）
DISK_DB_LIMIT = 12000      # 60 秒一条，7 天约 10080 条
MIN_PERDAY_SECONDS = 60.0  # 覆盖时长不足 60 秒，折算到每天没有意义 → null


@dataclass
class View:
    """前端视图参数，每次 set_view / 切换时整体下发。"""

    range_key: str = DEFAULT_RANGE
    expanded: set[str] = field(default_factory=set)
    show_ide: bool = False
    paused: bool = False
    admin: bool = False


# ---------------- 纯函数（可单测）----------------


def health_of(wear: int | None) -> str:
    """与旧 _disk_color 同阈值：>=80 crit，>=60 warn，其余 ok；读不到 unknown。"""
    if wear is None:
        return "unknown"
    if wear >= 80:
        return "crit"
    if wear >= 60:
        return "warn"
    return "ok"


def remaining_of(wear: int | None) -> int | None:
    """剩余寿命 = 100 - wear（wear 是 NVMe Percentage Used 原始字段）。界面只显示剩余寿命。"""
    if wear is None:
        return None
    return max(0, 100 - wear)


def _pick_indices(n: int, k: int) -> list[int]:
    """从 n 个点里等距挑出不超过 k 个下标，首尾必留。"""
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def wear_estimate(
    points: list[tuple[float, int]],
    t_start: float,
    now: float,
    wear: int | None,
    data_written: int | None,
) -> dict:
    """寿命下降估算（docs/rebuild-plan.md「寿命下降估算」）。

    bytesPerPct = data_written_bytes / wear，仅当 wear >= 1 且 data_written > 0 时成立。
    剩余寿命 100%（wear == 0）时寿命下降按盘的口径记 0，曲线为一条 0 线；
    读不到寿命（wear is None，如机械盘）时 rangeWearPct / perDayPct / curve 全部为 None。

    points：[(ts, written_bytes), ...] 升序；只取 [t_start, now] 内的点。
    起点值 = 区间内第一个点；rangeWrittenBytes = 最新值 - 起点值（负数按 0）。
    点数不足 2 个时四个字段全部为 None（没有区间可算）。
    """
    out: dict = {
        "rangeWrittenBytes": None,
        "rangeWearPct": None,
        "perDayPct": None,
        "curve": None,
    }
    pts = [(t, v) for t, v in points if t_start <= t <= now]
    if len(pts) < 2:
        return out

    start = pts[0][1]
    rng = max(0, pts[-1][1] - start)
    out["rangeWrittenBytes"] = int(rng)

    if wear is None:
        return out
    covered = pts[-1][0] - pts[0][0]             # 实际覆盖秒数

    if wear < 1:
        # 盘报剩余寿命 100%：按盘的口径寿命没掉，显示 0 而不是「—」
        out["rangeWearPct"] = 0.0
        out["perDayPct"] = 0.0 if covered >= MIN_PERDAY_SECONDS else None
        idx = _pick_indices(len(pts), CURVE_POINTS)
        out["curve"] = {"t": [pts[i][0] for i in idx], "wear": [0.0] * len(idx)}
        return out

    if not data_written or data_written <= 0:
        return out

    bpp = data_written / wear                    # 每 1 个百分点寿命对应的写入字节数
    range_wear = rng / bpp                       # 区间寿命下降（百分点）
    out["rangeWearPct"] = range_wear
    out["perDayPct"] = range_wear / covered * 86400 if covered >= MIN_PERDAY_SECONDS else None

    # 曲线：从 0 起、单调不减（计数器偶发回退时取running max，不让曲线往回掉）
    t_out: list[float] = []
    w_out: list[float] = []
    peak = 0.0
    for i in _pick_indices(len(pts), CURVE_POINTS):
        t, v = pts[i]
        peak = max(peak, max(0.0, (v - start) / bpp))
        t_out.append(t)
        w_out.append(peak)
    out["curve"] = {"t": t_out, "wear": w_out}
    return out


def resample(points, t_start: float, now: float, ids: list[str]) -> tuple[list[float], dict[str, list[float]]]:
    """把 [(ts, {agentId: bps})] 重采样到 SERIES_POINTS 个等宽时间桶。

    桶 i 覆盖 (t_start + i*step, t_start + (i+1)*step]，值 = 桶内各点的平均；
    空桶（采样断档，比如程序没开）记 0，不做插值，避免把断档画成假数据。
    t 每个点是桶右端时间，保证最后一点正好落在 now。
    """
    n = SERIES_POINTS
    step = (now - t_start) / n
    t = [t_start + (i + 1) * step for i in range(n)]
    sums = {k: [0.0] * n for k in ids}
    counts = [0] * n
    for ts, rates in points:
        if ts <= t_start or ts > now:
            continue
        i = min(n - 1, max(0, math.ceil((ts - t_start) / step) - 1))
        counts[i] += 1
        for k in ids:
            sums[k][i] += rates.get(k, 0.0)
    by = {k: [(sums[k][i] / counts[i]) if counts[i] else 0.0 for i in range(n)] for k in ids}
    return t, by


# ---------------- 构建器 ----------------


class SnapshotBuilder:
    """持有内存历史，把每次采样算成前端快照。所有方法线程安全（同一把 RLock）。"""

    def __init__(self, store, classifier, log=print) -> None:
        self._store = store
        self._clf = classifier
        self._log = log
        self._lock = threading.RLock()
        self._errs: set[str] = set()

        # agent 速率历史：[(ts, {agentId: bps})]，只含非 IDE harness（同旧 _push_history）
        self._mem: list[tuple[float, dict[str, float]]] = []
        self._mem_ts: list[float] = []
        # 数据库里的区间历史（按当前区间加载，约 60 秒重读一次）
        self._db: list[tuple[float, dict[str, float]]] = []
        self._db_key: str | None = None
        self._db_at: float | None = None

        # 磁盘
        self._disks: list = []
        self._disk_error = ""
        self._disk_mem: dict[str, list[tuple[float, int]]] = {}
        self._disk_mem_ts: dict[str, list[float]] = {}
        self._disk_db: dict[str, tuple[list[tuple[float, int]], float]] = {}

        # 库缓存
        self._today_val = 0
        self._today_at: float | None = None
        self._sum_val: dict[str, dict] = {}
        self._sum_at: float | None = None

    # ---------- 对外 ----------

    def set_disks(self, disks, error: str = "") -> None:
        """后台线程每 60 秒调一次：注入最新的磁盘健康列表（smart.DiskHealth 或同形对象）。"""
        with self._lock:
            self._disks = [d for d in disks if getattr(d, "name", "")]
            self._disk_error = error or next(
                (getattr(d, "error", "") for d in disks if getattr(d, "error", "")), ""
            )

    def build(self, snap, view: View) -> dict:
        """snap 为 Sampler.latest() 的结果（需有 ts / aggs / processes / exited / disk_written / total_bps()）。"""
        with self._lock:
            return self._build(snap, view)

    def reset_memory(self) -> None:
        """「清除数据」之后调用：清掉内存历史与缓存（库已由 store.reset 清空）。"""
        with self._lock:
            self._mem.clear()
            self._mem_ts.clear()
            self._db = []
            self._db_key = None
            self._db_at = None
            self._disk_mem.clear()
            self._disk_mem_ts.clear()
            self._disk_db.clear()
            self._today_at = None
            self._sum_at = None

    # ---------- 主流程 ----------

    def _build(self, snap, view: View) -> dict:
        now = float(snap.ts)
        key = view.range_key if view.range_key in RANGE_SPANS else DEFAULT_RANGE
        span = RANGE_SPANS[key]
        t_start = now - span

        self._record(snap, now)
        agents = self._agents(snap, view, now, span)

        ids = [a["id"] for a in agents if not a["isIde"]]
        pts = self._series_points(key, now, t_start)
        series_t, by_agent = resample(pts, t_start, now, ids)

        disks = [self._disk_out(d, now, t_start) for d in self._disks]

        return {
            "ts": now,
            "paused": bool(view.paused),
            "admin": bool(view.admin),
            "notice": self._notice(view),
            "range": {"key": key, "span": span},
            "totals": {
                "rateBps": float(snap.total_bps()),
                "todayBytes": self._today_bytes(now),
                "activeAgents": sum(1 for a in agents if a["live"]),
            },
            "series": {"t": series_t, "byAgent": by_agent},
            "agents": agents,
            "disks": disks,
        }

    def _notice(self, view: View) -> str:
        if view.paused:
            return "已暂停"
        if not view.admin:
            return "需要管理员权限才能读取磁盘寿命"
        if not self._disks and self._disk_error:
            return f"读盘失败: {self._disk_error}"
        return ""

    # ---------- 记录内存历史（每次 build 记一个点，同 ts 只记一次）----------

    def _record(self, snap, now: float) -> None:
        rates: dict[str, float] = {}
        for hid, a in snap.aggs.items():
            rule = self._clf.by_id.get(hid)
            if rule is None or rule.is_ide:
                continue
            rates[hid] = float(a.write_bps)
        if not self._mem_ts or now > self._mem_ts[-1]:
            self._mem.append((now, rates))
            self._mem_ts.append(now)
            self._trim(self._mem, self._mem_ts)

        for did, written in (getattr(snap, "disk_written", None) or {}).items():
            ts_list = self._disk_mem_ts.setdefault(did, [])
            pts = self._disk_mem.setdefault(did, [])
            if ts_list and now <= ts_list[-1]:
                continue
            pts.append((now, int(written)))
            ts_list.append(now)
            self._trim(pts, ts_list)

    @staticmethod
    def _trim(seq: list, ts_seq: list) -> None:
        if len(seq) > MEM_CAP + 1000:
            cut = len(seq) - MEM_CAP
            del seq[:cut]
            del ts_seq[:cut]

    # ---------- 区间历史 ----------

    def _series_points(self, key: str, now: float, t_start: float) -> list[tuple[float, dict[str, float]]]:
        self._refresh_db_series(key, now, now - t_start)
        # 库里的点只取「内存历史最早一点之前」的部分，两段首尾相接、不重叠
        cut = self._mem_ts[0] if self._mem_ts else float("inf")
        db_ts = [p[0] for p in self._db]
        lo = bisect.bisect_right(db_ts, t_start)
        hi = bisect.bisect_left(db_ts, cut)
        db_part = self._db[lo:hi]
        i0 = bisect.bisect_left(self._mem_ts, t_start)
        return db_part + self._mem[i0:]

    def _refresh_db_series(self, key: str, now: float, span: float) -> None:
        if self._db_key == key and self._db_at is not None and now - self._db_at < DB_SERIES_TTL:
            return
        bucket = max(1, int(span / 400))
        rows = self._try(
            "读区间历史",
            lambda: self._store.series(now - span, bucket_sec=bucket),
            [],
        )
        by_ts: dict[float, dict[str, float]] = {}
        for r in rows:
            harness = r["harness"]
            rule = self._clf.by_id.get(harness)
            if rule is None or rule.is_ide:
                continue
            by_ts.setdefault(float(r["bucket"]), {})[harness] = float(r["w"] or 0.0)
        self._db = sorted(by_ts.items(), key=lambda x: x[0])
        self._db_key = key
        self._db_at = now

    # ---------- agent 列表（照搬旧 _refresh_tree 的并集逻辑）----------

    def _agents(self, snap, view: View, now: float, span: float) -> list[dict]:
        since = now - span
        range_sum = self._try("区间汇总", lambda: self._store.sum_since(since), {})
        hsum = self._summary(now)
        expanded = set(view.expanded)

        out: list[dict] = []
        seen: set[str] = set()

        # 1. 本次运行有活跃进程的
        for hid, a in snap.aggs.items():
            rule = self._clf.by_id.get(hid)
            if rule is None or (rule.is_ide and not view.show_ide):
                continue
            h = hsum.get(hid, {})
            out.append(self._agent_out(
                hid, rule, a, range_sum.get(hid, a.dw),
                max(a.lifetime_write, h.get("write_bytes", 0)),
                a.procs, snap, hid in expanded,
            ))
            seen.add(hid)

        # 2. 数据库里有历史、但本次运行没有活跃进程的（已关掉的 agent 仍要显示）
        for hid, h in hsum.items():
            if hid in seen:
                continue
            rule = self._clf.by_id.get(hid)
            if rule is None or (rule.is_ide and not view.show_ide):
                continue
            if h.get("write_bytes", 0) <= 0 and not h.get("exited"):
                continue
            out.append(self._agent_out(
                hid, rule, None, range_sum.get(hid, 0),
                h.get("write_bytes", 0), h.get("alive", 0),
                snap, hid in expanded,
            ))

        out.sort(key=lambda r: -r["lifetimeBytes"])
        return out

    def _agent_out(self, hid, rule, agg, rng, life, alive, snap, expanded: bool) -> dict:
        live = agg is not None and alive > 0
        return {
            "id": hid,
            "name": rule.display,
            "color": rule.color,
            "isIde": bool(rule.is_ide),
            "live": bool(live),
            "rateBps": float(agg.write_bps) if live else 0.0,
            "rangeBytes": int(rng),
            "lifetimeBytes": int(life),
            "procs": int(alive),
            "processes": self._processes(hid, snap) if expanded else None,
        }

    def _processes(self, hid: str, snap) -> list[dict]:
        """存活进程 + 内存里的已退出 + 数据库里的已退出，合计 ≤ PROC_LIMIT，按写入量降序。"""
        items: list[dict] = []
        shown: set[tuple[int, int]] = set()
        for c in snap.processes:
            if c.harness != hid:
                continue
            p = c.proc
            items.append({
                "pid": int(p.pid), "exe": p.exe, "live": True,
                "writeBytes": int(p.write_bytes), "exitedAt": None,
            })
        for e in snap.exited:
            if e.harness != hid:
                continue
            items.append({
                "pid": int(e.pid), "exe": e.exe, "live": False,
                "writeBytes": int(e.write_bytes), "exitedAt": float(e.exited_at),
            })
            shown.add((e.pid, e.create_time))
        db_rows = self._try("进程明细", lambda: self._store.processes_of(hid, limit=200), [])
        for p in db_rows:
            if p.get("alive") or (p["pid"], p["create_time"]) in shown:
                continue
            items.append({
                "pid": int(p["pid"]), "exe": p["exe"], "live": False,
                "writeBytes": int(p["write_bytes"] or 0), "exitedAt": float(p["last_seen"]),
            })
        items.sort(key=lambda x: -x["writeBytes"])
        return items[:PROC_LIMIT]

    def _summary(self, now: float) -> dict[str, dict]:
        if self._sum_at is None or now - self._sum_at >= SUMMARY_TTL:
            self._sum_val = self._try("汇总", self._store.harness_summary, {})
            self._sum_at = now
        return self._sum_val

    def _today_bytes(self, now: float) -> int:
        if self._today_at is None or now - self._today_at >= TODAY_TTL:
            midnight = datetime.fromtimestamp(now).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            sums = self._try("今日增量", lambda: self._store.sum_since(midnight), {})
            self._today_val = int(sum(sums.values()))
            self._today_at = now
        return self._today_val

    # ---------- 磁盘 ----------

    def _disk_points(self, dev: str, now: float) -> list[tuple[float, int]]:
        cached = self._disk_db.get(dev)
        if cached is None or now - cached[1] >= DISK_DB_TTL:
            rows = self._try(
                "磁盘历史",
                lambda: self._store.disk_health_history(dev, limit=DISK_DB_LIMIT),
                [],
            )
            db = [(float(r["ts"]), int(r["written"])) for r in rows if r.get("written") is not None]
            self._disk_db[dev] = (db, now)
        db = self._disk_db[dev][0]
        mem = self._disk_mem.get(dev, [])
        mem_ts = self._disk_mem_ts.get(dev, [])
        cut = mem_ts[0] if mem_ts else float("inf")
        return [p for p in db if p[0] < cut] + mem

    def _disk_out(self, d, now: float, t_start: float) -> dict:
        dev = str(d.device_id)
        wear = d.wear
        written = d.data_written_bytes
        est = wear_estimate(self._disk_points(dev, now), t_start, now, wear, written)
        return {
            "id": dev,
            "name": d.name,
            "media": d.media_type,
            "sizeBytes": int(d.size_bytes or 0),
            "remainingPct": remaining_of(wear),
            "rangeWearPct": est["rangeWearPct"],
            "perDayPct": est["perDayPct"],
            "writtenBytes": int(written) if written is not None else None,
            "rangeWrittenBytes": est["rangeWrittenBytes"],
            "tempC": d.temperature,
            "powerOnHours": d.power_on_hours,
            "spare": d.available_spare,
            "mediaErrors": d.media_errors,
            "health": health_of(wear),
            "curve": est["curve"],
        }

    # ---------- 容错 ----------

    def _try(self, what: str, fn, default):
        """库读失败不能拖垮界面：返回默认值，同一条错误只打一次日志，避免每秒刷屏。"""
        try:
            return fn()
        except Exception as exc:
            msg = f"[snapshot] {what} 失败: {exc}"
            if msg not in self._errs:
                self._errs.add(msg)
                self._log(msg)
            return default
