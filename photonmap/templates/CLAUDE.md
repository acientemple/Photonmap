# HTML 前端（photonmap/templates）

`templates/photo_map.js` / `templates/photo_map.css` 是与 Python 无关的静态资源，导出时原样复制到 `<输出目录>/files/`，Leaflet / Google Maps / 百度地图三个模板共用同一份逻辑。

- 地图标记直接是照片缩略图（`files/markers/`），点开是灯箱大图（`files/full/`）；灯箱里显示拍摄时间 / 坐标（WGS-84 与地图系）/ 海拔 / 文件名，并提供「打开本地原图」——指向 `file://` URI，由 `html_base` 依据 `PhotoRecord.path` 生成。
- 侧边栏列表只有缩略图 + 文件名，**不显示坐标与时间**（这是产品要求，地图标记本身同理）。
- **按屏幕像素距离聚合**：当前缩放下像素距离小于 `gap`（三套模板统一 **80px**）的照片合并成一簇，地图上只显示「离簇中心最近」的那张作为代表标记，右下角叠数量角标；放大后距离拉开会自动拆成多个标记。标记实际占位 68px（CSS 64px + 2px 边框），`gap=80` 保证相邻簇不重叠。
- **关键约束**：两点的屏幕像素距离只取决于 zoom，与平移无关。所以聚类只在缩放结束后重算（`ClusterView.update()`），平移时只刷新角标位置（`syncBadges()`，必须用簇中心的经纬度重新换算当前像素，不能沿用缩放时算出的旧像素）。
- 聚类算法 `clusterByPixel()`：桶宽 = `gap` 的网格分桶 + 只查 3×3 邻桶 + union-find 单链合并（连通分量）。同一格子里照片超过 64 张（坐标几乎完全相同）时整格直接合并，避免两两比较退化成 O(n²)。
- **角标与胶卷统一用 body 级 `position: fixed` DOM 层**（`.pm-cluster-layer`），不依赖各家地图的 label API（Google `marker.setLabel` / 百度 `BMap.Label` 的行为与 z-index 差异大）。CSS 分层：cluster-layer 700 < strip 800 < 灯箱 9999。
- 点代表标记：簇内 1 张 → 直接打开灯箱；>1 张 → 底部弹出横向胶卷（`PhotoMap.Filmstrip`），列出该簇全部缩略图，点其中一张 → 灯箱切到该子集（`lightbox.setPhotos()`），页码变成簇内计数、←/→ 只在簇内循环。
- **胶卷多选移动**（只有 serve 模式给 `canMove: true`）：胶卷工具栏有「改变位置」按钮，点后进入选择态（缩略图打勾、可全选），点「移动到所选」通过 `photonmap:move-photos` 把照片 key 交给壳页面，复用定位浮层选点，确认后走 `/api/locate/batch`（`mode: "same"` + 每张各自的令牌）。静态导出没有写盘能力，不显示这个按钮。
- **单张照片标记可拖动**：`ClusterView` 只对「独占一个标记」的照片调用 `adapter.setMarkerDraggable(marker, true, onDrop)`（聚类代表标记拖走哪张说不清，保持不可拖）。拖完先弹 `.pm-drag-bar` 确认条，点「保存」才发 `photonmap:drag-move` 给壳页面；取消或写入失败时按记录的原始经纬度把标记放回去。
- **灯箱点图即关**：点图片本身、图片外空白或背景都关闭灯箱（不用去找 ×）；放大后拖动平移结束那一下不会被当成点击关闭（`movedPx > 3` 判定）。图片默认 `cursor: pointer`，放大后变 `grab`。
- Esc 只关闭最上层 UI：先关灯箱、再关胶卷（胶卷处于多选态时，Esc 先退出选择态）。归属在 `document` 的 **capture 阶段**判定（`event.__pmEscOwner` = `lightbox` / `strip`），灯箱与胶卷各自的 keydown 监听器据此决定是否响应——否则灯箱先关掉 DOM class 后，胶卷会误以为可以关闭，同一次 Esc 把两层一起关掉。
- 三个模板各自实现一份 `adapter`，把三套地图的 API 抹平。契约共 10 项：`container` / `toPixel` / `fromPixel` / `createMarker` / `setMarkerPosition` / `setMarkerVisible` / `removeMarker` / `onZoomChange` / `onMove`：
  - Leaflet：`map.latLngToContainerPoint()` / `map.containerPointToLatLng()`，事件 `zoomend` / `move`
  - Google：**不要用 `MapProjection`**。它的方法名在版本间反复变动（`fromLatLngToDivPixel` / `fromLatLngToContainerPixel` / `fromLatLngToPoint` 轮流缺），当前版本原型上只有 `fromLatLngToPoint` / `fromPointToLatLng`，且无效 key 下还是 stub。改成自己按 Web Mercator 换算容器像素（`mercatorWorld()` / `mercatorLatLon()`），只依赖 `getCenter()` / `getZoom()` / `getDiv()`；实测与 Google 渲染出的标记位置误差 < 1px。事件 `idle`（缩放过程中投影还没更新，只能等 `idle`） / `bounds_changed`
  - 百度：`map.pointToOverlayPixel()` / `map.overlayPixelToPoint()`（`BMap.Point` 是 (lng, lat) 顺序），事件 `zoomend` / `moving`。注意当前 API **没有导出 `BMap.OverlayPixel`**，但 `overlayPixelToPoint` 只读入参的 `x`/`y`，传普通对象 `{x, y}` 即可
  - `onZoomChange` / `onMove` **必须返回解绑函数**（`ClusterView.destroy()` 靠它摘掉监听，否则每次重建视图都会多挂一组监听）。`removeMarker` 用于销毁标记。
