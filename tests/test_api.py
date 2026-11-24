"""端到端业务规则测试（FastAPI TestClient + 临时 SQLite 库）。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import connect, initialize


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    # 预建表，避免 startup 事件在 TestClient 之外的差异
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


def _post(client, payload, expected=201):
    resp = client.post("/manifests", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def _schedule(client, start: str, end: str):
    resp = client.get("/schedule", params={"start_date": start, "end_date": end})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_empty_manifest_covers_range_as_confirmed_empty(client):
    """空清单：覆盖范围内全部为确认无放映，不逐日存储。"""
    _post(client, _manifest(1, "2026-01-01", "2026-01-10", []))
    data = _schedule(client, "2025-12-31", "2026-01-12")
    assert data["screenings"] == []
    assert data["confirmed_no_screening"] == [
        {"start_date": "2026-01-01", "end_date": "2026-01-10"}
    ]
    assert data["no_manifest_yet"] == [
        {"start_date": "2025-12-31", "end_date": "2025-12-31"},
        {"start_date": "2026-01-11", "end_date": "2026-01-12"},
    ]


def test_late_old_version_fills_gap_left_by_new_version(client):
    """迟到的旧版补齐新版未覆盖的日期。"""
    # 新版只覆盖 1/5~1/10
    _post(
        client,
        _manifest(
            2,
            "2026-01-05",
            "2026-01-10",
            [
                ["2026-01-05", 1, "新版片A"],
                ["2026-01-05", 2, "新版片B"],
            ],
        ),
    )
    # 迟到的旧版覆盖 1/1~1/7
    _post(
        client,
        _manifest(
            1,
            "2026-01-01",
            "2026-01-07",
            [
                ["2026-01-01", 1, "旧版片X"],
                ["2026-01-05", 3, "旧版片Y"],  # 与新版同日，应被新版压制
                ["2026-01-06", 1, "旧版片Z"],  # 1/6 在新版范围内 -> 撤下，不能补
            ],
        ),
    )
    data = _schedule(client, "2026-01-01", "2026-01-10")
    titles = {(s["date"], s["session_no"]): s["title"] for s in data["screenings"]}
    # 1/5 只取新版两场，旧版第三场撤下；1/6 旧场也因落入新版范围而撤下
    assert titles == {
        ("2026-01-05", 1): "新版片A",
        ("2026-01-05", 2): "新版片B",
        ("2026-01-01", 1): "旧版片X",
    }
    versions = {s["date"]: s["version"] for s in data["screenings"]}
    assert versions["2026-01-01"] == 1
    assert versions["2026-01-05"] == 2
    # 1/2~1/4 是新版范围之外、由旧版补齐但无场次 -> 确认无放映
    # 1/6~1/10 在新版范围内且新版未列场次（旧场撤下）-> 确认无放映
    assert data["confirmed_no_screening"] == [
        {"start_date": "2026-01-02", "end_date": "2026-01-04"},
        {"start_date": "2026-01-06", "end_date": "2026-01-10"},
    ]
    assert data["no_manifest_yet"] == []


def test_late_old_version_fills_outside_range_gap(client):
    """迟到旧版补齐新版覆盖范围之外的日期（“补齐”的准确含义）。"""
    # 新版只覆盖 1/5~1/10
    _post(
        client,
        _manifest(2, "2026-01-05", "2026-01-10", [["2026-01-08", 1, "新版片"]]),
    )
    # 旧版 1/1~1/4 完全落在新版范围之外，晚到后补齐这段
    _post(
        client,
        _manifest(1, "2026-01-01", "2026-01-04", [["2026-01-03", 1, "迟到旧片"]]),
    )
    data = _schedule(client, "2026-01-01", "2026-01-10")
    titles = {(s["date"], s["session_no"]): s["title"] for s in data["screenings"]}
    assert titles == {
        ("2026-01-03", 1): "迟到旧片",
        ("2026-01-08", 1): "新版片",
    }
    assert data["no_manifest_yet"] == []
    # 相邻的“确认无放映”日会合并成区间：1/4 与 1/5~1/7 接续 -> 1/4~1/7
    assert data["confirmed_no_screening"] == [
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        {"start_date": "2026-01-04", "end_date": "2026-01-07"},
        {"start_date": "2026-01-09", "end_date": "2026-01-10"},
    ]


def test_new_version_removes_old_screenings_inside_its_range(client):
    """新版范围内未列的旧场次从档案撤下（以新版空清单覆盖）。"""
    _post(
        client,
        _manifest(1, "2026-03-01", "2026-03-05", [["2026-03-02", 1, "旧片"]]),
    )
    # 新版空清单覆盖同一区间，旧片在新版范围内撤下
    _post(client, _manifest(2, "2026-03-01", "2026-03-05", []))
    data = _schedule(client, "2026-03-01", "2026-03-05")
    assert data["screenings"] == []
    assert data["confirmed_no_screening"] == [
        {"start_date": "2026-03-01", "end_date": "2026-03-05"}
    ]


def test_same_version_overlap_discrepancy_is_rejected_and_archive_unchanged(client):
    """同版交叠内容有出入：整份退回，原档保持不变。"""
    first = _post(
        client,
        _manifest(3, "2026-02-01", "2026-02-10", [["2026-02-03", 1, "原片"]]),
    )
    before = client.get("/manifests").json()

    resp = client.post(
        "/manifests",
        json=_manifest(
            3,
            "2026-02-05",
            "2026-02-15",
            [["2026-02-08", 1, "冲突片"]],
        ),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "same_version_conflict"
    assert detail["existing_manifest_id"] == first["manifest_id"]

    after = client.get("/manifests").json()
    assert before == after  # 原档完全不变
    data = _schedule(client, "2026-02-01", "2026-02-15")
    assert [(s["date"], s["title"]) for s in data["screenings"]] == [
        ("2026-02-03", "原片")
    ]


def test_same_version_identical_resubmit_is_idempotent(client):
    """完全相同的同版清单重复提交：幂等，不重复入库。"""
    payload = _manifest(1, "2026-04-01", "2026-04-03", [["2026-04-02", 1, "片"]])
    first = _post(client, payload)
    second = _post(client, payload)
    assert second["status"] == "duplicate"
    assert second["manifest_id"] == first["manifest_id"]
    assert len(client.get("/manifests").json()) == 1


def test_same_version_non_overlapping_ranges_are_accepted(client):
    """同版但日期不相交的两份清单可以共存。"""
    _post(client, _manifest(1, "2026-05-01", "2026-05-05", [["2026-05-02", 1, "甲"]]))
    _post(client, _manifest(1, "2026-05-10", "2026-05-15", [["2026-05-11", 1, "乙"]]))
    data = _schedule(client, "2026-05-01", "2026-05-15")
    assert len(data["screenings"]) == 2
    assert data["no_manifest_yet"] == [
        {"start_date": "2026-05-06", "end_date": "2026-05-09"}
    ]


def test_cross_year_manifest(client):
    """清单可跨年，区间与星期均正确。"""
    _post(
        client,
        _manifest(
            1,
            "2025-12-30",
            "2026-01-02",
            [
                ["2025-12-31", 1, "跨年片上"],
                ["2026-01-01", 1, "跨年片下"],
            ],
        ),
    )
    data = _schedule(client, "2025-12-29", "2026-01-03")
    assert [s["date"] for s in data["screenings"]] == ["2025-12-31", "2026-01-01"]
    assert data["confirmed_no_screening"] == [
        {"start_date": "2025-12-30", "end_date": "2025-12-30"},
        {"start_date": "2026-01-02", "end_date": "2026-01-02"},
    ]
    assert data["no_manifest_yet"] == [
        {"start_date": "2025-12-29", "end_date": "2025-12-29"},
        {"start_date": "2026-01-03", "end_date": "2026-01-03"},
    ]


def test_validation_errors(client):
    """入参校验：反向日期、场次越界、同日场次号重复。"""
    bad_range = client.post(
        "/manifests",
        json=_manifest(1, "2026-01-10", "2026-01-01", []),
    )
    assert bad_range.status_code == 422

    out_of_range = client.post(
        "/manifests",
        json=_manifest(1, "2026-01-01", "2026-01-05", [["2026-01-06", 1, "越界"]]),
    )
    assert out_of_range.status_code == 422

    dup_session = client.post(
        "/manifests",
        json=_manifest(
            1,
            "2026-01-01",
            "2026-01-05",
            [["2026-01-02", 1, "甲"], ["2026-01-02", 1, "乙"]],
        ),
    )
    assert dup_session.status_code == 422

    blank_title = client.post(
        "/manifests",
        json=_manifest(1, "2026-01-01", "2026-01-05", [["2026-01-02", 1, "   "]]),
    )
    assert blank_title.status_code == 422


def test_schedule_rejects_reversed_query_range(client):
    resp = client.get(
        "/schedule", params={"start_date": "2026-01-10", "end_date": "2026-01-01"}
    )
    assert resp.status_code == 422


def test_query_during_ingestion_uses_single_committed_snapshot(client):
    """查询与入库并发：整份结果要么来自入库前、要么来自入库后，不出现混合态。"""
    _post(
        client,
        _manifest(1, "2026-06-01", "2026-06-30", [["2026-06-15", 1, "旧"]]),
    )
    stop = threading.Event()
    errors: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            data = _schedule(client, "2026-06-01", "2026-06-30")
            titles = sorted(s["title"] for s in data["screenings"])
            # 合法快照只可能是：仅“旧” 或 仅“新”，绝不允许两者混合
            if titles not in (["旧"], ["新"]):
                errors.append(f"混合态: {titles}")
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(20):
            client.post(
                "/manifests",
                json=_manifest(2, "2026-06-01", "2026-06-30", [["2026-06-15", 1, "新"]]),
            )
            # 再用同版冲突制造回滚，确保回滚期间读者也只看到已提交状态
            conflict = client.post(
                "/manifests",
                json=_manifest(2, "2026-06-20", "2026-06-25", [["2026-06-21", 1, "冲突"]]),
            )
            assert conflict.status_code == 409
    finally:
        stop.set()
        t.join()
    assert errors == []


def test_interval_merging_adjacent_segments(client):
    """相邻且归属相同的生效段合并为一个区间输出。"""
    _post(client, _manifest(1, "2026-07-01", "2026-07-03", []))
    _post(client, _manifest(1, "2026-07-04", "2026-07-05", []))  # 同版相邻不相交
    data = _schedule(client, "2026-07-01", "2026-07-05")
    assert data["confirmed_no_screening"] == [
        {"start_date": "2026-07-01", "end_date": "2026-07-05"}
    ]
