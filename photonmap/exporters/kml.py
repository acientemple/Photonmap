"""导出 KML / KMZ：Google Earth 双击即开，也能导入 Google Maps 的「我的地图」。"""

from __future__ import annotations

import html

import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from ..models import PhotoRecord
from ..thumbnail import ensure_thumbnails
from .base import BaseExporter

KML_NS = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", KML_NS)


def _tag(name: str) -> str:
    return f"{{{KML_NS}}}{name}"


def _coordinates(lat: float, lon: float, alt: float | None = None) -> str:
    if alt is None:
        return f"{lon:.6f},{lat:.6f},0"
    return f"{lon:.6f},{lat:.6f},{alt:.1f}"


def _description(photo: PhotoRecord, assets_dir: Path) -> str:
    """KML 的 description 是 HTML，缩略图用相对路径引用。

    ElementTree 不支持原生 CDATA，手写 ``<![CDATA[...]]>`` 会被转义成正文，
    所以这里直接给普通 HTML，由 ElementTree 做 XML 转义。
    """
    parts = []
    if photo.thumbnail:
        try:
            rel = Path(photo.thumbnail).relative_to(assets_dir.parent).as_posix()
        except ValueError:
            rel = Path(photo.thumbnail).as_uri()
        parts.append(
            f'<img src="{rel}" alt="{html.escape(photo.name, quote=True)}" style="max-width:320px;max-height:240px;" />'
        )
    meta = []
    if photo.taken_at:
        meta.append(f"拍摄时间：{photo.taken_at:%Y-%m-%d %H:%M:%S}")
    if photo.altitude is not None:
        meta.append(f"海拔：{photo.altitude:.1f} m")
    meta.append(f"坐标：{photo.lat:.6f}, {photo.lon:.6f} (WGS-84)")
    parts.append("<br/>".join(meta))
    return "<br/>".join(parts)


class KmlExporter(BaseExporter):
    """生成 .kml 与打包了缩略图的 .kmz。"""

    name = "photos"
    extension = ".kml"

    def __init__(self, group_by_date: bool = True, with_route: bool = True, **options):
        super().__init__(group_by_date=group_by_date, with_route=with_route, **options)
        self.group_by_date = group_by_date
        self.with_route = with_route

    def _build_document(self, photos: list[PhotoRecord], assets_dir: Path) -> ET.Element:
        doc = ET.Element(_tag("Document"))
        ET.SubElement(doc, _tag("name")).text = "照片位置"

        style = ET.SubElement(doc, _tag("Style"), {"id": "photo-style"})
        icon = ET.SubElement(style, _tag("IconStyle"))
        ET.SubElement(icon, _tag("scale")).text = "0.8"

        groups: dict[str, list[PhotoRecord]] = defaultdict(list)
        for photo in photos:
            key = photo.taken_at.strftime("%Y-%m-%d") if photo.taken_at else "未知日期"
            groups[key].append(photo)

        for key in sorted(groups):
            folder = ET.SubElement(doc, _tag("Folder"))
            ET.SubElement(folder, _tag("name")).text = key
            for photo in groups[key]:
                placemark = ET.SubElement(folder, _tag("Placemark"))
                ET.SubElement(placemark, _tag("name")).text = photo.title
                ET.SubElement(placemark, _tag("description")).text = _description(
                    photo, assets_dir
                )
                point = ET.SubElement(placemark, _tag("Point"))
                ET.SubElement(point, _tag("coordinates")).text = _coordinates(
                    photo.lat, photo.lon, photo.altitude
                )

        if self.with_route and len(photos) > 1:
            ordered = sorted(
                photos, key=lambda p: (p.taken_at is None, p.taken_at or p.path.stat().st_mtime)
            )
            placemark = ET.SubElement(doc, _tag("Placemark"))
            ET.SubElement(placemark, _tag("name")).text = "拍摄轨迹"
            line = ET.SubElement(placemark, _tag("LineString"))
            ET.SubElement(line, _tag("tessellate")).text = "1"
            ET.SubElement(line, _tag("coordinates")).text = " ".join(
                _coordinates(p.lat, p.lon) for p in ordered
            )
        return doc

    def export(self, photos: list[PhotoRecord], output_dir: Path) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = output_dir / "files"
        prepared = ensure_thumbnails(photos, assets_dir)

        root = ET.Element(_tag("kml"))
        root.append(self._build_document(prepared, assets_dir))
        ET.indent(root, space="  ")
        tree = ET.ElementTree(root)

        kml_path = output_dir / f"{self.name}.kml"
        tree.write(kml_path, encoding="utf-8", xml_declaration=True)

        kmz_path = output_dir / f"{self.name}.kmz"
        with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(kml_path, arcname="doc.kml")
            for thumb in prepared:
                if thumb.thumbnail and Path(thumb.thumbnail).exists():
                    zf.write(thumb.thumbnail, arcname=f"files/{Path(thumb.thumbnail).name}")
        return kmz_path

    def export_kml_only(self, photos: list[PhotoRecord], output_dir: Path) -> Path:
        """只要 .kml（不需要 KMZ）时使用。"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = output_dir / "files"
        prepared = ensure_thumbnails(photos, assets_dir)
        root = ET.Element(_tag("kml"))
        root.append(self._build_document(prepared, assets_dir))
        ET.indent(root, space="  ")
        path = output_dir / f"{self.name}.kml"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path
