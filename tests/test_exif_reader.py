from datetime import datetime

import pytest
from PIL import Image

from photonmap.exif_reader import dms_to_decimal, read_exif, read_photo
from photonmap.scanner import scan_directory

from conftest import write_gps_photo


def test_dms_to_decimal_north_east():
    dms = ((31, 1), (13, 1), (4944, 100))
    assert dms_to_decimal(dms, "N") == pytest.approx(31.2304, abs=1e-3)
    assert dms_to_decimal(dms, "S") == pytest.approx(-31.2304, abs=1e-3)


def test_read_gps_and_time(tmp_path):
    path = write_gps_photo(
        tmp_path / "a.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0), altitude=15.5
    )
    data = read_exif(path)
    assert data.has_gps
    assert data.lat == pytest.approx(31.2304, abs=1e-3)
    assert data.lon == pytest.approx(121.4737, abs=1e-3)
    assert data.taken_at == datetime(2024, 5, 1, 12, 30, 0)
    assert data.altitude == pytest.approx(15.5, abs=0.2)


def test_south_west_is_negative(tmp_path):
    path = write_gps_photo(tmp_path / "sw.jpg", -33.8688, -151.2093)
    data = read_exif(path)
    assert data.lat == pytest.approx(-33.8688, abs=1e-3)
    assert data.lon == pytest.approx(-151.2093, abs=1e-3)


def test_photo_without_gps_returns_none(tmp_path):
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (32, 32), (0, 0, 0)).save(path)
    assert read_photo(path) is None
    assert not read_exif(path).has_gps


def test_scanner_filters_and_recurses(tmp_path):
    write_gps_photo(tmp_path / "a.jpg", 31.0, 121.0)
    write_gps_photo(tmp_path / "sub" / "b.jpg", 32.0, 122.0)
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    flat = scan_directory(tmp_path, recursive=False)
    assert [p.name for p in flat] == ["a.jpg"]

    deep = scan_directory(tmp_path, recursive=True)
    assert {p.name for p in deep} == {"a.jpg", "b.jpg"}


def test_scan_directory_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_directory(tmp_path / "nope")


def test_livp_container_exposes_embedded_photo(tmp_path):
    """vivo 的 .livp 是 zip 容器，里面装着静态图，读取时应该取出来。"""
    import zipfile

    from PIL import Image

    from photonmap.imaging import open_image

    inner = tmp_path / "IMG_0001.jpg"
    write_gps_photo(inner, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))

    livp = tmp_path / "shot.livp"
    with zipfile.ZipFile(livp, "w") as zf:
        zf.write(inner, "IMG_0001.HEIC.jpg")
        zf.writestr("IMG_0001.HEIC.mov", b"\x00" * 32)

    with open_image(livp) as img:
        assert img.size == (320, 240)

    record = read_photo(livp)
    assert record is not None
    assert record.lat == pytest.approx(31.2304, abs=1e-4)


def test_to_float_rejects_non_finite():
    from photonmap.exif_reader import _to_float

    assert _to_float(float("nan")) is None
    assert _to_float(float("inf")) is None
    assert _to_float("nan") is None
    assert _to_float((float("nan"), 1)) is None
