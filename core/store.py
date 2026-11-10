"""SQLite 持久化。

两张表：
    proc_runs  一个「进程运行」一行。write_bytes 是该进程的**生命周期累计**
               （GetProcessIoCounters 的值），所以把所有行加起来就是
               「所有被观测过的进程，各自从启动到退出/至今 写了多少」。
               这就是能重建出来的历史。
    samples    定期快照，存**区间增量** dw/dr，用于「今天/本周写了多少」。

身份键是 (pid, create_time)，不是 pid —— Windows 会复用 PID。
用 (pid, create_time) 做唯一索引，重启后同一个进程会被 UPDATE 而不是
重复 INSERT，这样 lifetime 值不会被重复累加。

不做任何自动清理。数据留多久是用户的决定。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    harness      TEXT    NOT NULL,
    pid          INTEGER NOT NULL,
    create_time  INTEGER NOT NULL,
    exe          TEXT    NOT NULL DEFAULT '',
    path         TEXT    NOT NULL DEFAULT '',
    cmdline      TEXT    NOT NULL DEFAULT '',
    first_seen   REAL    NOT NULL,
    last_seen    REAL    NOT NULL,
    read_bytes   INTEGER NOT NULL DEFAULT 0,
    write_bytes  INTEGER NOT NULL DEFAULT 0,
    alive        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_harness ON proc_runs(harness, last_seen);

CREATE TABLE IF NOT EXISTS samples (
    ts        REAL    NOT NULL,
    harness   TEXT    NOT NULL,
    write_bps REAL    NOT NULL DEFAULT 0,
    read_bps  REAL    NOT NULL DEFAULT 0,
    dw        INTEGER NOT NULL DEFAULT 0,
    dr        INTEGER NOT NULL DEFAULT 0,
    procs     INTEGER NOT NULL DEFAULT 0,
    kind      TEXT    NOT NULL DEFAULT 'delta',
    PRIMARY KEY (ts, harness)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(slots=True)
class RunRow:
    """一个进程的一次运行。read/write 是生命周期累计值。"""

    harness: str
    pid: int
    create_time: int
    exe: str
    path: str
    cmdline: str
    first_seen: float
    read_bytes: int
    write_bytes: int
    alive: bool = True


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """兼容旧库：补 kind 列、把唯一索引改成 (pid, create_time)。"""
        with self._lock:
            # samples.kind
            cols = {
                r["name"]
                for r in self.conn.execute("PRAGMA table_info(samples)").fetchall()
            }
            if "kind" not in cols:
                self.conn.execute(
                    "ALTER TABLE samples ADD COLUMN kind TEXT NOT NULL DEFAULT 'delta'"
                )

            # 唯一索引：一个进程运行只应有一行。
            # 旧索引带 first_seen，会导致每次重启都为同一进程新建一行，
            # lifetime 值被重复累加。先去掉重复行再重建。
            self.conn.execute(
                "DELETE FROM proc_runs WHERE run_id NOT IN ("
                "  SELECT MAX(run_id) FROM proc_runs GROUP BY pid, create_time"
                ")"
            )
            self.conn.execute("DROP INDEX IF EXISTS idx_runs_identity")
            try:
                self.conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity "
                    "ON proc_runs(pid, create_time)"
                )
            except sqlite3.IntegrityError:
                pass
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    # ---------- 进程运行记录 ----------

    def recover_stale(self) -> int:
        """把上轮遗留的 alive=1 全部标结束。

        它们的 write_bytes 保留着最后一次观测到的 lifetime 值，
        仍然算有效历史，只是不再增长。
        """
        with self._lock:
            n = self.conn.execute(
                "UPDATE proc_runs SET alive=0 WHERE alive=1"
            ).rowcount
            self.conn.commit()
        return n

    def flush_runs(self, live: list[RunRow], finished: list[RunRow], now: float) -> None:
        """一次性写入所有进程运行记录。

        live     → alive=1，读取最新 lifetime
        finished → alive=0，用退出前最后一次读数结算

        用 (pid, create_time) 做 UPSERT：同一进程跨重启只会更新同一行，
        lifetime 不会被重复累加。
        """
        if not live and not finished:
            return
        sql = (
            "INSERT INTO proc_runs"
            "(harness,pid,create_time,exe,path,cmdline,first_seen,last_seen,"
            " read_bytes,write_bytes,alive) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pid, create_time) DO UPDATE SET "
            "  harness=excluded.harness,"
            "  exe=excluded.exe,"
            "  path=excluded.path,"
            "  cmdline=excluded.cmdline,"
            "  first_seen=MIN(proc_runs.first_seen, excluded.first_seen),"
            "  last_seen=excluded.last_seen,"
            "  read_bytes=excluded.read_bytes,"
            "  write_bytes=excluded.write_bytes,"
            "  alive=excluded.alive"
        )
        with self._lock:
            rows = [
                (
                    r.harness, r.pid, r.create_time, r.exe, r.path, r.cmdline,
                    r.first_seen, now, r.read_bytes, r.write_bytes, 1 if r.alive else 0,
                )
                for r in live
            ]
            rows += [
                (
                    r.harness, r.pid, r.create_time, r.exe, r.path, r.cmdline,
                    r.first_seen, now, r.read_bytes, r.write_bytes, 0,
                )
                for r in finished
            ]
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # ---------- 区间采样 ----------

    def add_samples(
        self,
        ts: float,
        rows: list[tuple[str, float, float, int, int, int]],
        kind: str = "delta",
    ) -> None:
        """rows: [(harness, write_bps, read_bps, dw, dr, procs), ...]"""
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO samples"
                "(ts,harness,write_bps,read_bps,dw,dr,procs,kind) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(ts, h, w, r, dw, dr, n, kind) for h, w, r, dw, dr, n in rows],
            )
            self.conn.commit()

    # ---------- 查询 ----------

    def lifetime_totals(self) -> dict[str, int]:
        """各 harness 的 lifetime 累计写入：所有被观测进程各自启动至今之和。

        这是能重建出来的「历史」——只覆盖被观测过的进程。
        已退出进程用它退出前的最后读数。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT harness, SUM(write_bytes) w FROM proc_runs GROUP BY harness"
            ).fetchall()
        return {r["harness"]: int(r["w"] or 0) for r in rows}

    def sum_since(self, since: float) -> dict[str, int]:
        """自 since 起各 harness 的**区间增量**。不含基线。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT harness, SUM(dw) w FROM samples "
                "WHERE ts >= ? AND kind='delta' GROUP BY harness",
                (since,),
            ).fetchall()
        return {r["harness"]: int(r["w"] or 0) for r in rows}

    def series(self, since: float, bucket_sec: int = 300) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, harness, "
                "AVG(write_bps) w, SUM(dw) dw "
                "FROM samples WHERE ts >= ? AND kind='delta' "
                "GROUP BY bucket, harness ORDER BY bucket",
                (bucket_sec, bucket_sec, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def live_runs(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT harness,pid,exe,path,last_seen,read_bytes,write_bytes "
                "FROM proc_runs WHERE alive=1 ORDER BY write_bytes DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def run_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) n FROM proc_runs").fetchone()
        return int(row["n"])

    # ---------- 维护 ----------

    # 不做自动清理。历史一律保留，只在用户手动点「清除数据」时删除。

    def reset(self, confirm_token: str = "") -> None:
        """清空全部历史。**只能由界面上的「清除数据」按钮调用。**

        加了确认令牌，防止任何自动化代码顺手把用户的历史删掉。
        历史数据属于用户，程序没有资格替用户决定它该保留多久。
        """
        if confirm_token != "user-confirmed-reset":
            raise PermissionError(
                "清除历史必须由用户手动确认；拒绝无令牌的 reset() 调用"
            )
        with self._lock:
            self.conn.execute("DELETE FROM proc_runs")
            self.conn.execute("DELETE FROM samples")
            self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value)
            )
            self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default
