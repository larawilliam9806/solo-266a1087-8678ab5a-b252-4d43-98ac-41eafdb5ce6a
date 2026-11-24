"""运行期配置：矩形港区与 SQLite 路径，均可用环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PortRect:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def contains(self, lon: float, lat: float) -> bool:
        # 边界算港内（闭区间）
        return (
            self.min_lon <= lon <= self.max_lon
            and self.min_lat <= lat <= self.max_lat
        )


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def get_port_rect() -> PortRect:
    rect = PortRect(
        min_lon=_float_env("PORT_MIN_LON", 121.0),
        min_lat=_float_env("PORT_MIN_LAT", 31.0),
        max_lon=_float_env("PORT_MAX_LON", 122.0),
        max_lat=_float_env("PORT_MAX_LAT", 32.0),
    )
    if not (-180.0 <= rect.min_lon <= rect.max_lon <= 180.0):
        raise ValueError("港区经度配置非法，需满足 -180 <= min_lon <= max_lon <= 180")
    if not (-90.0 <= rect.min_lat <= rect.max_lat <= 90.0):
        raise ValueError("港区纬度配置非法，需满足 -90 <= min_lat <= max_lat <= 90")
    return rect


def get_db_path() -> str:
    return os.environ.get("AIS_DB_PATH", "/data/ais.db")
