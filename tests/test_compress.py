"""压缩编解码单测：无损往返、Unicode、空集与格式校验。"""
from __future__ import annotations

import zlib

import pytest

from app.compress import CODEC, decode_records, encode_records


def test_roundtrip_is_lossless():
    records = [
        {
            "seg_no": 0,
            "run_no": 0,
            "seg_key": "a|2026-11-24T08:00:00Z|b|2026-11-24T08:10:00Z",
            "entry_point": {
                "msg_no": "m1",
                "ts": "2026-11-24T08:05:00Z",
                "lon": 121.5,
                "lat": 31.5,
                "in_port": True,
            },
            "exit_point": {
                "msg_no": "m1",
                "ts": "2026-11-24T08:05:00Z",
                "lon": 121.5,
                "lat": 31.5,
                "in_port": True,
            },
            "before_point": None,
            "after_point": None,
            "reason": "港内点起始于段首，缺少进入前的紧邻港外点",
        }
    ]
    codec, payload, raw_size = encode_records(records)
    assert codec == CODEC
    assert raw_size > 0
    assert decode_records(payload, codec) == records


def test_unicode_and_empty_roundtrip():
    records = [{"reason": "缺少离开后的紧邻港外点", "seg_key": "船-1|时刻"}]
    codec, payload, _ = encode_records(records)
    assert decode_records(payload, codec) == records

    codec, payload, _ = encode_records([])
    assert decode_records(payload, codec) == []


def test_payload_is_actually_compressed():
    records = [{"seg_no": 0, "run_no": i, "seg_key": "k" * 40} for i in range(50)]
    _, payload, raw_size = encode_records(records)
    assert zlib.decompress(payload)  # 可解压
    assert len(payload) < raw_size


def test_unknown_codec_rejected():
    with pytest.raises(ValueError):
        decode_records(b"xx", "nope")

    codec, payload, _ = encode_records([{"seg_no": 0, "run_no": 0}])
    with pytest.raises(ValueError):
        decode_records(payload, "json+gzip")
