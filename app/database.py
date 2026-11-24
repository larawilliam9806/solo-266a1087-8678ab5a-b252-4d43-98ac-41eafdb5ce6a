"""SQLite 连接管理与表结构。

设计要点：
- WAL 模式，保证核算（写入新结果）期间，旧结果仍可被查询；
- 所有批次/消息/结果的写入在一个事务内完成，写入失败整体回滚，
  原批次与原结果得以保留；
- 同一时刻只允许一个写事务（写锁串行化），避免并发补交互相覆盖。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_db_path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batch (
    batch_no    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    replaced_at TEXT
);

-- 一条消息（按消息号去重后的现行内容）；换交失效时软删，保留历史可查
CREATE TABLE IF NOT EXISTS message (
    msg_no      TEXT PRIMARY KEY,
    ship_no     TEXT NOT NULL,
    ts          TEXT NOT NULL,           -- ISO-8601 UTC，Z 结尾
    lon         REAL NOT NULL,
    lat         REAL NOT NULL,
    content_sha TEXT NOT NULL,
    created_batch TEXT NOT NULL,         -- 首次纳入该消息的批次
    active      INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (created_batch) REFERENCES batch(batch_no)
);

-- 消息号 -> 现行仍在提供该消息的批次（补交批次叠加，换交时调整）
CREATE TABLE IF NOT EXISTS message_source (
    msg_no   TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    PRIMARY KEY (msg_no, batch_no),
    FOREIGN KEY (msg_no)   REFERENCES message(msg_no),
    FOREIGN KEY (batch_no) REFERENCES batch(batch_no)
);

-- 每次批次生效对应一个结果版本，版本永不删除，支持查询旧结果
CREATE TABLE IF NOT EXISTS result_version (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    batch_set   TEXT NOT NULL,           -- 当时现行批次号集合的哈希
    batches     TEXT NOT NULL,           -- 现行批次号 JSON 数组
    active      INTEGER NOT NULL DEFAULT 0
);

-- 停靠记录：连续港内点在同一段内前后都紧邻港外点
CREATE TABLE IF NOT EXISTS stop_record (
    version_id      INTEGER NOT NULL,
    ship_no         TEXT NOT NULL,
    seg_no          INTEGER NOT NULL,    -- 船内按时间排序的段序号
    run_no          INTEGER NOT NULL,    -- 段内连续港内点序号
    entry_point     TEXT NOT NULL,       -- 首个港内点 JSON
    exit_point      TEXT NOT NULL,       -- 末个港内点 JSON
    before_point    TEXT NOT NULL,       -- 可用港外点（港内段之前）JSON
    after_point     TEXT NOT NULL,       -- 可用港外点（港内段之后）JSON
    seg_key         TEXT NOT NULL,       -- 段稳定键，供增量沿用定位
    PRIMARY KEY (version_id, ship_no, seg_no, run_no),
    FOREIGN KEY (version_id) REFERENCES result_version(version_id)
);

-- 待补记录：港内连续点缺少一侧或两侧港外点
CREATE TABLE IF NOT EXISTS pending_record (
    version_id      INTEGER NOT NULL,
    ship_no         TEXT NOT NULL,
    seg_no          INTEGER NOT NULL,
    run_no          INTEGER NOT NULL,
    entry_point     TEXT NOT NULL,
    exit_point      TEXT NOT NULL,
    before_point     TEXT,               -- 可能为空
    after_point      TEXT,
    reason          TEXT NOT NULL,
    seg_key         TEXT NOT NULL,
    PRIMARY KEY (version_id, ship_no, seg_no, run_no),
    FOREIGN KEY (version_id) REFERENCES result_version(version_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_ship_ts ON message(ship_no, ts, active);
CREATE INDEX IF NOT EXISTS idx_src_batch   ON message_source(batch_no);
CREATE INDEX IF NOT EXISTS idx_stop_ver    ON stop_record(version_id, ship_no);
CREATE INDEX IF NOT EXISTS idx_pend_ver    ON pending_record(version_id, ship_no);
CREATE INDEX IF NOT EXISTS idx_ver_active  ON result_version(active);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = path.rsplit("/", 1)[0]
    if parent and parent not in (":memory:", ""):
        import os

        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """单写事务：BEGIN IMMEDIATE 立即取写锁，失败/异常一律回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
