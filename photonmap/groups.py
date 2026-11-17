"""把照片按拍摄时间聚成簇，并在簇的层面推断位置。

单张照片的推断（geo.suggest）只找「时间最近的已定位照片」；
簇级别的推断多了一层上下文：一簇照片本来就是同一次出行，
所以前后夹着的两张已定位照片之间可以插值出一个更准的点，
而组内多张已定位照片的公共位置（中位数）也能给整簇一个稳妥的落点。

完全离线，只用 EXIF（或文件名）里的时间与坐标，不联网、不识别画面内容。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

from .geo import MAX_GAP_SECONDS, confidence_of, haversine_km, interpolate_position
from .models import PhotoRecord

# 相邻拍摄时间差超过这个值就算换了一场，另起一簇
GROUP_GAP_SECONDS = 2 * 3600

# 组内多张已定位照片彼此相距超过这个距离时，中位数不可信
MEDIAN_MAX_SPREAD_KM = 5.0

# 置信度由高到低
CONFIDENCE_ORDER = ("很可能", "可能", "存疑")


@dataclass(frozen=True)
class GroupGuess:
    """一个簇级别的位置推断。"""

    lat: float
    lon: float
    confidence: str
    kind: str
    basis: str

    def to_dict(self) -> dict:
        return {
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "confidence": self.confidence,
            "kind": self.kind,
            "basis": self.basis,
        }


@dataclass
class PhotoGroup:
    """按时间聚起来的一簇照片。"""

    id: str
    start: datetime | None = None
    end: datetime | None = None
    photos: list[PhotoRecord] = field(default_factory=list)
    located: list[PhotoRecord] = field(default_factory=list)
    pending: list[PhotoRecord] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.photos)

    @property
    def pending_count(self) -> int:
        return len(self.pending)


def _stamp(record: PhotoRecord | datetime) -> float:
    """取时间戳；比较时用它，避免 naive 与带时区的时间互相比较报错。"""
    value = record.taken_at if isinstance(record, PhotoRecord) else record
    if value is None:
        return 0.0
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _group_id(photos: list[PhotoRecord]) -> str:
    """簇的稳定标识：用簇里第一张照片的路径哈希。"""
    anchor = photos[0] if photos else None
    if anchor is None:
        return ""
    try:
        resolved = str(anchor.path.resolve())
    except OSError:
        resolved = str(anchor.path)
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]


def _make_group(entries: list[tuple[PhotoRecord, bool]]) -> PhotoGroup:
    """把一批 (照片, 是否待确认) 组装成一个簇。"""
    photos = [photo for photo, _ in entries]
    located = [photo for photo, pending in entries if not pending]
    pending = [photo for photo, pending in entries if pending]

    timed = [photo for photo in photos if photo.taken_at is not None]
    timed.sort(key=_stamp)
    start = timed[0].taken_at if timed else None
    end = timed[-1].taken_at if timed else None

    return PhotoGroup(
        id=_group_id(photos),
        start=start,
        end=end,
        photos=photos,
        located=located,
        pending=pending,
    )


def build_groups(
    located: list[PhotoRecord],
    pending: list[PhotoRecord],
    gap: float = GROUP_GAP_SECONDS,
) -> list[PhotoGroup]:
    """按拍摄时间把照片聚成簇。

    已定位与待确认的照片混在一起按时间排开——一次出行里的照片
    本来就交替有 GPS 没 GPS，分开排会把同一次出行切碎。
    相邻两张的时间差超过 gap 就另起一簇；恰好等于 gap 不切。
    没有拍摄时间的照片单独归到「无时间」簇，不参与推断。
    """
    entries = [(photo, False) for photo in located]
    entries += [(photo, True) for photo in pending]

    timed = [(photo, flag) for photo, flag in entries if photo.taken_at is not None]
    untimed = [(photo, flag) for photo, flag in entries if photo.taken_at is None]
    timed.sort(key=lambda item: _stamp(item[0]))

    groups: list[PhotoGroup] = []
    current: list[tuple[PhotoRecord, bool]] = []
    previous_stamp: float | None = None
    for photo, flag in timed:
        stamp = _stamp(photo)
        if previous_stamp is not None and stamp - previous_stamp > gap:
            groups.append(_make_group(current))
            current = []
        current.append((photo, flag))
        previous_stamp = stamp
    if current:
        groups.append(_make_group(current))

    if untimed:
        groups.append(_make_group(untimed))
    return groups


def _center_time(photos: list[PhotoRecord]) -> datetime | None:
    """簇内拍摄时间的中位数（拿中间那张照片的时间，避免时区混算）。"""
    timed = [photo for photo in photos if photo.taken_at is not None]
    if not timed:
        return None
    timed.sort(key=_stamp)
    return timed[len(timed) // 2].taken_at


def _nearest(
    photos: list[PhotoRecord], target: datetime | None
) -> tuple[PhotoRecord, float] | None:
    """时间上离目标最近的一张，以及相差多少秒。"""
    if not photos:
        return None
    if target is None:
        return None
    stamp = _stamp(target)
    best = min(photos, key=lambda photo: abs(_stamp(photo) - stamp))
    return best, abs(_stamp(best) - stamp)


def _spread_km(photos: list[PhotoRecord]) -> float:
    """组内已定位照片彼此的最大距离（公里）。"""
    worst = 0.0
    for i, first in enumerate(photos):
        for second in photos[i + 1:]:
            worst = max(worst, haversine_km(first.lat, first.lon, second.lat, second.lon))
    return worst


def _downgrade(confidence: str) -> str:
    """置信度降一档。"""
    index = CONFIDENCE_ORDER.index(confidence) if confidence in CONFIDENCE_ORDER else 2
    return CONFIDENCE_ORDER[min(index + 1, len(CONFIDENCE_ORDER) - 1)]


def infer(group: PhotoGroup, located_all: list[PhotoRecord] | None = None) -> GroupGuess | None:
    """推断一簇照片大致在哪儿。

    依次尝试：
    1. **时间插值**：待确认的照片夹在两张已定位照片之间时按时间插值（最准）；
    2. **簇内中位数**：组内多张已定位照片且彼此很近时取中位数（置信度降一档）；
    3. **最近邻**：组内有已定位照片时取时间最近的那张；
    4. **全库最近邻**：组内一张已定位照片都没有时，退到全库里最近的那张。

    推不出来时返回 None——这种簇需要人工给一个锚点。
    """
    located = list(group.located)
    target = _center_time(group.pending) or _center_time(group.photos)

    # 1. 时间插值
    if target is not None and len(located) >= 2:
        stamp = _stamp(target)
        before = after = None
        for photo in sorted(located, key=_stamp):
            if _stamp(photo) <= stamp:
                before = photo
            elif after is None:
                after = photo
        if before is not None and after is not None and before is not after:
            # 不同相机的 EXIF 时间可能一个带时区一个不带，相减会抛异常
            try:
                point = interpolate_position(before, after, target)
            except (TypeError, ValueError):
                point = None
            if point is not None:
                return GroupGuess(
                    lat=point[0],
                    lon=point[1],
                    confidence="很可能",
                    kind="interpolated",
                    basis=f"由 {before.name} 与 {after.name} 之间的时间插值推得",
                )

    # 2. 簇内中位数
    if len(located) >= 2 and _spread_km(located) <= MEDIAN_MAX_SPREAD_KM:
        nearest = _nearest(located, target)
        confidence = confidence_of(nearest[1]) if nearest else "存疑"
        return GroupGuess(
            lat=median(photo.lat for photo in located),
            lon=median(photo.lon for photo in located),
            confidence=_downgrade(confidence),
            kind="median",
            basis=f"由组内 {len(located)} 张已定位照片的中位数推得",
        )

    # 3. 组内最近邻
    nearest = _nearest(located, target)
    if nearest is not None:
        photo, delta = nearest
        return GroupGuess(
            lat=photo.lat,
            lon=photo.lon,
            confidence=confidence_of(delta),
            kind="neighbor",
            basis=f"来自 {photo.name}（相差 {int(delta // 60)} 分钟）",
        )

    # 4. 全库最近邻
    if located_all:
        nearest = _nearest(located_all, target)
        if nearest is not None and nearest[1] <= MAX_GAP_SECONDS:
            photo, delta = nearest
            return GroupGuess(
                lat=photo.lat,
                lon=photo.lon,
                confidence=confidence_of(delta),
                kind="neighbor",
                basis=f"来自 {photo.name}（相差 {int(delta // 60)} 分钟）",
            )

    return None
