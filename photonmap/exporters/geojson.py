"""导出 GeoJSON：通用格式，能被 QGIS / ArcGIS / Mapbox / Leaflet 直接读取。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ..models import PhotoRecord
from ..thumbnail import ensure_thumbnails
from .base import BaseExporter


class GeoJsonExporter(BaseExporter):
    name = "photos"
    extension = ".geojson"

    def __init__(self, embed_thumbnails: bool = False, **options):
        super().__init__(embed_thumbnails=embed_thumbnails, **options)
        self.embed_thumbnails = embed_thumbnails

    def _feature(self, photo: PhotoRecord, assets_dir: Path) -> dict:
        properties = {
            "name": photo.name,
            "title": photo.title,
            "path": str(photo.path),
            "taken_at": photo.taken_at.isoformat() if photo.taken_at else None,
            "altitude": photo.altitude,
            "wgs84": [photo.lon, photo.lat],
        }
        if photo.thumbnail and Path(photo.thumbnail).exists():
            if self.embed_thumbnails:
                data = Path(photo.thumbnail).read_bytes()
                properties["thumbnail"] = "data:image/jpeg;base64," + base64.b64encode(data).decode()
            else:
                try:
                    rel = Path(photo.thumbnail).relative_to(assets_dir.parent).as_posix()
                except ValueError:
                    rel = Path(photo.thumbnail).as_posix()
                properties["thumbnail"] = rel
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [photo.lon, photo.lat]},
            "properties": properties,
        }

    def export(self, photos: list[PhotoRecord], output_dir: Path) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = output_dir / "files"
        prepared = ensure_thumbnails(photos, assets_dir)

        collection = {
            "type": "FeatureCollection",
            "name": "照片位置",
            "features": [self._feature(p, assets_dir) for p in prepared],
        }
        path = output_dir / f"{self.name}.geojson"
        path.write_text(
            json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
