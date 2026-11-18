import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
from conftest import write_gps_photo, write_timed_photo

from photonmap import coords
from photonmap.locate import remove_sidecar, write_sidecar
from photonmap.server import (
    AMAP_TILES,
    GOOGLE_TILES,
    PhotoLibrary,
    asset_key,
    build_server,
)

TILES = [
    {
        "name": "测试底图",
        "url": "https://tile.example/{z}/{x}/{y}.png",
        "attribution": "test",
        "maxZoom": 18,
    }
]


def _library(photo_dir: Path, tmp_path: Path) -> PhotoLibrary:
    return PhotoLibrary(photo_dir, tmp_path / "files")


def test_library_keeps_only_photos_with_gps(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    assert library.refresh() is True

    rows = library.rows()["photos"]
    assert len(rows) == 3
    assert all(row["name"] != "no_gps.jpg" for row in rows)
    # 坐标直接来自 EXIF，WGS-84
    assert rows[0]["lat"] == pytest.approx(31.2304, abs=1e-4)
    assert rows[0]["wgs_lat"] == pytest.approx(31.2304, abs=1e-4)


def test_library_orders_by_taken_at(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    library.refresh()

    names = [row["name"] for row in library.rows()["photos"]]
    assert names == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]


def test_library_detects_new_and_removed_photos(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    library.refresh()
    first_version = library.version

    # 没有变化时不应更新版本号
    assert library.refresh() is False
    assert library.version == first_version

    write_gps_photo(photo_dir / "IMG_0004.jpg", 31.5, 121.9, datetime(2024, 5, 3, 8, 0, 0))
    assert library.refresh() is True
    assert len(library.rows()["photos"]) == 4
    assert library.version == first_version + 1

    (photo_dir / "IMG_0004.jpg").unlink()
    assert library.refresh() is True
    assert len(library.rows()["photos"]) == 3


def test_asset_key_is_stable_and_unique(photo_dir, tmp_path):
    a = asset_key(photo_dir / "IMG_0001.jpg")
    b = asset_key(photo_dir / "IMG_0002.jpg")
    assert a == asset_key(photo_dir / "IMG_0001.jpg")
    assert a != b


def test_derived_assets_use_stable_names(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    library.refresh()

    rows = library.rows()["photos"]
    for row in rows:
        assert row["marker"].startswith("/files/markers/")
        assert row["thumb"].startswith("/files/thumbs/")
        # 大图由服务端按需转码，不落盘
        assert row["full"].startswith("/files/full/")
        assert row["original"].startswith("api/original/")

    # 3 张已定位 + 1 张待确认，都会生成标记图
    markers = list((tmp_path / "files" / "markers").glob("*.jpg"))
    assert len(markers) == 4

    # 再扫描一次不应重新生成（文件名不变）
    names_before = sorted(p.name for p in markers)
    library.refresh()
    assert sorted(p.name for p in (tmp_path / "files" / "markers").glob("*.jpg")) == names_before


def test_library_splits_located_and_pending(photo_dir, tmp_path):
    """没有 GPS 的照片进待确认队列，并给出候选位置。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    library = _library(photo_dir, tmp_path)
    library.refresh()

    data = library.rows()
    assert len(data["photos"]) == 3
    assert len(data["pending"]) == 1

    waiting = data["pending"][0]
    assert waiting["name"] == "no_gps.jpg"
    assert waiting["status"] == "pending"
    # 目标照片夹在两张已定位照片之间 → 首选时间插值出来的点
    assert waiting["candidate"]["kind"] == "interpolated"
    assert "IMG_0001.jpg" in waiting["candidate"]["source_name"]
    # 备选里是两张邻近的照片本身
    sources = [c["source_name"] for c in waiting["candidates"]]
    assert "IMG_0001.jpg" in sources
    assert "IMG_0002.jpg" in sources


def test_pending_without_time_has_no_candidate(photo_dir, tmp_path):
    """连拍摄时间都没有的，就给不出候选位置。"""
    library = _library(photo_dir, tmp_path)
    library.refresh()

    waiting = library.rows()["pending"][0]
    assert waiting["candidate"] is None
    assert waiting["candidates"] == []


def test_pending_becomes_located_after_sidecar_written(photo_dir, tmp_path):
    """写入旁车文件后，这张照片从待确认变成已定位。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    library = _library(photo_dir, tmp_path)
    library.refresh()
    assert len(library.rows()["pending"]) == 1

    target = photo_dir / "no_gps.jpg"
    write_sidecar(target, 30.5, 120.2)
    library.refresh()

    data = library.rows()
    assert len(data["pending"]) == 0
    row = next(r for r in data["photos"] if r["name"] == "no_gps.jpg")
    assert row["lat"] == pytest.approx(30.5, abs=1e-4)
    assert row["lon"] == pytest.approx(120.2, abs=1e-4)


def test_library_falls_back_to_default_location(photo_dir, tmp_path):
    """没有 GPS 的照片用兜底坐标顶上。"""
    library = PhotoLibrary(photo_dir, tmp_path / "files", default_location=(31.0, 121.0))
    library.refresh()

    rows = {row["name"]: row for row in library.rows()["photos"]}
    assert "no_gps.jpg" in rows
    assert rows["no_gps.jpg"]["lat"] == pytest.approx(31.0, abs=1e-4)


def test_library_uses_per_file_location_table(photo_dir, tmp_path):
    """--locations 逐张指定的坐标优先于默认坐标。"""
    library = PhotoLibrary(
        photo_dir,
        tmp_path / "files",
        default_location=(31.0, 121.0),
        location_table={"no_gps.jpg": (22.5, 114.0)},
    )
    library.refresh()

    rows = {row["name"]: row for row in library.rows()["photos"]}
    assert rows["no_gps.jpg"]["lat"] == pytest.approx(22.5, abs=1e-4)
    assert rows["IMG_0001.jpg"]["lat"] == pytest.approx(31.2304, abs=1e-4)


def test_server_serves_page_and_api(photo_dir, tmp_path):
    server = build_server(
        photo_dir,
        TILES,
        workdir=tmp_path / "work",
        port=0,
        poll_ms=500,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"

        with urllib.request.urlopen(base + "/") as res:
            shell = res.read().decode("utf-8")
        assert "照片地图" in shell
        assert "id=\"tabs\"" in shell
        assert "/map/" in shell

        with urllib.request.urlopen(base + "/map/leaflet") as res:
            page = res.read().decode("utf-8")
        assert "tile.example" in page
        assert "/files/vendor/leaflet.js" in page

        with urllib.request.urlopen(base + "/api/photos") as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["photos"]) == 3
        assert payload["version"] >= 1

        row = payload["photos"][0]
        with urllib.request.urlopen(base + row["marker"]) as res:
            assert res.status == 200
            assert res.headers["Content-Type"] == "image/jpeg"

        with urllib.request.urlopen(base + row["full"]) as res:
            assert res.status == 200
            assert res.headers["Content-Type"] == "image/jpeg"
            assert res.read()[:2] == b"\xff\xd8"  # JPEG magic
    finally:
        server.shutdown()
        server.server_close()


def _post_json(base: str, path: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request)


def _open_token(base: str, key: str, session: str) -> str:
    """模拟用户打开确认浮层：服务端这时才发放一次性令牌。"""
    with _post_json(base, "/api/locate/open", {"key": key, "session": session}) as res:
        return json.loads(res.read().decode("utf-8"))["token"]


def test_locate_api_writes_position(photo_dir, tmp_path):
    """确认位置后写入旁车文件，照片变成已定位。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = next(p["key"] for p in server.library.rows()["pending"])
        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate", {
            "key": key, "lat": 30.5, "lon": 120.2,
            "session": server.session, "token": token,
        }) as res:
            assert res.status == 200

        rows = server.library.rows()["photos"]
        row = next(r for r in rows if r["key"] == key)
        assert row["lat"] == pytest.approx(30.5, abs=1e-4)
        assert not server.library.rows()["pending"]
    finally:
        server.shutdown()
        server.server_close()


def test_locate_api_rejects_missing_token(photo_dir, tmp_path):
    """没打开过浮层的页面提交坐标 → 没有令牌，拒掉且不写盘。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = next(p["key"] for p in server.library.rows()["pending"])
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate", {
                "key": key, "lat": 30.5, "lon": 120.2, "session": server.session,
            })
        assert excinfo.value.code == 409
        assert server.library.rows()["pending"]
    finally:
        server.shutdown()
        server.server_close()


def test_locate_token_is_one_time(photo_dir, tmp_path):
    """令牌用过一次就作废，重复提交要被拒掉。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = next(p["key"] for p in server.library.rows()["pending"])
        token = _open_token(base, key, server.session)
        payload = {"key": key, "lat": 30.5, "lon": 120.2,
                   "session": server.session, "token": token}
        with _post_json(base, "/api/locate", payload):
            pass
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate", payload)
        assert excinfo.value.code == 409
    finally:
        server.shutdown()
        server.server_close()


def test_locate_open_rejects_stale_session(photo_dir, tmp_path):
    """旧页面连令牌都拿不到。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = next(p["key"] for p in server.library.rows()["pending"])
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/open", {"key": key, "session": "旧会话"})
        assert excinfo.value.code == 409
    finally:
        server.shutdown()
        server.server_close()


def test_locate_api_rejects_stale_session(photo_dir, tmp_path):
    """上一次运行留下的旧页面带着旧会话标识来提交，要拒掉。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = next(p["key"] for p in server.library.rows()["pending"])
        body = json.dumps({"key": key, "lat": 30.5, "lon": 120.2,
                           "session": "旧会话"}).encode("utf-8")
        request = urllib.request.Request(
            base + "/api/locate", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 409
        # 没有写入
        assert server.library.rows()["pending"]
    finally:
        server.shutdown()
        server.server_close()


def test_server_rejects_path_traversal(photo_dir, tmp_path):
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + "/files/../../../secret.txt")
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def _shell_engines(base: str) -> dict:
    """从壳页面里解析出引擎列表。"""
    with urllib.request.urlopen(base + "/") as res:
        shell = res.read().decode("utf-8")
    marker = "const ENGINES = "
    start = shell.index(marker) + len(marker)
    return {e["id"]: e for e in json.loads(shell[start:shell.index(";", start)])}


def test_google_and_amap_need_no_key(photo_dir, tmp_path):
    """Google 与高德走官方公开瓦片，没有密钥也能用。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        engines = _shell_engines(base)
        assert engines["google"]["available"] is True
        assert engines["amap"]["available"] is True
        # 百度仍然要自己的 AK
        assert engines["baidu"]["available"] is False
        assert engines["baidu"]["reason"] == "缺少密钥"
    finally:
        server.shutdown()
        server.server_close()


def test_baidu_available_with_ak(photo_dir, tmp_path):
    """给了百度 AK 就该能用。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0,
                          baidu_ak="测试AK")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        engines = _shell_engines(base)
        assert engines["baidu"]["available"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_google_engine_serves_free_tiles(photo_dir, tmp_path):
    """Google 引擎用官方瓦片渲染，不需要 api_key。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/map/google") as res:
            page = res.read().decode("utf-8")
        assert "mt1.google.com" in page
        assert "lyrs=m" in page and "lyrs=s" in page
    finally:
        server.shutdown()
        server.server_close()


def test_amap_engine_serves_free_tiles(photo_dir, tmp_path):
    """高德引擎用公开瓦片渲染。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/map/amap") as res:
            page = res.read().decode("utf-8")
        assert "is.autonavi.com" in page
        assert '"subdomains"' in page
    finally:
        server.shutdown()
        server.server_close()


def test_google_engine_uses_gcj02(photo_dir, tmp_path):
    """Google / 高德在国内是 GCJ-02 基准，接口要按 gcj02 返回坐标。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/photos?system=gcj02") as res:
            payload = json.loads(res.read().decode("utf-8"))
        with urllib.request.urlopen(base + "/api/photos") as res:
            wgs = json.loads(res.read().decode("utf-8"))
        shifted = payload["photos"][0]
        origin = wgs["photos"][0]
        assert shifted["lat"] != origin["lat"] or shifted["lon"] != origin["lon"]
    finally:
        server.shutdown()
        server.server_close()


def test_rows_convert_from_library_system(photo_dir, tmp_path):
    """库基准不是 wgs84 时，换算只能做一次。

    行里的坐标已经是库基准了，再换算就得带上 source，
    否则偏移过的值会被当成 WGS-84 又偏一次。
    """
    library = PhotoLibrary(photo_dir, tmp_path / "files", coord_system="gcj02")
    library.refresh()

    row = library.rows("bd09")["photos"][0]
    origin = library.rows("wgs84")["photos"][0]
    expected_lat, expected_lon = coords.convert(origin["wgs_lat"], origin["wgs_lon"], "bd09")
    assert row["lat"] == pytest.approx(expected_lat, abs=1e-6)
    assert row["lon"] == pytest.approx(expected_lon, abs=1e-6)


def test_pending_candidates_follow_library_system(photo_dir, tmp_path):
    """待确认照片的候选位置要和照片坐标用同一个基准。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 45, 0))
    library = PhotoLibrary(photo_dir, tmp_path / "files", coord_system="gcj02")
    library.refresh()

    candidate = library.rows("gcj02")["pending"][0]["candidate"]
    assert candidate is not None
    # 候选夹在两张已定位照片之间，WGS-84 下就是两者的中点
    wgs_lat = (31.2304 + 31.2400) / 2
    wgs_lon = (121.4737 + 121.4800) / 2
    expected_lat, expected_lon = coords.convert(wgs_lat, wgs_lon, "gcj02")
    assert candidate["lat"] == pytest.approx(expected_lat, abs=1e-6)
    assert candidate["lon"] == pytest.approx(expected_lon, abs=1e-6)


def test_sidecar_overrides_exif_gps(photo_dir, tmp_path):
    """人工标定的旁车位置要盖过相机 GPS。

    否则「随时改一下位置」对已经有 GPS 的照片根本不生效——旁车被忽略了。
    """
    library = PhotoLibrary(photo_dir, tmp_path / "files")
    library.refresh()
    key = asset_key(photo_dir / "IMG_0001.jpg")
    before = next(row for row in library.rows()["photos"] if row["key"] == key)
    assert before["lat"] == pytest.approx(31.2304, abs=1e-4)
    assert before["source"] == "exif"

    write_sidecar(photo_dir / "IMG_0001.jpg", 22.5, 114.0)
    library.refresh()
    after = next(row for row in library.rows()["photos"] if row["key"] == key)
    assert after["lat"] == pytest.approx(22.5, abs=1e-4)
    assert after["source"] == "sidecar"


def test_engine_tiles_declare_per_layer_system():
    """每片底图都要声明自己的坐标基准——Google 三层并不一致。"""
    google = {tile["name"]: tile for tile in GOOGLE_TILES}
    assert google["路网"]["coord_system"] == "gcj02"
    assert google["卫星"]["coord_system"] == "wgs84"
    assert google["混合"]["coord_system"] == "wgs84"

    amap = {tile["name"]: tile for tile in AMAP_TILES}
    assert set(amap) == {"路网", "卫星", "混合"}
    assert all(tile["coord_system"] == "gcj02" for tile in AMAP_TILES)
    # 混合层 = 卫星影像 + 一层透明的路网标注
    assert amap["混合"]["overlay"]["url"].startswith("https://webst0")
    assert "style=8" in amap["混合"]["overlay"]["url"]


def test_amap_engine_page_has_hybrid_overlay(photo_dir, tmp_path):
    """高德引擎的页面要带上混合层与逐层基准。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/map/amap") as res:
            page = res.read().decode("utf-8")
        assert '"overlay"' in page
        assert '"coord_system": "gcj02"' in page
        assert "baselayerchange" in page
        assert "layerSystems" in page
    finally:
        server.shutdown()
        server.server_close()
# ---------------------------------------------------------------- 时间簇


def test_groups_endpoint_lists_clusters(photo_dir, tmp_path):
    """簇列表给出每簇的照片数、待确认数与簇级推断。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            payload = json.loads(res.read().decode("utf-8"))
        groups = payload["groups"]
        # 相隔一天的两张照片分成两簇
        assert len(groups) == 2
        group = next(g for g in groups if g["pending_count"] == 1)
        assert group["count"] == 3
        assert group["pending_count"] == 1
        assert len(group["members"]) == 3
        assert len(group["pending_members"]) == 1
        assert group["thumb"].startswith("/files/thumbs/")
        # 簇里有已定位照片，待确认的照片夹在中间 → 插值推断
        assert group["guess"]["kind"] == "interpolated"
        assert group["guess"]["confidence"] == "很可能"
    finally:
        server.shutdown()
        server.server_close()


def test_groups_endpoint_converts_guess_to_requested_system(photo_dir, tmp_path):
    """簇级推断的坐标按请求的基准返回。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups?system=gcj02") as res:
            shifted = json.loads(res.read().decode("utf-8"))["groups"][0]["guess"]
        with urllib.request.urlopen(base + "/api/groups?system=wgs84") as res:
            origin = json.loads(res.read().decode("utf-8"))["groups"][0]["guess"]
        expected_lat, expected_lon = coords.convert(origin["lat"], origin["lon"], "gcj02")
        assert shifted["lat"] == pytest.approx(expected_lat, abs=1e-6)
        assert shifted["lon"] == pytest.approx(expected_lon, abs=1e-6)
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 批量定位


def _group_tokens(base: str, group_id: str, session: str) -> dict:
    """按簇签发一次性令牌：每张待确认照片一个。"""
    with _post_json(base, "/api/locate/open", {"group": group_id, "session": session}) as res:
        return json.loads(res.read().decode("utf-8"))["tokens"]


def test_locate_open_group_issues_one_token_per_photo(photo_dir, tmp_path):
    """按簇打开浮层时，簇里每张待确认照片各拿到一个令牌。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    write_timed_photo(photo_dir / "no_gps2.jpg", datetime(2024, 5, 1, 12, 50, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        tokens = _group_tokens(base, group["id"], server.session)
        assert set(tokens) == set(group["pending_members"])
        assert all(token for token in tokens.values())
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_assign_writes_each_photo(photo_dir, tmp_path):
    """按各自坐标批量写入，每张照片落到自己那个点。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    write_timed_photo(photo_dir / "no_gps2.jpg", datetime(2024, 5, 1, 12, 50, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        tokens = _group_tokens(base, group["id"], server.session)
        items = [
            {"key": key, "lat": 30.1 + i, "lon": 120.2, "token": token}
            for i, (key, token) in enumerate(sorted(tokens.items()))
        ]
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "assign", "items": items,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["ok"] is True
        assert len(payload["written"]) == 2
        assert not payload["errors"]
        assert not server.library.rows()["pending"]
        for row in server.library.rows()["photos"]:
            if row["name"].startswith("no_gps"):
                assert row["source"] == "sidecar"
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_same_applies_one_point(photo_dir, tmp_path):
    """整簇钉在同一个点上。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    write_timed_photo(photo_dir / "no_gps2.jpg", datetime(2024, 5, 1, 12, 50, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        tokens = _group_tokens(base, group["id"], server.session)
        items = [{"key": key, "token": token} for key, token in sorted(tokens.items())]
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "same",
            "target": {"lat": 30.5, "lon": 120.5}, "items": items,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["written"]) == 2
        for row in server.library.rows()["photos"]:
            if row["name"].startswith("no_gps"):
                assert row["lat"] == pytest.approx(30.5, abs=1e-4)
                assert row["lon"] == pytest.approx(120.5, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_shift_keeps_relative_positions(photo_dir, tmp_path):
    """整簇平移：组内照片的相对位置保持不变。"""
    write_sidecar(photo_dir / "IMG_0001.jpg", 30.0, 120.0)
    write_sidecar(photo_dir / "IMG_0002.jpg", 30.0, 120.1)
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        anchor = asset_key(photo_dir / "IMG_0001.jpg")
        other = asset_key(photo_dir / "IMG_0002.jpg")
        tokens = {
            anchor: _open_token(base, anchor, server.session),
            other: _open_token(base, other, server.session),
        }
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "shift",
            "target": {"lat": 31.0, "lon": 121.0}, "anchor_key": anchor,
            "items": [{"key": key, "token": token} for key, token in tokens.items()],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["written"]) == 2
        rows = {row["key"]: row for row in server.library.rows()["photos"]}
        # 锚点被移到目标点
        assert rows[anchor]["lat"] == pytest.approx(31.0, abs=1e-4)
        assert rows[anchor]["lon"] == pytest.approx(121.0, abs=1e-4)
        # 另一张跟着平移，相对位置（经度差）不变
        assert rows[other]["lat"] == pytest.approx(31.0, abs=1e-4)
        assert rows[other]["lon"] == pytest.approx(121.1, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_rejects_missing_token(photo_dir, tmp_path):
    """缺令牌 → 整批拒掉，一张都不写。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/batch", {
                "session": server.session, "system": "wgs84", "mode": "assign",
                "items": [{"key": key, "lat": 30.0, "lon": 120.0, "token": "假令牌"}],
            })
        assert excinfo.value.code == 409
        assert server.library.rows()["pending"]
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_rejects_reused_token(photo_dir, tmp_path):
    """令牌用过一次就作废，批量提交也要拒掉。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        token = _open_token(base, key, server.session)
        payload = {
            "session": server.session, "system": "wgs84", "mode": "assign",
            "items": [{"key": key, "lat": 30.0, "lon": 120.0, "token": token}],
        }
        with _post_json(base, "/api/locate/batch", payload):
            pass
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/batch", payload)
        assert excinfo.value.code == 409
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_converts_from_requested_system(photo_dir, tmp_path):
    """请求里的坐标按 system 换算回 WGS-84 再落盘。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        token = _open_token(base, key, server.session)
        gcj_lat, gcj_lon = coords.convert(30.0, 120.0, "gcj02")
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "gcj02", "mode": "assign",
            "items": [{"key": key, "lat": gcj_lat, "lon": gcj_lon, "token": token}],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        written = payload["written"][0]
        assert written["lat"] == pytest.approx(30.0, abs=1e-4)
        assert written["lon"] == pytest.approx(120.0, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 清除定位


def test_locate_clear_removes_sidecar(photo_dir, tmp_path):
    """清除定位后，照片回到相机 GPS 上（没有 GPS 的照片回到待确认）。"""
    write_sidecar(photo_dir / "IMG_0001.jpg", 30.5, 120.5)
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = asset_key(photo_dir / "IMG_0001.jpg")
        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate/clear", {
            "session": server.session, "key": key, "token": token,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["ok"] is True
        row = next(r for r in server.library.rows()["photos"] if r["key"] == key)
        assert row["lat"] == pytest.approx(31.2304, abs=1e-4)
        assert not (photo_dir / "IMG_0001.jpg.photonmap.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_locate_clear_restores_exif_from_backup(photo_dir, tmp_path):
    """改写过 EXIF 的照片清除定位时，要从备份还原原文件。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0, write_exif=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = asset_key(photo_dir / "IMG_0001.jpg")
        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate", {
            "key": key, "lat": 30.5, "lon": 120.2,
            "session": server.session, "token": token,
        }) as res:
            assert res.status == 200
        # EXIF 里已经是人工标定的坐标
        row = next(r for r in server.library.rows()["photos"] if r["key"] == key)
        assert row["lat"] == pytest.approx(30.5, abs=1e-4)

        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate/clear", {
            "session": server.session, "key": key, "token": token,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["exif_restored"] is True
        row = next(r for r in server.library.rows()["photos"] if r["key"] == key)
        assert row["lat"] == pytest.approx(31.2304, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_clear_rejects_missing_token(photo_dir, tmp_path):
    """没令牌不许清除。"""
    write_sidecar(photo_dir / "IMG_0001.jpg", 30.5, 120.5)
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        key = asset_key(photo_dir / "IMG_0001.jpg")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/clear", {
                "session": server.session, "key": key, "token": "假令牌",
            })
        assert excinfo.value.code == 409
        assert (photo_dir / "IMG_0001.jpg.photonmap.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_shift_uses_candidate_for_pending_photos(photo_dir, tmp_path):
    """整簇平移时，待确认照片用请求里带的候选位置当平移起点。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        anchor = asset_key(photo_dir / "IMG_0001.jpg")
        pending = asset_key(photo_dir / "no_gps.jpg")
        tokens = {
            anchor: _open_token(base, anchor, server.session),
            pending: _open_token(base, pending, server.session),
        }
        # 待确认照片的候选位置（服务端给的是库基准，这里直接用 WGS-84）
        candidate = (31.2352, 121.4768)
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "shift",
            "target": {"lat": 32.0, "lon": 122.0}, "anchor_key": anchor,
            "items": [
                {"key": anchor, "token": tokens[anchor]},
                {"key": pending, "lat": candidate[0], "lon": candidate[1],
                 "token": tokens[pending]},
            ],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["written"]) == 2
        rows = {row["key"]: row for row in server.library.rows()["photos"]}
        # 锚点平移到目标点
        assert rows[anchor]["lat"] == pytest.approx(32.0, abs=1e-4)
        # 位移 = 目标点 − 锚点当前坐标；待确认照片按候选位置加上同样的位移
        delta_lat = 32.0 - 31.2304
        delta_lon = 122.0 - 121.4737
        assert rows[pending]["lat"] == pytest.approx(candidate[0] + delta_lat, abs=1e-4)
        assert rows[pending]["lon"] == pytest.approx(candidate[1] + delta_lon, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_open_members_issues_tokens(photo_dir, tmp_path):
    """按成员列表签发令牌：整簇平移时簇里的照片既有已定位也有待确认。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        keys = [asset_key(photo_dir / "IMG_0001.jpg"), asset_key(photo_dir / "no_gps.jpg")]
        with _post_json(base, "/api/locate/open", {"members": keys, "session": server.session}) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert set(payload["tokens"]) == set(keys)

        # 不存在的照片不签发
        with _post_json(base, "/api/locate/open", {
            "members": keys + ["不存在的照片"], "session": server.session,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert "不存在的照片" not in payload["tokens"]
        assert set(payload["tokens"]) == set(keys)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_keeps_going_when_one_write_fails(photo_dir, tmp_path, monkeypatch):
    """一张写盘失败不能把整批打断：已成功的要如实报出来，失败的进 errors。"""
    import photonmap.server as server_module

    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    write_timed_photo(photo_dir / "no_gps2.jpg", datetime(2024, 5, 1, 12, 50, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)

    real_write = server_module.write_sidecar
    bad_key = asset_key(photo_dir / "no_gps2.jpg")

    def flaky(source, lat, lon, *args, **kwargs):
        if str(source).endswith("no_gps2.jpg"):
            raise OSError("文件被占住了")
        return real_write(source, lat, lon, *args, **kwargs)

    monkeypatch.setattr(server_module, "write_sidecar", flaky)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        tokens = _group_tokens(base, group["id"], server.session)
        items = [
            {"key": key, "lat": 30.0, "lon": 120.0, "token": token}
            for key, token in tokens.items()
        ]
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "assign", "items": items,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["written"]) == 1
        assert len(payload["errors"]) == 1
        assert payload["errors"][0]["key"] == bad_key
        # 失败的那张没有写进去
        assert not (photo_dir / "no_gps2.jpg.photonmap.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_rejects_out_of_range_coordinates(photo_dir, tmp_path):
    """坐标越界要当场拒掉：写进旁车的话，读的时候会被当成没有位置。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "assign",
            "items": [{"key": key, "lat": 999.0, "lon": 120.0, "token": token}],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["written"] == []
        assert len(payload["errors"]) == 1
        assert not (photo_dir / "no_gps.jpg.photonmap.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_rejects_duplicate_keys(photo_dir, tmp_path):
    """同一张照片出现两次要拒掉：一个令牌不能写两遍。"""
    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        token = _open_token(base, key, server.session)
        item = {"key": key, "lat": 30.0, "lon": 120.0, "token": token}
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/batch", {
                "session": server.session, "system": "wgs84", "mode": "assign",
                "items": [item, dict(item)],
            })
        assert excinfo.value.code == 400
        assert not (photo_dir / "no_gps.jpg.photonmap.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_consumes_tokens_before_writing(photo_dir, tmp_path):
    """写盘失败的项，令牌也要作废——不能 5 分钟内被拿来重复提交。"""
    import photonmap.server as server_module

    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)

    def boom(source, lat, lon, *args, **kwargs):
        raise OSError("写不了")

    original = server_module.write_sidecar
    server_module.write_sidecar = boom
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/groups") as res:
            group = json.loads(res.read().decode("utf-8"))["groups"][0]
        key = group["pending_members"][0]
        token = _open_token(base, key, server.session)
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "assign",
            "items": [{"key": key, "lat": 30.0, "lon": 120.0, "token": token}],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["written"] == []
        # 令牌已经作废，再来一次要 409
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/locate/batch", {
                "session": server.session, "system": "wgs84", "mode": "assign",
                "items": [{"key": key, "lat": 30.0, "lon": 120.0, "token": token}],
            })
        assert excinfo.value.code == 409
    finally:
        server_module.write_sidecar = original
        server.shutdown()
        server.server_close()


