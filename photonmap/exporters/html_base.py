"""HTML 导出器的公共部分：模板渲染 + 数据整理。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..fsutil import copy_with_retry
from ..models import PhotoRecord, finite_or_none
from ..thumbnail import ensure_marker_thumbnails, ensure_thumbnails, prepare_full_image, safe_stem
from .base import BaseExporter

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# 静态资源：模板里用相对路径引用，导出时复制到输出目录
STATIC_FILES = ("photo_map.js", "photo_map.css")
# 第三方库（Leaflet）随包发布，导出后完全离线可用
VENDOR_DIR = TEMPLATE_DIR / "vendor"

COORD_LABELS = {"wgs84": "WGS-84", "gcj02": "GCJ-02", "bd09": "BD-09"}


def photo_row(
    photo: PhotoRecord,
    assets_dir: Path,
    full: Path | None = None,
    coord_system: str = "wgs84",
    original: str | None = None,
    prefix: str = "files",
) -> dict:
    """把一张照片整理成前端要的一行数据。

    坐标按 coord_system 转换（原始 WGS-84 另存一份），
    缩略图等路径转成相对 assets_dir.parent 的相对路径。
    prefix 是资源在页面里的路径前缀：静态导出用 "files"，
    本地服务用 "/files"（页面在子路径下，相对路径会解析错）。
    """
    lat, lon = photo.coordinates(coord_system)
    wgs_lat, wgs_lon = photo.coordinates("wgs84")
    if original is None:
        try:
            original = photo.path.resolve().as_uri()
        except (OSError, ValueError):
            original = ""
    return {
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "wgs_lat": round(wgs_lat, 7),
        "wgs_lon": round(wgs_lon, 7),
        "name": photo.name,
        "title": photo.title,
        "taken_at": photo.taken_at.strftime("%Y-%m-%d %H:%M:%S") if photo.taken_at else "",
        "altitude": finite_or_none(photo.altitude),
        "thumb": _prefixed(photo.thumbnail, assets_dir, prefix),
        "marker": _prefixed(photo.marker, assets_dir, prefix),
        "full": _prefixed(full, assets_dir, prefix),
        "original": original,
        "status": "located",
        "source": photo.source,
    }


def _prefixed(path: Path | None, assets_dir: Path, prefix: str) -> str:
    """把资源路径拼上页面里的路径前缀。

    `_relative()` 给出的路径形如 files/markers/x.jpg，
    这里把开头那截资源目录名换成 prefix（静态导出 "files"、服务 "/files"）。
    """
    if not path:
        return ""
    # 绝大多数调用都是资源目录下的派生图：直接用字符串拼接，
    # 比 Path.relative_to() 快一个数量级（一页几千张照片时差别很明显）
    base = str(Path(assets_dir).parent)
    text = str(Path(path))
    if text.startswith(base + os.sep) or text.startswith(base + "/"):
        relative = text[len(base) + 1:].replace("\\", "/")
    else:
        relative = HtmlExporter._relative(Path(path), assets_dir)
    if not relative:
        return ""
    if relative.startswith("/"):
        return relative  # 不在资源目录里，原样返回
    _, _, rest = relative.partition("/")
    return f"{prefix}/{rest}" if rest else prefix


class HtmlExporter(BaseExporter):
    """把照片渲染成一个可以双击打开的网页地图。"""

    template_name = ""
    extension = ".html"
    # 底图配置：只有 Leaflet 模板用得上
    tiles: list[dict] = []

    def __init__(self, **options):
        super().__init__(**options)
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    @staticmethod
    def _relative(path: Path | None, assets_dir: Path) -> str:
        if not path:
            return ""
        path = Path(path)
        try:
            return path.relative_to(assets_dir.parent).as_posix()
        except ValueError:
            return path.as_posix()

    def _full_images(self, photos: list[PhotoRecord], assets_dir: Path) -> list[Path | None]:
        """把原图复制/转码成浏览器能显示的大图，返回对应路径列表。"""
        full_dir = assets_dir / "full"
        full_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path | None] = []
        used: set[str] = set()
        for index, photo in enumerate(photos, start=1):
            name = f"{index:04d}_{safe_stem(photo.path)}.jpg"
            while name in used:
                name = f"{index:04d}_{len(used)}_{safe_stem(photo.path)}.jpg"
            used.add(name)
            paths.append(prepare_full_image(photo.path, full_dir / name))
        return paths

    def _row(
        self,
        photo: PhotoRecord,
        assets_dir: Path,
        full: Path | None = None,
    ) -> dict:
        return photo_row(photo, assets_dir, full, self.coord_system)

    def export(self, photos: list[PhotoRecord], output_dir: Path) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = output_dir / "files"
        prepared = ensure_thumbnails(photos, assets_dir)
        prepared = ensure_marker_thumbnails(prepared, assets_dir)
        fulls = self._full_images(prepared, assets_dir)

        for name in STATIC_FILES:
            copy_with_retry(TEMPLATE_DIR / name, assets_dir / name)
        if VENDOR_DIR.is_dir():
            shutil.copytree(VENDOR_DIR, assets_dir / "vendor", dirs_exist_ok=True)

        rows = [self._row(p, assets_dir, f) for p, f in zip(prepared, fulls)]
        template = self.env.get_template(self.template_name)
        options = dict(self.options)
        options.setdefault("live", False)
        html = template.render(
            photos=rows,
            pending=[],
            version=0,
            options=options,
            coord_label=COORD_LABELS.get(self.coord_system, self.coord_system),
            coord_system=self.coord_system,
            asset_prefix="files",
            tiles=self.tiles,
            poll_ms=0,
        )

        path = output_dir / f"{self.name}{self.extension}"
        path.write_text(html, encoding="utf-8")
        return path
