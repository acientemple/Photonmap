"""从照片 EXIF 中提取 GPS 坐标、拍摄时间与海拔。

GPS 在 EXIF 里以「度分秒」（DMS）存放，例如 (31, 14, 24.5)，
并带 N/S、E/W 方向标记，需要还原成十进制度（南纬、西经为负）。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from PIL import ExifTags

from .imaging import HEIF_AVAILABLE, open_image
from .models import PhotoRecord

log = logging.getLogger(__name__)

# EXIF GPS IFD 内的标签号
_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4
_GPS_ALT_REF = 5
_GPS_ALT = 6
_GPS_TIMESTAMP = 7
_GPS_DATESTAMP = 29

# Exif 子 IFD 内的标签号
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DATETIME_DIGITIZED = 0x9004

# 文件名里的拍摄时间，如 2026-10-01 100701 / 20261001_120000 / IMG_20261001_120000
_FILENAME_STAMP = re.compile(
    r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})[\s_]?(\d{2})(\d{2})(\d{2})(?!\d)"
)


@dataclass
class ExifData:
    """从一张照片中解析出的原始信息。"""

    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None
    taken_at: datetime | None = None

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None


def _to_float(value) -> float | None:
    """把 EXIF 的 Rational / tuple / 数值统一成 float。

    坏 ENEX 里会出现 nan/inf（例如 0/0 的 Rational），这种值一路写进 JSON 会让
    浏览器的 JSON.parse 直接报错，所以这里统一当「没有这个值」处理。
    """
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if not value:
            return None
        # Rational 元组的 (num, den) 要先相除
        if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
            try:
                number = float(value[0]) / float(value[1]) if value[1] else None
            except (TypeError, ZeroDivisionError):
                return None
            return number if number is not None and math.isfinite(number) else None
        return _to_float(value[0])
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def dms_to_decimal(dms, ref: str | None) -> float | None:
    """(31, 14, 24.5) + 'N' → 31.240138…，南纬/西经为负。"""
    if dms is None:
        return None
    parts = [_to_float(p) for p in dms]
    if not parts or parts[0] is None:
        return None
    while len(parts) < 3:
        parts.append(0.0)
    value = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if ref and str(ref).strip().upper()[:1] in ("S", "W"):
        return -value
    return value


def _parse_exif_datetime(raw) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_gps_datetime(gps_ifd: dict) -> datetime | None:
    """用 GPSDateStamp + GPSTimeStamp 拼出 UTC 时间。"""
    date_raw = gps_ifd.get(_GPS_DATESTAMP)
    time_raw = gps_ifd.get(_GPS_TIMESTAMP)
    if not date_raw:
        return None
    date_text = str(date_raw).strip().rstrip("\x00")
    try:
        date_part = datetime.strptime(date_text, "%Y:%m:%d")
    except ValueError:
        return None
    time_part = datetime.min.time()
    if time_raw:
        values = [_to_float(v) for v in time_raw]
        if len(values) >= 3 and all(v is not None for v in values[:3]):
            time_part = time(
                int(values[0]),
                int(values[1]),
                int(values[2]),
                tzinfo=datetime.now().astimezone().tzinfo,
            )
    return datetime.combine(date_part.date(), time_part)


def _read_with_pillow(path: Path) -> ExifData:
    data = ExifData()
    with open_image(path) as img:
        exif = img.getexif()
        if not exif:
            return data

        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) or {}
        if gps_ifd:
            lat = dms_to_decimal(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF))
            lon = dms_to_decimal(gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF))
            if lat is not None and lon is not None:
                data.lat, data.lon = lat, lon
            alt = _to_float(gps_ifd.get(_GPS_ALT))
            if alt is not None:
                ref = gps_ifd.get(_GPS_ALT_REF)
                data.altitude = -alt if ref in (1, b"\x01") else alt

        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
        for tag in (_EXIF_DATETIME_ORIGINAL, _EXIF_DATETIME_DIGITIZED):
            data.taken_at = data.taken_at or _parse_exif_datetime(exif_ifd.get(tag))
        if data.taken_at is None:
            data.taken_at = _parse_exif_datetime(exif.get(0x9003))
        if data.taken_at is None:
            data.taken_at = _parse_exif_datetime(exif.get(0x0132))

    if data.taken_at is None and gps_ifd:
        data.taken_at = _parse_gps_datetime(gps_ifd)
    return data


def _read_with_piexif(path: Path) -> ExifData:
    """piexif 兜底：部分相机写出的 Exif 子 IFD 结构不标准时更可靠。"""
    import piexif

    data = ExifData()
    exif_dict = piexif.load(str(path))
    gps = exif_dict.get("GPS") or {}
    if gps:
        lat = dms_to_decimal(gps.get(piexif.GPSIFD.GPSLatitude), gps.get(piexif.GPSIFD.GPSLatitudeRef))
        lon = dms_to_decimal(gps.get(piexif.GPSIFD.GPSLongitude), gps.get(piexif.GPSIFD.GPSLongitudeRef))
        if lat is not None and lon is not None:
            data.lat, data.lon = lat, lon
        alt = _to_float(gps.get(piexif.GPSIFD.GPSAltitude))
        if alt is not None:
            data.altitude = -alt if gps.get(piexif.GPSIFD.GPSAltitudeRef) == 1 else alt
    exif = exif_dict.get("Exif") or {}
    for tag in (piexif.ExifIFD.DateTimeOriginal, piexif.ExifIFD.DateTimeDigitized):
        data.taken_at = data.taken_at or _parse_exif_datetime(exif.get(tag))
    return data


def filename_timestamp(path: str | Path) -> datetime | None:
    """从文件名里的时间戳还原拍摄时间（很多手机相册导出后会丢 EXIF 时间）。"""
    match = _FILENAME_STAMP.search(Path(path).stem)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(v) for v in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def read_exif(path: str | Path) -> ExifData:
    """读取单张照片的 EXIF 信息，失败时返回空结果而不是抛异常。"""
    path = Path(path)
    try:
        data = _read_with_pillow(path)
    except Exception as exc:
        log.debug("Pillow 读取 %s 失败: %s", path, exc)
        data = ExifData()

    if not data.has_gps or data.taken_at is None:
        try:
            fallback = _read_with_piexif(path)
        except Exception as exc:
            log.debug("piexif 读取 %s 失败: %s", path, exc)
            fallback = ExifData()
        if not data.has_gps and fallback.has_gps:
            data.lat, data.lon = fallback.lat, fallback.lon
            data.altitude = data.altitude if data.altitude is not None else fallback.altitude
        if data.taken_at is None:
            data.taken_at = fallback.taken_at
    return data


def read_photo(
    path: str | Path,
    fallback: tuple[float, float] | None = None,
) -> PhotoRecord | None:
    """把照片读成 PhotoRecord。

    EXIF 里没有 GPS 时，若给了 fallback 就用它代替坐标；两者都没有则返回 None。
    """
    path = Path(path)
    data = read_exif(path)
    if data.has_gps:
        lat, lon = data.lat, data.lon
    elif fallback is not None:
        lat, lon = fallback
    else:
        return None

    taken_at = data.taken_at or filename_timestamp(path)
    return PhotoRecord(
        path=path,
        lat=lat,
        lon=lon,
        altitude=data.altitude,
        taken_at=taken_at,
    )
