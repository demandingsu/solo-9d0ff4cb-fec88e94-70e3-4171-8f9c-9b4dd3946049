"""电影资料馆放映档案 FastAPI 入口。

- POST /manifests  录入一份清单（整份接受或整份退回）
- GET  /schedule   按日期范围查询生效场次与空日期段
- GET  /manifests  查看已入库清单
- GET  /docs       Swagger UI，可直接录入和查档
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlite3 import Connection

from .database import (
    SameVersionConflict,
    connect,
    initialize,
    insert_manifest,
    list_manifests,
)
from .repository import get_schedule
from .schemas import (
    IngestResponse,
    ManifestIn,
    ManifestRecord,
    ScheduleResponse,
)

DB_PATH = os.getenv("ARCHIVE_DB_PATH", "data/archive.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect(DB_PATH)
    try:
        initialize(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="电影资料馆放映档案",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "把乱序到达的放映清单（带整数版次与覆盖起止日期）并入可查档案。"
        "每天以覆盖当日的最高版次为准；同版交叠内容不一致整份退回；"
        "空日期以区间表达，不逐日展开。"
    ),
)


def get_conn() -> Iterator[Connection]:
    """每请求一个连接，结束即关闭；读事务/写事务均在服务层内显式管理。"""
    conn = connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/healthz", tags=["运维"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/manifests",
    response_model=IngestResponse,
    status_code=201,
    tags=["清单录入"],
    summary="录入一份放映清单",
)
def ingest_manifest(
    payload: ManifestIn, conn: Connection = Depends(get_conn)
) -> IngestResponse:
    try:
        manifest_id, status = insert_manifest(conn, payload)
    except SameVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "same_version_conflict",
                "message": str(exc),
                "existing_manifest_id": exc.existing_id,
                "overlap_start": exc.overlap_start,
                "overlap_end": exc.overlap_end,
            },
        )
    return IngestResponse(
        manifest_id=manifest_id,
        version=payload.version,
        start_date=payload.start_date,
        end_date=payload.end_date,
        screening_count=len(payload.screenings),
        status=status,
    )


@app.get(
    "/schedule",
    response_model=ScheduleResponse,
    tags=["档案查询"],
    summary="按日期范围返回有效场次与空日期段",
)
def query_schedule(
    start_date: date = Query(..., description="查询起始日，含当天"),
    end_date: date = Query(..., description="查询截止日，含当天"),
    conn: Connection = Depends(get_conn),
) -> ScheduleResponse:
    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="end_date 不能早于 start_date",
        )
    # 整个计算在单条快照事务内完成，结果来自同一个已提交状态
    return get_schedule(conn, start_date, end_date)


@app.get(
    "/manifests",
    response_model=list[ManifestRecord],
    tags=["清单录入"],
    summary="列出已入库清单",
)
def get_manifests(conn: Connection = Depends(get_conn)) -> list[ManifestRecord]:
    rows = list_manifests(conn)
    return [
        ManifestRecord(
            id=r["id"],
            version=r["version"],
            start_date=date.fromisoformat(r["start_date"]),
            end_date=date.fromisoformat(r["end_date"]),
            screening_count=r["screening_count"],
            accepted_at=datetime.fromisoformat(r["accepted_at"]),
        )
        for r in rows
    ]
