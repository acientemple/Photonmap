"""导出 Leaflet 网页：无需任何密钥，用 OpenStreetMap 瓦片。"""

from __future__ import annotations

from .html_base import HtmlExporter

DEFAULT_TILES = [
    {
        "name": "OpenStreetMap",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "&copy; OpenStreetMap contributors",
        "maxZoom": 19,
    }
]


class LeafletExporter(HtmlExporter):
    name = "leaflet"
    template_name = "leaflet.html"
    coord_system = "wgs84"
    tiles = DEFAULT_TILES
