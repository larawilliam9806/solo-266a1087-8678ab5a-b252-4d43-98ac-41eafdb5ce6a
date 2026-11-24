"""业务服务层：批次补交/换交的校验、原子生效与增量核算，以及查询。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .config import PortRect
from .engine import (
    InPortRun,
    Point,
    Segment,
    ValidationError,
    find_inport_runs,
    mark_in_port,
    parse_utc,
    run_reusable,
    split_segments,
    to_iso_z,
)


# ---------------------------------------------------------------- 工具函数

def _now() -> str:
    return to_iso_z(datetime.now(timezone.utc))


def _content_sha(ship_no: str, ts: str, lon: float, lat: float) -> str:
    payload = json.dumps(
        [ship_no, ts, lon, lat], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _point_json(p: Point) -> str:
    return json.dumps(
        {
            "msg_no": p.msg_no,
            "ts": to_iso_z(p.ts),
            "lon": p.lon,
            "lat": p.lat,
            "in_port": bool(p.in_port),
        },
        ensure_ascii=False,
    )


def _batch_set_hash(batch_nos: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(batch_nos, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


# ---------------------------------------------------------------- 批次校验

def _validate_incoming(
    conn: sqlite3.Connection,
    batch_no: str,
    messages: list[dict[str, Any]],
    mode: str,
) -> dict[str, dict[str, Any]]:
    """批次内 + 与现行有效消息之间的校验，返回 msg_no -> 规范化内容。"""
    if mode == "submit":
        if conn.execute(
            "SELECT 1 FROM batch WHERE batch_no=?", (batch_no,)
        ).fetchone():
            raise ValidationError(f"批号 {batch_no!r} 已存在，补交请使用新批号")
    else:
        if not conn.execute(
            "SELECT 1 FROM batch WHERE batch_no=?", (batch_no,)
        ).fetchone():
            raise ValidationError(f"批号 {batch_no!r} 不存在，无法换交")

    by_msg: dict[str, dict[str, Any]] = {}
    for m in messages:
        ts_dt = parse_utc(m["ts"])
        ts_z = to_iso_z(ts_dt)
        content = {
            "ship_no": m["ship_no"],
            "ts": ts_z,
            "lon": float(m["lon"]),
            "lat": float(m["lat"]),
        }
        if m["msg_no"] in by_msg and by_msg[m["msg_no"]] != content:
            raise ValidationError(
                f"同号异值：消息号 {m['msg_no']!r} 在本批中出现不同内容，整批退回"
            )
        by_msg.setdefault(m["msg_no"], content)

    # 同船同刻异号
    seen_st: dict[tuple[str, str], str] = {}
    for msg_no, c in by_msg.items():
        key = (c["ship_no"], c["ts"])
        if key in seen_st and seen_st[key] != msg_no:
            raise ValidationError(
                f"同船同刻异号：船 {c['ship_no']} 在 {c['ts']} "
                f"存在消息 {seen_st[key]!r} 与 {msg_no!r}，整批退回"
            )
        seen_st.setdefault(key, msg_no)

    # 与现行有效消息冲突（active=1；软删消息行保留但不参与）
    for msg_no, c in by_msg.items():
        row = conn.execute(
            "SELECT ship_no, ts, lon, lat, content_sha FROM message "
            "WHERE msg_no=? AND active=1",
            (msg_no,),
        ).fetchone()
        if row is not None:
            if row["content_sha"] != _content_sha(
                c["ship_no"], c["ts"], c["lon"], c["lat"]
            ):
                raise ValidationError(
                    f"同号异值：消息号 {msg_no!r} 与现行有效批次内容冲突，整批退回"
                )
        clash = conn.execute(
            "SELECT msg_no FROM message WHERE ship_no=? AND ts=? AND msg_no<>? "
            "AND active=1",
            (c["ship_no"], c["ts"], msg_no),
        ).fetchone()
        if clash is not None:
            raise ValidationError(
                f"同船同刻异号：船 {c['ship_no']} 在 {c['ts']} 已有消息 "
                f"{clash['msg_no']!r}，整批退回"
            )
    return by_msg


# ---------------------------------------------------------------- 主事务

def apply_batch(
    conn: sqlite3.Connection,
    batch_no: str,
    messages: list[dict[str, Any]],
    mode: str,
    rect: PortRect,
) -> dict[str, Any]:
    """在调用方已开启的写事务内执行：校验 -> 落批次/消息 -> 增量核算 -> 切版本。

    任何异常都会由外层 transaction 上下文回滚，原批次与原结果保留。
    """
    by_msg = _validate_incoming(conn, batch_no, messages, mode)

    # 换交：找出本批不再提供的消息及其旧内容（作为虚拟删除标记点）
    removed_rows: list[sqlite3.Row] = []
    if mode == "replace":
        placeholders = ",".join("?" * len(by_msg)) if by_msg else ""
        sql = (
            "SELECT m.* FROM message m JOIN message_source s ON s.msg_no=m.msg_no "
            f"WHERE s.batch_no=? AND m.active=1 AND m.msg_no NOT IN ({placeholders})"
        )
        removed_rows = conn.execute(
            sql, [batch_no, *by_msg.keys()]
        ).fetchall()

    affected_ships = {c["ship_no"] for c in by_msg.values()}
    affected_ships.update(r["ship_no"] for r in removed_rows)

    # 1) 批次表
    if mode == "submit":
        conn.execute(
            "INSERT INTO batch(batch_no, created_at) VALUES(?,?)",
            (batch_no, _now()),
        )
    else:
        conn.execute(
            "UPDATE batch SET replaced_at=? WHERE batch_no=?", (_now(), batch_no)
        )

    # 2) 消息与来源。
    #    改动点 = 新纳入的消息（首次出现）或换交中被删除的消息；
    #    已由其他批次提供、本批只是重复提供的同内容消息不算改动点。
    newly_inserted: set[str] = set()
    for msg_no, c in by_msg.items():
        row = conn.execute(
            "SELECT active FROM message WHERE msg_no=?", (msg_no,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO message(msg_no, ship_no, ts, lon, lat, content_sha, "
                "created_batch, active) VALUES(?,?,?,?,?,?,?,1)",
                (
                    msg_no,
                    c["ship_no"],
                    c["ts"],
                    c["lon"],
                    c["lat"],
                    _content_sha(c["ship_no"], c["ts"], c["lon"], c["lat"]),
                    batch_no,
                ),
            )
            newly_inserted.add(msg_no)
        elif row["active"] == 0:
            # 历史上被软删、同号同内容重新纳入 -> 重新生效，属于改动点
            conn.execute(
                "UPDATE message SET active=1 WHERE msg_no=?", (msg_no,)
            )
            newly_inserted.add(msg_no)
        conn.execute(
            "INSERT OR IGNORE INTO message_source(msg_no, batch_no) VALUES(?,?)",
            (msg_no, batch_no),
        )

    deleted_msg_nos: set[str] = set()
    for r in removed_rows:
        conn.execute(
            "DELETE FROM message_source WHERE msg_no=? AND batch_no=?",
            (r["msg_no"], batch_no),
        )
        left = conn.execute(
            "SELECT 1 FROM message_source WHERE msg_no=?", (r["msg_no"],)
        ).fetchone()
        if not left:
            # 其他批次也不再提供 -> 消息软删失效，行保留以支撑历史版本查询
            conn.execute(
                "UPDATE message SET active=0 WHERE msg_no=?", (r["msg_no"],)
            )
            deleted_msg_nos.add(r["msg_no"])

    # 3) 结果版本
    old_version = conn.execute(
        "SELECT version_id FROM result_version WHERE active=1 ORDER BY version_id DESC"
    ).fetchone()
    old_vid = old_version["version_id"] if old_version else None

    active_batches = [
        r["batch_no"]
        for r in conn.execute("SELECT batch_no FROM batch ORDER BY batch_no")
    ]
    cur = conn.execute(
        "INSERT INTO result_version(created_at, batch_set, batches, active) "
        "VALUES(?,?,?,0)",
        (_now(), _batch_set_hash(active_batches), json.dumps(active_batches)),
    )
    new_vid = cur.lastrowid

    reused = 0
    recomputed = 0

    if old_vid is not None and affected_ships:
        marks = ",".join("?" * len(affected_ships))
        conn.execute(
            f"INSERT INTO stop_record(version_id, ship_no, seg_no, run_no, "
            f"entry_point, exit_point, before_point, after_point, seg_key) "
            f"SELECT ?, ship_no, seg_no, run_no, entry_point, exit_point, "
            f"before_point, after_point, seg_key FROM stop_record "
            f"WHERE version_id=? AND ship_no NOT IN ({marks})",
            [new_vid, old_vid, *affected_ships],
        )
        conn.execute(
            f"INSERT INTO pending_record(version_id, ship_no, seg_no, run_no, "
            f"entry_point, exit_point, before_point, after_point, reason, seg_key) "
            f"SELECT ?, ship_no, seg_no, run_no, entry_point, exit_point, "
            f"before_point, after_point, reason, seg_key FROM pending_record "
            f"WHERE version_id=? AND ship_no NOT IN ({marks})",
            [new_vid, old_vid, *affected_ships],
        )
    elif old_vid is not None:
        conn.execute(
            "INSERT INTO stop_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, seg_key) "
            "SELECT ?, ship_no, seg_no, run_no, entry_point, exit_point, "
            "before_point, after_point, seg_key FROM stop_record WHERE version_id=?",
            (new_vid, old_vid),
        )
        conn.execute(
            "INSERT INTO pending_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, reason, seg_key) "
            "SELECT ?, ship_no, seg_no, run_no, entry_point, exit_point, "
            "before_point, after_point, reason, seg_key FROM pending_record "
            "WHERE version_id=?",
            (new_vid, old_vid),
        )

    # 4) 受影响船逐船重建（含段级/港内段级沿用）
    old_records: dict[tuple[str, str, str], dict[str, str]] = {}
    if old_vid is not None:
        for table in ("stop_record", "pending_record"):
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE version_id=? AND ship_no IN "
                f"({','.join('?' * len(affected_ships))})"
                if affected_ships
                else f"SELECT * FROM {table} WHERE 0",
                [old_vid, *affected_ships] if affected_ships else [],
            ).fetchall():
                entry_no = json.loads(row["entry_point"])["msg_no"]
                exit_no = json.loads(row["exit_point"])["msg_no"]
                old_records[(row["seg_key"], entry_no, exit_no)] = dict(row)

    # 仅真正失效的消息作为删除标记；其他批次仍提供的消息不产生任何变化。
    # 删除标记不参与实际判定（判定只基于现存点），仅用于增量沿用的触及判断。
    removed_by_ship: dict[str, list[sqlite3.Row]] = {}
    for r in removed_rows:
        if r["msg_no"] in deleted_msg_nos:
            removed_by_ship.setdefault(r["ship_no"], []).append(r)

    for ship_no in sorted(affected_ships):
        points: list[Point] = []
        for row in conn.execute(
            "SELECT * FROM message WHERE ship_no=? AND active=1 ORDER BY ts, msg_no",
            (ship_no,),
        ):
            points.append(
                Point(
                    msg_no=row["msg_no"],
                    ship_no=ship_no,
                    ts=parse_utc(row["ts"]),
                    lon=row["lon"],
                    lat=row["lat"],
                    changed=row["msg_no"] in newly_inserted,
                )
            )
        removed_markers = [
            Point(
                msg_no=r["msg_no"],
                ship_no=ship_no,
                ts=parse_utc(r["ts"]),
                lon=r["lon"],
                lat=r["lat"],
            )
            for r in removed_by_ship.get(ship_no, [])
        ]
        mark_in_port(points, rect)
        segments = split_segments(points)

        # 段触及判定：段内含改动点，或被删消息的旧时刻落在段的时间跨度内
        # （说明该段的结构可能因删除而变化，保守重算；其他段与所有改动点之间
        #  都隔着未变的港外点，沿用判定且不读取其港内明细）。
        def _seg_touched(seg: Segment) -> bool:
            if seg.touched:
                return True
            start_ts = seg.points[0].ts
            end_ts = seg.points[-1].ts
            return any(start_ts <= mk.ts <= end_ts for mk in removed_markers)

        for seg in segments:
            runs = find_inport_runs(seg)
            if not runs:
                continue
            if not _seg_touched(seg) and old_vid is not None:
                # 整段未触及：整段沿用，不读港内明细
                seg_old = [
                    rec
                    for rec in old_records.values()
                    if rec["ship_no"] == ship_no and rec["seg_key"] == seg.seg_key()
                ]
                if seg_old:
                    for rec in seg_old:
                        _copy_old_record(conn, new_vid, rec)
                        reused += 1
                    continue
            seg_has_removal = any(
                seg.points[0].ts <= mk.ts <= seg.points[-1].ts
                for mk in removed_markers
            )
            for run in runs:
                old_key = (
                    seg.seg_key(),
                    run.entry.msg_no,
                    run.exit_point.msg_no,
                )
                old_rec = old_records.get(old_key)
                if (
                    old_vid is not None
                    and old_rec is not None
                    and not seg_has_removal
                    and run_reusable(run)
                ):
                    _copy_old_record(conn, new_vid, old_rec)
                    reused += 1
                else:
                    _insert_run_record(conn, new_vid, run)
                    recomputed += 1

    # 5) 切换现行版本
    conn.execute("UPDATE result_version SET active=0 WHERE active=1")
    conn.execute(
        "UPDATE result_version SET active=1 WHERE version_id=?", (new_vid,)
    )
    return {
        "version_id": new_vid,
        "messages_accepted": len(by_msg),
        "affected_ships": sorted(affected_ships),
        "reused_records": reused,
        "recomputed_records": recomputed,
    }


def _copy_old_record(conn: sqlite3.Connection, new_vid: int, rec: dict[str, Any]):
    if "reason" in rec:
        conn.execute(
            "INSERT INTO pending_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, reason, seg_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                new_vid,
                rec["ship_no"],
                rec["seg_no"],
                rec["run_no"],
                rec["entry_point"],
                rec["exit_point"],
                rec["before_point"],
                rec["after_point"],
                rec["reason"],
                rec["seg_key"],
            ),
        )
    else:
        conn.execute(
            "INSERT INTO stop_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, seg_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                new_vid,
                rec["ship_no"],
                rec["seg_no"],
                rec["run_no"],
                rec["entry_point"],
                rec["exit_point"],
                rec["before_point"],
                rec["after_point"],
                rec["seg_key"],
            ),
        )


def _insert_run_record(conn: sqlite3.Connection, new_vid: int, run: InPortRun):
    entry_json = _point_json(run.entry)
    exit_json = _point_json(run.exit_point)
    before_json = _point_json(run.before) if run.before is not None else None
    after_json = _point_json(run.after) if run.after is not None else None
    if run.is_stop:
        conn.execute(
            "INSERT INTO stop_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, seg_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                new_vid,
                run.entry.ship_no,
                run.seg_no,
                run.run_no,
                entry_json,
                exit_json,
                before_json,
                after_json,
                run.seg_key,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO pending_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, reason, seg_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                new_vid,
                run.entry.ship_no,
                run.seg_no,
                run.run_no,
                entry_json,
                exit_json,
                before_json,
                after_json,
                run.pending_reason,
                run.seg_key,
            ),
        )


# ---------------------------------------------------------------- 查询

def resolve_version(
    conn: sqlite3.Connection, version_id: Optional[int]
) -> Optional[sqlite3.Row]:
    if version_id is not None:
        return conn.execute(
            "SELECT * FROM result_version WHERE version_id=?", (version_id,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM result_version WHERE active=1 ORDER BY version_id DESC"
    ).fetchone()


def _enrich_point(conn: sqlite3.Connection, raw: Optional[str]) -> Optional[dict]:
    if raw is None:
        return None
    point = json.loads(raw)
    batches = [
        r["batch_no"]
        for r in conn.execute(
            "SELECT batch_no FROM message_source WHERE msg_no=? ORDER BY batch_no",
            (point["msg_no"],),
        )
    ]
    point["source_batches"] = batches
    return point


def _row_to_record(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> dict:
    base = {
        "ship_no": row["ship_no"],
        "seg_no": row["seg_no"],
        "run_no": row["run_no"],
        "seg_key": row["seg_key"],
        "entry_point": _enrich_point(conn, row["entry_point"]),
        "exit_point": _enrich_point(conn, row["exit_point"]),
    }
    if kind == "stop":
        base.update(
            kind="stop",
            before_point=_enrich_point(conn, row["before_point"]),
            after_point=_enrich_point(conn, row["after_point"]),
        )
    else:
        base.update(
            kind="pending",
            before_point=_enrich_point(conn, row["before_point"]),
            after_point=_enrich_point(conn, row["after_point"]),
            reason=row["reason"],
        )
    return base


def query_records(
    conn: sqlite3.Connection, ship_no: str, kind: str, version_id: Optional[int]
) -> list[dict]:
    version = resolve_version(conn, version_id)
    if version is None:
        return []
    table = "stop_record" if kind == "stop" else "pending_record"
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE version_id=? AND ship_no=? "
        f"ORDER BY seg_no, run_no",
        (version["version_id"], ship_no),
    ).fetchall()
    return [_row_to_record(conn, r, kind) for r in rows]


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM result_version ORDER BY version_id"
    ).fetchall()
    return [
        {
            "version_id": r["version_id"],
            "created_at": r["created_at"],
            "batches": json.loads(r["batches"]),
            "active": bool(r["active"]),
        }
        for r in rows
    ]


def get_batch_info(conn: sqlite3.Connection, batch_no: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM batch WHERE batch_no=?", (batch_no,)
    ).fetchone()
    if row is None:
        return None
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM message_source WHERE batch_no=?", (batch_no,)
    ).fetchone()["c"]
    return {
        "batch_no": row["batch_no"],
        "created_at": row["created_at"],
        "replaced_at": row["replaced_at"],
        "active": True,
        "message_count": count,
    }
