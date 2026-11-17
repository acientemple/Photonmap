"""坐标系转换：WGS-84 ↔ GCJ-02 ↔ BD-09。

- WGS-84：GPS 原始坐标，Google Earth / 境外 Google Maps / OSM 使用
- GCJ-02：中国大陆的「火星坐标」，高德、腾讯、中国大陆 Google Maps 使用
- BD-09：百度坐标，在 GCJ-02 基础上再偏移一层

GCJ-02 的加密只在中国境内生效，境外坐标原样返回。
"""

from __future__ import annotations

import math

WGS84 = "wgs84"
GCJ02 = "gcj02"
BD09 = "bd09"

SYSTEMS = (WGS84, GCJ02, BD09)

# 克拉索夫斯基椭球参数（GCJ-02 加密算法使用）
_A = 6378245.0
_EE = 0.00669342162296594323

_PI = math.pi

# 境外判定范围（粗略框住中国大陆）
_CN_LON = (72.004, 137.8347)
_CN_LAT = (0.8293, 55.8271)


def _out_of_china(lat: float, lon: float) -> bool:
    return not (_CN_LON[0] <= lon <= _CN_LON[1] and _CN_LAT[0] <= lat <= _CN_LAT[1])


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = 1 - _EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """逆向转换用迭代逼近（加密变换没有解析逆解）。"""
    if _out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(30):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        dlat = glat - lat
        dlon = glon - lon
        if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
            break
        wlat -= dlat
        wlon -= dlon
    return wlat, wlon


def gcj02_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    z = math.sqrt(lat * lat + lon * lon) + 0.00002 * math.sin(lat * _PI * 3000.0 / 180.0)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _PI * 3000.0 / 180.0)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def bd09_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    x = lon - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _PI * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _PI * 3000.0 / 180.0)
    return z * math.sin(theta), z * math.cos(theta)


def wgs84_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    return gcj02_to_bd09(*wgs84_to_gcj02(lat, lon))


def bd09_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    return gcj02_to_wgs84(*bd09_to_gcj02(lat, lon))


_CONVERTERS = {
    (WGS84, WGS84): lambda lat, lon: (lat, lon),
    (WGS84, GCJ02): wgs84_to_gcj02,
    (WGS84, BD09): wgs84_to_bd09,
    (GCJ02, GCJ02): lambda lat, lon: (lat, lon),
    (GCJ02, WGS84): gcj02_to_wgs84,
    (GCJ02, BD09): gcj02_to_bd09,
    (BD09, BD09): lambda lat, lon: (lat, lon),
    (BD09, GCJ02): bd09_to_gcj02,
    (BD09, WGS84): bd09_to_wgs84,
}


def convert(lat: float, lon: float, target: str, source: str = WGS84) -> tuple[float, float]:
    """把 (lat, lon) 从 source 基准转到 target 基准。"""
    target = target.lower()
    source = source.lower()
    if target not in SYSTEMS:
        raise ValueError(f"未知坐标系: {target}（可选 {', '.join(SYSTEMS)}）")
    if source not in SYSTEMS:
        raise ValueError(f"未知坐标系: {source}（可选 {', '.join(SYSTEMS)}）")
    converter = _CONVERTERS[(source, target)]
    return converter(lat, lon)
