"""core/snapshot.py 单测：寿命下降估算、区间重采样、健康阈值、agent 列表口径。

全部用替身对象（FakeStore / FakeSnap / FakeClf），不碰真库，不 import webview / tkinter。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.snapshot import (  # noqa: E402
    CURVE_POINTS,
    SERIES_POINTS,
    SnapshotBuilder,
    View,
    health_of,
    remaining_of,
    wear_estimate,
)

GiB = 2**30
TiB = 2**40


# ---------------- 替身对象 ----------------


@dataclass
class FakeRule:
    display: str
    color: str
    is_ide: bool = False


class FakeClf:
    def __init__(self, rules: dict[str, FakeRule]) -> None:
        self.by_id = rules


@dataclass
class FakeAgg:
    harness: str
    write_bps: float = 0.0
    dw: int = 0
    procs: int = 0
    lifetime_write: int = 0


@dataclass
class FakeProc:
    pid: int
    exe: str = "x.exe"
    create_time: int = 1
    write_bytes: int = 0


@dataclass
class FakeClassified:
    harness: str
    proc: FakeProc


@dataclass
class FakeExited:
    harness: str
    pid: int
    create_time: int
    exe: str
    write_bytes: int
    exited_at: float


@dataclass
class FakeSnap:
    ts: float
    aggs: dict[str, FakeAgg] = field(default_factory=dict)
    processes: list = field(default_factory=list)
    exited: list = field(default_factory=list)
    disk_written: dict[str, int] = field(default_factory=dict)

    def total_bps(self) -> float:
        return sum(a.write_bps for a in self.aggs.values())


class FakeStore:
    def __init__(self) -> None:
        self.sums: dict[str, int] = {}
        self.summary: dict[str, dict] = {}
        self.series_rows: list[dict] = []
        self.disk_rows: dict[str, list[dict]] = {}
        self.procs: dict[str, list[dict]] = {}

    def sum_since(self, since):
        return dict(self.sums)

    def harness_summary(self):
        return dict(self.summary)

    def series(self, since, bucket_sec=300):
        return list(self.series_rows)

    def disk_health_history(self, device_id, limit=500):
        return list(self.disk_rows.get(device_id, []))[-limit:]

    def processes_of(self, harness, limit=600):
        return list(self.procs.get(harness, []))[:limit]


class FakeDisk:
    """替身 smart.DiskHealth：只保留 snapshot 用到的字段。"""

    def __init__(self, device_id="1", name="INTEL SSD", media_type="NVMe", wear=37,
                 written=118 * TiB, temperature=51, hours=21880, spare=100, merr=0,
                 size=1 * TiB, error=""):
        self.device_id = device_id
        self.name = name
        self.media_type = media_type
        self.size_bytes = size
        self.wear = wear
        self.data_written_bytes = written
        self.temperature = temperature
        self.power_on_hours = hours
        self.available_spare = spare
        self.media_errors = merr
        self.error = error


RULES = {
    "z-code": FakeRule("Z Code", "#7c9cff"),
    "claude-code": FakeRule("Claude Code", "#e8a26a"),
    "cursor": FakeRule("Cursor", "#000000", is_ide=True),
}


def make_builder(store: FakeStore | None = None) -> tuple[SnapshotBuilder, FakeStore]:
    store = store or FakeStore()
    return SnapshotBuilder(store, FakeClf(RULES), log=lambda *_: None), store


# ---------------- 寿命下降估算 ----------------


def test_wear_zero_gives_zero_not_null():
    now = 1_000_000.0
    pts = [(now - 300, 0), (now, GiB)]
    est = wear_estimate(pts, now - 300, now, wear=0, data_written=118 * TiB)
    # 剩余寿命 100%：寿命下降显示 0，而不是「—」
    assert est["rangeWearPct"] == 0
    assert est["perDayPct"] == 0
    assert est["curve"] is not None
    assert set(est["curve"]["wear"]) == {0.0}
    # 区间写入量与寿命无关，仍然可报
    assert est["rangeWrittenBytes"] == GiB


def test_wear_zero_via_builder_shows_zero():
    b, _ = make_builder()
    now = 1_000_000.0
    b.set_disks([FakeDisk(wear=0)])
    snap = FakeSnap(ts=now, disk_written={"1": 0})
    b.build(snap, View())
    snap2 = FakeSnap(ts=now + 300, disk_written={"1": GiB})
    d = b.build(snap2, View(range_key="5m", admin=True))["disks"][0]
    assert d["remainingPct"] == 100
    assert d["rangeWearPct"] == 0
    assert d["perDayPct"] == 0
    assert d["curve"] is not None


def test_wear_estimate_numbers_match_formula():
    now = 1_000_000.0
    t_start = now - 300
    pts = [(t_start, 10 * GiB), (now, 11 * GiB)]          # 区间写 1GB
    est = wear_estimate(pts, t_start, now, wear=37, data_written=118 * TiB)
    bytes_per_pct = 118 * TiB / 37
    expected_wear = GiB / bytes_per_pct
    assert est["rangeWrittenBytes"] == GiB
    assert est["rangeWearPct"] == pytest.approx(expected_wear, rel=1e-12)
    assert est["perDayPct"] == pytest.approx(expected_wear / 300 * 86400, rel=1e-12)


def test_per_day_null_under_60_seconds():
    now = 1_000_000.0
    pts = [(now - 30, 0), (now, GiB)]
    est = wear_estimate(pts, now - 300, now, wear=37, data_written=118 * TiB)
    assert est["rangeWearPct"] is not None          # 区间寿命照算
    assert est["perDayPct"] is None                 # 不足 60 秒不折算到每天


def test_curve_starts_at_zero_monotone_and_capped():
    now = 1_000_000.0
    n = 1000
    pts = []
    for i in range(n):
        t = now - 300 + i * 0.3
        v = 5 * GiB + i * 1_000_000
        if i == 500:           # 模拟计数器偶发回退
            v -= 50_000_000
        pts.append((t, v))
    est = wear_estimate(pts, now - 300, now + 1000, wear=17, data_written=205 * TiB)
    curve = est["curve"]
    assert curve is not None
    assert len(curve["t"]) == len(curve["wear"]) <= CURVE_POINTS
    assert curve["wear"][0] == 0
    assert all(b >= a for a, b in zip(curve["wear"], curve["wear"][1:]))
    assert curve["t"] == sorted(curve["t"])


def test_estimate_none_with_less_than_two_points():
    now = 1_000_000.0
    est = wear_estimate([(now, GiB)], now - 300, now, wear=37, data_written=118 * TiB)
    assert est["rangeWearPct"] is None
    assert est["curve"] is None
    assert est["rangeWrittenBytes"] is None


def test_range_written_negative_clamped_to_zero():
    now = 1_000_000.0
    pts = [(now - 300, 5 * GiB), (now, 4 * GiB)]
    est = wear_estimate(pts, now - 300, now, wear=37, data_written=118 * TiB)
    assert est["rangeWrittenBytes"] == 0
    assert est["rangeWearPct"] == 0


# ---------------- 健康阈值与剩余寿命 ----------------


@pytest.mark.parametrize("wear, expected", [
    (None, "unknown"), (0, "ok"), (59, "ok"), (60, "warn"),
    (79, "warn"), (80, "crit"), (100, "crit"),
])
def test_health_thresholds(wear, expected):
    assert health_of(wear) == expected


def test_remaining_is_100_minus_wear():
    assert remaining_of(37) == 63
    assert remaining_of(0) == 100
    assert remaining_of(None) is None


# ---------------- 区间重采样 ----------------


def test_series_resample_length_and_alignment():
    b, _ = make_builder()
    now = 2_000_000.0
    # 300 秒的 1Hz 历史，超过 240 点
    for i in range(300):
        ts = now - 300 + i + 1
        snap = FakeSnap(ts=ts, aggs={"z-code": FakeAgg("z-code", write_bps=1000.0 + i, procs=2)})
        b.build(snap, View(range_key="5m"))
    snap = FakeSnap(ts=now, aggs={"z-code": FakeAgg("z-code", write_bps=5.0, procs=2)})
    out = b.build(snap, View(range_key="5m"))
    t = out["series"]["t"]
    assert 0 < len(t) <= SERIES_POINTS
    assert len(t) == SERIES_POINTS
    assert t == sorted(t)
    assert t[-1] == pytest.approx(now)
    for arr in out["series"]["byAgent"].values():
        assert len(arr) == len(t)


def test_series_gaps_are_zero_not_interpolated():
    b, _ = make_builder()
    now = 2_000_000.0
    # 只在最后 30 秒有数据
    for i in range(30):
        snap = FakeSnap(ts=now - 30 + i, aggs={"z-code": FakeAgg("z-code", write_bps=100.0, procs=1)})
        b.build(snap, View(range_key="5m"))
    out = b.build(FakeSnap(ts=now, aggs={"z-code": FakeAgg("z-code", write_bps=100.0, procs=1)}),
                  View(range_key="5m"))
    arr = out["series"]["byAgent"]["z-code"]
    assert arr[0] == 0.0          # 窗口最早端没有数据
    assert arr[-1] == pytest.approx(100.0)


def test_series_excludes_ide_and_rows_follow_show_ide():
    b, _ = make_builder()
    now = 2_000_000.0
    snap = FakeSnap(ts=now, aggs={
        "z-code": FakeAgg("z-code", write_bps=10.0, procs=1),
        "cursor": FakeAgg("cursor", write_bps=99.0, procs=1),
    })
    out = b.build(snap, View(range_key="1m", show_ide=False))
    assert [a["id"] for a in out["agents"]] == ["z-code"]
    assert "cursor" not in out["series"]["byAgent"]
    out2 = b.build(snap, View(range_key="1m", show_ide=True))
    assert {a["id"] for a in out2["agents"]} == {"z-code", "cursor"}
    # 活跃数口径：开了显示 IDE 才把 IDE 计入（与旧 _refresh_tree 的过滤一致）
    assert out2["totals"]["activeAgents"] == 2
    assert out["totals"]["activeAgents"] == 1


# ---------------- agent 列表与进程展开 ----------------


def test_processes_only_when_expanded_and_capped_sorted():
    store = FakeStore()
    store.procs["z-code"] = [
        {"pid": 900 + i, "create_time": 77 + i, "exe": "old.exe", "write_bytes": 10 * i,
         "last_seen": 1_999_000.0, "alive": 0} for i in range(60)
    ]
    b, _ = make_builder(store)
    now = 2_000_000.0
    live = [FakeClassified("z-code", FakeProc(pid=i, write_bytes=(i + 1) * 1000)) for i in range(50)]
    exited = [FakeExited("z-code", 9999, 5, "gone.exe", 123456, now - 10)]
    snap = FakeSnap(ts=now, aggs={"z-code": FakeAgg("z-code", write_bps=1.0, procs=50)},
                    processes=live, exited=exited)

    closed = b.build(snap, View(range_key="5m", expanded=set()))
    assert closed["agents"][0]["processes"] is None

    opened = b.build(snap, View(range_key="5m", expanded={"z-code"}))
    procs = opened["agents"][0]["processes"]
    assert len(procs) <= 40
    writes = [p["writeBytes"] for p in procs]
    assert writes == sorted(writes, reverse=True)
    assert any(not p["live"] for p in procs)
    assert any(p["live"] for p in procs)


def test_agent_stopped_from_db_and_lifetime_takes_max():
    store = FakeStore()
    store.summary = {
        "claude-code": {"write_bytes": 5 * GiB, "alive": 0, "exited": 3, "last_seen": 1.0},
        "z-code": {"write_bytes": 2 * GiB, "alive": 1, "exited": 0, "last_seen": 1.0},
    }
    store.sums = {"claude-code": 7 * GiB}
    b, _ = make_builder(store)
    now = 2_000_000.0
    snap = FakeSnap(ts=now, aggs={"z-code": FakeAgg("z-code", write_bps=3.0, dw=50, procs=1,
                                                    lifetime_write=3 * GiB)})
    out = b.build(snap, View(range_key="24h"))
    by = {a["id"]: a for a in out["agents"]}
    assert by["claude-code"]["live"] is False
    assert by["claude-code"]["rateBps"] == 0
    assert by["claude-code"]["rangeBytes"] == 7 * GiB
    assert by["z-code"]["lifetimeBytes"] == 3 * GiB          # max(live 3G, db 2G)
    # 区间增量：库里没有 z-code 的区间汇总时，回退到本次运行的 dw
    assert by["z-code"]["rangeBytes"] == 50


def test_today_bytes_and_rate_totals():
    store = FakeStore()
    store.sums = {"z-code": 100, "claude-code": 23}
    b, _ = make_builder(store)
    snap = FakeSnap(ts=2_000_000.0, aggs={
        "z-code": FakeAgg("z-code", write_bps=40.0, procs=1),
        "claude-code": FakeAgg("claude-code", write_bps=2.0, procs=1),
    })
    out = b.build(snap, View())
    assert out["totals"]["todayBytes"] == 123
    assert out["totals"]["rateBps"] == pytest.approx(42.0)
    assert out["totals"]["activeAgents"] == 2


# ---------------- 顶层字段与提示 ----------------


def test_top_level_contract_fields():
    b, _ = make_builder()
    snap = FakeSnap(ts=2_000_000.0)
    out = b.build(snap, View(range_key="7d", paused=False, admin=True))
    assert set(out) >= {"ts", "paused", "admin", "notice", "range", "totals", "series", "agents", "disks"}
    assert out["range"] == {"key": "7d", "span": 604800}
    assert out["notice"] == ""


def test_notice_priority_paused_then_admin_then_disk_error():
    b, _ = make_builder()
    snap = FakeSnap(ts=2_000_000.0)
    assert b.build(snap, View(paused=True, admin=True))["notice"] == "已暂停"
    assert b.build(snap, View(paused=False, admin=False))["notice"].startswith("需要管理员权限")
    b.set_disks([], error="PowerShell 调用失败: boom")
    assert b.build(snap, View(admin=True))["notice"] == "读盘失败: PowerShell 调用失败: boom"


# ---------------- 磁盘字段 ----------------


def test_disk_fields_and_curve_from_memory_history():
    b, _ = make_builder()
    now = 2_000_000.0
    b.set_disks([FakeDisk(wear=37, written=118 * TiB)])
    for i in range(301):
        ts = now - 300 + i
        snap = FakeSnap(ts=ts, disk_written={"1": 10 * GiB + i * (GiB // 300)})
        b.build(snap, View(range_key="5m", admin=True))
    out = b.build(FakeSnap(ts=now, disk_written={"1": 11 * GiB}), View(range_key="5m", admin=True))
    d = out["disks"][0]
    assert d["id"] == "1"
    assert d["remainingPct"] == 63
    assert d["health"] == "ok"
    assert d["writtenBytes"] == 118 * TiB
    assert d["rangeWearPct"] is not None and d["rangeWearPct"] > 0
    assert d["perDayPct"] is not None
    assert d["curve"] is not None
    assert len(d["curve"]["t"]) <= CURVE_POINTS
    assert d["curve"]["wear"][0] == 0


def test_disk_db_history_merged_before_memory():
    store = FakeStore()
    now = 2_000_000.0
    store.disk_rows["1"] = [
        {"ts": now - 280, "written": 10 * GiB},
        {"ts": now - 220, "written": 10 * GiB + 3 * GiB},
    ]
    b, _ = make_builder(store)
    b.set_disks([FakeDisk(wear=37, written=118 * TiB)])
    out = b.build(FakeSnap(ts=now, disk_written={"1": 11 * GiB}), View(range_key="5m", admin=True))
    d = out["disks"][0]
    # 起点来自库里的 now-280，最新值来自内存的 11G：区间写入 = 1G（11 - 10）
    assert d["rangeWrittenBytes"] == 1 * GiB
    assert d["rangeWearPct"] == pytest.approx(GiB / (118 * TiB / 37), rel=1e-12)


def test_disk_without_wear_is_unknown_and_null():
    b, _ = make_builder()
    b.set_disks([FakeDisk(wear=None, written=None)])
    out = b.build(FakeSnap(ts=2_000_000.0), View(admin=True))
    d = out["disks"][0]
    assert d["health"] == "unknown"
    assert d["remainingPct"] is None
    assert d["rangeWearPct"] is None
    assert d["curve"] is None


def test_set_disks_drops_unnamed_entries():
    b, _ = make_builder()
    b.set_disks([FakeDisk(name=""), FakeDisk(device_id="2", name="WD")], error="")
    out = b.build(FakeSnap(ts=2_000_000.0), View(admin=True))
    assert [d["id"] for d in out["disks"]] == ["2"]
