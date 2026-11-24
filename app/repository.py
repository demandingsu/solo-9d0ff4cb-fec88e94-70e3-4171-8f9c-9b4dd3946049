"""放映档案查询服务：按日期范围计算当前生效状态。

结果三部分：
1. ``screenings``             —— 生效清单（覆盖当日的最高版次）列出的场次；
2. ``confirmed_no_screening`` —— 已被清单覆盖、但生效清单在当天无场次的日期段；
3. ``no_manifest_yet``        —— 没有任何清单覆盖到的日期段。

全程区间运算，不逐日展开；读操作在单条快照事务内完成。
"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import intervals as iv
from .database import snapshot
from .schemas import DateInterval, ScheduleResponse, ScreeningOut


def _load_manifests(
    conn: sqlite3.Connection, q_start: int, q_end: int
) -> list[tuple[int, int, int, int]]:
    """只取与查询窗口相交的清单，裁剪到窗口内，返回 (id, version, start, end)。"""
    rows = conn.execute(
        """
        SELECT id, version, start_date, end_date FROM manifests
        WHERE start_date <= ? AND end_date >= ?
        ORDER BY id
        """,
        (
            date.fromordinal(q_end).isoformat(),
            date.fromordinal(q_start).isoformat(),
        ),
    ).fetchall()
    result: list[tuple[int, int, int, int]] = []
    for r in rows:
        start = max(iv.to_ord(date.fromisoformat(r["start_date"])), q_start)
        end = min(iv.to_ord(date.fromisoformat(r["end_date"])), q_end)
        result.append((r["id"], r["version"], start, end))
    return result


def _load_screenings(
    conn: sqlite3.Connection,
    manifest_ids: list[int],
    q_start_iso: str,
    q_end_iso: str,
) -> dict[int, list[tuple[str, int, str]]]:
    if not manifest_ids:
        return {}
    placeholders = ",".join("?" for _ in manifest_ids)
    rows = conn.execute(
        f"""
        SELECT manifest_id, date, session_no, title
        FROM screenings
        WHERE manifest_id IN ({placeholders})
          AND date >= ? AND date <= ?
        ORDER BY date, session_no
        """,
        (*manifest_ids, q_start_iso, q_end_iso),
    ).fetchall()
    grouped: dict[int, list[tuple[str, int, str]]] = {}
    for r in rows:
        grouped.setdefault(r["manifest_id"], []).append(
            (r["date"], r["session_no"], r["title"])
        )
    return grouped


def get_schedule(
    conn: sqlite3.Connection, query_start: date, query_end: date
) -> ScheduleResponse:
    """在单条快照事务内计算查询窗口内的完整生效状态。"""
    q_start = iv.to_ord(query_start)
    q_end = iv.to_ord(query_end)
    window = (q_start, q_end)

    with snapshot(conn):
        manifests = _load_manifests(conn, q_start, q_end)
        segments = iv.governing_intervals(manifests)

        # 已覆盖区间（所有生效段的并集）与其补集
        covered = iv.merge_intervals([(s, e) for s, e, _, _ in segments])
        uncovered = iv.subtract_intervals(window, covered)

        manifest_ids = sorted({m_id for _, _, _, m_id in segments})
        screenings_by_manifest = _load_screenings(
            conn, manifest_ids, query_start.isoformat(), query_end.isoformat()
        )

        effective: list[ScreeningOut] = []
        confirmed_empty: list[tuple[int, int]] = []

        for seg_start, seg_end, version, manifest_id in segments:
            seg_window = iv.intersect_intervals((seg_start, seg_end), window)
            assert seg_window is not None
            seg_start, seg_end = seg_window

            rows = screenings_by_manifest.get(manifest_id, [])
            kept: list[tuple[str, int, str]] = []
            screening_day_ords: list[int] = []
            for d_iso, session_no, title in rows:
                d_ord = iv.to_ord(date.fromisoformat(d_iso))
                if seg_start <= d_ord <= seg_end:
                    kept.append((d_iso, session_no, title))
                    screening_day_ords.append(d_ord)

            for d_iso, session_no, title in kept:
                effective.append(
                    ScreeningOut(
                        date=date.fromisoformat(d_iso),
                        session_no=session_no,
                        title=title,
                        version=version,
                    )
                )

            # 该生效清单在本段内实际有场次的日子（同日多场并为一天）
            screening_days = iv.merge_intervals(
                [(o, o) for o in screening_day_ords]
            )
            # 覆盖但无场次的部分 = 生效段挖去有场次的日子
            confirmed_empty.extend(
                iv.subtract_intervals((seg_start, seg_end), screening_days)
            )

    effective.sort(key=lambda x: (x.date, x.session_no))
    confirmed_empty_merged = iv.merge_intervals(confirmed_empty)

    return ScheduleResponse(
        query_start=query_start,
        query_end=query_end,
        screenings=effective,
        confirmed_no_screening=[
            DateInterval(start_date=iv.to_date(s), end_date=iv.to_date(e))
            for s, e in confirmed_empty_merged
        ],
        no_manifest_yet=[
            DateInterval(start_date=iv.to_date(s), end_date=iv.to_date(e))
            for s, e in uncovered
        ],
    )
