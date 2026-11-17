import json
import re
import zipfile
from pathlib import Path

import pytest

from photonmap.exif_reader import read_photo
from photonmap.exporters import (
    BaiduMapExporter,
    GeoJsonExporter,
    GoogleMapsExporter,
    KmlExporter,
    LeafletExporter,
)


def _photos(photo_dir: Path):
    return [p for p in (read_photo(f) for f in sorted(photo_dir.glob("*.jpg"))) if p]


def _embedded_photos(html: str) -> list[dict]:
    """从导出的 HTML 里取出内嵌的 PHOTOS 数据。"""
    match = re.search(r"(?:const|let) PHOTOS = (\[.*?\]);", html, re.S)
    assert match, "HTML 中没有内嵌 PHOTOS"
    return json.loads(match.group(1))


def test_kml_export_creates_kml_and_kmz(photo_dir, tmp_path):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    exporter = KmlExporter()
    kmz_path = exporter.export(photos, out)

    assert kmz_path.exists() and kmz_path.suffix == ".kmz"
    assert (out / "photos.kml").exists()

    with zipfile.ZipFile(kmz_path) as zf:
        names = zf.namelist()
        assert "doc.kml" in names
        assert any(n.startswith("files/") for n in names)
        kml = zf.read("doc.kml").decode("utf-8")
        assert "<Placemark>" in kml
        assert "31.230" in kml or "121.47" in kml


def test_kml_groups_by_date(photo_dir, tmp_path):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    KmlExporter(group_by_date=True).export(photos, out)
    kml = (out / "photos.kml").read_text(encoding="utf-8")
    assert "2024-05-01" in kml
    assert "2024-05-02" in kml


def test_geojson_export(photo_dir, tmp_path):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    path = GeoJsonExporter().export(photos, out)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 3
    feature = data["features"][0]
    assert feature["geometry"]["type"] == "Point"
    assert len(feature["geometry"]["coordinates"]) == 2
    assert feature["properties"]["name"].endswith(".jpg")


def test_geojson_embedded_thumbnail(photo_dir, tmp_path):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    path = GeoJsonExporter(embed_thumbnails=True).export(photos, out)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["features"][0]["properties"]["thumbnail"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize(
    "exporter_cls,kwargs",
    [
        (LeafletExporter, {}),
        (GoogleMapsExporter, {"api_key": "TEST-KEY"}),
        (BaiduMapExporter, {"ak": "TEST-AK"}),
    ],
)
def test_html_exporters(photo_dir, tmp_path, exporter_cls, kwargs):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    exporter = exporter_cls(**kwargs)
    path = exporter.export(photos, out)

    lat, lon = photos[0].coordinates(exporter.coord_system)
    html = path.read_text(encoding="utf-8")
    assert "<html" in html
    assert f"{round(lat, 7)}" in html or f"{round(lon, 7)}" in html
    assert (out / "files").exists()


@pytest.mark.parametrize(
    "exporter_cls,kwargs",
    [
        (LeafletExporter, {}),
        (GoogleMapsExporter, {"api_key": "TEST-KEY"}),
        (BaiduMapExporter, {"ak": "TEST-AK"}),
    ],
)
def test_html_exporters_embed_thumbnail_markers(photo_dir, tmp_path, exporter_cls, kwargs):
    """地图标记用照片缩略图，点击后能放大看真实相片并显示坐标与时间。"""
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    exporter = exporter_cls(**kwargs)
    path = exporter.export(photos, out)
    html = path.read_text(encoding="utf-8")

    rows = _embedded_photos(html)
    assert len(rows) == len(photos)
    for row in rows:
        assert row["marker"].startswith("files/markers/")
        assert row["full"].startswith("files/full/")
        assert row["thumb"].startswith("files/")
        assert row["taken_at"]
        assert row["original"].startswith("file:///")

    assert (out / "files" / "photo_map.js").exists()
    assert (out / "files" / "photo_map.css").exists()
    assert len(list((out / "files" / "markers").glob("*.jpg"))) == len(photos)
    assert len(list((out / "files" / "full").glob("*.jpg"))) == len(photos)

    # 侧边栏只显示缩略图和文件名，不显示坐标与时间
    assert "PhotoMap.renderList" in html
    assert "PhotoMap.Lightbox" in html
    # 按缩放聚合 + 胶卷视图
    assert "PhotoMap.ClusterView" in html
    assert "PhotoMap.Filmstrip" in html


def test_baidu_export_uses_bd09(photo_dir, tmp_path):
    photos = _photos(photo_dir)
    out = tmp_path / "out"
    path = BaiduMapExporter(ak="TEST-AK").export(photos, out)
    html = path.read_text(encoding="utf-8")

    rows = _embedded_photos(html)
    first = rows[0]
    # 地图坐标经过 BD-09 偏移，与原始 WGS-84 不同；wgs_lat 仍保留原始值
    assert first["lat"] != first["wgs_lat"]
    assert first["lon"] != first["wgs_lon"]
    assert first["wgs_lat"] == round(photos[0].lat, 7)


def test_photo_row_drops_non_finite_altitude(tmp_path):
    from photonmap.exporters.html_base import photo_row
    from photonmap.models import PhotoRecord

    record = PhotoRecord(path=tmp_path / "a.jpg", lat=1.0, lon=2.0, altitude=float("nan"))
    row = photo_row(record, tmp_path / "files", coord_system="wgs84", prefix="/files")
    assert row["altitude"] is None
    # allow_nan=False 才能证明浏览器那边 JSON.parse 不会炸
    json.dumps(row, allow_nan=False)


def test_index_entry_drops_non_finite_altitude(tmp_path):
    from photonmap import server as server_module
    from photonmap.models import PhotoRecord

    record = PhotoRecord(path=tmp_path / "a.jpg", lat=1.0, lon=2.0, altitude=float("inf"))
    entry = {"name": "x", "record": record, "stamp": (1.0, 2, 3.0), "pending": False}
    payload = server_module._entry_to_json(entry)
    json.dumps(payload, allow_nan=False)
    assert payload["record"]["altitude"] is None