def test_refresh_reports_progress(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    seen = []
    library.refresh(progress=lambda done, total: seen.append((done, total)))
    assert seen and seen[-1] == (4, 4)
    assert all(done <= total for done, total in seen)


def test_marker_is_generated_from_thumbnail(photo_dir, tmp_path, monkeypatch):
    import photonmap.server as server_module

    sources = []
    real = server_module.make_square_thumbnail

    def spy(source, destination, *args, **kwargs):
        sources.append(Path(source))
        return real(source, destination, *args, **kwargs)

    monkeypatch.setattr(server_module, "make_square_thumbnail", spy)
    library = _library(photo_dir, tmp_path)
    library.refresh()

    assert sources
    assert all(source.parent.name == "thumbs" for source in sources)


def test_index_cache_reused_on_second_open(photo_dir, tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    first = PhotoLibrary(photo_dir, workdir / "files")
    assert first.refresh() is True
    assert (workdir / "index.json").exists()

    calls = []
    real = PhotoLibrary._read_record

    def spy(self, path):
        calls.append(path)
        return real(self, path)

    monkeypatch.setattr(PhotoLibrary, "_read_record", spy)
    second = PhotoLibrary(photo_dir, workdir / "files")
    assert second.refresh() is True
    assert calls == []
    rows = second.rows()
    assert len(rows["photos"]) == 3
    assert len(rows["pending"]) == 1


def test_index_cache_invalidated_when_file_changes(photo_dir, tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    PhotoLibrary(photo_dir, workdir / "files").refresh()

    target = Path(photo_dir) / "IMG_0001.jpg"
    write_gps_photo(target, 30.0, 120.0, datetime(2024, 5, 1, 12, 30, 0))
    stat = target.stat()
    os.utime(target, (stat.st_atime, stat.st_mtime + 10))

    calls = []
    real = PhotoLibrary._read_record

    def spy(self, path):
        calls.append(Path(path).name)
        return real(self, path)

    monkeypatch.setattr(PhotoLibrary, "_read_record", spy)
    second = PhotoLibrary(photo_dir, workdir / "files")
    second.refresh()
    assert calls == ["IMG_0001.jpg"]
    rows = {row["name"]: row for row in second.rows()["photos"]}
    assert rows["IMG_0001.jpg"]["lat"] == pytest.approx(30.0, abs=1e-4)


def test_index_cache_ignores_corrupt_file(photo_dir, tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True)
    (workdir / "index.json").write_text("{not json", encoding="utf-8")
    library = PhotoLibrary(photo_dir, workdir / "files")
    assert library.refresh() is True
    assert len(library.rows()["photos"]) == 3


def test_index_cache_regenerates_missing_thumbnail(photo_dir, tmp_path):
    workdir = tmp_path / "work"
    PhotoLibrary(photo_dir, workdir / "files").refresh()

    thumbs = sorted((workdir / "files" / "thumbs").iterdir())
    victim = thumbs[0]
    deleted_name = victim.name
    victim.unlink()

    second = PhotoLibrary(photo_dir, workdir / "files")
    second.refresh()
    assert (workdir / "files" / "thumbs" / deleted_name).exists()


def test_apply_sidecars_updates_without_rescan(photo_dir, tmp_path, monkeypatch):
    library = _library(photo_dir, tmp_path)
    library.refresh()
    target = Path(photo_dir) / "no_gps.jpg"
    write_sidecar(target, 30.0, 120.0)

    import photonmap.server as server_module

    def boom(*args, **kwargs):
        raise AssertionError("不应该重新遍历目录")

    monkeypatch.setattr(server_module, "scan_directory", boom)

    assert library.apply_sidecars([target]) == 1
    rows = library.rows()
    assert rows["pending"] == []
    located = {row["name"]: row for row in rows["photos"]}
    assert located["no_gps.jpg"]["lat"] == pytest.approx(30.0, abs=1e-6)
    assert located["no_gps.jpg"]["lon"] == pytest.approx(120.0, abs=1e-6)


def test_apply_sidecars_after_clear_returns_to_pending(photo_dir, tmp_path):
    library = _library(photo_dir, tmp_path)
    library.refresh()
    target = Path(photo_dir) / "no_gps.jpg"

    write_sidecar(target, 30.0, 120.0)
    assert library.apply_sidecars([target]) == 1
    assert library.rows()["pending"] == []

    remove_sidecar(target)
    assert library.apply_sidecars([target]) == 1
    assert [row["name"] for row in library.rows()["pending"]] == ["no_gps.jpg"]


def test_geocode_endpoint_passes_selected_engine(photo_dir, tmp_path):
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    calls = []

    def fake_search(query, limit=5, engine=None):
        calls.append((query, engine))
        return [{"label": "西湖", "detail": "", "lat": 30.25, "lon": 120.15, "source": "baidu"}]

    server.geocoder.search = fake_search
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/geocode?q=%E8%A5%BF%E6%B9%96&engine=amap") as res:
            data = json.loads(res.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["results"][0]["label"] == "西湖"
        assert calls == [("西湖", "amap")]
    finally:
        server.shutdown()
        server.server_close()


def test_locate_open_issues_tokens_for_selected_members(photo_dir, tmp_path):
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        keys = [row["key"] for row in server.library.rows()["photos"]][:2]
        with _post_json(base, "/api/locate/open", {
            "members": keys, "session": server.session,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert set(payload["tokens"]) == set(keys)
    finally:
        server.shutdown()
        server.server_close()


def test_locate_batch_same_moves_selected_members(photo_dir, tmp_path):
    """胶卷里多选的任意几张照片一起钉到同一个点（不依赖时间簇）。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        keys = [row["key"] for row in server.library.rows()["photos"]][:2]
        with _post_json(base, "/api/locate/open", {
            "members": keys, "session": server.session,
        }) as res:
            tokens = json.loads(res.read().decode("utf-8"))["tokens"]
        items = [{"key": key, "token": tokens[key]} for key in keys]
        with _post_json(base, "/api/locate/batch", {
            "session": server.session, "system": "wgs84", "mode": "same",
            "target": {"lat": 30.5, "lon": 120.5}, "items": items,
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert len(payload["written"]) == 2
        rows = {row["key"]: row for row in server.library.rows()["photos"]}
        for key in keys:
            assert rows[key]["lat"] == pytest.approx(30.5, abs=1e-4)
            assert rows[key]["lon"] == pytest.approx(120.5, abs=1e-4)
    finally:
        server.shutdown()
        server.server_close()


def test_watcher_pauses_when_scan_is_slower_than_interval():
    """大目录一次扫描比轮询间隔还久：扫完要歇一下，不能不间断扫盘。"""
    from photonmap.server import Watcher

    watcher = Watcher(library=None, interval=0.5)
    watcher.interval = 0.05  # __init__ 会把间隔夹到 0.5 以上，测试里直接改小
    started = time.monotonic()
    watcher._pause(0.12)
    spent = time.monotonic() - started
    assert spent >= 0.09
    assert spent < 0.4

    started = time.monotonic()
    watcher._pause(0.01)
    assert time.monotonic() - started < 0.05


def test_photos_payload_is_gzipped_when_client_accepts(photo_dir, tmp_path):
    """照片列表动辄几 MB，压缩后只有几百 KB。"""
    import gzip

    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(base + "/api/photos", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(request) as res:
            assert res.headers.get("Content-Encoding") == "gzip"
            body = gzip.decompress(res.read())
        data = json.loads(body.decode("utf-8"))
        assert len(data["photos"]) == 3
    finally:
        server.shutdown()
        server.server_close()


def test_full_cache_evicts_by_total_bytes():
    from photonmap.server import _FullCache

    cache = _FullCache(maxsize=10, max_bytes=25)
    cache.put("a", b"x" * 10)
    cache.put("b", b"x" * 10)
    cache.put("c", b"x" * 10)  # 30 字节超过预算，最老的 a 被淘汰
    assert cache.get("a") is None
    assert cache.get("b") == b"x" * 10
    assert cache.get("c") == b"x" * 10


def test_library_prepares_thumbnails_on_multiple_threads(photo_dir, tmp_path, monkeypatch):
    """首次扫描的解码/编码要并行，否则上百张的新目录要等几分钟。"""
    import threading
    import time as _time

    seen = set()
    lock = threading.Lock()
    real = PhotoLibrary._prepare

    def slow_prepare(self, record, name):
        with lock:
            seen.add(threading.current_thread().name)
        _time.sleep(0.05)
        return real(self, record, name)

    # 多准备几张照片才有并行的空间
    for index in range(10):
        write_gps_photo(
            photo_dir / f"extra{index}.jpg",
            31.0 + index * 0.001,
            121.0 + index * 0.001,
            datetime(2024, 5, 1, 14, index, 0),
        )

    monkeypatch.setattr(PhotoLibrary, "_prepare", slow_prepare)
    library = _library(photo_dir, tmp_path)
    library.refresh()

    assert len(seen) > 1


def test_groups_are_cached_by_version(photo_dir, tmp_path, monkeypatch):
    """分组推断要好几秒，同一个版本不该每次请求都重算。"""
    import photonmap.server as server_module

    calls = []
    real = server_module.build_groups

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(server_module, "build_groups", counting)
    library = PhotoLibrary(photo_dir, tmp_path / "files")
    library.refresh()
    first = library.groups()
    second = library.groups()
    assert first == second
    assert len(calls) == 1

    # 写了一张照片 → 版本号变了 → 重新推断
    target = Path(photo_dir) / "no_gps.jpg"
    write_sidecar(target, 30.0, 120.0)
    library.apply_sidecars([target])
    library.groups()
    assert len(calls) == 2


def test_images_are_not_gzipped(photo_dir, tmp_path):
    """JPEG 已经压过，再 gzip 一遍纯属浪费 CPU——一页几千张图时更明显。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        row = server.library.rows()["photos"][0]
        request = urllib.request.Request(base + row["marker"], headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(request) as res:
            assert res.headers.get("Content-Encoding") is None
            assert res.read()[:2] == b"\xff\xd8"  # JPEG 魔数
    finally:
        server.shutdown()
        server.server_close()


def test_groups_expose_folder(photo_dir, tmp_path):
    """分组带上照片所在文件夹，侧栏可以按「同一地点（相册）」合并。"""
    album = photo_dir / "albums" / "trip"
    write_gps_photo(album / "a.jpg", 31.0, 121.0, datetime(2024, 5, 1, 10, 0, 0))
    write_timed_photo(album / "b.jpg", datetime(2024, 5, 1, 10, 30, 0))
    library = _library(photo_dir, tmp_path)
    library.refresh()
    folders = {group["folder"].replace("\\", "/") for group in library.groups()}
    assert "albums/trip" in folders


def test_rejects_dns_rebinding_host(photo_dir, tmp_path):
    """Host 不是本机地址时一律 403（防 DNS rebinding）。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(base + "/api/photos", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_rejects_cross_origin_post(photo_dir, tmp_path):
    """跨站 POST 一律 403（Origin 不是本服务）。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            base + "/api/locate/open",
            data=json.dumps({"key": "x", "session": server.session}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_post_requires_json_content_type(photo_dir, tmp_path):
    """非 application/json 的请求体直接 415，挡掉跨站「简单请求」。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            base + "/api/locate/open",
            data=b"key=x",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 415
    finally:
        server.shutdown()
        server.server_close()


def test_ai_endpoint_requires_session(photo_dir, tmp_path):
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/ai/locate", {"key": "x"})
        assert excinfo.value.code == 409
    finally:
        server.shutdown()
        server.server_close()


def test_rejects_cross_site_fetch_without_origin(photo_dir, tmp_path):
    """Sec-Fetch-Site: cross-site 即使没有 Origin 也要拒。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            base + "/api/photos", headers={"Sec-Fetch-Site": "cross-site"}
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_exposed_server_allows_ip_host_but_not_domain(photo_dir, tmp_path):
    """--host 0.0.0.0 时局域网用 IP 能访问，域名（rebinding 用的）一律拒。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0, host="0.0.0.0")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        ok = urllib.request.Request(base + "/api/photos", headers={"Host": f"192.168.1.50:{port}"})
        with urllib.request.urlopen(ok) as res:
            assert res.status == 200

        bad = urllib.request.Request(base + "/api/photos", headers={"Host": f"evil.example:{port}"})
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(bad)
        assert excinfo.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_server_rejects_lan_ip_host(photo_dir, tmp_path):
    """只监听本机时，哪怕 Host 是 IP 字面量（局域网地址）也不认。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/photos",
            headers={"Host": f"192.168.1.50:{port}"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_bye_endpoint_flags_shutdown_and_new_request_clears_it(photo_dir, tmp_path):
    """页面关闭发 /api/bye；随后又有请求（刷新）就把关停信号清掉。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            base + "/api/bye?session=" + server.session, data=b"", method="POST"
        )
        with urllib.request.urlopen(request) as res:
            assert res.status == 204
        assert server.bye_at is not None

        with urllib.request.urlopen(base + "/api/photos") as res:
            assert res.status == 200
        assert server.bye_at is None
    finally:
        server.shutdown()
        server.server_close()


def test_bye_requires_session(photo_dir, tmp_path):
    """关停信号要带 session，否则别的站点能把本地服务关掉。"""
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                urllib.request.Request(base + "/api/bye", data=b"", method="POST")
            )
        assert excinfo.value.code == 403
        assert server.bye_at is None

        with urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/bye?session=" + server.session, data=b"", method="POST"
            )
        ) as res:
            assert res.status == 204
        assert server.bye_at is not None
    finally:
        server.shutdown()
        server.server_close()


def test_library_supports_multiple_roots(tmp_path):
    """一个库可以同时看多个照片目录，文件夹标签带根目录名。"""
    first = tmp_path / "album_a"
    second = tmp_path / "album_b"
    write_gps_photo(first / "a.jpg", 31.0, 121.0, datetime(2024, 5, 1, 10, 0, 0))
    write_gps_photo(second / "sub" / "b.jpg", 30.0, 120.0, datetime(2024, 5, 2, 10, 0, 0))
    write_timed_photo(second / "sub" / "c.jpg", datetime(2024, 5, 2, 10, 30, 0))

    library = PhotoLibrary([first, second], tmp_path / "files")
    assert library.refresh() is True
    rows = library.rows()
    assert sorted(row["name"] for row in rows["photos"]) == ["a.jpg", "b.jpg"]
    assert [row["name"] for row in rows["pending"]] == ["c.jpg"]
    folders = {group["folder"] for group in library.groups()}
    assert "album_a" in folders
    assert "album_b/sub" in folders  # 目录分隔符按平台


def test_folders_api_switches_roots(photo_dir, tmp_path):
    other = tmp_path / "other"
    write_gps_photo(other / "x.jpg", 30.0, 120.0, datetime(2024, 6, 1, 9, 0, 0))
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/folders") as res:
            data = json.loads(res.read().decode("utf-8"))
        assert data["roots"] == [str(photo_dir)]

        with _post_json(base, "/api/folders", {
            "session": server.session, "folders": [str(other)],
        }) as res:
            payload = json.loads(res.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["roots"] == [str(other)]

        with urllib.request.urlopen(base + "/api/photos") as res:
            rows = json.loads(res.read().decode("utf-8"))
        assert [row["name"] for row in rows["photos"]] == ["x.jpg"]
    finally:
        server.shutdown()
        server.server_close()


def test_folders_api_rejects_missing_dir(photo_dir, tmp_path):
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(base, "/api/folders", {
                "session": server.session, "folders": [str(tmp_path / "nope")],
            })
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_browse_lists_subdirectories(photo_dir, tmp_path):
    (photo_dir / "sub1").mkdir()
    (photo_dir / "sub2").mkdir()
    (photo_dir / "file.jpg").write_bytes(b"x")
    server = build_server(photo_dir, TILES, workdir=tmp_path / "work", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        url = base + "/api/browse?path=" + urllib.parse.quote(str(photo_dir))
        with urllib.request.urlopen(url) as res:
            data = json.loads(res.read().decode("utf-8"))
        names = [item["name"] for item in data["dirs"]]
        assert "sub1" in names and "sub2" in names
        assert "file.jpg" not in names
    finally:
        server.shutdown()
        server.server_close()
