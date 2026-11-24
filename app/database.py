"""SQLite 持久层：连接管理、建表、清单入库事务。

设计要点：
- 只存清单覆盖区间和场次行，空日期以区间表达，不逐日展开；
- WAL + ``busy_timeout`` 支持读写并发；
- 读请求在单条快照事务内完成，保证整份结果来自同一个已提交状态；
- 入库在单条写事务内完成同版交叠校验与插入，冲突整体回滚，原档不变。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .schemas import ManifestIn

DEFAULT_DB_PATH = Path("data/archive.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS manifests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    start_date  TEXT    NOT NULL,
    end_date    TEXT    NOT NULL,
    accepted_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS screenings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id  INTEGER NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
    date         TEXT    NOT NULL,
    session_no   INTEGER NOT NULL,
    title        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manifests_version_range
    ON manifests(version, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_screenings_manifest
    ON screenings(manifest_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_screenings_manifest_date_session
    ON screenings(manifest_id, date, session_no);
"""


class SameVersionConflict(Exception):
    """同版清单在交叠日期内内容有出入，整份退回（HTTP 409）。"""

    def __init__(self, existing_id: int, overlap_start: str, overlap_end: str):
        self.existing_id = existing_id
        self.overlap_start = overlap_start
        self.overlap_end = overlap_end
        super().__init__(
            f"同版清单与已入库清单 id={existing_id} 在 "
            f"[{overlap_start}, {overlap_end}] 交叠且内容不一致，整份退回"
        )


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开一个带行工厂和并发设置的连接（不自动开启事务）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={30_000}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """幂等建表。"""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式开启一个只读快照事务（BEGIN DEFERRED 不获取写锁）。

    事务内所有读视图一致；提交时无写入，仅释放快照。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，rollback 与 commit 等效，仅结束快照


def _fetch_overlapping_same_version(
    conn: sqlite3.Connection, version: int, start_iso: str, end_iso: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, start_date, end_date FROM manifests
        WHERE version = ?
          AND start_date <= ?
          AND end_date >= ?
        ORDER BY id
        """,
        (version, end_iso, start_iso),
    ).fetchall()


def _screening_signature(
    conn: sqlite3.Connection, manifest_id: int
) -> set[tuple[str, int, str]]:
    rows = conn.execute(
        "SELECT date, session_no, title FROM screenings WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchall()
    return {(r["date"], r["session_no"], r["title"]) for r in rows}


def insert_manifest(
    conn: sqlite3.Connection, payload: ManifestIn
) -> tuple[int, str]:
    """在单条写事务内入库一份清单。

    同版交叠校验：
    - 与已有同版清单无日期交叠 -> 接受；
    - 交叠但交叠日内场次完全一致（两份清单各自全量一致，覆盖区间也相同）
      -> 幂等，按重复处理，不重复入库；
    - 交叠且内容有出入 -> 抛 :class:`SameVersionConflict`，事务回滚，原档不变。

    返回 ``(manifest_id, status)``，status 为 ``accepted`` 或 ``duplicate``。
    """
    start_iso = payload.start_date.isoformat()
    end_iso = payload.end_date.isoformat()
    incoming = {
        (s.date.isoformat(), s.session_no, s.title) for s in payload.screenings
    }

    conn.execute("BEGIN IMMEDIATE")
    try:
        overlaps = _fetch_overlapping_same_version(
            conn, payload.version, start_iso, end_iso
        )
        for row in overlaps:
            # 只有“覆盖区间相同 + 全量场次一致”才视为完全重复
            if (
                row["start_date"] == start_iso
                and row["end_date"] == end_iso
                and _screening_signature(conn, row["id"]) == incoming
            ):
                conn.rollback()
                return row["id"], "duplicate"
            # 其余任何同版交叠（区间不同，或同区间内容不一致）一律整份退回
            overlap_start = max(row["start_date"], start_iso)
            overlap_end = min(row["end_date"], end_iso)
            raise SameVersionConflict(row["id"], overlap_start, overlap_end)

        cursor = conn.execute(
            "INSERT INTO manifests (version, start_date, end_date, accepted_at) "
            "VALUES (?, ?, ?, ?)",
            (payload.version, start_iso, end_iso, datetime.utcnow().isoformat()),
        )
        manifest_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO screenings (manifest_id, date, session_no, title) "
            "VALUES (?, ?, ?, ?)",
            [
                (manifest_id, s.date.isoformat(), s.session_no, s.title)
                for s in payload.screenings
            ],
        )
        conn.commit()
        return manifest_id, "accepted"
    except SameVersionConflict:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def list_manifests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.id, m.version, m.start_date, m.end_date, m.accepted_at,
               (SELECT COUNT(*) FROM screenings s WHERE s.manifest_id = m.id)
                   AS screening_count
        FROM manifests m
        ORDER BY m.version, m.start_date, m.id
        """
    ).fetchall()
