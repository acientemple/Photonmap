"""测试用的样例照片生成。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import piexif
import pytest
from PIL import Image


def _dms(value: float) -> tuple:
    """十进制度 → EXIF 的度分秒三元组（秒以有理数表示）。"""
    a = abs(value)
    total = a * 3600
    d = int(total // 3600)
    m = int((total - d * 3600) // 60)
    s = total - d * 3600 - m * 60
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def write_gps_photo(
    path: Path,
    lat: float,
    lon: float,
    taken_at: datetime | None = None,
    altitude: float | None = None,
    size: tuple[int, int] = (320, 240),
) -> Path:
    """写一张带 GPS EXIF 的 JPEG。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (40, 110, 180))

    gps = {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _dms(lon),
    }
    if altitude is not None:
        gps[piexif.GPSIFD.GPSAltitude] = (int(abs(altitude) * 100), 100)
        gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if altitude >= 0 else 1

    stamp = taken_at.strftime("%Y:%m:%d %H:%M:%S") if taken_at else "2024:01:01 00:00:00"
    exif = {
        "0th": {piexif.ImageIFD.Make: b"TestCam", piexif.ImageIFD.DateTime: stamp.encode()},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp.encode()},
        "GPS": gps,
    }
    img.save(path, exif=piexif.dump(exif))
    return path


def write_timed_photo(
    path: Path,
    taken_at: datetime | None = None,
    size: tuple[int, int] = (320, 240),
) -> Path:
    """写一张有拍摄时间但没有 GPS 的 JPEG。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (40, 110, 180))

    stamp = taken_at.strftime("%Y:%m:%d %H:%M:%S") if taken_at else "2024:01:01 00:00:00"
    exif = {
        "0th": {piexif.ImageIFD.Make: b"TestCam", piexif.ImageIFD.DateTime: stamp.encode()},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp.encode()},
    }
    img.save(path, exif=piexif.dump(exif))
    return path


@pytest.fixture
def photo_dir(tmp_path: Path) -> Path:
    """一个包含 3 张带 GPS 照片和 1 张无 GPS 照片的目录。"""
    directory = tmp_path / "photos"
    write_gps_photo(directory / "IMG_0001.jpg", 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    write_gps_photo(directory / "IMG_0002.jpg", 31.2400, 121.4800, datetime(2024, 5, 1, 13, 0, 0), 12.5)
    write_gps_photo(directory / "IMG_0003.jpg", 39.9042, 116.4074, datetime(2024, 5, 2, 9, 15, 0))
    Image.new("RGB", (64, 48), (10, 10, 10)).save(directory / "no_gps.jpg")
    return directory
