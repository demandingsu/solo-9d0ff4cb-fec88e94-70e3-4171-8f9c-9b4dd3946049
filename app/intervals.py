"""闭区间日期工具：全程按整数序数区间运算，绝不逐日展开。

所有函数接收/返回的区间均为 ``(start_ord, end_ord)`` 闭区间，
``start_ord <= end_ord``。日期与序数通过 ``date.toordinal`` 互转。
"""
from __future__ import annotations

from datetime import date

# 一条带归属的闭区间：(起始序数, 截止序数, 版次, 清单 id)
GoverningInterval = tuple[int, int, int, int]
# 普通闭区间
Interval = tuple[int, int]


def to_ord(d: date) -> int:
    return d.toordinal()


def to_date(value: int) -> date:
    return date.fromordinal(value)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """合并相交或相邻（首尾接续）的闭区间。空输入返回空列表。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1] + 1:
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def subtract_intervals(base: Interval, holes: list[Interval]) -> list[Interval]:
    """从单个闭区间 ``base`` 中挖去 ``holes``（可乱序、可重叠），返回剩余合并区间。"""
    base_start, base_end = base
    if base_end < base_start:
        return []
    cut: list[Interval] = []
    for h_start, h_end in merge_intervals(holes):
        h_start = max(h_start, base_start)
        h_end = min(h_end, base_end)
        if h_end < h_start:
            continue
        cut.append((h_start, h_end))
    result: list[Interval] = []
    cursor = base_start
    for h_start, h_end in cut:
        if h_start > cursor:
            result.append((cursor, h_start - 1))
        cursor = max(cursor, h_end + 1)
    if cursor <= base_end:
        result.append((cursor, base_end))
    return result


def intersect_intervals(a: Interval, b: Interval) -> Interval | None:
    """两个闭区间的交集，无交集返回 None。"""
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    return (start, end) if start <= end else None


def governing_intervals(
    manifests: list[tuple[int, int, int, int]],
) -> list[GoverningInterval]:
    """根据若干 ``(id, version, start_ord, end_ord)`` 清单，
    计算每个日期段上生效的最高版次清单。

    返回互不重叠、按序排列的闭区间段，每段标注
    ``(start_ord, end_ord, version, manifest_id)``。
    扫描线事件驱动 + 活跃清单计数 + 相邻同归属段合并，
    复杂度 O(n log n)，不逐日展开。
    """
    # 事件：(位置, 类型[0=进入, 1=离开], 版次, 清单 id)
    events: list[tuple[int, int, int, int]] = []
    for m_id, version, start_ord, end_ord in manifests:
        events.append((start_ord, 0, version, m_id))
        events.append((end_ord + 1, 1, version, m_id))
    events.sort()

    active: dict[tuple[int, int], int] = {}  # (version, id) -> 引用计数
    result: list[GoverningInterval] = []
    cursor: int | None = None

    for pos, kind, version, m_id in events:
        key = (version, m_id)
        # 事件位置之间 [cursor, pos-1] 的生效状态在上一轮事件后已固定
        if cursor is not None and cursor < pos and active:
            win_version, win_id = max(active)  # 版次优先，id 仅作平局兜底
            if (
                result
                and result[-1][2] == win_version
                and result[-1][3] == win_id
                and result[-1][1] == cursor - 1
            ):
                prev = result[-1]
                result[-1] = (prev[0], pos - 1, win_version, win_id)
            else:
                result.append((cursor, pos - 1, win_version, win_id))
        # 应用当前事件
        if kind == 0:
            active[key] = active.get(key, 0) + 1
        else:
            count = active.get(key, 0) - 1
            if count <= 0:
                active.pop(key, None)
            else:
                active[key] = count
        cursor = pos
    return result


def governing_at(
    segments: list[GoverningInterval], target_ord: int
) -> GoverningInterval | None:
    """二分查找某个序数落在哪个生效段，不存在返回 None。"""
    lo, hi = 0, len(segments) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, _, _ = segments[mid]
        if target_ord < start:
            hi = mid - 1
        elif target_ord > end:
            lo = mid + 1
        else:
            return segments[mid]
    return None


def intervals_to_dates(intervals: list[Interval]) -> list[dict[str, str]]:
    """把序数闭区间转成 API 输出的 ISO 日期段。"""
    return [
        {
            "start_date": to_date(start).isoformat(),
            "end_date": to_date(end).isoformat(),
        }
        for start, end in intervals
    ]
