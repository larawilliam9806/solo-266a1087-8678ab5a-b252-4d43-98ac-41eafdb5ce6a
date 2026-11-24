"""引擎纯逻辑单测：矩形边界、30 分钟断段、停靠/待补判定、沿用判定。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import PortRect
from app.engine import (
    Point,
    ValidationError,
    find_inport_runs,
    mark_in_port,
    parse_utc,
    run_reusable,
    split_segments,
)

RECT = PortRect(121.0, 31.0, 122.0, 32.0)
T0 = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


def pt(msg: str, ts: datetime, lon: float, lat: float, **kw) -> Point:
    return Point(msg_no=msg, ship_no="S1", ts=ts, lon=lon, lat=lat, **kw)


def test_port_boundary_is_inside():
    assert RECT.contains(121.0, 31.0)   # 西南角边界算港内
    assert RECT.contains(122.0, 32.0)   # 东北角边界算港内
    assert RECT.contains(121.5, 31.5)
    assert not RECT.contains(120.99, 31.5)
    assert not RECT.contains(121.5, 32.01)


def test_parse_utc_forms_and_rejects_naive():
    assert parse_utc("2026-11-24T08:00:00Z") == T0
    assert parse_utc("2026-11-24T16:00:00+08:00") == T0
    with pytest.raises(ValidationError):
        parse_utc("2026-11-24T08:00:00")  # 无时区
    with pytest.raises(ValidationError):
        parse_utc("not-a-time")


def test_split_on_more_than_30_min_gap():
    pts = [
        pt("a", T0, 120.0, 30.0),
        pt("b", T0 + timedelta(minutes=30), 120.0, 30.0),   # 恰好 30 分钟不断
        pt("c", T0 + timedelta(minutes=60, seconds=1), 120.0, 30.0),
    ]
    segs = split_segments(pts)
    assert len(segs) == 2
    assert [p.msg_no for p in segs[0].points] == ["a", "b"]
    assert [p.msg_no for p in segs[1].points] == ["c"]
    assert segs[0].seg_no == 0 and segs[1].seg_no == 1


def _run_over(pts):
    mark_in_port(pts, RECT)
    segs = split_segments(pts)
    assert len(segs) == 1
    return find_inport_runs(segs[0])


def test_stop_requires_outside_points_on_both_sides():
    # 港外 -> 港内 x2 -> 港外：停靠
    pts = [
        pt("o1", T0, 120.0, 30.0),
        pt("i1", T0 + timedelta(minutes=5), 121.5, 31.5),
        pt("i2", T0 + timedelta(minutes=10), 121.6, 31.6),
        pt("o2", T0 + timedelta(minutes=15), 123.0, 33.0),
    ]
    runs = _run_over(pts)
    assert len(runs) == 1
    run = runs[0]
    assert run.is_stop
    assert run.entry.msg_no == "i1" and run.exit_point.msg_no == "i2"
    assert run.before.msg_no == "o1" and run.after.msg_no == "o2"


def test_pending_when_segment_starts_in_port():
    pts = [
        pt("i1", T0, 121.5, 31.5),
        pt("i2", T0 + timedelta(minutes=5), 121.6, 31.6),
        pt("o2", T0 + timedelta(minutes=10), 123.0, 33.0),
    ]
    run = _run_over(pts)[0]
    assert not run.is_stop
    assert run.before is None and run.after.msg_no == "o2"
    assert "进入前" in run.pending_reason


def test_multiple_inport_runs_in_one_segment():
    pts = [
        pt("o1", T0, 120.0, 30.0),
        pt("i1", T0 + timedelta(minutes=5), 121.5, 31.5),
        pt("o2", T0 + timedelta(minutes=10), 120.0, 30.0),
        pt("i2", T0 + timedelta(minutes=15), 121.5, 31.5),
        pt("o3", T0 + timedelta(minutes=20), 120.0, 30.0),
    ]
    runs = _run_over(pts)
    assert len(runs) == 2
    assert all(r.is_stop for r in runs)
    assert [r.run_no for r in runs] == [0, 1]


def test_run_reusable_only_when_separated_by_unchanged_outside():
    pts = [
        pt("o1", T0, 120.0, 30.0),
        pt("i1", T0 + timedelta(minutes=5), 121.5, 31.5),
        pt("i2", T0 + timedelta(minutes=10), 121.6, 31.6),
        pt("o2", T0 + timedelta(minutes=15), 123.0, 33.0),
        pt("i3", T0 + timedelta(minutes=20), 121.5, 31.5, changed=True),
    ]
    mark_in_port(pts, RECT)
    segs = split_segments(pts)
    runs = find_inport_runs(segs[0])
    assert len(runs) == 2
    # 第一个港内段与改动点 i3 之间隔着未变港外点 o2 -> 可沿用
    assert run_reusable(runs[0]) is True
    # 第二个港内段本身含改动点 -> 不可沿用
    assert run_reusable(runs[1]) is False
