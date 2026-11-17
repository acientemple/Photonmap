"""生成缩略图：地图上点击标记点时能直接看到照片。"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageOps

from .fsutil import discard, replace_with_retry
from .imaging import open_image
from .models import PhotoRecord

log = logging.getLogger(__name__)

DEFAULT_SIZE = 320
DEFAULT_QUALITY = 80
MARKER_SIZE = 96

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def safe_stem(path: Path) -> str:
    stem = _SAFE_NAME.sub("_", path.stem)
    return stem[:60] or "photo"


def _save_atomic(img, destination: Path, **options) -> None:
    """先写临时文件再改名，避免留下半截文件被当成成品缓存。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    try:
        img.save(temp, "JPEG", **options)
        replace_with_retry(temp, destination)
    finally:
        if temp.exists():
            discard(temp)


def make_thumbnail(
    source: Path,
    destination: Path,
    size: int = DEFAULT_SIZE,
    quality: int = DEFAULT_QUALITY,
) -> Path | None:
    """生成单张缩略图，失败返回 None。"""
    try:
        with open_image(source) as img:
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            img.thumbnail((size, size), Image.LANCZOS)
            _save_atomic(img, destination, quality=quality, optimize=True)
        return destination
    except Exception as exc:
        log.warning("缩略图生成失败 %s: %s", source, exc)
        return None


def ensure_thumbnails(
    photos: list[PhotoRecord],
    assets_dir: Path,
    size: int = DEFAULT_SIZE,
    quality: int = DEFAULT_QUALITY,
) -> list[PhotoRecord]:
    """为每张照片生成缩略图到 assets_dir，返回带 thumbnail 的新记录列表。

    已经指向有效缩略图的记录会被复用，不重复生成。
    """
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    result: list[PhotoRecord] = []
    used: set[str] = set()
    for index, photo in enumerate(photos, start=1):
        if photo.thumbnail and Path(photo.thumbnail).exists():
            result.append(photo)
            continue
        name = f"{index:04d}_{safe_stem(photo.path)}.jpg"
        while name in used:
            name = f"{index:04d}_{len(used)}_{safe_stem(photo.path)}.jpg"
        used.add(name)
        thumb = make_thumbnail(photo.path, assets_dir / name, size, quality)
        result.append(replace(photo, thumbnail=thumb))
    return result


def make_square_thumbnail(
    source: Path,
    destination: Path,
    size: int = MARKER_SIZE,
    quality: int = 82,
) -> Path | None:
    """裁成正方形的小图，用作地图上的照片标记。"""
    try:
        with open_image(source) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            width, height = img.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((size, size), Image.LANCZOS)
            _save_atomic(img, destination, quality=quality, optimize=True)
        return destination
    except Exception as exc:
        log.warning("标记缩略图生成失败 %s: %s", source, exc)
        return None


def ensure_marker_thumbnails(
    photos: list[PhotoRecord],
    assets_dir: Path,
    size: int = MARKER_SIZE,
) -> list[PhotoRecord]:
    """为每张照片生成正方形标记图到 assets_dir/markers，返回带 marker 的新记录列表。"""
    markers_dir = Path(assets_dir) / "markers"
    markers_dir.mkdir(parents=True, exist_ok=True)

    result: list[PhotoRecord] = []
    used: set[str] = set()
    for index, photo in enumerate(photos, start=1):
        if photo.marker and Path(photo.marker).exists():
            result.append(photo)
            continue
        name = f"{index:04d}_{safe_stem(photo.path)}.jpg"
        while name in used:
            name = f"{index:04d}_{len(used)}_{safe_stem(photo.path)}.jpg"
        used.add(name)
        marker = make_square_thumbnail(photo.path, markers_dir / name, size)
        result.append(replace(photo, marker=marker))
    return result


def prepare_full_image(source: Path, destination: Path, quality: int = 90) -> Path | None:
    """准备浏览器能直接显示的大图：JPEG 原样复制，HEIC 等格式转码。"""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in (".jpg", ".jpeg"):
            temp = destination.with_name(destination.name + ".part")
            try:
                shutil.copyfile(source, temp)
                replace_with_retry(temp, destination)
            finally:
                if temp.exists():
                    discard(temp)
        else:
            with open_image(source) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                _save_atomic(img, destination, quality=quality, optimize=True)
        return destination
    except Exception as exc:
        log.warning("大图准备失败 %s: %s", source, exc)
        return None
