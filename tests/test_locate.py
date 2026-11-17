from datetime import datetime
from pathlib import Path

import pytest
from conftest import write_gps_photo, write_timed_photo

from photonmap.exif_reader import read_photo
from photonmap.locate import (
    backup_name,
    read_sidecar,
    remove_sidecar,
    restore_backup,
    sidecar_path,
    write_exif_gps,
    write_sidecar,
)


def test_sidecar_roundtrip(tmp_path):
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))

    assert read_sidecar(photo) is None
    path = write_sidecar(photo, 30.5, 120.25)
    assert path.name == "a.jpg.photonmap.json"
    assert path.exists()
    assert read_sidecar(photo) == pytest.approx((30.5, 120.25))


def test_sidecar_ignores_broken_file(tmp_path):
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    sidecar_path(photo).write_text("这不是 JSON", encoding="utf-8")
    assert read_sidecar(photo) is None


def test_sidecar_rejects_out_of_range(tmp_path):
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    write_sidecar(photo, 999.0, 121.0)
    assert read_sidecar(photo) is None


def test_remove_sidecar(tmp_path):
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    write_sidecar(photo, 30.5, 120.25)
    assert remove_sidecar(photo) is True
    assert read_sidecar(photo) is None
    assert remove_sidecar(photo) is False


def test_write_exif_gps_jpeg(tmp_path):
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))

    backups = tmp_path / "backups"
    assert write_exif_gps(photo, 30.123456, 120.654321, backup_dir=backups) is True

    record = read_photo(photo)
    assert record is not None
    assert record.lat == pytest.approx(30.123456, abs=1e-5)
    assert record.lon == pytest.approx(120.654321, abs=1e-5)
    # 原文件被备份了
    assert (backups / backup_name(photo)).exists()


def test_write_exif_gps_heif(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    photo = tmp_path / "a.heic"
    Image.new("RGB", (64, 48), (30, 90, 160)).save(photo, format="HEIF")

    assert write_exif_gps(photo, 22.5, 114.0, backup_dir=tmp_path / "backups") is True

    record = read_photo(photo)
    assert record is not None
    assert record.lat == pytest.approx(22.5, abs=1e-5)
    assert record.lon == pytest.approx(114.0, abs=1e-5)


def test_write_exif_gps_skips_unsupported_format(tmp_path):
    """容器格式改写要重打包 zip，本次不支持，只写旁车文件。"""
    import zipfile

    inner = tmp_path / "IMG.heic.jpg"
    write_gps_photo(inner, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    livp = tmp_path / "shot.livp"
    with zipfile.ZipFile(livp, "w") as archive:
        archive.write(inner, "IMG.HEIC.heic")

    assert write_exif_gps(livp, 30.0, 120.0) is False
    assert not sidecar_path(livp).exists()


def test_write_exif_gps_keeps_original_when_failed(tmp_path):
    """解码不了的文件直接返回 False，原文件不动。"""
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    assert write_exif_gps(broken, 30.0, 120.0) is False
    assert broken.read_bytes() == b"not an image"


def test_backup_names_do_not_collide(tmp_path):
    """不同目录下的同名照片不能共用一个备份文件。"""
    first = tmp_path / "a" / "IMG_0001.jpg"
    second = tmp_path / "b" / "IMG_0001.jpg"
    write_gps_photo(first, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    write_gps_photo(second, 39.9042, 116.4074, datetime(2024, 5, 1, 12, 30, 0))

    backups = tmp_path / "backups"
    assert write_exif_gps(first, 30.0, 120.0, backup_dir=backups) is True
    assert write_exif_gps(second, 31.0, 121.0, backup_dir=backups) is True

    names = sorted(p.name for p in backups.glob("*"))
    assert len(names) == 2
    assert backup_name(first) != backup_name(second)


def test_backup_keeps_the_first_original(tmp_path):
    """第二次改写不会把带人工坐标的版本当成原文件覆盖掉备份。"""
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    backups = tmp_path / "backups"

    assert write_exif_gps(photo, 30.0, 120.0, backup_dir=backups) is True
    assert write_exif_gps(photo, 29.0, 119.0, backup_dir=backups) is True

    # 备份里躺着的还是最初的原文件
    record = read_photo(backups / backup_name(photo))
    assert record is not None
    assert record.lat == pytest.approx(31.2304, abs=1e-4)


def test_restore_backup_brings_the_original_back(tmp_path):
    """从备份还原后，EXIF 里的坐标回到原样。"""
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    backups = tmp_path / "backups"

    assert write_exif_gps(photo, 30.0, 120.0, backup_dir=backups) is True
    assert restore_backup(photo, backups) is True

    record = read_photo(photo)
    assert record is not None
    assert record.lat == pytest.approx(31.2304, abs=1e-4)


def test_restore_backup_without_backup_file(tmp_path):
    """没有备份时如实返回 False。"""
    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))
    assert restore_backup(photo, tmp_path / "backups") is False


def test_write_exif_gps_aborts_when_backup_fails(tmp_path, monkeypatch):
    """备份失败就不能改写 EXIF——否则原文件的相机 GPS 被覆盖且还原不回来。"""
    import photonmap.locate as locate_module

    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))

    def boom(src, dst, *args, **kwargs):
        raise PermissionError("文件被同步软件占住了")

    monkeypatch.setattr(locate_module, "copy_with_retry", boom)

    assert write_exif_gps(photo, 30.0, 120.0, backup_dir=tmp_path / "backups") is False
    # 原文件没被动过
    record = read_photo(photo)
    assert record is not None
    assert record.lat == pytest.approx(31.2304, abs=1e-4)


def test_write_sidecar_retries_on_locked_file(tmp_path, monkeypatch):
    """同步软件短暂占住文件时要退避重试，不能直接失败。"""
    import photonmap.fsutil as fsutil_module

    photo = tmp_path / "a.jpg"
    write_gps_photo(photo, 31.2304, 121.4737, datetime(2024, 5, 1, 12, 30, 0))

    calls = {"n": 0}
    real = fsutil_module.replace_with_retry

    def flaky(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("暂时占住")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(fsutil_module, "replace_with_retry", flaky)
    write_sidecar(photo, 30.5, 120.5)
    assert read_sidecar(photo) == pytest.approx((30.5, 120.5))
    assert calls["n"] >= 3
    # 不留半截的 .part 文件
    assert not (tmp_path / "a.jpg.photonmap.json.part").exists()
