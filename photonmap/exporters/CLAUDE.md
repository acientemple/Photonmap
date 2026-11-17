# 导出器（photonmap/exporters）

`exporters/base.py` 的 `BaseExporter` 是 ABC，约定 `name` / `extension` / `coord_system` 三个类属性和 `export(photos, output_dir) -> Path`。

- `KmlExporter`：`photos.kml` + `photos.kmz`（KMZ 内 `doc.kml` 与 `files/` 缩略图），可按日期分组、可画 LineString 轨迹。用 ElementTree + KML 命名空间 `{http://www.opengis.net/kml/2.2}`。description 是普通 HTML 字符串——ElementTree 不支持原生 CDATA，手写 `<![CDATA[...]]>` 会被转义成正文显示。
- `GeoJsonExporter`：`photos.geojson`。`embed_thumbnails=True` 时缩略图变成 `data:image/jpeg;base64,...` 内联。
- HTML 导出器（`LeafletExporter` / `GoogleMapsExporter` / `BaiduMapExporter`）继承 `html_base.HtmlExporter`：Jinja2 `FileSystemLoader` 渲染 `photonmap/templates/*.html`，缩略图输出到 `<输出目录>/files/`，模板里用相对路径引用。

`HtmlExporter._row()` 里的坐标是 `round(lat, 7)`，测试断言导出内容时应按导出器自己的 `coord_system` 计算期望值——百度导出的是 BD-09，数值与原始 WGS-84 不同。行数据的实际构造在模块级函数 `html_base.photo_row()`，serve 模式也用它（多一个 `prefix` 参数决定资源路径前缀）。
