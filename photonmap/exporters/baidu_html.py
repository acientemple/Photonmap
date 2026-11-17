"""导出百度地图网页。需要百度开放平台的 AK，坐标会转成 BD-09。"""

from __future__ import annotations

import os

from .html_base import HtmlExporter

ENV_KEY = "BAIDU_MAP_AK"


class BaiduMapExporter(HtmlExporter):
    name = "baidu_maps"
    template_name = "baidu_maps.html"
    coord_system = "bd09"

    def __init__(self, ak: str | None = None, **options):
        resolved = ak or os.environ.get(ENV_KEY, "")
        super().__init__(ak=resolved, **options)
        self.ak = resolved
