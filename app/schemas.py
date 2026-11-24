"""Pydantic schemas for manifest ingestion and schedule queries."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ScreeningIn(BaseModel):
    """单场放映：日期、当日唯一的场次号、片名。"""

    date: date
    session_no: int = Field(ge=1, description="场次号，正整数，同一清单同一天内唯一")
    title: str = Field(min_length=1, description="片名，不可为空白")


class ManifestIn(BaseModel):
    """一份放映清单：整数版次 + 覆盖起止日期（含首尾）+ 范围内全部场次，可为空。"""

    version: int = Field(ge=1, description="整数版次，越大越新")
    start_date: date = Field(description="覆盖起始日，含当天")
    end_date: date = Field(description="覆盖截止日，含当天")
    screenings: list[ScreeningIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "ManifestIn":
        if self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        seen: set[tuple[date, int]] = set()
        normalized: list[ScreeningIn] = []
        for s in self.screenings:
            if not (self.start_date <= s.date <= self.end_date):
                raise ValueError(
                    f"场次日期 {s.date.isoformat()} 超出清单覆盖范围 "
                    f"[{self.start_date.isoformat()}, {self.end_date.isoformat()}]"
                )
            key = (s.date, s.session_no)
            if key in seen:
                raise ValueError(
                    f"{s.date.isoformat()} 的场次号 {s.session_no} 重复"
                )
            seen.add(key)
            title = s.title.strip()
            if not title:
                raise ValueError("片名不可为空白")
            normalized.append(
                ScreeningIn(date=s.date, session_no=s.session_no, title=title)
            )
        self.screenings = normalized
        return self


class DateInterval(BaseModel):
    """闭区间日期段，含首尾两天。"""

    start_date: date
    end_date: date


class ScreeningOut(BaseModel):
    date: date
    session_no: int
    title: str
    version: int = Field(description="该场次当前生效所依据的清单版次")


class ScheduleResponse(BaseModel):
    query_start: date
    query_end: date
    screenings: list[ScreeningOut] = Field(description="当前生效场次，按日期、场次号排序")
    confirmed_no_screening: list[DateInterval] = Field(
        description="已被清单覆盖、但覆盖清单确认当天无放映的日期段"
    )
    no_manifest_yet: list[DateInterval] = Field(
        description="还没有任何清单覆盖到的日期段"
    )


class ManifestRecord(BaseModel):
    id: int
    version: int
    start_date: date
    end_date: date
    screening_count: int
    accepted_at: datetime


class IngestResponse(BaseModel):
    manifest_id: int
    version: int
    start_date: date
    end_date: date
    screening_count: int
    status: Literal["accepted", "duplicate"] = Field(
        description="accepted=新入库；duplicate=完全相同的清单已存在，原样保留未重复入库"
    )
