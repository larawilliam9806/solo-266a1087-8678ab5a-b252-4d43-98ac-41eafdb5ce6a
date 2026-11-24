"""停靠判定的纯逻辑：时刻解析、分段、港内连续点识别。

规则（需求原文对应实现）：
- 逐船按时刻排列，相邻点相隔超过 30 分钟就断开成不同段；
- 连续港内点在同一段内前后紧邻港外点才记为停靠，否则列为待补；
- 矩形港区边界算港内（闭区间，见 config.PortRect.contains）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import PortRect

GAP_LIMIT = timedelta(minutes=30)


class ValidationError(Exception):
    """批次校验失败，整批退回。"""


def parse_utc(raw: str) -> datetime:
    """解析 ISO-8601 UTC 时刻，统一得到 tz-aware UTC datetime。"""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"时刻格式非法: {raw!r}，应为 ISO-8601 UTC") from exc
    if dt.tzinfo is None:
        raise ValidationError(f"时刻缺少时区信息，需为 UTC: {raw!r}")
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Point:
    msg_no: str
    ship_no: str
    ts: datetime
    lon: float
    lat: float
    changed: bool = False          # 本次补交/换交改动点（新增或变化）
    in_port: Optional[bool] = None

    def key(self) -> tuple[str, str]:
        return (self.msg_no, to_iso_z(self.ts))


@dataclass
class Segment:
    seg_no: int
    points: list[Point] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        """段内是否存在改动点。"""
        return any(p.changed for p in self.points)

    def seg_key(self) -> str:
        first = self.points[0]
        last = self.points[-1]
        return f"{first.msg_no}|{to_iso_z(first.ts)}|{last.msg_no}|{to_iso_z(last.ts)}"


@dataclass
class InPortRun:
    seg_no: int
    run_no: int
    seg_key: str
    entry: Point
    exit_point: Point
    before: Optional[Point]
    after: Optional[Point]

    @property
    def is_stop(self) -> bool:
        return self.before is not None and self.after is not None

    @property
    def pending_reason(self) -> str:
        if self.before is None and self.after is None:
            return "段内首末均为港内点，前后均无紧邻港外点"
        if self.before is None:
            return "港内点起始于段首，缺少进入前的紧邻港外点"
        return "港内点结束于段尾，缺少离开后的紧邻港外点"


def split_segments(points: list[Point]) -> list[Segment]:
    """按 30 分钟间隔断段。points 需已按 (ts, msg_no) 排序。"""
    segments: list[Segment] = []
    current = Segment(seg_no=0)
    prev_ts: Optional[datetime] = None
    for p in points:
        if prev_ts is not None and p.ts - prev_ts > GAP_LIMIT:
            segments.append(current)
            current = Segment(seg_no=current.seg_no + 1)
        current.points.append(p)
        prev_ts = p.ts
    if current.points:
        segments.append(current)
    return segments


def mark_in_port(points: list[Point], rect: PortRect) -> None:
    for p in points:
        p.in_port = rect.contains(p.lon, p.lat)


def find_inport_runs(seg: Segment) -> list[InPortRun]:
    """识别段内每一段连续港内点，并取其前后紧邻港外点（同段内）。"""
    runs: list[InPortRun] = []
    run_no = 0
    i = 0
    pts = seg.points
    while i < len(pts):
        if not pts[i].in_port:
            i += 1
            continue
        start = i
        while i < len(pts) and pts[i].in_port:
            i += 1
        end = i - 1
        before = pts[start - 1] if start - 1 >= 0 and not pts[start - 1].in_port else None
        after = pts[end + 1] if end + 1 < len(pts) and not pts[end + 1].in_port else None
        runs.append(
            InPortRun(
                seg_no=seg.seg_no,
                run_no=run_no,
                seg_key=seg.seg_key(),
                entry=pts[start],
                exit_point=pts[end],
                before=before,
                after=after,
            )
        )
        run_no += 1
    return runs


def run_reusable(run: InPortRun) -> bool:
    """段内沿用判定：港内连续点及其紧邻港外点均未被改动，
    即改动点与该段港内明细之间隔着未变的港外点时，沿用原判定。
    （删除导致的结构变化由调用方另行判定，不在此函数范围内。）"""
    candidates = [run.entry, run.exit_point]
    if run.before is not None:
        candidates.append(run.before)
    if run.after is not None:
        candidates.append(run.after)
    return all(not p.changed for p in candidates)
