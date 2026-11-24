"""复映检索 API 测试：对齐、场次号无关、停映日、退回原因、跨段隔离。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, reruns
from app.database import connect, initialize


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    initialize(connect(str(db_path)))
    return TestClient(app=main.app)


def _manifest(version: int, start: str, end: str, screenings: list[list]):
    return {
        "version": version,
        "start_date": start,
        "end_date": end,
        "screenings": [
            {"date": d, "session_no": no, "title": title}
            for d, no, title in screenings
        ],
    }


def _post(client, payload):
    resp = client.post("/manifests", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _search(client, ref_start, ref_end, search_start, search_end, expected=200):
    resp = client.get(
        "/reruns",
        params={
            "reference_start": ref_start,
            "reference_end": ref_end,
            "search_start": search_start,
            "search_end": search_end,
        },
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def _matches(data):
    return [(m["start_date"], m["end_date"]) for m in data["matches"]]


def test_finds_identical_program_with_different_session_numbers(client):
    """节目完整复映：片名与顺序相同即可，场次号可以不同。"""
    _post(
        client,
        _manifest(
            1,
            "2026-01-01",
            "2026-01-12",
            [
                # 参考段 1/3~1/5：片A、片B（同日两场）、停映、片A
                ["2026-01-03", 1, "甲"],
                ["2026-01-04", 2, "乙"],
                ["2026-01-04", 5, "丙"],
                ["2026-01-05", 1, "甲"],
                # 复映 1/8~1/10：场次号不同，顺序片名一致
                ["2026-01-08", 3, "甲"],
                ["2026-01-09", 1, "乙"],
                ["2026-01-09", 2, "丙"],
                ["2026-01-10", 3, "甲"],
            ],
        ),
    )
    data = _search(client, "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-12")
    assert _matches(data) == [("2026-01-08", "2026-01-10")]
    assert data["reference_length_days"] == 3


def test_session_number_gap_is_ignored_but_order_matters(client):
    """场次号不参与比较；但同一天片名的出场顺序必须一致。"""
    _post(
        client,
        _manifest(
            1,
            "2026-02-01",
            "2026-02-08",
            [
                ["2026-02-01", 1, "甲"],
                ["2026-02-01", 2, "乙"],
                ["2026-02-02", 1, "甲"],  # 参考段 2/1~2/2
                # 顺序反了：不是复映
                ["2026-02-06", 1, "乙"],
                ["2026-02-06", 2, "甲"],
                ["2026-02-07", 1, "甲"],
            ],
        ),
    )
    data = _search(client, "2026-02-01", "2026-02-02", "2026-02-05", "2026-02-08")
    assert _matches(data) == []


def test_same_film_twice_on_one_day_compared_twice(client):
    """一天同一片放两场就比两次；只放一场不算复映。"""
    _post(
        client,
        _manifest(
            1,
            "2026-03-01",
            "2026-03-06",
            [
                ["2026-03-01", 1, "甲"],
                ["2026-03-01", 2, "甲"],
                ["2026-03-04", 1, "甲"],
                ["2026-03-04", 2, "甲"],
                ["2026-03-05", 1, "甲"],  # 少一场
            ],
        ),
    )
    data = _search(client, "2026-03-01", "2026-03-01", "2026-03-02", "2026-03-06")
    assert _matches(data) == [("2026-03-04", "2026-03-04")]


def test_leading_trailing_and_middle_dark_days_must_match(client):
    """参考段首尾和中间的停映日都要对应；候选段在停映日放了片则不匹配。"""
    _post(
        client,
        _manifest(
            1,
            "2026-04-01",
            "2026-04-18",
            [
                # 参考段 4/2~4/6：停映、甲、停映、停映、乙（首尾均为停映）
                ["2026-04-03", 1, "甲"],
                ["2026-04-06", 1, "乙"],
                # 正确复映 4/8~4/12
                ["2026-04-09", 1, "甲"],
                ["2026-04-12", 1, "乙"],
                # 干扰候选 4/13~4/17：中间停映日 4/16 却有片
                ["2026-04-14", 1, "甲"],
                ["2026-04-16", 1, "乙"],
            ],
        ),
    )
    data = _search(client, "2026-04-02", "2026-04-06", "2026-04-01", "2026-04-18")
    assert _matches(data) == [("2026-04-08", "2026-04-12")]


def test_candidate_with_a_screening_on_reference_dark_day_is_rejected(client):
    """参考段某天停映，候选段同偏移日却有放映：不算复映。"""
    _post(
        client,
        _manifest(
            1,
            "2026-05-01",
            "2026-05-06",
            [
                ["2026-05-01", 1, "甲"],
                ["2026-05-02", 1, "乙"],  # 参考段
                ["2026-05-04", 1, "甲"],
                ["2026-05-05", 1, "乙"],
                ["2026-05-06", 9, "丙"],  # 候选段 5/4~5/6 多出的片子
            ],
        ),
    )
    data = _search(client, "2026-05-01", "2026-05-03", "2026-05-04", "2026-05-06")
    assert _matches(data) == []


def test_reference_itself_is_excluded(client):
    """候选段与参考段完全相同（落在搜索段内）时不计入结果。"""
    _post(
        client,
        _manifest(
            1,
            "2026-06-01",
            "2026-06-10",
            [
                ["2026-06-02", 1, "甲"],
                ["2026-06-03", 1, "乙"],
                ["2026-06-07", 1, "甲"],
                ["2026-06-08", 1, "乙"],
            ],
        ),
    )
    data = _search(client, "2026-06-02", "2026-06-03", "2026-06-01", "2026-06-10")
    assert _matches(data) == [("2026-06-07", "2026-06-08")]


def test_search_range_cut_off_by_window_boundary(client):
    """候选段必须完整落在搜索段内：跨出搜索段末尾的不返回。"""
    _post(
        client,
        _manifest(
            1,
            "2026-07-01",
            "2026-07-10",
            [
                ["2026-07-01", 1, "甲"],
                ["2026-07-02", 1, "乙"],
                ["2026-07-09", 1, "甲"],
                ["2026-07-10", 1, "乙"],
            ],
        ),
    )
    data = _search(client, "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-09")
    assert _matches(data) == []


def test_reference_with_unreported_date_is_rejected(client):
    """参考段含未报日期：409，并列出未覆盖区间。"""
    _post(
        client,
        _manifest(1, "2026-08-01", "2026-08-03", [["2026-08-01", 1, "甲"]]),
    )
    _post(
        client,
        _manifest(1, "2026-08-06", "2026-08-08", [["2026-08-07", 1, "甲"]]),
    )
    data = _search(
        client, "2026-08-01", "2026-08-07", "2026-08-10", "2026-08-20", expected=409
    )
    detail = data["detail"]
    assert detail["error"] == "reference_not_fully_reported"
    assert detail["uncovered"] == [
        {"start_date": "2026-08-04", "end_date": "2026-08-05"}
    ]


def test_reference_with_no_screenings_is_rejected(client):
    """参考段报齐但无任何放映：409 说明原因。"""
    _post(client, _manifest(1, "2026-09-01", "2026-09-10", []))
    data = _search(
        client, "2026-09-01", "2026-09-05", "2026-09-06", "2026-09-10", expected=409
    )
    assert data["detail"]["error"] == "reference_has_no_screenings"
    assert data["detail"]["message"]


def test_rerun_allowed_after_late_backfill(client):
    """检索期间照常补录：先缺报被退回，补录后即可查到复映。"""
    _post(
        client,
        _manifest(
            1,
            "2026-10-01",
            "2026-10-02",
            [["2026-10-01", 1, "甲"], ["2026-10-02", 1, "乙"]],
        ),
    )
    # 参考段取 10/1~10/3，其中 10/3 尚未被任何清单覆盖
    _post(
        client,
        _manifest(
            1,
            "2026-10-08",
            "2026-10-12",
            [["2026-10-08", 1, "甲"], ["2026-10-09", 1, "乙"]],
        ),
    )
    _search(
        client, "2026-10-01", "2026-10-03", "2026-10-08", "2026-10-12", expected=409
    )
    # 补录参考段缺的 10/3（同版不交叠，正常入库），期间不中断检索
    _post(client, _manifest(1, "2026-10-03", "2026-10-03", []))
    data = _search(client, "2026-10-01", "2026-10-03", "2026-10-08", "2026-10-12")
    # 参考段 = 甲(10/1)、乙(10/2)、停映(10/3)，复映从 10/8 起对齐
    assert _matches(data) == [("2026-10-08", "2026-10-10")]


def test_search_does_not_read_rows_between_the_two_windows(client, monkeypatch):
    """两段之间的无关场次不会被读取。"""
    _post(
        client,
        _manifest(
            1,
            "2025-01-01",
            "2026-12-31",
            [
                ["2025-06-01", 1, "甲"],
                ["2026-05-05", 1, "甲"],
            ],
        ),
    )
    loaded: list[str] = []
    original = reruns.load_effective_state

    def spy(conn, q_start, q_end):
        loaded.append(
            f"{__import__('datetime').date.fromordinal(q_start)}.."
            f"{__import__('datetime').date.fromordinal(q_end)}"
        )
        return original(conn, q_start, q_end)

    monkeypatch.setattr(reruns, "load_effective_state", spy)
    _search(client, "2025-06-01", "2025-06-01", "2026-05-01", "2026-05-10")
    assert loaded == ["2025-06-01..2025-06-01", "2026-05-01..2026-05-10"]


def test_new_version_changing_film_updates_matching(client):
    """新版把片名改掉后，按生效片名检索：旧节目不再算复映，新节目才算。"""
    _post(
        client,
        _manifest(
            1,
            "2026-01-01",
            "2026-01-10",
            [
                ["2026-01-01", 1, "旧片"],
                ["2026-01-02", 1, "陪衬"],
                ["2026-01-06", 1, "旧片"],
                ["2026-01-07", 1, "陪衬"],
            ],
        ),
    )
    # 新版覆盖 1/6~1/8，把 1/6 的片名换掉
    _post(
        client,
        _manifest(
            2,
            "2026-01-06",
            "2026-01-08",
            [["2026-01-06", 1, "新片"]],
        ),
    )
    data = _search(client, "2026-01-01", "2026-01-02", "2026-01-05", "2026-01-10")
    # 旧节目（旧片+陪衬）在 1/6~1/7 已不复存在，没有复映
    assert _matches(data) == []

    # 以新版生效后的 1/6~1/7 作为参考段，可查到它自身以外没有复映
    data = _search(client, "2026-01-06", "2026-01-07", "2026-01-05", "2026-01-10")
    assert _matches(data) == []


def test_rerun_program_spanning_manifest_boundary(client):
    """复映段可以横跨两份清单，比较的是逐日生效状态而非单份清单。"""
    _post(
        client,
        _manifest(
            1,
            "2026-02-01",
            "2026-03-31",
            [
                # 参考段 2/02~2/03
                ["2026-02-02", 1, "甲"],
                ["2026-02-03", 1, "乙"],
            ],
        ),
    )
    # 复映节目分属两份不同清单
    _post(client, _manifest(2, "2026-03-09", "2026-03-09", [["2026-03-09", 7, "甲"]]))
    _post(client, _manifest(2, "2026-03-10", "2026-03-10", [["2026-03-10", 8, "乙"]]))
    data = _search(client, "2026-02-02", "2026-02-03", "2026-03-05", "2026-03-15")
    assert _matches(data) == [("2026-03-09", "2026-03-10")]


def test_cross_year_rerun(client):
    """跨年检索：参考段在年末，复映在次年。"""
    _post(
        client,
        _manifest(
            1,
            "2025-12-29",
            "2026-01-08",
            [
                # 参考段 12/30~1/1：跨年片、停映（12/31）、元旦片
                ["2025-12-30", 1, "跨年片"],
                ["2026-01-01", 1, "元旦片"],
                # 复映 1/5~1/7：跨年片、停映（1/6）、元旦片
                ["2026-01-05", 2, "跨年片"],
                ["2026-01-07", 2, "元旦片"],
            ],
        ),
    )
    data = _search(client, "2025-12-30", "2026-01-01", "2026-01-02", "2026-01-08")
    assert _matches(data) == [("2026-01-05", "2026-01-07")]


def test_candidate_spanning_unreported_gap_is_rejected(client):
    """候选取值里有一天没有任何清单覆盖（未报，不等于停映），不能算复映。"""
    # 参考段 1/1 有片、1/2 确认停映
    _post(
        client,
        _manifest(1, "2026-01-01", "2026-01-04", [["2026-01-01", 1, "甲"]]),
    )
    # 1/6 有片、1/7 有片，但 1/5 与 1/7 之后没有清单覆盖
    _post(
        client,
        _manifest(
            1,
            "2026-01-06",
            "2026-01-07",
            [["2026-01-06", 1, "甲"], ["2026-01-07", 1, "乙"]],
        ),
    )
    data = _search(client, "2026-01-01", "2026-01-02", "2026-01-05", "2026-01-10")
    # 候选段 1/6~1/7 的 1/7 被“乙”占用；1/7 之后再无候选起点落在覆盖内
    assert _matches(data) == []


def test_rerun_rejects_reversed_ranges(client):
    resp = client.get(
        "/reruns",
        params={
            "reference_start": "2026-01-05",
            "reference_end": "2026-01-01",
            "search_start": "2026-01-01",
            "search_end": "2026-01-10",
        },
    )
    assert resp.status_code == 422

    resp = client.get(
        "/reruns",
        params={
            "reference_start": "2026-01-01",
            "reference_end": "2026-01-05",
            "search_start": "2026-01-10",
            "search_end": "2026-01-01",
        },
    )
    assert resp.status_code == 422
