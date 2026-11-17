import pytest

import json

from photonmap.cli import main


def test_scan_lists_photos(photo_dir, capsys):
    code = main(["scan", str(photo_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert "找到 3 张带 GPS 的照片" in out
    assert "IMG_0001.jpg" in out


def test_scan_in_gcj02(photo_dir, capsys):
    main(["scan", str(photo_dir), "--coord-system", "gcj02"])
    out = capsys.readouterr().out
    # GCJ-02 下上海坐标会偏移
    assert "31.2304" not in out


def test_export_kml_and_geojson(photo_dir, tmp_path, capsys):
    out_dir = tmp_path / "out"
    code = main(["export", str(photo_dir), "-o", str(out_dir), "-f", "kml", "-f", "geojson"])
    assert code == 0
    assert (out_dir / "photos.kml").exists()
    assert (out_dir / "photos.kmz").exists()
    assert (out_dir / "photos.geojson").exists()

    data = json.loads((out_dir / "photos.geojson").read_text(encoding="utf-8"))
    assert len(data["features"]) == 3


def test_export_default_format_is_kml(photo_dir, tmp_path):
    out_dir = tmp_path / "out"
    main(["export", str(photo_dir), "-o", str(out_dir)])
    assert (out_dir / "photos.kmz").exists()


def test_export_no_gps_photos(tmp_path, capsys):
    from PIL import Image

    empty = tmp_path / "empty"
    empty.mkdir()
    Image.new("RGB", (16, 16)).save(empty / "a.jpg")
    code = main(["export", str(empty), "-o", str(tmp_path / "out")])
    assert code == 1


def test_locate_set_writes_sidecar(photo_dir, capsys):
    """--set 给指定文件钉一个点。"""
    code = main([
        "locate", str(photo_dir),
        "--set", "30.1,120.2",
        "--files", "IMG_0001.jpg",
    ])
    assert code == 0
    from photonmap.locate import read_sidecar

    assert read_sidecar(photo_dir / "IMG_0001.jpg") == pytest.approx((30.1, 120.2))


def test_locate_set_all(photo_dir):
    """--all 给目录里所有照片钉一个点。"""
    code = main(["locate", str(photo_dir), "--set", "30.1,120.2", "--all"])
    assert code == 0
    from photonmap.locate import read_sidecar

    for name in ("IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"):
        assert read_sidecar(photo_dir / name) == pytest.approx((30.1, 120.2))


def test_locate_clear_removes_sidecar(photo_dir):
    """--clear 删掉旁车文件。"""
    main(["locate", str(photo_dir), "--set", "30.1,120.2", "--all"])
    code = main(["locate", str(photo_dir), "--clear", "--files", "IMG_0001.jpg"])
    assert code == 0
    from photonmap.locate import read_sidecar, sidecar_path

    assert read_sidecar(photo_dir / "IMG_0001.jpg") is None
    assert not sidecar_path(photo_dir / "IMG_0001.jpg").exists()
    # 其它照片不动
    assert read_sidecar(photo_dir / "IMG_0002.jpg") is not None


def test_locate_apply_candidates(photo_dir, capsys):
    """--apply-candidates 按各自的最佳候选批量补位置。"""
    from datetime import datetime

    from conftest import write_timed_photo
    from photonmap.locate import read_sidecar

    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    code = main(["locate", str(photo_dir), "--apply-candidates"])
    assert code == 0
    out = capsys.readouterr().out
    assert "no_gps.jpg" in out
    lat, lon = read_sidecar(photo_dir / "no_gps.jpg")
    # 夹在 12:30 与 13:00 两张已定位照片之间的 12:40 → 按时间插值
    ratio = 10 / 30
    assert lat == pytest.approx(31.2304 + (31.2400 - 31.2304) * ratio, abs=1e-4)
    assert lon == pytest.approx(121.4737 + (121.4800 - 121.4737) * ratio, abs=1e-4)


def test_locate_dry_run_writes_nothing(photo_dir, capsys):
    """--dry-run 只打印将要做什么，不写盘。"""
    from datetime import datetime

    from conftest import write_timed_photo
    from photonmap.locate import read_sidecar

    write_timed_photo(photo_dir / "no_gps.jpg", datetime(2024, 5, 1, 12, 40, 0))
    code = main(["locate", str(photo_dir), "--apply-candidates", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "no_gps.jpg" in out
    assert read_sidecar(photo_dir / "no_gps.jpg") is None


def test_locate_missing_directory(tmp_path):
    code = main(["locate", str(tmp_path / "不存在"), "--set", "30,120", "--all"])
    assert code == 2


def test_locate_clear_dry_run_keeps_sidecar(photo_dir):
    """--dry-run 不能真删文件：用户以为没动，位置却已经被抹掉。"""
    from photonmap.locate import read_sidecar, write_sidecar

    write_sidecar(photo_dir / "IMG_0001.jpg", 30.5, 120.5)
    code = main([
        "locate", str(photo_dir), "--clear", "--files", "IMG_0001.jpg", "--dry-run",
    ])
    assert code == 0
    assert read_sidecar(photo_dir / "IMG_0001.jpg") == pytest.approx((30.5, 120.5))


def test_locate_apply_candidates_keeps_manual_position(photo_dir):
    """人工标定过的位置不能被候选推断覆盖。"""
    from datetime import datetime

    from conftest import write_timed_photo
    from photonmap.locate import read_sidecar, write_sidecar

    photo = photo_dir / "no_gps.jpg"
    write_timed_photo(photo, datetime(2024, 5, 1, 12, 40, 0))
    write_sidecar(photo, 22.5, 114.0)
    main(["locate", str(photo_dir), "--apply-candidates"])
    assert read_sidecar(photo) == pytest.approx((22.5, 114.0))


def test_locate_apply_candidates_respects_files(photo_dir):
    """--apply-candidates 也要认 --files：只处理选中的照片。"""
    from datetime import datetime

    from conftest import write_timed_photo
    from photonmap.locate import read_sidecar

    write_timed_photo(photo_dir / "a.jpg", datetime(2024, 5, 1, 12, 40, 0))
    write_timed_photo(photo_dir / "b.jpg", datetime(2024, 5, 1, 12, 50, 0))
    main(["locate", str(photo_dir), "--apply-candidates", "--files", "a.jpg"])
    assert read_sidecar(photo_dir / "a.jpg") is not None
    assert read_sidecar(photo_dir / "b.jpg") is None
