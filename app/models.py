"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class AISMessageIn(BaseModel):
    msg_no: str = Field(min_length=1, description="消息号")
    ship_no: str = Field(min_length=1, description="船号")
    ts: str = Field(description="UTC 时刻，ISO-8601，如 2026-11-24T08:00:00Z")
    lon: float = Field(ge=-180.0, le=180.0, description="经度")
    lat: float = Field(ge=-90.0, le=90.0, description="纬度")

    @field_validator("ts")
    @classmethod
    def _check_utc(cls, v: str) -> str:
        from .engine import ValidationError, parse_utc

        try:
            parse_utc(v)  # 抛异常即 422
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        return v


class BatchSubmit(BaseModel):
    messages: list[AISMessageIn] = Field(min_length=1)


class PointOut(BaseModel):
    msg_no: str
    ts: str
    lon: float
    lat: float
    in_port: bool
    source_batches: list[str]


class StopRecordOut(BaseModel):
    ship_no: str
    seg_no: int
    run_no: int
    seg_key: str
    entry_point: PointOut
    exit_point: PointOut
    before_point: PointOut
    after_point: PointOut
    kind: Literal["stop"] = "stop"


class PendingRecordOut(BaseModel):
    ship_no: str
    seg_no: int
    run_no: int
    seg_key: str
    entry_point: PointOut
    exit_point: PointOut
    before_point: Optional[PointOut]
    after_point: Optional[PointOut]
    reason: str
    kind: Literal["pending"] = "pending"


class BatchResult(BaseModel):
    batch_no: str
    mode: Literal["submit", "replace"]
    status: Literal["accepted", "rejected"]
    version_id: Optional[int] = None
    error: Optional[str] = None
    messages_accepted: int
    affected_ships: list[str] = []
    reused_records: int = 0
    recomputed_records: int = 0


class BatchInfo(BaseModel):
    batch_no: str
    created_at: str
    replaced_at: Optional[str]
    active: bool
    message_count: int


class VersionInfo(BaseModel):
    version_id: int
    created_at: str
    batches: list[str]
    active: bool
