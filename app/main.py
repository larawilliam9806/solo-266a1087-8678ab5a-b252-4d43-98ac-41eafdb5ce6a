"""FastAPI 入口：AIS 补报批次与停靠记录查询。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import get_port_rect
from .database import connect, init_db, transaction
from .engine import ValidationError
from .models import (
    BatchInfo,
    BatchResult,
    BatchSubmit,
    PendingRecordOut,
    StopRecordOut,
    VersionInfo,
)
from . import service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    get_port_rect()  # 启动即校验港区配置
    yield


app = FastAPI(
    title="港口 AIS 停靠记录服务",
    description="基于 AIS 补报批次，整理有进出港依据的停靠记录与待补记录。",
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(ValidationError)
def _validation_handler(_request, exc: ValidationError) -> JSONResponse:
    # 整批退回：不落任何批次/结果，返回 409
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "status": "rejected"},
    )


@app.get("/health")
def health(conn=Depends(get_conn)) -> dict:
    rect = get_port_rect()
    row = conn.execute(
        "SELECT version_id FROM result_version WHERE active=1 ORDER BY version_id DESC"
    ).fetchone()
    return {
        "status": "ok",
        "port_rect": rect.__dict__,
        "active_version_id": row["version_id"] if row else None,
    }


@app.post("/batches/{batch_no}", response_model=BatchResult)
def submit_or_replace(
    batch_no: str,
    payload: BatchSubmit,
    mode: str = Query("submit", pattern="^(submit|replace)$"),
    conn=Depends(get_conn),
) -> BatchResult:
    messages = [m.model_dump() for m in payload.messages]
    rect = get_port_rect()
    try:
        with transaction(conn):
            summary = service.apply_batch(conn, batch_no, messages, mode, rect)
    except ValidationError:
        raise
    except Exception as exc:  # 写入失败：事务已回滚，原批次与原结果保留
        raise HTTPException(status_code=500, detail=f"写入失败，已回滚: {exc}")
    return BatchResult(
        batch_no=batch_no,
        mode=mode,
        status="accepted",
        version_id=summary["version_id"],
        messages_accepted=summary["messages_accepted"],
        affected_ships=summary["affected_ships"],
        reused_records=summary["reused_records"],
        recomputed_records=summary["recomputed_records"],
        error=None,
    )


@app.get("/batches/{batch_no}", response_model=BatchInfo)
def get_batch(batch_no: str, conn=Depends(get_conn)) -> BatchInfo:
    info = service.get_batch_info(conn, batch_no)
    if info is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return BatchInfo(**info)


@app.get("/ships/{ship_no}/stops", response_model=list[StopRecordOut])
def get_stops(
    ship_no: str,
    version_id: Optional[int] = Query(None, description="缺省查现行版本，可指定历史版本"),
    conn=Depends(get_conn),
) -> list[StopRecordOut]:
    if version_id is not None and service.resolve_version(conn, version_id) is None:
        raise HTTPException(status_code=404, detail="结果版本不存在")
    return service.query_records(conn, ship_no, "stop", version_id)


@app.get("/ships/{ship_no}/pending", response_model=list[PendingRecordOut])
def get_pending(
    ship_no: str,
    version_id: Optional[int] = Query(None, description="缺省查现行版本，可指定历史版本"),
    conn=Depends(get_conn),
) -> list[PendingRecordOut]:
    if version_id is not None and service.resolve_version(conn, version_id) is None:
        raise HTTPException(status_code=404, detail="结果版本不存在")
    return service.query_records(conn, ship_no, "pending", version_id)


@app.get("/versions", response_model=list[VersionInfo])
def versions(conn=Depends(get_conn)) -> list[VersionInfo]:
    return [VersionInfo(**v) for v in service.list_versions(conn)]
