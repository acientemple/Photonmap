"""地理编码：把地名、地址解析成坐标。

放在服务端做代理而不是让浏览器直连，有三个理由：
- Nominatim 的使用政策要求带可识别的 User-Agent，并且每秒不超过一次请求；
- 限流和缓存只有放在服务端才管得住；
- 可选的高德 key 不能进浏览器。

回退顺序：高德（有 key 时）→ Nominatim → Photon。
高德返回的是 GCJ-02，要折回 WGS-84；Nominatim 与 Photon 本来就是 WGS-84。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import coords

log = logging.getLogger(__name__)

USER_AGENT = "photonmap/0.1 (local photo map)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api/"
AMAP_URL = "https://restapi.amap.com/v3/geocode/geo"
# 百度网页版搜索：免密钥，国内 POI 覆盖好；返回 BD-09 墨卡托坐标
BAIDU_SEARCH_URL = "https://map.baidu.com/"

DEFAULT_TIMEOUT = 6.0
CACHE_TTL = 600.0
CACHE_MAX = 256
# Nominatim 的使用政策：每秒最多一次请求
MIN_INTERVAL = 1.1

# 百度墨卡托 → 经纬度的分段拟合系数（百度 JS 里那一套，原样搬过来）
_BD_MC_BANDS = [12890594.86, 8362377.87, 5591021, 3481989.83, 1678043.12, 0]
_BD_MC2LL = [
    [1.410526172116255e-8, 0.00000898305509648872, -1.9939833816331,
     200.9824383106796, -187.2403703815547, 91.6087516669843,
     -23.38765649603339, 2.57121317296198, -0.03801003308653, 17337981.2],
    [-7.435856389565537e-9, 0.000008983055097726239, -0.78625201886289,
     96.32687599759846, -1.85204757529826, -59.36935905485877,
     47.40033549296737, -16.50741931063887, 2.28786674699375, 10260144.86],
    [-3.030883460898826e-8, 0.00000898305509983578, 0.30071316287616,
     59.74293618442277, 7.357984074871, -25.38371002664745,
     13.45380521110908, -3.29883767235584, 0.32710905363475, 6856817.37],
    [-1.981981304930552e-8, 0.000008983055099779535, 0.03278182852591,
     40.31678527705744, 0.65659298677277, -4.44255534477492,
     0.85341911805263, 0.12923347998204, -0.04625736007561, 4482777.06],
    [3.09191371068437e-9, 0.000008983055096812155, 0.00006995724062,
     23.10934304144901, -0.00023663490511, -0.6321817810242,
     -0.00663494467273, 0.03430082397953, -0.00466043876332, 2555164.4],
    [2.890871144776878e-9, 0.000008983055095805407, -3.068298e-8,
     7.47137025468032, -0.00000353937994, -0.02145144861037,
     -0.00001234426596, 0.00010322952773, -0.00000323890364, 826088.5],
]


def baidu_mc_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """百度墨卡托（BD09MC）→ WGS-84。"""
    factors = _BD_MC2LL[-1]
    for band, row in zip(_BD_MC_BANDS, _BD_MC2LL):
        if abs(y) >= band:
            factors = row
            break
    lng = factors[0] + factors[1] * abs(x)
    ratio = abs(y) / factors[9]
    lat = factors[2]
    for power, factor in enumerate(factors[3:9], start=1):
        lat += factor * ratio**power
    if x < 0:
        lng = -lng
    if y < 0:
        lat = -lat
    return coords.convert(lat, lng, coords.WGS84, source=coords.BD09)


class Geocoder:
    """地名 → WGS-84 坐标。线程安全。"""

    def __init__(self, amap_key: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.amap_key = (amap_key or "").strip() or None
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}

    # -- 对外 -------------------------------------------------------------

    def search(self, query: str, limit: int = 5, engine: str | None = None) -> list[dict]:
        """解析地名。

        engine 决定优先用哪套数据：amap → 高德（有 key 时）或百度兜底；
        baidu → 百度；其余（leaflet / google）→ OSM。
        返回 [{label, detail, lat, lon, source}]，坐标一律 WGS-84。
        全部源都失败时返回空列表（调用方据此提示"没找到"）。
        """
        query = (query or "").strip()
        if not query:
            return []

        engine = (engine or "").strip().lower() or None
        cache_key = (engine, self.amap_key is not None, query, limit)
        hit = self._cache_get(cache_key)
        if hit is not None:
            return hit

        for name, fetch in self._sources(engine):
            try:
                results = fetch(query, limit)
            except Exception as exc:  # 网络问题、返回格式不对……都当这一源失败
                log.info("地理编码源 %s 失败: %s", name, exc)
                continue
            if results:
                self._cache_put(cache_key, results)
                return results
        return []

    def _sources(self, engine: str | None) -> list[tuple[str, object]]:
        """按选中的地图引擎给出一串数据源，前面的优先。"""
        osm = [("nominatim", self._nominatim), ("photon", self._photon)]
        if engine == "amap":
            # 高德没有免密钥的搜索接口：配了 key 就用高德官方，
            # 没有就用百度网页搜索兜底（国内覆盖比 OSM 好得多）。
            chain: list[tuple[str, object]] = []
            if self.amap_key:
                chain.append(("amap", self._amap))
            return chain + [("baidu", self._baidu_web)] + osm
        if engine == "baidu":
            return [("baidu", self._baidu_web)] + osm
        if engine is None:
            # 老调用方不指定引擎：有高德 key 就先用高德，其余退回 OSM
            chain = [("amap", self._amap)] if self.amap_key else []
            return chain + osm
        return osm

    # -- 各数据源 ---------------------------------------------------------

    def _amap(self, query: str, limit: int) -> list[dict]:
        """高德地理编码。境内效果最好，但需要 key。返回的是 GCJ-02。"""
        self._throttle("amap")
        params = urllib.parse.urlencode(
            {"key": self.amap_key, "address": query, "output": "json"}
        )
        payload = self._get_json(f"{AMAP_URL}?{params}")
        results = []
        for item in (payload.get("geocodes") or [])[:limit]:
            location = item.get("location") or ""
            parts = location.split(",")
            if len(parts) != 2:
                continue
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            # 高德给的是 GCJ-02，折回 WGS-84
            lat, lon = coords.convert(lat, lon, coords.WGS84, source=coords.GCJ02)
            results.append(
                {
                    "label": item.get("formatted_address") or query,
                    "detail": item.get("formatted_address") or "",
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "source": "amap",
                }
            )
        return results

    def _baidu_web(self, query: str, limit: int) -> list[dict]:
        """百度网页版的 POI 搜索，免密钥。返回的是 BD-09 墨卡托坐标。"""
        self._throttle("baidu")
        params = urllib.parse.urlencode(
            {"qt": "s", "wd": query, "rn": limit, "ie": "utf-8"}
        )
        payload = self._get_json(
            f"{BAIDU_SEARCH_URL}?{params}",
            referer="https://map.baidu.com/",
            user_agent=BROWSER_UA,
        )
        results = []
        for item in (payload.get("content") or [])[:limit]:
            name = item.get("name")
            x, y = item.get("x"), item.get("y")
            if not name or x is None or y is None:
                continue
            try:
                x, y = float(x), float(y)
                # 百度网页接口给的是墨卡托 ×100
                if abs(x) > 1e8 or abs(y) > 1e8:
                    x, y = x / 100.0, y / 100.0
                lat, lon = baidu_mc_to_wgs84(x, y)
            except (TypeError, ValueError):
                continue
            results.append(
                {
                    "label": name,
                    "detail": item.get("addr") or "",
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "source": "baidu",
                }
            )
        return results

    def _nominatim(self, query: str, limit: int) -> list[dict]:
        """OpenStreetMap 的 Nominatim。免密钥，WGS-84。"""
        self._throttle("nominatim")
        params = urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "q": query,
                "limit": limit,
                "accept-language": "zh-CN",
            }
        )
        payload = self._get_json(f"{NOMINATIM_URL}?{params}")
        results = []
        for item in payload if isinstance(payload, list) else []:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            full = item.get("display_name") or ""
            results.append(
                {
                    "label": item.get("name") or full.split(",")[0] or query,
                    "detail": full,
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "source": "nominatim",
                }
            )
        return results

    def _photon(self, query: str, limit: int) -> list[dict]:
        """Photon（OSM 的另一套搜索服务）。免密钥，WGS-84。"""
        self._throttle("photon")
        params = urllib.parse.urlencode({"q": query, "limit": limit, "lang": "zh"})
        payload = self._get_json(f"{PHOTON_URL}?{params}")
        results = []
        for feature in (payload.get("features") or [])[:limit]:
            coords_ = feature.get("geometry") or {}
            position = coords_.get("coordinates") or []
            if len(position) < 2:
                continue
            try:
                lon, lat = float(position[0]), float(position[1])
            except (TypeError, ValueError):
                continue
            props = feature.get("properties") or {}
            label = props.get("name") or query
            detail = ", ".join(
                str(part)
                for part in (props.get("street"), props.get("district"), props.get("city"))
                if part
            )
            results.append(
                {
                    "label": label,
                    "detail": detail,
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "source": "photon",
                }
            )
        return results

    # -- 内部工具 ---------------------------------------------------------

    def _get_json(self, url: str, referer: str | None = None, user_agent: str | None = None):
        headers = {"User-Agent": user_agent or USER_AGENT, "Accept": "application/json"}
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8"))

    def _throttle(self, source: str) -> None:
        """同一个源两次请求之间至少隔 MIN_INTERVAL 秒。"""
        with self._lock:
            now = time.monotonic()
            last = self._last_call.get(source, 0.0)
            wait = MIN_INTERVAL - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_call[source] = now

    def _cache_get(self, key: tuple) -> list[dict] | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stamp, results = entry
            if time.monotonic() - stamp > CACHE_TTL:
                self._cache.pop(key, None)
                return None
            return results

    def _cache_put(self, key: tuple, results: list[dict]) -> None:
        with self._lock:
            if len(self._cache) >= CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.monotonic(), results)


def search(query: str, limit: int = 5, amap_key: str | None = None) -> list[dict]:
    """一次性查询（不保留缓存）。"""
    return Geocoder(amap_key=amap_key).search(query, limit)
