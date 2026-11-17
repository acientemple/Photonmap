"""统一的数据模型：所有照片先解析成 PhotoRecord，再交给各导出器。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def finite_or_none(value) -> float | None:
    """把 nan/inf 变成 None。

    JSON 标准里没有 NaN/Infinity，浏览器 `JSON.parse` 遇到会直接抛错
    （Python 的 json.dumps 默认却会照写），所以所有要进 JSON 的浮点数都要过这一道。
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class PhotoRecord:
    """一张带位置信息的照片。

    坐标一律以 WGS-84 存放（照片 EXIF 里的原始坐标），
    需要其它基准时由 coords 模块在导出环节转换。
    """

    path: Path
    lat: float
    lon: float
    altitude: float | None = None
    taken_at: datetime | None = None
    thumbnail: Path | None = None
    marker: Path | None = None
    # 坐标的来源：exif（相机 GPS）/ sidecar（人工标定）/ fallback（命令行兜底）
    source: str = "exif"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def title(self) -> str:
        if self.taken_at:
            return f"{self.taken_at:%Y-%m-%d %H:%M:%S} {self.path.name}"
        return self.path.name

    def coordinates(self, system: str = "wgs84") -> tuple[float, float]:
        """按指定坐标系返回 (lat, lon)。"""
        from . import coords

        return coords.convert(self.lat, self.lon, system)
