"""复映检索：在搜索段内找出与参考段节目完全相同、等长的日期段。

匹配口径
--------
- 两段按日对齐：参考段第 i 天对应候选段第 i 天；
- 有放映的日子，生效片名按场次号顺序逐一相同；场次号本身可以不同，
  同一片一天放两场就比两次（片名元组长度与顺序都要一致）；
- 参考段中确认无放映的日子（含首尾与中间的停映日）在候选段也必须是
  确认无放映；未报日期不算停映，参考段里出现未报日期直接退回；
- 参考段一场放映都没有时无法作为节目模板，退还原因；
- 结果排除参考段本身。

性能
----
参考段与搜索段各自只读取本窗口内的清单与生效场次，两段之间的长空档
不做任何场次读取；停映日与覆盖状态始终以闭区间表示，用二分判断候选段
是否落在“确认无放映”区间内，全程不逐日展开。候选起点以参考段中
最罕见的一场放映为锚点生成，避免扫描搜索段里的每一天。
"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import intervals as iv
from .database import snapshot
from .schemas import RerunMatch, RerunSearchResponse
from .repository import load_effective_state


class ReferenceNotReported(Exception):
    """参考段内存在尚未收到任何清单的日期，无法确定节目。"""

    def __init__(self, uncovered: list[iv.Interval]):
        self.uncovered = uncovered
        super().__init__(
            "参考段内存在尚未收到清单的日期，无法确定该日节目；"
            "请先补齐这些日期的清单后再检索"
        )


class ReferenceHasNoScreenings(Exception):
    """参考段虽然已全部报齐，但一场放映都没有，无法作为节目模板。"""

    def __init__(self) -> None:
        super().__init__(
            "参考段已全部报齐，但一场放映都没有，无法作为复映节目模板；"
            "请改选包含放映的日期段"
        )


# 某一天的生效片名序列（按场次号顺序，仅片名，场次号不参与比较）
DayTitles = tuple[str, ...]


def _contains(merged: list[iv.Interval], inner: iv.Interval) -> bool:
    """``inner`` 是否完整落在已合并的闭区间列表的某一段内（二分）。"""
    lo, hi = 0, len(merged)
    while lo < hi:
        mid = (lo + hi) // 2
        if merged[mid][0] <= inner[0]:
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    return idx >= 0 and merged[idx][1] >= inner[1]


class _State:
    """一个日期窗口内的生效状态（全部为序数/区间，不含逐日表）。"""

    __slots__ = ("covered", "empty", "days")

    def __init__(
        self,
        covered: list[iv.Interval],
        empty: list[iv.Interval],
        days: dict[int, DayTitles],
    ):
        self.covered = covered
        self.empty = empty
        self.days = days


def _collect(
    segments: list[iv.GoverningInterval],
    screenings_by_manifest: dict[int, list[tuple[str, int, str]]],
    window: iv.Interval,
) -> _State:
    """把生效段与场次整理成覆盖区间、确认无放映区间、逐日片名序列。

    逐日片名只保存有放映的日子（其数量受场次行数约束），停映日仍是区间。
    """
    covered: list[iv.Interval] = []
    empty: list[iv.Interval] = []
    days: dict[int, DayTitles] = {}

    for seg_start, seg_end, _version, manifest_id in segments:
        seg_window = iv.intersect_intervals((seg_start, seg_end), window)
        assert seg_window is not None
        seg_start, seg_end = seg_window
        covered.append((seg_start, seg_end))

        per_day: dict[int, list[tuple[int, str]]] = {}
        screening_days: list[iv.Interval] = []
        for d_iso, session_no, title in screenings_by_manifest.get(manifest_id, []):
            d_ord = iv.to_ord(date.fromisoformat(d_iso))
            if seg_start <= d_ord <= seg_end:
                per_day.setdefault(d_ord, []).append((session_no, title))
                screening_days.append((d_ord, d_ord))
        for d_ord, items in per_day.items():
            items.sort(key=lambda x: x[0])
            days[d_ord] = tuple(title for _, title in items)

        empty.extend(
            iv.subtract_intervals(
                (seg_start, seg_end), iv.merge_intervals(screening_days)
            )
        )

    return _State(
        covered=iv.merge_intervals(covered),
        empty=iv.merge_intervals(empty),
        days=days,
    )


def _load_window(
    conn: sqlite3.Connection, start_ord: int, end_ord: int
) -> _State:
    segments, screenings = load_effective_state(conn, start_ord, end_ord)
    return _collect(segments, screenings, (start_ord, end_ord))


def search_reruns(
    conn: sqlite3.Connection,
    reference_start: date,
    reference_end: date,
    search_start: date,
    search_end: date,
) -> RerunSearchResponse:
    """在单条快照事务内完成复映检索，两段取自同一个已提交档案状态。"""
    ref_start = iv.to_ord(reference_start)
    ref_end = iv.to_ord(reference_end)
    search_lo = iv.to_ord(search_start)
    search_hi = iv.to_ord(search_end)
    length = ref_end - ref_start + 1

    with snapshot(conn):
        # 两段分别加载：参考段与搜索段之间的长空档不做任何场次读取
        ref_state = _load_window(conn, ref_start, ref_end)
        uncovered = iv.subtract_intervals((ref_start, ref_end), ref_state.covered)
        if uncovered:
            raise ReferenceNotReported(uncovered)
        if not ref_state.days:
            raise ReferenceHasNoScreenings()
        search_state = _load_window(conn, search_lo, search_hi)

    # 参考段的节目签名：有放映日的“偏移 -> 片名序列”，停映日以偏移区间表示
    ref_offsets: dict[int, DayTitles] = {
        d_ord - ref_start: titles for d_ord, titles in ref_state.days.items()
    }
    ref_empty_runs = [
        (s - ref_start, e - ref_start)
        for s, e in iv.subtract_intervals(
            (ref_start, ref_end),
            iv.merge_intervals([(d, d) for d in ref_state.days]),
        )
    ]
    signature: list[tuple[int, DayTitles]] = sorted(ref_offsets.items())

    # 候选起点以参考段中最罕见的一场放映为锚点：
    # 先把搜索段里的“有放映日”按片名序列分组，锚点出现次数即候选数。
    days_by_titles: dict[DayTitles, list[int]] = {}
    for d_ord, titles in search_state.days.items():
        days_by_titles.setdefault(titles, []).append(d_ord)
    anchor_offset, anchor_titles = min(
        signature,
        key=lambda item: len(days_by_titles.get(item[1], ())),
    )
    anchor_days = days_by_titles.get(anchor_titles, [])

    latest_start = search_hi - length + 1
    matches: list[RerunMatch] = []
    for anchor_day in anchor_days:
        candidate_start = anchor_day - anchor_offset
        if candidate_start < search_lo or candidate_start > latest_start:
            continue
        if candidate_start == ref_start:  # 排除参考段本身
            continue
        candidate_end = candidate_start + length - 1
        # 候选段必须整段有清单覆盖（每天非“有放映”即“确认停映”）
        if not _contains(search_state.covered, (candidate_start, candidate_end)):
            continue
        # 有放映的日：片名序列逐一相同
        if any(
            search_state.days.get(candidate_start + offset) != titles
            for offset, titles in signature
        ):
            continue
        # 停映日（含首尾与中间）必须落在搜索段的确认无放映区间内
        if any(
            not _contains(
                search_state.empty,
                (candidate_start + s, candidate_start + e),
            )
            for s, e in ref_empty_runs
        ):
            continue
        matches.append(
            RerunMatch(
                start_date=iv.to_date(candidate_start),
                end_date=iv.to_date(candidate_end),
            )
        )

    matches.sort(key=lambda m: m.start_date)
    return RerunSearchResponse(
        reference_start=reference_start,
        reference_end=reference_end,
        reference_length_days=length,
        search_start=search_start,
        search_end=search_end,
        matches=matches,
    )
