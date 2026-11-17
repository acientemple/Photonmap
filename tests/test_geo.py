from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import write_gps_photo

from photonmap.exif_reader import read_photo
from photonmap.geo import Candidate, haversine_km, suggest
from photonmap.models import PhotoRecord


def _photo(path: Path, lat: float, lon: float, taken_at: datetime) -> PhotoRecord:
    write_gps_photo(path, lat, lon, taken_at)
    record = read_photo(path)
    assert record is not None
    return record


def _target(path: Path, taken_at: datetime) -> PhotoRecord:
    return PhotoRecord(path=path, lat=0.0, lon=0.0, taken_at=taken_at)


def test_suggest_uses_nearest_neighbour(tmp_path):
    a = _photo(tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 10, 0, 0))
    b = _photo(tmp_path / "b.jpg", 31.2400, 121.4900, datetime(2024, 5, 1, 14, 0, 0))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 10, 20, 0))

    candidates = suggest([a, b], target)
    assert candidates
    top = candidates[0]
    # 20 分钟的间隔算「很可能」
    assert top.source_name == "a.jpg"
    assert top.confidence == "很可能"
    assert top.kind == "neighbor"
    assert top.delta_seconds == pytest.approx(1200)


def test_suggest_confidence_drops_with_time_gap(tmp_path):
    a = _photo(tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 10, 0, 0))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 12, 0, 0))

    candidates = suggest([a], target)
    assert candidates[0].confidence == "可能"  # 2 小时

    target2 = _target(tmp_path / "t2.jpg", datetime(2024, 5, 1, 15, 0, 0))
    candidates2 = suggest([a], target2)
    assert candidates2[0].confidence == "存疑"  # 5 小时


def test_suggest_interpolates_between_two_photos(tmp_path):
    a = _photo(tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 10, 0, 0))
    b = _photo(tmp_path / "b.jpg", 31.2400, 121.4900, datetime(2024, 5, 1, 11, 0, 0))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 10, 30, 0))

    candidates = suggest([a, b], target)
    top = candidates[0]
    assert top.kind == "interpolated"
    # 时间正好在一半，位置也该在一半
    assert top.lat == pytest.approx((a.lat + b.lat) / 2, abs=1e-6)
    assert top.lon == pytest.approx((a.lon + b.lon) / 2, abs=1e-6)


def test_suggest_ignores_faraway_photos(tmp_path):
    a = _photo(tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 10, 0, 0))
    far = _photo(tmp_path / "far.jpg", 39.9042, 116.4074, datetime(2024, 5, 20, 10, 0, 0))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 11, 0, 0))

    candidates = suggest([a, far], target)
    assert [c.source_name for c in candidates] == ["a.jpg"]


def test_suggest_needs_a_timestamp(tmp_path):
    a = _photo(tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 10, 0, 0))
    target = PhotoRecord(path=tmp_path / "t.jpg", lat=0.0, lon=0.0, taken_at=None)
    assert suggest([a], target) == []


def test_haversine_known_distance():
    # 上海到杭州，大约 165 km
    d = haversine_km(31.2304, 121.4737, 30.2489, 120.1460)
    assert 150 < d < 180


def test_candidate_to_dict():
    candidate = Candidate(
        lat=31.0, lon=121.0, source_name="a.jpg", delta_seconds=60,
        confidence="很可能", kind="neighbor",
    )
    data = candidate.to_dict()
    assert data["lat"] == 31.0
    assert data["source_name"] == "a.jpg"
    assert data["confidence"] == "很可能"


def test_suggest_survives_mixed_timezones():
    """目录里混了带时区与不带时区的 EXIF 时间时，候选推断不能崩。

    异常会从 PhotoLibrary._reindex() 冒到 refresh()，整个库就停止更新了。
    """
    from datetime import timezone

    naive = PhotoRecord(
        path=Path("/photos/a.jpg"), lat=30.0, lon=120.0,
        taken_at=datetime(2024, 5, 1, 12, 0, 0),
    )
    aware = PhotoRecord(
        path=Path("/photos/b.jpg"), lat=30.1, lon=120.0,
        taken_at=datetime(2024, 5, 1, 13, 0, 0, tzinfo=timezone.utc),
    )
    target = PhotoRecord(
        path=Path("/photos/t.jpg"), lat=0.0, lon=0.0,
        taken_at=datetime(2024, 5, 1, 12, 30, 0),
    )

    candidates = suggest([naive, aware], target)
    assert candidates
    assert all(c.kind in ("neighbor", "interpolated") for c in candidates)


def test_suggest_sorted_matches_suggest(tmp_path):
    from photonmap.geo import prepare_timeline, suggest_sorted

    a = _photo(tmp_path / "a.jpg", 30.0, 120.0, datetime(2024, 5, 1, 10, 0))
    b = _photo(tmp_path / "b.jpg", 30.2, 120.2, datetime(2024, 5, 1, 10, 40))
    c = _photo(tmp_path / "c.jpg", 31.0, 121.0, datetime(2024, 5, 1, 15, 0))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 10, 20))

    plain = suggest([a, b, c], target)
    ordered = suggest_sorted(prepare_timeline([a, b, c]), target)
    assert ordered == plain


def test_prepare_timeline_skips_photos_without_time(tmp_path):
    from photonmap.geo import prepare_timeline

    a = _photo(tmp_path / "a.jpg", 30.0, 120.0, datetime(2024, 5, 1, 10, 0))
    no_time = PhotoRecord(path=tmp_path / "x.jpg", lat=1.0, lon=2.0, taken_at=None)
    stamps, photos = prepare_timeline([no_time, a])
    assert photos == [a]
    assert len(stamps) == 1


def test_suggest_keeps_only_nearest_candidates(tmp_path):
    """邻居很多时只取最近的 limit 个，不必把整段时间窗都算一遍。"""
    photos = []
    for i in range(10):
        photos.append(_photo(tmp_path / f"p{i}.jpg", 30.0 + i * 0.01, 120.0,
                             datetime(2024, 5, 1, 10) + timedelta(minutes=i * 10)))
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 10, 44))
    candidates = suggest(photos, target, limit=3)
    # 最近的邻居 + 可能的时间插值点，最多 3 条
    assert len(candidates) <= 3
    assert candidates[0].kind == "interpolated"
    deltas = [c.delta_seconds for c in candidates if c.kind == "neighbor"]
    assert deltas == sorted(deltas)


def test_suggest_builds_only_a_few_candidates(tmp_path, monkeypatch):
    """邻居有几百张时也只构造最近那几个候选，不然每张待确认照片都要建几百个对象。"""
    import photonmap.geo as geo

    built = []
    real = geo.Candidate

    class Counting(real):
        def __init__(self, *args, **kwargs):
            built.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(geo, "Candidate", Counting)
    photos = [
        _photo(tmp_path / f"p{i}.jpg", 30.0 + i * 0.0001, 120.0,
               datetime(2024, 5, 1, 10) + timedelta(minutes=i * 3))
        for i in range(200)
    ]
    target = _target(tmp_path / "t.jpg", datetime(2024, 5, 1, 13, 15))
    candidates = suggest(photos, target, limit=3)
    assert 1 <= len(candidates) <= 3
    assert len(built) <= 5
