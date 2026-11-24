"""结果明细的无损压缩编解码。

新版结果按 (版本, 船, 类型) 把整船明细序列化为紧凑 JSON 后做 zlib 无损压缩；
查询时只解码被查询船的那一块，核算时只解码受影响船的那一块。
"""
from __future__ import annotations

import json
import zlib
from typing import Any

CODEC = "json+zlib"


def encode_records(records: list[dict[str, Any]]) -> tuple[str, bytes, int]:
    """返回 (codec, 压缩后字节, 压缩前字节数)。"""
    raw = json.dumps(
        records, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return CODEC, zlib.compress(raw, 9), len(raw)


def decode_records(payload: bytes, codec: str) -> list[dict[str, Any]]:
    if codec != CODEC:
        raise ValueError(f"不支持的结果压缩格式: {codec!r}")
    raw = zlib.decompress(payload)
    records = json.loads(raw.decode("utf-8"))
    if not isinstance(records, list):
        raise ValueError("结果压缩块内容非法，应为记录数组")
    return records
