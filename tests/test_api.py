"""端到端 API 测试：批次校验、停靠/待补、换交、旧版本查询、原子回滚、增量沿用。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AIS_DB_PATH", db_path)
    monkeypatch.setenv("PORT_MIN_LON", "121.0")
    monkeypatch.setenv("PORT_MIN_LAT", "31.0")
    monkeypatch.setenv("PORT_MAX_LON", "122.0")
    monkeypatch.setenv("PORT_MAX_LAT", "32.0")
    # 延迟导入，确保环境变量先生效
    from app import database
    from app.main import app

    database.init_db(database.connect(db_path))

    # 让依赖注入始终用同一个临时库路径
    def _override():
        conn = database.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    from app.main import get_conn

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def m(msg, ship, ts, lon, lat):
    return {"msg_no": msg, "ship_no": ship, "ts": ts, "lon": lon, "lat": lat}


B = "2026-11-24T08:{:02d}:00Z"


def submit(client, batch_no, messages, mode="submit"):
    return client.post(f"/batches/{batch_no}?mode={mode}", json={"messages": messages})


# ---------------------------------------------------------------- 基本判定

def test_health_and_empty_query(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["active_version_id"] is None
    assert client.get("/ships/S1/stops").json() == []


def test_stop_and_pending_classification(client):
    # S1: 港外 -> 港内x2 -> 港外（停靠）；S2: 港内开头 -> 港外（待补）
    msgs = [
        m("a1", "S1", B.format(0), 120.0, 30.0),
        m("a2", "S1", B.format(5), 121.5, 31.5),
        m("a3", "S1", B.format(10), 121.6, 31.6),
        m("a4", "S1", B.format(15), 123.0, 33.0),
        m("b1", "S2", B.format(0), 121.5, 31.5),
        m("b2", "S2", B.format(8), 123.0, 33.0),
    ]
    r = submit(client, "B1", msgs)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    v1 = r.json()["version_id"]

    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 1
    stop = stops[0]
    assert stop["kind"] == "stop"
    assert stop["entry_point"]["msg_no"] == "a2"
    assert stop["exit_point"]["msg_no"] == "a3"
    assert stop["before_point"]["msg_no"] == "a1"
    assert stop["after_point"]["msg_no"] == "a4"
    for p in (
        stop["entry_point"],
        stop["exit_point"],
        stop["before_point"],
        stop["after_point"],
    ):
        assert p["source_batches"] == ["B1"]
    assert client.get("/ships/S1/pending").json() == []

    pending = client.get("/ships/S2/pending").json()
    assert len(pending) == 1
    assert pending[0]["entry_point"]["msg_no"] == "b1"
    assert pending[0]["before_point"] is None
    assert pending[0]["after_point"]["msg_no"] == "b2"
    assert pending[0]["reason"]
    assert client.get("/ships/S2/stops").json() == []

    # 版本表
    versions = client.get("/versions").json()
    assert [v["version_id"] for v in versions] == [v1]
    assert versions[0]["active"] is True


def test_30min_gap_splits_segment_and_creates_two_pending(client):
    # 两个港内点相隔 31 分钟 -> 两段，每段都缺港外点
    msgs = [
        m("g1", "S9", B.format(0), 121.5, 31.5),
        m("g2", "S9", "2026-11-24T08:31:00Z", 121.6, 31.6),
    ]
    submit(client, "BG", msgs)
    pending = client.get("/ships/S9/pending").json()
    assert len(pending) == 2
    assert {p["entry_point"]["msg_no"] for p in pending} == {"g1", "g2"}
    assert {p["seg_no"] for p in pending} == {0, 1}


def test_point_on_rectangle_boundary_counts_as_in_port(client):
    # 恰好落在港区西南角边界上 -> 算港内，单点为待补
    r = submit(client, "BB", [m("x1", "S7", B.format(0), 121.0, 31.0)])
    assert r.status_code == 200
    pending = client.get("/ships/S7/pending").json()
    assert len(pending) == 1
    assert pending[0]["entry_point"]["in_port"] is True
    assert client.get("/ships/S7/stops").json() == []


def test_invalid_payload_returns_422(client):
    # 时刻无时区
    r = client.post(
        "/batches/B1",
        json={"messages": [m("a1", "S1", "2026-11-24T08:00:00", 121.5, 31.5)]},
    )
    assert r.status_code == 422
    # 经纬度越界
    r2 = client.post(
        "/batches/B2",
        json={"messages": [m("a2", "S1", B.format(0), 200.0, 31.5)]},
    )
    assert r2.status_code == 422
    # 空批次
    r3 = client.post("/batches/B3", json={"messages": []})
    assert r3.status_code == 422
    assert client.get("/health").json()["active_version_id"] is None


# ---------------------------------------------------------------- 批次校验

def test_duplicate_same_content_counts_once(client):
    msgs = [
        m("a1", "S1", B.format(0), 120.0, 30.0),
        m("a1", "S1", B.format(0), 120.0, 30.0),  # 同号同内容重复
    ]
    r = submit(client, "B1", msgs)
    assert r.status_code == 200
    assert r.json()["messages_accepted"] == 1
    info = client.get("/batches/B1").json()
    assert info["message_count"] == 1


def test_same_msg_no_different_value_rejects_whole_batch(client):
    msgs = [
        m("a1", "S1", B.format(0), 120.0, 30.0),
        m("a1", "S1", B.format(0), 120.5, 30.5),
    ]
    r = submit(client, "B1", msgs)
    assert r.status_code == 409 and r.json()["status"] == "rejected"
    assert client.get("/health").json()["active_version_id"] is None
    assert client.get("/batches/B1").status_code == 404  # 整批未落库


def test_same_ship_same_ts_different_msg_no_rejects(client):
    msgs = [
        m("a1", "S1", B.format(0), 120.0, 30.0),
        m("a2", "S1", B.format(0), 120.0, 30.0),
    ]
    r = submit(client, "B1", msgs)
    assert r.status_code == 409
    assert "同船同刻异号" in r.json()["detail"]


def test_cross_batch_conflicting_msg_no_rejects(client):
    submit(client, "B1", [m("a1", "S1", B.format(0), 120.0, 30.0)])
    r = submit(client, "B2", [m("a1", "S1", B.format(0), 120.9, 30.9)])
    assert r.status_code == 409 and "同号异值" in r.json()["detail"]
    # B2 未生效，仍只有一个版本
    assert len(client.get("/versions").json()) == 1


def test_duplicate_batch_no_submit_rejected(client):
    submit(client, "B1", [m("a1", "S1", B.format(0), 120.0, 30.0)])
    r = submit(client, "B1", [m("a2", "S1", B.format(1), 120.0, 30.0)])
    assert r.status_code == 409 and "已存在" in r.json()["detail"]


def test_replace_unknown_batch_rejected(client):
    r = submit(client, "BX", [m("a1", "S1", B.format(0), 120.0, 30.0)], mode="replace")
    assert r.status_code == 409


# ---------------------------------------------------------------- 补交叠加与来源

def test_supplement_from_second_batch_completes_stop(client):
    # B1 只有港内点 -> 待补
    submit(client, "B1", [m("i1", "S1", B.format(5), 121.5, 31.5)])
    assert len(client.get("/ships/S1/pending").json()) == 1

    # B2 补交两侧港外点 -> 停靠；i1 现行来源仍是 B1
    r = submit(
        client,
        "B2",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    assert r.status_code == 200
    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 1
    assert stops[0]["entry_point"]["source_batches"] == ["B1"]
    assert stops[0]["before_point"]["source_batches"] == ["B2"]
    assert stops[0]["after_point"]["source_batches"] == ["B2"]
    assert client.get("/ships/S1/pending").json() == []


def test_same_message_supplied_by_two_batches_lists_both_sources(client):
    submit(client, "B1", [m("a1", "S1", B.format(0), 121.5, 31.5)])
    submit(client, "B2", [m("a1", "S1", B.format(0), 121.5, 31.5)])  # 同号同内容
    pending = client.get("/ships/S1/pending").json()
    assert pending[0]["entry_point"]["source_batches"] == ["B1", "B2"]


# ---------------------------------------------------------------- 换交

def test_replace_removes_message_other_batches_keep_valid(client):
    # B1 给港内点 i1，B2 给港外点 o1/o2 构成停靠
    submit(client, "B1", [m("i1", "S1", B.format(5), 121.5, 31.5)])
    submit(
        client,
        "B2",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    assert len(client.get("/ships/S1/stops").json()) == 1

    # 整批换交 B2：只保留 o1，o2 失效（其他批次未提供）-> 回到待补
    r = submit(
        client, "B2", [m("o1", "S1", B.format(0), 120.0, 30.0)], mode="replace"
    )
    assert r.status_code == 200
    assert len(client.get("/ships/S1/stops").json()) == 0
    pending = client.get("/ships/S1/pending").json()
    assert len(pending) == 1
    assert pending[0]["entry_point"]["msg_no"] == "i1"
    assert pending[0]["after_point"] is None
    # B1 的消息继续有效
    assert pending[0]["entry_point"]["source_batches"] == ["B1"]
    assert client.get("/batches/B2").json()["message_count"] == 1


def test_replace_same_size_unchanged_creates_new_version_same_records(client):
    msgs = [
        m("o1", "S1", B.format(0), 120.0, 30.0),
        m("i1", "S1", B.format(5), 121.5, 31.5),
        m("o2", "S1", B.format(10), 123.0, 33.0),
    ]
    r1 = submit(client, "B1", msgs)
    v1 = r1.json()["version_id"]
    r2 = submit(client, "B1", msgs, mode="replace")
    v2 = r2.json()["version_id"]
    assert v2 > v1
    new_stop = client.get(f"/ships/S1/stops?version_id={v2}").json()
    old_stop = client.get(f"/ships/S1/stops?version_id={v1}").json()
    assert len(new_stop) == len(old_stop) == 1
    assert new_stop[0]["entry_point"]["msg_no"] == "i1"


def test_old_version_still_queryable_after_recompute(client):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    v1 = client.get("/versions").json()[0]["version_id"]
    # 换交去掉两个港外点
    submit(client, "B1", [m("i1", "S1", B.format(5), 121.5, 31.5)], mode="replace")
    v2 = client.get("/versions").json()[-1]["version_id"]
    assert v2 != v1

    old = client.get(f"/ships/S1/stops?version_id={v1}").json()
    cur_pending = client.get("/ships/S1/pending").json()
    assert len(old) == 1 and old[0]["after_point"]["msg_no"] == "o2"
    assert len(cur_pending) == 1
    # 旧版本的点按现行来源解析：o2 已失效，来源为空
    assert old[0]["after_point"]["source_batches"] == []
    assert client.get("/ships/S1/stops?version_id=9999").status_code == 404


# ---------------------------------------------------------------- 增量沿用

def test_segment_reuse_when_changes_separated_by_unchanged_outside(client):
    # 第一段 08:00-08:10 构成停靠；新增点在 10:00，间隔 >30 分钟形成另一段
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    before = client.get("/ships/S1/stops").json()
    assert len(before) == 1

    r = submit(
        client, "B2", [m("z1", "S1", "2026-11-24T10:00:00Z", 121.5, 31.5)]
    )
    # 第一段未被触及 -> 沿用；新段一个港内点 -> 待补
    assert r.json()["reused_records"] == 1
    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 1
    assert stops[0]["entry_point"]["msg_no"] == "i1"
    assert stops[0]["seg_key"] == before[0]["seg_key"]
    pending = client.get("/ships/S1/pending").json()
    assert len(pending) == 1 and pending[0]["entry_point"]["msg_no"] == "z1"


def test_run_reuse_within_touched_segment(client):
    # 港外 o3 把两个港内段隔开，改动落在第二段
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(2), 121.5, 31.5),
            m("o2", "S1", B.format(4), 123.0, 33.0),
            m("i2", "S1", B.format(6), 121.5, 31.5),
            m("o3", "S1", B.format(8), 120.0, 30.0),
        ],
    )
    assert len(client.get("/ships/S1/stops").json()) == 2

    # 改动点 i3 紧邻第二个港内段；第一个港内段与改动点隔着未变港外点 o2
    r = submit(
        client,
        "B2",
        [
            m("i3", "S1", B.format(7), 121.6, 31.6),  # 与 i2 同港内连续段
        ],
    )
    assert r.status_code == 200
    assert r.json()["reused_records"] == 1
    assert r.json()["recomputed_records"] == 1
    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 2
    first = next(s for s in stops if s["entry_point"]["msg_no"] == "i1")
    assert first["before_point"]["msg_no"] == "o1"


# ---------------------------------------------------------------- 原子性

def test_write_failure_rolls_back_keeping_original(client, monkeypatch):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    versions_before = client.get("/versions").json()
    assert len(versions_before) == 1

    # 注入写入故障：版本插入后、提交前抛错
    from app import service

    real_insert_run = service._insert_run_record

    def boom(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(service, "_insert_run_record", boom)
    r = submit(client, "B2", [m("x1", "S1", "2026-11-24T11:00:00Z", 121.5, 31.5)])
    assert r.status_code == 500
    monkeypatch.setattr(service, "_insert_run_record", real_insert_run)

    # 原批次与原结果保留
    assert client.get("/versions").json() == versions_before
    assert client.get("/batches/B2").status_code == 404
    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 1 and stops[0]["entry_point"]["msg_no"] == "i1"


# ---------------------------------------------------------------- 压缩存储

def _db(client):
    """测试库连接（与 client 共用同一临时文件）。"""
    from app import database

    return database.connect(os.environ["AIS_DB_PATH"])


def test_new_versions_store_only_compressed_blobs(client):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
            m("p1", "S2", B.format(0), 121.5, 31.5),
        ],
    )
    conn = _db(client)
    try:
        # 新版本明细只落压缩块，不再写明文表
        assert conn.execute("SELECT COUNT(*) FROM stop_record").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pending_record").fetchone()[0] == 0
        blobs = conn.execute(
            "SELECT ship_no, kind, codec, record_count, raw_size, payload "
            "FROM record_blob ORDER BY ship_no, kind"
        ).fetchall()
        assert {(r["ship_no"], r["kind"]) for r in blobs} == {
            ("S1", "stop"),
            ("S2", "pending"),
        }
        for r in blobs:
            assert r["codec"] == "json+zlib"
            assert r["record_count"] == 1
            assert len(r["payload"]) < r["raw_size"]  # 确实压缩过
        version = conn.execute("SELECT storage FROM result_version").fetchone()
        assert version["storage"] == "blob"
    finally:
        conn.close()


def test_query_decodes_only_requested_ship(client, monkeypatch):
    for i in range(4):
        submit(
            client,
            f"B{i}",
            [
                m(f"o{i}", f"S{i}", B.format(0), 120.0, 30.0),
                m(f"i{i}", f"S{i}", B.format(5), 121.5, 31.5),
                m(f"q{i}", f"S{i}", B.format(10), 123.0, 33.0),
            ],
        )
    from app import compress

    calls: list[bytes] = []
    real_decode = compress.decode_records

    def tracking(payload, codec):
        calls.append(payload)
        return real_decode(payload, codec)

    from app import service

    monkeypatch.setattr(service, "decode_records", tracking)
    stops = client.get("/ships/S2/stops").json()
    assert [s["entry_point"]["msg_no"] for s in stops] == ["i2"]
    # 只解码被查询船的那一块
    assert len(calls) == 1


def test_unaffected_ships_carry_compressed_blob_without_decoding(client, monkeypatch):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
            m("o3", "S2", B.format(0), 120.0, 30.0),
            m("i2", "S2", B.format(5), 121.5, 31.5),
            m("o4", "S2", B.format(10), 123.0, 33.0),
        ],
    )
    conn = _db(client)
    try:
        old_payload = conn.execute(
            "SELECT payload FROM record_blob WHERE ship_no='S2' AND kind='stop'"
        ).fetchone()["payload"]
    finally:
        conn.close()

    from app import compress, service

    decoded: list[bytes] = []
    real_decode = compress.decode_records

    def tracking(payload, codec):
        decoded.append(payload)
        return real_decode(payload, codec)

    # 新增只影响 S1；S2 的压缩块直接沿用，不会被解码
    monkeypatch.setattr(service, "decode_records", tracking)
    r = submit(client, "B2", [m("z1", "S1", "2026-11-24T10:00:00Z", 121.5, 31.5)])
    assert r.status_code == 200
    assert old_payload not in decoded

    conn = _db(client)
    try:
        row = conn.execute(
            "SELECT payload FROM record_blob WHERE version_id=? AND "
            "ship_no='S2' AND kind='stop'",
            (r.json()["version_id"],),
        ).fetchone()
        assert row["payload"] == old_payload
    finally:
        conn.close()
    stops = client.get("/ships/S2/stops").json()
    assert [s["entry_point"]["msg_no"] for s in stops] == ["i2"]


def test_reuse_of_previous_decision_from_compressed_version(client):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    before = client.get("/ships/S1/stops").json()[0]
    # 新点与第一段相隔 >30 分钟：整段沿用压缩版上的旧判定
    r = submit(client, "B2", [m("z1", "S1", "2026-11-24T10:00:00Z", 121.5, 31.5)])
    assert r.json()["reused_records"] == 1
    after = client.get("/ships/S1/stops").json()[0]
    assert after["seg_key"] == before["seg_key"]
    assert after["entry_point"] == before["entry_point"]


def test_compress_write_failure_rolls_back_batch_and_version(client, monkeypatch):
    submit(
        client,
        "B1",
        [
            m("o1", "S1", B.format(0), 120.0, 30.0),
            m("i1", "S1", B.format(5), 121.5, 31.5),
            m("o2", "S1", B.format(10), 123.0, 33.0),
        ],
    )
    versions_before = client.get("/versions").json()

    from app import service

    def boom(*_args, **_kwargs):
        raise RuntimeError("compress failed")

    monkeypatch.setattr(service, "encode_records", boom)
    r = submit(client, "B2", [m("z1", "S1", "2026-11-24T10:00:00Z", 121.5, 31.5)])
    assert r.status_code == 500

    # 原批次与原版本供数不受影响
    assert client.get("/versions").json() == versions_before
    assert client.get("/batches/B2").status_code == 404
    stops = client.get("/ships/S1/stops").json()
    assert len(stops) == 1 and stops[0]["entry_point"]["msg_no"] == "i1"


def test_legacy_plaintext_version_queryable_and_upgraded_on_first_blob_version(
    client,
):
    """旧库历史版本（明文明细）可直接查；首次生成压缩版时读取上一版明文。"""
    import json

    from app import database

    db_path = os.environ["AIS_DB_PATH"]
    conn = database.connect(db_path)
    try:
        # 手工构造一个升级前遗留的明文版本（storage=plain）
        old_version = conn.execute(
            "INSERT INTO result_version(created_at, batch_set, batches, active, storage) "
            "VALUES('2026-11-01T00:00:00Z','legacy','[\"OLD1\"]',1,'plain')"
        ).lastrowid
        conn.execute(
            "INSERT INTO batch(batch_no, created_at) VALUES('OLD1','2026-11-01T00:00:00Z')"
        )
        point = json.dumps(
            {
                "msg_no": "i9",
                "ts": "2026-11-01T08:05:00Z",
                "lon": 121.5,
                "lat": 31.5,
                "in_port": True,
            }
        )
        before_pt = json.dumps(
            {
                "msg_no": "o8",
                "ts": "2026-11-01T08:00:00Z",
                "lon": 120.0,
                "lat": 30.0,
                "in_port": False,
            }
        )
        after_pt = json.dumps(
            {
                "msg_no": "o9",
                "ts": "2026-11-01T08:10:00Z",
                "lon": 123.0,
                "lat": 33.0,
                "in_port": False,
            }
        )
        seg_key = "o8|2026-11-01T08:00:00Z|o9|2026-11-01T08:10:00Z"
        conn.execute(
            "INSERT INTO stop_record(version_id, ship_no, seg_no, run_no, "
            "entry_point, exit_point, before_point, after_point, seg_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (old_version, "S9", 0, 0, point, point, before_pt, after_pt, seg_key),
        )
    finally:
        conn.close()

    # 旧版本明文记录可直接查
    legacy_stop = client.get(f"/ships/S9/stops?version_id={old_version}").json()
    assert len(legacy_stop) == 1
    assert legacy_stop[0]["entry_point"]["msg_no"] == "i9"

    # 新批次生效生成压缩版，未涉及 S9 -> 从上一版明文首次压缩沿用
    r = submit(client, "B2", [m("z1", "S1", "2026-11-24T10:00:00Z", 121.5, 31.5)])
    assert r.status_code == 200
    new_vid = r.json()["version_id"]

    conn = database.connect(db_path)
    try:
        # 新版本 S9 明细以压缩块承载，旧版本明文仍在
        blob = conn.execute(
            "SELECT record_count FROM record_blob WHERE version_id=? "
            "AND ship_no='S9' AND kind='stop'",
            (new_vid,),
        ).fetchone()
        assert blob is not None and blob["record_count"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stop_record WHERE version_id=? AND ship_no='S9'",
                (new_vid,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stop_record WHERE version_id=? AND ship_no='S9'",
                (old_version,),
            ).fetchone()[0]
            == 1
        )
        # 历史明文版本仍在，可继续查
        storage = conn.execute(
            "SELECT storage FROM result_version WHERE version_id=?", (old_version,)
        ).fetchone()["storage"]
        assert storage == "plain"
    finally:
        conn.close()

    carried = client.get(f"/ships/S9/stops?version_id={new_vid}").json()
    assert carried == legacy_stop
