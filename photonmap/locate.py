"""定位结果的读写。

默认写旁车文件（<照片名>.photonmap.json），原图一个字节都不动，
删掉旁车文件即可撤销。加 --write-exif 时才改写原文件的 EXIF，
改写前会把原文件备份到工作目录。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import copy_with_retry, write_text_atomic
from .imaging import open_image

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".photonmap.json"

# 支持改写 EXIF 的格式；.livp 是 zip 容器，改写要重打包，暂不支持
_EXIF_WRITABLE = {".jpg", ".jpeg", ".heic", ".heif"}


def sidecar_path(photo_path: Path) -> Path:
    """旁车文件的路径。"""
    photo_path = Path(photo_path)
    return photo_path.with_name(photo_path.name + SIDECAR_SUFFIX)


def read_sidecar(photo_path: Path) -> tuple[float, float] | None:
    """读旁车文件里的坐标；没有或损坏时返回 None。"""
    path = sidecar_path(photo_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def write_sidecar(photo_path: Path, lat: float, lon: float, source: str = "manual") -> Path:
    """把坐标写进旁车文件。"""
    path = sidecar_path(photo_path)
    payload = {
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
    }
    # 原子写 + 退避重试：照片目录多半在 OneDrive 里，写的时候会被临时占住
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def remove_sidecar(photo_path: Path) -> bool:
    """删掉旁车文件，让照片回到「待确认」状态。"""
    path = sidecar_path(photo_path)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _dms(value: float) -> tuple:
    """十进制度 → EXIF 的度分秒三元组（秒以有理数表示）。"""
    a = abs(value)
    total = a * 3600
    d = int(total // 3600)
    m = int((total - d * 3600) // 60)
    s = total - d * 3600 - m * 60
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def _gps_ifd(lat: float, lon: float) -> dict:
    import piexif

    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _dms(lon),
    }


def _backup(photo_path: Path, backup_dir: Path | None) -> Path | None:
    """备份原文件，返回备份路径。

    只备份一次：留下的是「第一次改写之前」的原文件，
    否则第二次改写会把已经带人工坐标的版本当成原文件备份下来，
    「清除定位」时就还原不回真正的原点了。

    备份失败会抛异常——调用方必须因此放弃改写：
    没有备份就改写 EXIF，原文件里的相机 GPS 就再也找不回来了。
    """
    if backup_dir is None:
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / backup_name(photo_path)
    if target.exists():
        return target
    copy_with_retry(Path(photo_path), target)
    return target


def backup_name(photo_path: Path) -> str:
    """备份文件名。

    按路径哈希命名——不同目录下的同名照片（IMG_0001.jpg 很常见）
    用原文件名做备份名会互相覆盖。
    """
    photo_path = Path(photo_path)
    try:
        resolved = str(photo_path.resolve())
    except OSError:
        resolved = str(photo_path)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    return f"{digest}_{photo_path.name}.bak"


def restore_backup(photo_path: Path, backup_dir: Path | None) -> bool:
    """从备份还原原文件；没有备份或还原失败时返回 False。"""
    if backup_dir is None:
        return False
    source = Path(backup_dir) / backup_name(photo_path)
    if not source.is_file():
        return False
    try:
        copy_with_retry(source, Path(photo_path))
    except OSError as exc:
        log.warning("还原 %s 失败: %s", photo_path.name, exc)
        return False
    return True


def write_exif_gps(
    photo_path: Path,
    lat: float,
    lon: float,
    backup_dir: Path | None = None,
) -> bool:
    """把 GPS 写进原文件的 EXIF；格式不支持或失败时返回 False。

    扩展名不可靠——iPhone 的 Live Photo 叫 .heic 其实是 JPEG 基底的 MPO，
    所以按文件内容判断该用哪种写法。
    """
    photo_path = Path(photo_path)
    if photo_path.suffix.lower() not in _EXIF_WRITABLE:
        log.info("不改动 %s 的 EXIF（暂不支持该格式）", photo_path.name)
        return False

    try:
        with open_image(photo_path) as img:
            image_format = (img.format or "").upper()
    except Exception as exc:
        log.warning("无法识别 %s 的格式: %s", photo_path.name, exc)
        return False

    try:
        _backup(photo_path, backup_dir)
    except Exception as exc:
        # 没有备份就不能改写：原文件里的相机 GPS 会被覆盖，且再也还原不回来
        log.warning("备份 %s 失败，已取消改写 EXIF: %s", photo_path.name, exc)
        return False

    try:
        if image_format in ("JPEG", "MPO"):
            return _write_jpeg(photo_path, lat, lon)
        if image_format == "HEIF":
            return _write_heif(photo_path, lat, lon)
    except Exception as exc:
        log.warning("改写 %s 的 EXIF 失败: %s", photo_path.name, exc)
        return False
    log.info("不改动 %s 的 EXIF（未知格式 %s）", photo_path.name, image_format)
    return False


def _write_jpeg(photo_path: Path, lat: float, lon: float) -> bool:
    import piexif

    exif_dict = piexif.load(str(photo_path))
    exif_dict["GPS"] = _gps_ifd(lat, lon)
    piexif.insert(piexif.dump(exif_dict), str(photo_path))
    return True


def _write_heif(photo_path: Path, lat: float, lon: float) -> bool:
    import piexif
    from PIL import ExifTags, Image
    from pillow_heif import HeifFile

    # 先把原 EXIF 读出来（连同厂商信息），补上 GPS 再写回。
    # 子 IFD 要显式取一次，否则不会被序列化。
    with Image.open(photo_path) as img:
        exif = img.getexif()
        exif.get_ifd(ExifTags.IFD.GPSInfo)
        exif.get_ifd(ExifTags.IFD.Exif)
        raw = exif.tobytes()

    if raw:
        exif_dict = piexif.load(raw)
    else:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["GPS"] = _gps_ifd(lat, lon)
    payload = piexif.dump(exif_dict)

    # 写临时文件再替换，避免边读边写损坏原图
    temp = photo_path.with_name(photo_path.stem + ".part" + photo_path.suffix)
    try:
        heif = HeifFile(photo_path)
        try:
            heif.save(temp, exif=payload)
        finally:
            _close(heif)
        temp.replace(photo_path)
    finally:
        if temp.exists():
            temp.unlink()
    return True


def _close(heif) -> None:
    close = getattr(heif, "close", None)
    if callable(close):
        close()
