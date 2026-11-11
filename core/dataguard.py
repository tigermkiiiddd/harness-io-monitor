"""保护真实数据目录不被测试/清理脚本误删。

这个模块存在的理由：
开发过程中有人（包括 AI agent）反复用 `rm -rf data` 清空真实数据库
来"让目录干净"或"重新测试"，导致用户积累的历史彻底丢失（rm 不进回收站）。

防护措施：
  1. data/ 是用户数据，不是构建产物。只能通过界面上的「清除数据」删除，
     该操作有二次确认且会生成备份。
  2. 测试一律使用临时目录，绝不读写 data/。
  3. 本模块提供 guard_data_dir()，任何想删除 data/ 的代码必须先过这道检查。

用法：在测试或维护脚本开头调用：
    from core.dataguard import assert_not_user_data
    assert_not_user_data(some_path)   # 指向 data/ 会直接抛异常
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

# 真实用户数据目录（相对项目根）
USER_DATA_DIRNAME = "data"
USER_DB_NAME = "monitor.db"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    return project_root() / USER_DATA_DIRNAME


def user_db_path() -> Path:
    return user_data_dir() / USER_DB_NAME


def assert_not_user_data(path: str | Path) -> None:
    """调用方即将删除/覆盖 path 时先过这里。

    指向用户数据目录（或其父级）会被拒绝。
    """
    p = Path(path).resolve()
    protected = user_data_dir().resolve()
    if p == protected or p in protected.parents:
        raise PermissionError(
            f"拒绝操作 {p}：这是用户的真实数据目录。\n"
            f"测试请用 tempfile.TemporaryDirectory()；\n"
            f"清除数据请由用户在界面上点「清除数据」。"
        )
    if p == protected.parent:
        raise PermissionError(
            f"拒绝操作 {p}：它是用户数据目录的父级，删除会连带清掉 data/"
        )


def backup_db(dest: Path | None = None) -> Path | None:
    """把当前数据库连同 WAL 一起备份一份。返回备份路径。

    WAL 模式下只拷 .db 会丢掉未 checkpoint 的数据，必须三个文件一起拷。
    """
    db = user_db_path()
    if not db.exists():
        return None
    if dest is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = user_data_dir() / "backups" / f"monitor-{ts}.db"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    shutil.copy2(db, dest)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    return dest


def data_dir_status() -> dict:
    """给界面/诊断用：数据目录现状。"""
    d = user_data_dir()
    db = user_db_path()
    out = {
        "dir": str(d),
        "exists": d.exists(),
        "db": str(db),
        "db_exists": db.exists(),
        "db_bytes": db.stat().st_size if db.exists() else 0,
    }
    if d.exists():
        total = 0
        try:
            for f in d.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except OSError:
            pass
        out["total_bytes"] = total
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(data_dir_status(), indent=2, ensure_ascii=False))
