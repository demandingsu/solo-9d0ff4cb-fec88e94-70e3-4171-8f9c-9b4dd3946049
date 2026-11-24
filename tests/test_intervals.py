"""区间纯函数测试：合并、挖空、扫描线生效版次。"""
from app.intervals import (
    governing_intervals,
    intersect_intervals,
    merge_intervals,
    subtract_intervals,
)


def test_merge_intervals_basic():
    assert merge_intervals([]) == []
    assert merge_intervals([(1, 3)]) == [(1, 3)]
    # 相交、包含、相邻（接续）都合并
    assert merge_intervals([(1, 3), (3, 5), (8, 9), (6, 7)]) == [(1, 9)]
    # 不相邻则保留两段
    assert merge_intervals([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]


def test_subtract_intervals_basic():
    assert subtract_intervals((1, 10), []) == [(1, 10)]
    assert subtract_intervals((1, 10), [(1, 10)]) == []
    assert subtract_intervals((1, 10), [(3, 4), (7, 7)]) == [
        (1, 2),
        (5, 6),
        (8, 10),
    ]
    # 洞超出基座时裁剪
    assert subtract_intervals((5, 8), [(1, 6)]) == [(7, 8)]


def test_intersect_intervals():
    assert intersect_intervals((1, 5), (5, 9)) == (5, 5)
    assert intersect_intervals((1, 4), (5, 9)) is None


def test_governing_intervals_highest_version_wins():
    # id, version, start, end
    manifests = [
        (1, 1, 1, 10),
        (2, 2, 4, 6),
        (3, 3, 6, 8),
    ]
    segments = governing_intervals(manifests)
    assert segments == [
        (1, 3, 1, 1),
        (4, 5, 2, 2),
        (6, 8, 3, 3),
        (9, 10, 1, 1),
    ]


def test_governing_intervals_old_fills_gap():
    # 新版中间有空档，旧版补齐
    manifests = [
        (10, 2, 1, 2),
        (11, 2, 8, 9),
        (20, 1, 1, 9),
    ]
    segments = governing_intervals(manifests)
    assert segments == [
        (1, 2, 2, 10),
        (3, 7, 1, 20),
        (8, 9, 2, 11),
    ]


def test_governing_intervals_empty():
    assert governing_intervals([]) == []


def test_governing_intervals_same_version_adjacent_pieces():
    # 同版两份不相交清单，中间无覆盖 -> 两段，空档不出现
    manifests = [(1, 1, 1, 3), (2, 1, 5, 7)]
    assert governing_intervals(manifests) == [
        (1, 3, 1, 1),
        (5, 7, 1, 2),
    ]
