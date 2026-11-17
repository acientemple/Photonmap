"""导出 Google Maps 网页。需要 Google Maps JavaScript API 的 Key。"""

from __future__ import annotations

import os

from .html_base import HtmlExporter

ENV_KEY = "GOOGLE_MAPS_API_KEY"


class GoogleMapsExporter(HtmlExporter):
    name = "google_maps"
    template_name = "google_maps.html"
    coord_system = "wgs84"

    def __init__(self, api_key: str | None = None, **options):
        resolved = api_key or os.environ.get(ENV_KEY, "")
        super().__init__(api_key=resolved, **options)
        self.api_key = resolved
