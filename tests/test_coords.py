import math
import re
from pathlib import Path

import pytest

from photonmap import coords


def _reference_transform_lat(x: float, y: float) -> float:
    """GCJ-02 标准算法的参照实现（用来盯住 coords.py 的写法）。"""
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _reference_transform_lon(x: float, y: float) -> float:
    """GCJ-02 标准算法的参照实现。

    注意第三项用的是 **y**：这里写成 x 会让经度方向系统性差上几十米，
    而且正逆变换都跟着偏，肉眼很难发现。
    """
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _reference_wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lat, lon
    dlat = _reference_transform_lat(lon - 105.0, lat - 35.0)
    dlon = _reference_transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - 0.00669342162296594323 * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (6378245.0 / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def test_out_of_china_unchanged():
    lat, lon = 35.6762, 139.6503  # 东京
    assert coords.wgs84_to_gcj02(lat, lon) == (lat, lon)
    assert coords.wgs84_to_bd09(lat, lon) == (lat, lon)


def test_shanghai_offset_is_hundreds_of_meters():
    lat, lon = 31.2304, 121.4737
    gcj_lat, gcj_lon = coords.wgs84_to_gcj02(lat, lon)
    offset_m = math.hypot((gcj_lat - lat) * 111_320, (gcj_lon - lon) * 95_000)
    assert 100 < offset_m < 1000


def test_roundtrip_wgs84_gcj02():
    lat, lon = 31.2304, 121.4737
    back_lat, back_lon = coords.gcj02_to_wgs84(*coords.wgs84_to_gcj02(lat, lon))
    assert back_lat == pytest.approx(lat, abs=1e-6)
    assert back_lon == pytest.approx(lon, abs=1e-6)


def test_roundtrip_bd09_gcj02():
    lat, lon = 31.2304, 121.4737
    back_lat, back_lon = coords.bd09_to_gcj02(*coords.gcj02_to_bd09(lat, lon))
    assert back_lat == pytest.approx(lat, abs=1e-6)
    assert back_lon == pytest.approx(lon, abs=1e-6)


def test_bd09_is_further_from_wgs84_than_gcj02():
    lat, lon = 31.2304, 121.4737
    gcj = coords.wgs84_to_gcj02(lat, lon)
    bd = coords.wgs84_to_bd09(lat, lon)
    gcj_d = math.hypot(gcj[0] - lat, gcj[1] - lon)
    bd_d = math.hypot(bd[0] - lat, bd[1] - lon)
    assert bd_d > gcj_d


def test_convert_dispatch():
    lat, lon = 31.2304, 121.4737
    assert coords.convert(lat, lon, "gcj02") == coords.wgs84_to_gcj02(lat, lon)
    assert coords.convert(lat, lon, "wgs84", "wgs84") == (lat, lon)
    with pytest.raises(ValueError):
        coords.convert(lat, lon, "someth")


@pytest.mark.parametrize(
    "point",
    [(39.90874, 116.39750), (31.23040, 121.47370), (23.12911, 113.26438), (30.57227, 104.06654)],
)
def test_matches_reference_gcj02_algorithm(point):
    """换算结果要和 GCJ-02 的标准算法逐位一致。

    这条盯的是 transform_lon 第三项用 x 还是 y——用错的话经度会差几十米，
    正逆变换还都自洽，肉眼看不出来。
    """
    lat, lon = point
    assert coords.wgs84_to_gcj02(lat, lon) == pytest.approx(
        _reference_wgs84_to_gcj02(lat, lon), abs=1e-12
    )


def test_frontend_conversion_matches_backend():
    """前端 photo_map.js 里的换算要和 Python 一致（常量与写法都要对得上）。"""
    script = (
        Path(__file__).parent.parent / "photonmap" / "templates" / "photo_map.js"
    ).read_text(encoding="utf-8")
    a_match = re.search(r"var GCJ_A = ([0-9.]+);", script)
    e_match = re.search(r"var GCJ_EE = ([0-9.]+);", script)
    assert a_match and e_match
    assert float(a_match.group(1)) == coords._A
    assert float(e_match.group(1)) == coords._EE
    # transform_lon 的第三项必须是 y
    lon_block = re.search(r"function transformLon\(x, y\) \{(.*?)\n  \}", script, re.S)
    assert lon_block is not None
    body = lon_block.group(1)
    assert "Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)" in body
    assert "Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)" not in body
