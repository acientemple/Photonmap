"""给没有 GPS 的照片推断可能的拍摄位置。

同一个目录里的照片通常出自同一次出行，按拍摄时间排开就是一条轨迹。
拿目标照片的拍摄时间去找最近的已定位照片，就能得到一个合理的猜测，
并且能说清楚这个猜测来自哪张照片、差多久。

完全离线，只用 EXIF（或文件名）里的时间，不联网、不识别画面内容。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime

from .models import PhotoRecord

# 相邻参考照片之间的最大时间间隔，超过就不给候选了
MAX_GAP_SECONDS = 24 * 3600

# 判定「很可能 / 可能 / 存疑」的时间差门槛
VERY_LIKELY = 30 * 60
LIKELY = 3 * 3600

# 插值候选的适用条件
INTERPOLATE_MAX_SPAN = 2 * 3600
INTERPOLATE_MAX_DISTANCE_KM = 50.0

EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class Candidate:
    """一个候选拍摄位置。"""

    lat: float
    lon: float
    source_name: str
    delta_seconds: float
    confidence: str
    kind: str = "neighbor"

    def to_dict(self) -> dict:
        return {
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "source_name": self.source_name,
            "delta_seconds": round(self.delta_seconds),
            "confidence": self.confidence,
            "kind": self.kind,
        }


def _stamp(value: datetime) -> float:
    """时间戳。

    排序与算时间差都用它，而不是直接比较 datetime——
    不同相机的 EXIF 时间可能一个带时区一个不带，
    那种情况下 `a < b` 会抛 TypeError，
    异常一路冒到 PhotoLibrary.refresh()，整个库就停止更新了。
    """
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间的球面距离（公里）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _confidence(delta_seconds: float) -> str:
    if delta_seconds <= VERY_LIKELY:
        return "很可能"
    if delta_seconds <= LIKELY:
        return "可能"
    return "存疑"


def confidence_of(delta_seconds: float) -> str:
    """按时间差给出置信度档位（很可能 / 可能 / 存疑）。"""
    return _confidence(delta_seconds)


def _same_day(a: datetime, b: datetime) -> bool:
    return a.date() == b.date()


def _interpolate(
    before: PhotoRecord, after: PhotoRecord, target_time: datetime
) -> tuple[float, float] | None:
    """目标照片夹在两张已定位照片之间时，按时间线性插值出位置。"""
    span = (after.taken_at - before.taken_at).total_seconds()
    if span <= 0 or span > INTERPOLATE_MAX_SPAN:
        return None
    distance = haversine_km(before.lat, before.lon, after.lat, after.lon)
    if distance > INTERPOLATE_MAX_DISTANCE_KM:
        return None
    ratio = (target_time - before.taken_at).total_seconds() / span
    ratio = min(1.0, max(0.0, ratio))
    return (
        before.lat + (after.lat - before.lat) * ratio,
        before.lon + (after.lon - before.lon) * ratio,
    )


def interpolate_position(
    before: PhotoRecord, after: PhotoRecord, target_time: datetime
) -> tuple[float, float] | None:
    """按时间在两张已定位照片之间插值出位置。

    跨度超过两小时、或两点相距超过 50 公里时不给结果——那时候
    「插值」出来的点多半是在编造。
    """
    return _interpolate(before, after, target_time)


def prepare_timeline(photos: list[PhotoRecord]) -> tuple[list[float], list[PhotoRecord]]:
    """把已定位照片按拍摄时间排好序，供多次候选推断复用。

    给几千张待确认照片逐张推断时，每张都重排一遍已定位照片会退化成
    O(待确认 × 已定位 log 已定位)——4000 张的目录能卡上几分钟。
    """
    entries = [(_stamp(p.taken_at), p) for p in photos if p.taken_at is not None]
    entries.sort(key=lambda item: item[0])
    return [stamp for stamp, _ in entries], [photo for _, photo in entries]


def suggest(
    photos: list[PhotoRecord],
    target: PhotoRecord,
    limit: int = 3,
) -> list[Candidate]:
    """给没有 GPS 的照片找出候选位置，按可信度从高到低排列。

    photos 是已定位的照片；target 是待确认的那张。
    目标照片没有拍摄时间就没法推断，返回空列表。
    一次要给多张照片推断时，先用 prepare_timeline() 排好序再调 suggest_sorted()。
    """
    return suggest_sorted(prepare_timeline(photos), target, limit)


def suggest_sorted(
    timeline: tuple[list[float], list[PhotoRecord]],
    target: PhotoRecord,
    limit: int = 3,
) -> list[Candidate]:
    """suggest() 的排序版：timeline 由 prepare_timeline() 产出。"""
    stamps, timed = timeline
    if target.taken_at is None or not stamps:
        return []
    target_time = target.taken_at
    target_stamp = _stamp(target_time)

    # 只要时间差最小的 limit 张：置信度随差值单调下降，再远的不可能排进前几名。
    # 全窗口扫一遍的话，几千张照片的目录里每张待确认照片都要构造上百个候选对象。
    position = bisect.bisect_right(stamps, target_stamp)
    left = position - 1
    right = position
    nearest: list[tuple[float, int]] = []
    while len(nearest) < limit and (left >= 0 or right < len(stamps)):
        left_delta = target_stamp - stamps[left] if left >= 0 else math.inf
        right_delta = stamps[right] - target_stamp if right < len(stamps) else math.inf
        if left_delta <= right_delta:
            if left_delta > MAX_GAP_SECONDS:
                break
            nearest.append((left_delta, left))
            left -= 1
        else:
            if right_delta > MAX_GAP_SECONDS:
                break
            nearest.append((right_delta, right))
            right += 1

    if not nearest:
        return []

    candidates: list[Candidate] = [
        Candidate(
            lat=timed[index].lat,
            lon=timed[index].lon,
            source_name=timed[index].name,
            delta_seconds=delta,
            confidence=_confidence(delta),
            kind="neighbor",
        )
        for delta, index in nearest
    ]
    candidates.sort(key=lambda c: c.delta_seconds)

    # 目标照片夹在两张已定位照片之间时，给一个时间插值点
    before = timed[position - 1] if position > 0 else None
    after = timed[position] if position < len(timed) else None
    if before is not None and after is not None and before is not after:
        try:
            point = _interpolate(before, after, target_time)
        except (TypeError, ValueError):
            # 两张照片的时间一个带时区一个不带，跨度算不出来——
            # 跳过这个插值候选，其余候选照给
            point = None
        if point is not None:
            candidates.append(
                Candidate(
                    lat=point[0],
                    lon=point[1],
                    source_name=f"{before.name} 与 {after.name} 之间",
                    delta_seconds=0.0,
                    confidence="很可能",
                    kind="interpolated",
                )
            )

    candidates.sort(key=lambda c: (c.confidence != "很可能", c.delta_seconds))
    return candidates[:limit]
