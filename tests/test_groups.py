"""时间簇分组与簇级位置推断。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonmap.groups import (
    GROUP_GAP_SECONDS,
    build_groups,
    infer,
)
from photonmap.models import PhotoRecord


def record(name: str, lat: float, lon: float, taken_at: datetime | None) -> PhotoRecord:
    return PhotoRecord(
        path=Path(f"/photos/{name}.jpg"),
        lat=lat,
        lon=lon,
        taken_at=taken_at,
        source="exif" if lat else "none",
    )


T0 = datetime(2026, 4, 5, 12, 0, 0)


def test_build_groups_splits_on_gap():
    """相邻时间差恰好等于 gap 时不切，超过才切。"""
    located = [record("a", 30.0, 120.0, T0)]
    pending = [
        record("b", 0, 0, T0 + timedelta(seconds=GROUP_GAP_SECONDS)),
        record("c", 0, 0, T0 + timedelta(seconds=2 * GROUP_GAP_SECONDS + 1)),
    ]

    groups = build_groups(located, pending)

    assert len(groups) == 2
    assert [p.name for p in groups[0].photos] == ["a.jpg", "b.jpg"]
    assert [p.name for p in groups[1].photos] == ["c.jpg"]


def test_build_groups_keeps_untimed_photos_separate():
    located = [record("a", 30.0, 120.0, T0)]
    pending = [record("b", 0, 0, None)]

    groups = build_groups(located, pending)

    assert len(groups) == 2
    untimed = groups[-1]
    assert untimed.start is None and untimed.end is None
    assert [p.name for p in untimed.photos] == ["b.jpg"]


def test_build_groups_splits_located_and_pending():
    """已定位与待确认的照片按时间混在一起分簇。"""
    located = [record("a", 30.0, 120.0, T0)]
    pending = [record("b", 0, 0, T0 + timedelta(minutes=10))]

    groups = build_groups(located, pending)

    assert len(groups) == 1
    group = groups[0]
    assert [p.name for p in group.located] == ["a.jpg"]
    assert [p.name for p in group.pending] == ["b.jpg"]
    assert group.start == T0
    assert group.end == T0 + timedelta(minutes=10)


def test_infer_prefers_interpolation():
    """待确认照片夹在两张已定位照片之间时用时间插值。"""
    located = [
        record("a", 30.0, 120.0, T0),
        record("b", 30.1, 120.0, T0 + timedelta(hours=1)),
    ]
    pending = [record("p", 0, 0, T0 + timedelta(minutes=30))]

    group = build_groups(located, pending)[0]
    guess = infer(group)

    assert guess is not None
    assert guess.kind == "interpolated"
    assert guess.confidence == "很可能"
    # 半小时处，正好落在两点中间
    assert guess.lat == pytest.approx(30.05, abs=1e-6)
    assert guess.lon == pytest.approx(120.0, abs=1e-6)


def test_infer_falls_back_to_nearest():
    """没有前后夹住时，退到时间最近的那张已定位照片。"""
    located = [record("a", 30.0, 120.0, T0)]
    pending = [record("p", 0, 0, T0 + timedelta(minutes=20))]

    group = build_groups(located, pending)[0]
    guess = infer(group)

    assert guess is not None
    assert guess.kind == "neighbor"
    assert guess.confidence == "很可能"
    assert guess.lat == pytest.approx(30.0, abs=1e-6)


def test_infer_confidence_drops_with_distance():
    located = [record("a", 30.0, 120.0, T0)]
    pending = [record("p", 0, 0, T0 + timedelta(hours=2))]

    group = build_groups(located, pending)[0]
    guess = infer(group)

    assert guess is not None
    assert guess.confidence == "可能"


def test_infer_uses_library_when_group_has_no_located_photo():
    """簇里一张已定位照片都没有时，退到全库里最近的那张。"""
    located = [record("a", 30.0, 120.0, T0)]
    pending = [record("p", 0, 0, T0 + timedelta(minutes=15))]

    # 待确认照片自己成簇（人为拉开时间）
    groups = build_groups(located, pending, gap=60)
    group = groups[-1]
    assert group.located == []

    guess = infer(group, located_all=located)
    assert guess is not None
    assert guess.kind == "neighbor"
    assert guess.lat == pytest.approx(30.0, abs=1e-6)


def test_infer_none_without_located_photo():
    """全库也没有已定位照片时就没有推断结果。"""
    pending = [record("p", 0, 0, T0)]

    group = build_groups([], pending)[0]
    assert infer(group) is None
    assert infer(group, located_all=[]) is None


def test_infer_none_for_untimed_group():
    """没有拍摄时间的簇不参与推断。"""
    pending = [record("p", 0, 0, None)]

    group = build_groups([], pending)[0]
    assert infer(group) is None


def test_infer_median_when_group_located_are_close():
    """组内多张已定位照片彼此很近时取中位数，置信度降一档。

    待确认的照片排在已定位照片之前，插值用不上，才轮到中位数。
    """
    located = [
        record("a", 30.0000, 120.0000, T0),
        record("b", 30.0004, 120.0002, T0 + timedelta(minutes=5)),
    ]
    pending = [record("p", 0, 0, T0 - timedelta(minutes=10))]

    group = build_groups(located, pending)[0]
    guess = infer(group)

    assert guess is not None
    assert guess.kind == "median"
    assert guess.lat == pytest.approx(30.0002, abs=1e-6)
    assert guess.confidence == "可能"


def test_infer_skips_median_when_located_are_far_apart():
    """组内两张已定位照片相距很远时，中位数会落到无人区，改用最近邻。"""
    located = [
        record("a", 30.0, 120.0, T0),
        record("b", 31.0, 120.0, T0 + timedelta(minutes=5)),
    ]
    pending = [record("p", 0, 0, T0 + timedelta(minutes=1))]

    group = build_groups(located, pending)[0]
    guess = infer(group)

    assert guess is not None
    assert guess.kind == "neighbor"
    assert guess.lat == pytest.approx(30.0, abs=1e-6)


def test_infer_survives_mixed_timezones():
    """不同相机的 EXIF 时间可能一个带时区一个不带，不能因此把接口打挂。"""
    from datetime import timezone

    located = [
        record("a", 30.0, 120.0, T0),
        record("b", 30.1, 120.0, datetime(2026, 4, 5, 13, 0, 0, tzinfo=timezone.utc)),
    ]
    pending = [record("p", 0, 0, T0 + timedelta(minutes=30))]

    group = build_groups(located, pending)[0]
    guess = infer(group)          # 不抛异常
    assert guess is not None
    assert guess.kind in ("interpolated", "neighbor")
