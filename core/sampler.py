"""采样循环：内存持续更新，低频落库。

**内存 vs 磁盘**

进程状态全部在内存里维护，每秒刷新。磁盘只在三个时机写：

    1. 启动时        写一次基线（存活进程的 lifetime 总量）
    2. 每 20 分钟    写一次区间增量快照
    3. 关闭时        强制 flush 未落库的部分

这样即使异常掉电，最多丢 20 分钟的区间增量；而 lifetime 基线在启动时
已经落过，不会丢。代价远小于每秒写库（200+ 行 UPDATE × 60 次/分钟）。

**为什么不能每秒写 proc_runs**

`GetProcessIoCounters` 返回的是进程生命周期累计，它只在进程退出时才"定格"。
把每轮的 lifetime 值写进库并不产生新信息，只是把同一个数字反复覆盖。
真正随时间变化的是「两轮之间的差值」，也就是区间增量 dw ——
那个才需要定期落库。

**排除自身**

只排监控程序自己的进程，不级联排它的子孙。级联会把从同一 shell
启动的其他进程一起排掉，导致测出 0。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .classify import Classified, Classifier
from .store import RunRow, Store
from .winproc import ProcInfo, Prober

EXITED_KEEP = 500   # 内存里保留多少条已退出进程记录


@dataclass
class HarnessAgg:
    """一个 harness 本轮的状态。"""

    harness: str
    write_bps: float = 0.0
    read_bps: float = 0.0
    dw: int = 0                 # 本轮增量
    dr: int = 0
    procs: int = 0
    lifetime_write: int = 0     # 当前存活进程的 lifetime 累计之和
    lifetime_read: int = 0
    # 自上次落库以来累积的增量
    pending_dw: int = 0
    pending_dr: int = 0


@dataclass
class Snapshot:
    """一次采样结果，供 GUI 取用。"""

    ts: float
    aggs: dict[str, HarnessAgg]
    processes: list[Classified]          # 当前存活
    sample_ms: float
    admin: bool = False
    exited: list["ExitedProc"] = field(default_factory=list)   # 已退出（保留展示）
    # 每块盘的累计写入量快照：device_id -> bytes
    disk_written: dict[str, int] = field(default_factory=dict)

    def total_bps(self) -> float:
        return sum(a.write_bps for a in self.aggs.values())

    def total_lifetime_write(self) -> int:
        return sum(a.lifetime_write for a in self.aggs.values())


@dataclass
class _ProcState:
    """内存里跟踪的单个进程。"""

    pid: int
    create_time: int
    harness: str
    exe: str
    path: str
    cmdline: str
    first_seen: float
    last_read: int = 0
    last_write: int = 0

    @property
    def key(self) -> tuple[int, int]:
        return (self.pid, self.create_time)


@dataclass
class ExitedProc:
    """已退出进程的定格快照，用于在界面上保留一行。

    进程退出后 IO 计数器就消失了，所以必须在这里把最后的读数与元信息
    留住，否则用户看不到「它曾经写过多少」。
    """

    harness: str
    pid: int
    create_time: int
    exe: str
    path: str
    cmdline: str
    first_seen: float
    exited_at: float
    read_bytes: int
    write_bytes: int


class Sampler:
    def __init__(
        self,
        store: Store,
        classifier: Classifier,
        interval: float = 1.0,
        exclude_pids: set[int] | None = None,
        persist_minutes: float = 20.0,
    ) -> None:
        self.store = store
        self.clf = classifier
        self.interval = interval
        self.exclude_pids = set(exclude_pids or ())
        self.prober = Prober(with_cmdline=True)
        self.persist_every = max(30.0, persist_minutes * 60.0)

        # 全部状态在内存：key -> _ProcState
        self._procs: dict[tuple[int, int], _ProcState] = {}
        # 已退出、等待落库的进程
        self._finished: dict[tuple[int, int], _ProcState] = {}
        # 已退出进程的展示列表（环形，最多保留 EXITED_KEEP 条）
        self._exited: list[ExitedProc] = []
        # harness -> 待落库增量
        self._pending: dict[str, list[int]] = {}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: Snapshot | None = None
        self._last_persist = time.time()
        self._dirty = False          # 有待落库的增量

        # 磁盘 SMART 轮询。SMART 是内存映射读寄存器，不写闪存，
        # 和进程采样同频（每秒）也不会伤盘。
        self.read_disks = False       # 由 GUI 在拿到权限后开启
        self._disks_cache: list = []
        self._disk_written: dict[str, int] = {}

    # ---------- 控制 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sampler")
        self._thread.start()

    def stop(self) -> None:
        """停止采样，并把内存里的东西全部落盘。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.flush(force=True)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def set_interval(self, seconds: float) -> None:
        self.interval = max(0.25, min(10.0, seconds))

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self._latest

    def time_to_next_persist(self) -> float:
        return max(0.0, self.persist_every - (time.time() - self._last_persist))

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        # 上一轮留下的 alive=1 收尾：它们的 lifetime 值仍然是有效历史
        try:
            self.store.recover_stale()
        except Exception as exc:
            print(f"[sampler] 收尾旧记录失败: {exc}")

        while not self._stop.is_set():
            started = time.perf_counter()
            if not self._paused.is_set():
                try:
                    self._tick()
                except Exception as exc:
                    print(f"[sampler] 采样失败: {exc}")

            # 到点就落库
            if time.time() - self._last_persist >= self.persist_every:
                try:
                    self.flush(force=False)
                except Exception as exc:
                    print(f"[sampler] 落库失败: {exc}")

            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.05, self.interval - elapsed))

    def _tick(self) -> None:
        t0 = time.perf_counter()
        now = time.time()
        raw = self.prober.sample()
        elapsed = max(1e-6, time.perf_counter() - t0)

        raw = [p for p in raw if p.pid not in self.exclude_pids]
        classified = self.clf.classify(raw)

        aggs: dict[str, HarnessAgg] = {}
        seen: set[tuple[int, int]] = set()

        for c in classified:
            p = c.proc
            key = p.key
            seen.add(key)

            # 首次见到：只记基线，增量记 0。
            # lifetime 值包含监控启动前的历史，算进本轮就是惊天误差。
            st = self._procs.get(key)
            if st is None:
                st = _ProcState(
                    pid=p.pid,
                    create_time=p.create_time,
                    harness=c.harness or "",
                    exe=p.exe,
                    path=p.path,
                    cmdline=p.cmdline[:500],
                    first_seen=now,
                    last_read=p.read_bytes,
                    last_write=p.write_bytes,
                )
                self._procs[key] = st
                dw = dr = 0
            else:
                dw = p.write_bytes - st.last_write if p.write_bytes >= st.last_write else 0
                dr = p.read_bytes - st.last_read if p.read_bytes >= st.last_read else 0
                st.last_write = p.write_bytes
                st.last_read = p.read_bytes
                st.harness = c.harness or st.harness

            if c.harness is None:
                continue

            a = aggs.get(c.harness)
            if a is None:
                a = HarnessAgg(harness=c.harness)
                aggs[c.harness] = a
            a.dw += dw
            a.dr += dr
            a.procs += 1
            a.lifetime_write += p.write_bytes
            a.lifetime_read += p.read_bytes

            pend = self._pending.setdefault(c.harness, [0, 0])
            pend[0] += dw
            pend[1] += dr

        if self._pending:
            self._dirty = True

        dt = max(0.2, self.interval)
        for a in aggs.values():
            a.write_bps = a.dw / dt
            a.read_bps = a.dr / dt
            pend = self._pending.get(a.harness)
            if pend:
                a.pending_dw, a.pending_dr = pend[0], pend[1]

        # 进程消失：把它最后一次读数定格，等落库时写出去。
        # 不能直接丢掉——那是这个进程一生写入的最终值，是真实历史。
        vanished = [k for k in self._procs if k not in seen]
        for k in vanished:
            st = self._procs.pop(k)
            if not st.harness:
                continue
            self._finished[k] = st
            self._exited.append(
                ExitedProc(
                    harness=st.harness,
                    pid=st.pid,
                    create_time=st.create_time,
                    exe=st.exe,
                    path=st.path,
                    cmdline=st.cmdline,
                    first_seen=st.first_seen,
                    exited_at=now,
                    read_bytes=st.last_read,
                    write_bytes=st.last_write,
                )
            )
            self._dirty = True
        if len(self._exited) > EXITED_KEEP:
            del self._exited[:-EXITED_KEEP]

        # ---- 磁盘 SMART 轮询（与进程采样同频）----
        # 只在 GUI 确认有管理员权限后开启（read_disks）。
        # 每块 NVMe 一次 IOCTL，7 块盘约 2-3 ms，可以跟着每秒跑。
        if self.read_disks:
            try:
                from . import smart
                self._disks_cache = smart.read_disks(fast=True)
                self._disk_written = {
                    d.device_id: int(d.data_written_bytes)
                    for d in self._disks_cache
                    if d.data_written_bytes is not None
                }
            except Exception:
                pass

        snap = Snapshot(
            ts=now,
            aggs=aggs,
            processes=classified,
            sample_ms=elapsed * 1000,
            exited=list(reversed(self._exited)),
            disk_written=dict(self._disk_written),
        )
        with self._lock:
            self._latest = snap

    # ---------- 落库 ----------

    def flush(self, force: bool = False) -> None:
        """把内存状态写到磁盘。

        * proc_runs: 当前所有存活进程 + 本轮观测到的 lifetime 值
        * samples:   自上次落库以来的区间增量

        force=True 时即使没有增量也会写（关闭时用）。
        """
        now = time.time()

        live: list[RunRow] = []
        for st in list(self._procs.values()):
            if not st.harness:
                continue
            live.append(
                RunRow(
                    harness=st.harness,
                    pid=st.pid,
                    create_time=st.create_time,
                    exe=st.exe,
                    path=st.path,
                    cmdline=st.cmdline,
                    first_seen=st.first_seen,
                    read_bytes=st.last_read,
                    write_bytes=st.last_write,
                    alive=True,
                )
            )

        finished: list[RunRow] = []
        for st in list(self._finished.values()):
            if not st.harness:
                continue
            finished.append(
                RunRow(
                    harness=st.harness,
                    pid=st.pid,
                    create_time=st.create_time,
                    exe=st.exe,
                    path=st.path,
                    cmdline=st.cmdline,
                    first_seen=st.first_seen,
                    read_bytes=st.last_read,
                    write_bytes=st.last_write,
                    alive=False,
                )
            )

        self.store.flush_runs(live, finished, now)
        self._finished.clear()

        if self._pending:
            span = max(1e-6, now - self._last_persist)
            with self._lock:
                snap = self._latest
            procs = {h: a.procs for h, a in (snap.aggs.items() if snap else [])}
            self.store.add_samples(
                now,
                [
                    (hid, pdw / span, pdr / span, pdw, pdr, procs.get(hid, 0))
                    for hid, (pdw, pdr) in self._pending.items()
                ],
                kind="delta",
            )
            self._pending.clear()
            self._dirty = False
        elif force:
            pass

        self._last_persist = now

    def write_baseline(self) -> int:
        """启动时立刻落一次基线：存活进程的 lifetime 总量。

        这样程序一启动，「历史累计」就有数可看，而不是从 0 开始等 20 分钟。
        返回写入的进程数。
        """
        now = time.time()
        snap = self.latest()
        if snap is None:
            return 0
        live: list[RunRow] = []
        for st in list(self._procs.values()):
            if not st.harness:
                continue
            live.append(
                RunRow(
                    harness=st.harness,
                    pid=st.pid,
                    create_time=st.create_time,
                    exe=st.exe,
                    path=st.path,
                    cmdline=st.cmdline,
                    first_seen=st.first_seen,
                    read_bytes=st.last_read,
                    write_bytes=st.last_write,
                    alive=True,
                )
            )
        self.store.flush_runs(live, [], now)
        return len(live)