- **胶卷按业务 key 记照片，不记数组下标**：`Filmstrip` 打开时把 `photo.key`（静态导出退到 marker+坐标）存进 `keys`，每次渲染/点击都用 `findIndex` 重新解析成当前 `PHOTOS` 里的下标。照片列表在 serve 模式随时会被整体替换（确认位置、轮询发现变化），存旧下标会让点击串位甚至打开空照片——表现就是「点了没反应」。`onPick(k, photoIndex, indices)` 的第三个参数是当前解析出的下标表，模板用它组灯箱子集，不要再依赖打开时记住的簇下标。
- **图片一律 `loading="lazy" decoding="async"`**（地图标记、侧栏缩略图、胶卷缩略图）：4000 张照片的目录一次会建几千个 `<img>`，不加懒加载就是几千个并发请求，服务端和浏览器都会被打爆（连接被拒、部分图片加载失败）。现在只有视口附近的那几十张真正下载。
- **地图「隐藏照片」**：Leaflet 系引擎（叶子/Google/高德）把「照片标记」作为**右上角图层控件里的一项**（`L.control.layers` 的 overlay，默认勾选；取消勾选切 `body.pm-hide-markers`，CSS 隐藏 `.leaflet-marker-pane` / `.pm-cluster-layer`）。百度没有图层控件，用左上角一个紧凑的 `.pm-tool` 按钮（文字「照片」）切换。
- **胶卷支持滚轮横向滚动**：`.pm-strip-track` 的 `wheel` 监听把纵向 delta 加到 `scrollLeft`（`passive:false` + `preventDefault`），照片超出屏幕时不用拖滚动条；内容没超出时不拦截。
- **放大后把同点位的少数几张散开（spiderfy）**：`ClusterView.update()` 里，簇内 2–16 张且 `adapter.getZoom() >= 15` 时，按圆周把成员摆开（半径 `max(72, n*76/2π)`），每张单独可点、可拖；此时点标记直接开灯箱而不是弹胶卷（`select()` 里 `cluster.spidered` 分支）。照片更多或缩放不够时仍按整体显示 + 数量角标。
- **照片列表可动态替换**（`serve` 模式需要）：`ClusterView.setPhotos(photos)` 内容没变只重算聚类，变了就 `destroy()` 后整体重建标记；`Lightbox.setAllPhotos(photos)` 更新完整图集（正在看某一簇子集时不动它）；`Filmstrip.setPhotos(photos)` 更新胶卷引用的照片表。比较是否变化用 `photoKey()`（标记图路径 + 坐标），不能用对象引用——服务端每次都返回新对象。
- **待确认定位的标记**（`PhotoMap.PendingView`）：位置还没定的照片按候选位置显示，角标是虚线「?」。**不参与聚类**——位置本来就不确定，聚在一起没有意义。这些标记用 `createMarker(..., zIndexOffset=-1000)` 压在已定位照片下面（Leaflet 用 `zIndexOffset`，Google 用 `zIndex`，百度尽量 `setTop(false)`），否则候选点常常和已定位点重合，「?」会盖住真位置、把点击截走。
- **`createMarker(lat, lon, photo, onClick)` 传的是照片对象不是下标**：`PendingView` 有自己的照片数组，用下标会跟 `ClusterView` 的下标冲突。图标 URL 从 `photo.marker` 取。
- **Leaflet 随包发布**：`templates/vendor/` 下是 Leaflet 1.9.4 的 js/css 与配图，导出时复制到 `files/vendor/`，页面引用 `files/vendor/leaflet.js`。这样导出的页面完全离线可用。
- `PhotoMap.coordCount()`：所有照片挤在同一个坐标时 `fitBounds` 会缩到街道级别，模板里改用固定 zoom 15 兜底。
