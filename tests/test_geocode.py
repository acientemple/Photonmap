import pytest

from photonmap import geocode


def test_baidu_mercator_converts_to_hangzhou():
    # 百度网页搜索「西湖」第一条（西湖风景区）返回的 x/y（BD-09 墨卡托，已除以 100）
    lat, lon = geocode.baidu_mc_to_wgs84(13372712.44, 3511297.99)
    assert lat == pytest.approx(30.25, abs=0.3)
    assert lon == pytest.approx(120.13, abs=0.5)


def test_engine_sources_amap_falls_back_to_baidu(monkeypatch):
    geocoder = geocode.Geocoder()
    order = []
    monkeypatch.setattr(geocoder, "_baidu_web", lambda q, n: order.append("baidu") or [])
    monkeypatch.setattr(geocoder, "_nominatim", lambda q, n: order.append("nominatim") or [])
    monkeypatch.setattr(geocoder, "_photon", lambda q, n: order.append("photon") or [])

    geocoder.search("西湖", engine="amap")
    assert order == ["baidu", "nominatim", "photon"]


def test_engine_sources_leaflet_uses_osm(monkeypatch):
    geocoder = geocode.Geocoder()
    order = []
    monkeypatch.setattr(geocoder, "_baidu_web", lambda q, n: order.append("baidu") or [])
    monkeypatch.setattr(geocoder, "_nominatim", lambda q, n: order.append("nominatim") or [])
    monkeypatch.setattr(geocoder, "_photon", lambda q, n: order.append("photon") or [])

    geocoder.search("西湖", engine="leaflet")
    assert order == ["nominatim", "photon"]


def test_amap_key_is_used_first_when_configured(monkeypatch):
    geocoder = geocode.Geocoder(amap_key="key")
    order = []
    monkeypatch.setattr(geocoder, "_amap", lambda q, n: order.append("amap") or [])
    monkeypatch.setattr(geocoder, "_baidu_web", lambda q, n: order.append("baidu") or [])
    monkeypatch.setattr(geocoder, "_nominatim", lambda q, n: order.append("nominatim") or [])
    monkeypatch.setattr(geocoder, "_photon", lambda q, n: order.append("photon") or [])

    geocoder.search("西湖", engine="amap")
    assert order == ["amap", "baidu", "nominatim", "photon"]


def test_cache_is_per_engine(monkeypatch):
    geocoder = geocode.Geocoder()
    hits = []

    def fake_baidu(q, n):
        hits.append("baidu")
        return [{"label": "百度", "detail": "", "lat": 30.0, "lon": 120.0, "source": "baidu"}]

    monkeypatch.setattr(geocoder, "_baidu_web", fake_baidu)
    monkeypatch.setattr(geocoder, "_nominatim", lambda q, n: [])
    monkeypatch.setattr(geocoder, "_photon", lambda q, n: [])

    first = geocoder.search("西湖", engine="amap")
    second = geocoder.search("西湖", engine="leaflet")
    assert first and first[0]["source"] == "baidu"
    assert second == []
    assert hits == ["baidu"]
