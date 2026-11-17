# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

photonmap 把带 GPS 信息的照片导出成可在地图上查看的文件（KML/KMZ、GeoJSON、Leaflet / Google Maps / 百度地图 HTML），也可以起一个本地服务实时显示某个照片目录。纯 Python CLI，无数据库。

底图不依赖任何地图服务商的 JS API：要么用一条自定的瓦片 URL 模板，要么用随包的 Leaflet 加载在线瓦片，照片坐标一律 WGS-84，与 WGS-84 底图直接对齐。

本地服务（`serve`）提供三件事：
- **人工补录定位**：没有 GPS 的照片按同目录其它照片的拍摄时间推断出候选位置，用户在地图上拖动确认后写入坐标。
- **多地图引擎切换**：Leaflet / Google Maps / 高德地图 / 百度地图。Google 与高德走官方公开瓦片，**不需要密钥**；只有百度要自己的 AK。
- **图层切换**：每个引擎右上角有底图切换控件（路网 / 卫星 / 混合）。

包名是 **`photonmap`**（p-h-o-t-o-**n**-m-a-p），导入名 `photonmap`，不存在 `photomap`。

## 环境与命令

虚拟环境在 `.venv/`（Windows 布局，解释器是 `.venv/Scripts/python.exe`）。所有命令都在仓库根目录执行。

```bash
# 安装（可编辑模式 + 开发依赖）
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# 可选：装上 AI 识别地名的能力（核心功能不需要）
.venv/Scripts/python.exe -m pip install -e ".[ai]"

# 跑全部测试
.venv/Scripts/python.exe -m pytest -q

# 跑单个测试文件 / 单个用例
.venv/Scripts/python.exe -m pytest tests/test_coords.py -q
.venv/Scripts/python.exe -m pytest tests/test_coords.py::test_roundtrip_bd09_gcj02 -q
.venv/Scripts/python.exe -m pytest -k "baidu or html" -q

# CLI
.venv/Scripts/python.exe -m photonmap.cli scan <照片目录> [--coord-system gcj02]
.venv/Scripts/python.exe -m photonmap.cli export <照片目录> -o <输出目录> -f kml -f leaflet

# 批量定位置：按各自候选补位置 / 钉到指定点 / 清除人工定位
.venv/Scripts/python.exe -m photonmap.cli locate <照片目录> --apply-candidates
.venv/Scripts/python.exe -m photonmap.cli locate <照片目录> --set 30.1,120.2 --files a.jpg,b.jpg
.venv/Scripts/python.exe -m photonmap.cli locate <照片目录> --clear --all --dry-run

# 起本地服务，实时显示目录里的照片（往目录里丢照片会自动出现）
.venv/Scripts/python.exe -m photonmap.cli serve <照片目录> \
  --tile "路网=https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" \
  --tile "卫星=https://example.com/{z}/{x}/{y}.jpg" \
  --port 8765 --interval 2
```

仓库没有 lint / format 配置，也没有 CI。不要臆造 `ruff`/`black` 命令。

双击版入口（GUI 启动器）：`.venv/Scripts/python.exe -m photonmap.launcher [照片目录]` —— 不给目录就弹选择框，起服务后自动打开浏览器，关窗即停。打包成单文件 exe 用 `.\build_exe.ps1`（PyInstaller + `packaging/photonmap.spec`，产出 `dist/PhotonMap.exe`，无控制台、不含 AI 依赖）。

## 架构

各层职责严格分离，导出器不碰文件系统扫描、也不自己解析 EXIF。

### 两种运行形态

- **export**：一次性产出静态文件，照片列表内嵌在 HTML 里。
- **serve**：起一个 localhost 服务，后台轮询照片目录，页面按版本号增量拉取 `/api/photos`。目录里增删照片后页面自动更新，不用刷新。

两者共用同一套前端逻辑（`templates/photo_map.js`），差别只在照片列表从哪来：静态模板用内嵌的 `PHOTOS`，serve 模式用接口。

### 人工补录定位

`geo.py` 推断候选位置，`locate.py` 负责存。

- **候选推断**（`geo.suggest()`）：同一个目录里的照片通常出自同一次出行，按拍摄时间排开就是一条轨迹。拿目标照片的拍摄时间去找**最近的已定位照片**，给出候选点；置信度按时间差分档（≤30min 很可能 / ≤3h 可能 / 同一天 存疑，跨天不给候选）。目标照片夹在两张已定位照片之间时，额外给一个**时间插值点**（前后两张间隔 <2h 且直线距离 <50km 才给），这是最准的候选。
- **存储**：默认写**旁车文件** `<照片名>.photonmap.json`（原图一个字节不动，删掉即撤销）。加 `--write-exif` 时才改写原文件的 EXIF，改前备份到 `<workdir>/backups/`。**备份按路径哈希命名**（`locate.backup_name()`），不同目录下的同名照片不会互相覆盖；而且**只备份第一次**——留的是「第一次改写前」的原文件，否则第二次改写会把带人工坐标的版本当成原文件存下来，「清除定位」就还原不回真正的原点了。
- **改写 EXIF 的坑**：扩展名不可靠——iPhone 的 Live Photo 叫 `.heic` 其实是 JPEG 基底的 **MPO**。所以 `locate.write_exif_gps()` **按文件内容**判断该用哪种写法（Pillow 读出的 `format` 是 JPEG/MPO 就走 piexif，是 HEIF 就走 pillow_heif）。`.livp` 是 zip 容器，改写要重打包，暂不支持，只写旁车文件。
- **前端**：待确认的照片在主地图上按候选位置显示，角标是虚线的「?」（`.pm-pending-badge`）；侧边栏有独立的「待确认定位」分区，显示候选来自哪张照片、差多久。点「定位」→ 弹出一张独立的 Leaflet 地图，标记可拖动，还有候选按钮可一键切换 → 确认后 `POST /api/locate` → 服务端写坐标 → 版本号 +1 → 侧边栏与地图自动更新。
- **记住上一次定位的位置**：确认或拖动写入成功后把 WGS-84 坐标存进 `localStorage['photonmap.lastLocate']`；浮层初始点顺序是 **候选位置 → 上次位置 → 地图当前视野中心 → 兜底**。**不要用 `PHOTOS[0]`**——列表里第一张可能是坏 EXIF（实测有 -74.7° 的南极坐标），以前浮层就因此总落在南极。
- **确认浮层铺满窗口**：`#locate .panel` 是 96vw × 94vh（以前 720px 宽，地图太小不好拖）。初始点优先用候选位置；没有候选就向 iframe 问当前视野中心（`photonmap:get-view` / `photonmap:view` 一问一答），再不行才退到某张已定位照片——**待确认照片的 lat/lon 是 0，绝不能直接拿来当起点**（之前会落到大西洋上的 0,0）。浮层标记用自绘的 `.pm-locate-pin` divIcon，不依赖 Leaflet 默认图标 png。
- **确认位置直接在主地图上改**：浮层不再自带地图，而是底部一条工具栏（`#locate .panel`，96vw 以内、可滚动），主地图里由 iframe 放一个可拖动的 `.pm-locate-pin`。协议：`photonmap:edit-start`（lat/lon，外加 `pan`＝工具栏高度一半，让标记不被面板挡住）/ `edit-move` / `edit-end`，iframe 拖动后回 `photonmap:edit-point`（含当前坐标基准 `system`）。坐标基准跟着主地图走（Google 不同图层都不一样），开浮层时先 `askMapView()` 取 `system` 再拉 `/api/photos?system=`。搜索：百度底图用 `BMap.PlaceSearch`，其余走服务端 `/api/geocode`（返回 WGS-84，落点前换算）。**坐标换算只有一份实现**：`photo_map.js` 里的 `PhotoMap.coord` 与 `photonmap/coords.py` 是同一套算法、常量逐字对齐。
- **确认浮层里点「确认位置」不需要先拖动标记**：直接取当前点写入。候选坐标由服务端按 `system` 换算过，前端直接用；只有 OSM 搜索结果需要前端换算（`toOverlay()`）。
- **侧栏照片点击 = 跳地图**：壳页面侧栏「照片」列表点一行，会用 `photonmap:goto` 把地图移到那张照片的位置（zoom 17），不再直接开灯箱；要开大图就点地图上的标记。
- **已定位的照片也能改位置**：侧边栏照片列表项上有「改位置」按钮（`photo_map.js` 的 `renderList()` 多一个 `onEdit` 回调，静态导出不用它），复用同一个浮层，初始点用照片当前的位置。浮层里还能**直接输入经纬度**（按当前底图基准解释），以及**清除定位**（`POST /api/locate/clear`，删旁车；若服务以 `--write-exif` 启动，还要从 `<workdir>/backups/` 还原被改写过的 EXIF）。
- **图上直接改**：单张照片的标记可以拖动（聚类代表标记不可拖），松手弹确认条，确认后写入；胶卷里可多选照片，点「改变位置」后整批移动到同一个点（复用定位浮层 + 批量接口）。
- **浮层里还有「AI 识别地名」**（可选功能，见下）：模型给出候选地名，点一个就走一次地理编码换成坐标。
- **写入要一次性令牌**：浮层打开时前端调 `POST /api/locate/open` 拿一枚**一次性令牌**（服务端签发、绑定照片 key、5 分钟过期），确认时必须带上，否则 409。令牌由 `server.TokenStore` 管理，用掉即作废。这样「浮层没打开却提交了坐标」的路径根本走不通。另有 `session` 校验挡掉服务重启前的旧页面。

### 规模化定位：时间簇 + 批量写入

一张一张点太慢，所以服务端把照片按拍摄时间**聚成簇**（`groups.py`），一簇一次处理。

- **分簇**（`groups.build_groups()`）：已定位与待确认的照片**混在一起**按时间排开（一次出行里的照片本来就交替有 GPS 没 GPS，分开排会把同一次出行切碎），相邻时间差 **> 2 小时**才另起一簇（恰好等于 2h 不切）；没有拍摄时间的照片单独归到「无时间」簇，不参与推断。
- **簇级推断**（`groups.infer()`），依次尝试：
  1. **时间插值**：待确认的照片夹在两张已定位照片之间 → 按时间插值（复用 `geo.interpolate_position()` 的限幅：跨度 ≤2h、距离 ≤50km），置信度「很可能」；
  2. **簇内中位数**：组内 ≥2 张已定位且彼此 ≤5km → 取中位数，置信度**降一档**（两张相距很远时中位数会落到无人区，这时不用中位数）；
  3. **最近邻**：组内有已定位照片 → 取时间最近的那张，置信度按时间差分档；
  4. **全库最近邻**：组内一张已定位照片都没有 → 退到全库里最近的那张（≤24h）。
  推不出来时没有猜测，这一簇需要人工给锚点。
- **待确认列表也能批量**：每行带勾选框，头部有「全选 / 所选同点 / 各自候选」。所选同点复用 `openLocateMany()`，各自候选复用 `applyKeysAssign()`（与分组面板同一套接口）。
- **侧栏只列「有待确认照片」的簇**：`renderGroups()` 过滤掉 `pending_count === 0` 的簇——它们的操作按钮全是禁用的，列出来只会淹没真正要处理的簇。「按时间分组 N」的数字也是过滤后的数量。
- **按地点（文件夹）合并**：分组头有「按地点合并」开关。待确认照片大多没有坐标（实测 2663 张里只有 15 张能推断），所以合并键优先用 **照片所在文件夹**（`/api/groups` 的 `folder` 字段，相对照片根目录）——同一相册基本就是同一地点；没有文件夹的再按推断坐标 300 米内归并。合并行的「整簇同点/各自候选」作用于这一组全部待确认照片。
- **点分组行展开缩略图**：分组行（不是按钮）可点开，展开该组全部照片的缩略图（待确认的描橙色边），点缩略图弹出壳页面的照片预览（大图 + 名称/时间/坐标，←/→ 翻页、Esc 关闭）。用来目视判断推断的位置是否靠谱。
- **手动合并所选**：勾选任意几行（可跨文件夹、跨时间）→「合并所选」，生成一条「手动合并 · N 个时间段」的组（`MANUAL_MERGES`，仅当前页面有效），它的「各自候选/整簇同点」作用于合并后的全部待确认照片；「取消合并」拆回原样。用于「不同文件夹其实拍在同一个地方」这种只有人知道的情况。
- **多簇一起处理**：分组头有「全选」和「所选同点」。前者把侧栏所有待确认簇一次勾上；后者把勾选簇里的待确认照片合并成一个 key 列表，直接调 `openLocateMany()`（和胶卷多选走同一条路：`/api/locate/open` 按 members 签令牌 → `/api/locate/batch` `mode= same`）。「应用所选」则是逐簇套用各自的候选位置（`assign`）。
- **批量写入**（`POST /api/locate/batch`）三种模式：`assign`（每张用各自的坐标）、`same`（整簇钉一点）、`shift`（整簇平移，保留组内相对位置）。批量写**只写旁车文件**，不动 EXIF。
- **批量令牌**：`POST /api/locate/open` 带 `{"group": "<簇 id>"}` 时，为簇里**每张**待确认照片各签一枚令牌（`{"ok":true,"tokens":{key:token}}`）。`TokenStore.check()` 只看不销，批量接口先把所有令牌验一遍，任何一张不合法就整批 409，一张都不写。
- **整簇平移的锚点**：请求里给 `anchor_key` 与 `target`；服务端取锚点照片当前的 WGS-84 坐标算位移 Δ，组内每张按「当前 + Δ」写。待确认照片本身没有位置，前端把它的候选位置一起发过来当平移起点。

### 地址搜索

- **`geocode.py`**：只依赖标准库。**数据源跟着当前选中的地图引擎走**（前端带 `engine=` 参数）：`amap` → 高德官方（配了 `AMAP_API_KEY` 时）→ 百度网页搜索 → Nominatim → Photon；`baidu` → 百度网页搜索 → Nominatim → Photon；`leaflet` / `google` → Nominatim → Photon。每个源 5s 超时、失败降级下一个。坐标一律折回 **WGS-84**（高德给的是 GCJ-02，百度网页接口给的是 BD-09 墨卡托，`baidu_mc_to_wgs84()` 负责换算）。Nominatim 按其使用政策带真实 User-Agent 且**每秒最多一次**请求。**高德没有免密钥的搜索接口**，所以没配 key 时用百度网页搜索兜底——国内 POI 覆盖比 OSM 好得多。
- **`GET /api/geocode?q=&limit=`** → `{"ok":true,"query":...,"source":...,"results":[{"label","detail","lat","lon","source"}]}`。放在服务端代理是为了统一限流与缓存，也免得把 key 暴露在浏览器里。
- **壳页面侧栏顶部**有搜索框，命中后 `postMessage({type:'photonmap:goto'})` 让当前引擎的地图跳过去（`leaflet.html` / `baidu_maps.html` 各自监听并按自己的底图基准换算）。

### AI 识别地名（可选）

- **`ai.py`** 是可选功能：核心依赖里没有 `anthropic`，`pip install -e ".[ai]"` 才装得上；没有凭据时整个功能降级（页面上的按钮隐藏，端点返回 501）。
- **模型只出地名，不出坐标**——模型给的坐标一定是编的。地名再交给 `geocode` 反查成坐标，与人工标定走同一条落地链路。
- **`POST /api/ai/locate` `{"key":...}`** → `{"ok":true,"suggestions":["断桥残雪","杭州西湖"]}`。图片走服务端已有的 2000px 转码（`encode_full_image`）→ base64。
- 模型用 `claude-opus-5-5`（可用 `PHOTONMAP_AI_MODEL` 覆盖），**不传 temperature**（2026-10-21 之后的模型传采样参数会 400），`output_config.effort` 用 `low`。

### 地图引擎

四个引擎，只有百度需要密钥：

| 引擎 | 底图 | 密钥 | 坐标基准 | 3D |
|---|---|---|---|---|
| Leaflet | `--tile` 配的瓦片（默认 OSM） | 不需要 | WGS-84 | 无 |
| Google Maps | 官方瓦片 `mt1.google.com/vt/lyrs=m\|s\|y`（路网/卫星/混合） | **不需要** | **逐层不同**（路网 GCJ-02 / 卫星·混合 WGS-84） | 无 |
| 高德地图 | 公开瓦片 `webrd0*.is.autonavi.com`（路网）/ `webst0*.is.autonavi.com`（卫星·混合） | **不需要** | GCJ-02（三层一致） | 无 |
| 百度地图 | 百度 JS API | **要 AK** | BD-09 | 无 |

- **壳页面默认打开高德**（`PREFERRED_ENGINE = 'amap'`，不可用才退回第一个可用引擎）；`AMAP_TILES` 把**混合**排在第一位，底图层初始化取 `baseNames[0]`，所以首屏就是高德混合底图。
- **Google / 高德都用 Leaflet 渲染**（`/map/google`、`/map/amap` 渲染的是 `leaflet.html`），底图由引擎自带（`ENGINES[...]["tiles"]`），`--tile` 配的底图只留给 Leaflet 引擎。多套底图时右上角出现 `L.control.layers` 切换控件。
- **每片底图各自声明坐标基准**（瓦片字典里的 `coord_system`）：Google 的**路网是 GCJ-02、卫星与混合是 WGS-84**（卫星影像没做偏移，实测与高德卫星正好差一个 GCJ-02 的量；混合图的格网跟着卫星走，它叠的路网注记是 Google 自带的错位，消不掉）。高德三层都是 GCJ-02。**切换底图时必须按新基准重新取一次照片坐标并重建标记**，否则标记还停在旧基准上（卫星底图上偏出几百米就是这么来的）。
- **高德的「混合」是两层瓦片**：`webst0{s} style=6` 的卫星影像 + `webst0{s} style=8` 的透明路网与中文标注，叠加后自洽。瓦片字典里的 `overlay` 字段描述这层，两层各自的 `subdomains` 都要配齐，否则 `{s}` 展开失败。
- **`--tile` 可以带基准**：`--tile "名称=URL@gcj02"`（`rsplit("@", 1)` 且只认 wgs84/gcj02/bd09 三个值，免得把 URL 里本来就有的 `@` 误当成基准）。
- **百度仍然是官方 JS API**（`baidu_maps.html`）：JS API 强制要 AK，官方也不再公开示例 AK；百度的底图瓦片主机（`maponline*.bdimg.com`）在部分网络下直接连不通，所以没 AK 时宁可置灰也不要糊弄。壳页面右上角的齿轮里可以填 AK（存浏览器 localStorage）。
- 静态导出仍用 `google_maps.html`（需用户自己的 `GOOGLE_MAPS_API_KEY`）；serve 模式的 Google 引擎已经不需要 key 了。

各引擎能力不同（查过官方文档）：

| 引擎 | 图层 | 3D |
|---|---|---|
| Leaflet | `L.control.layers`，底图来自 `--tile` 配置 | **无**（1.9.4 没有倾斜视角），不显示按钮 |
| Google（静态导出用的 JS API） | `google.maps.MapTypeControl`（ROADMAP/SATELLITE/HYBRID/TERRAIN），方法名是 `setMapTypeId()` 不是 `setMapType()` | `setTilt()` 上限 67.5°、`setHeading()`。**注意 `fitBounds()` 会把 tilt/heading 清零**，每次 fit 完要重新应用 |
| 百度 | `BMap.MapTypeControl`（普通/卫星/混合），**要先 `map.setCurrentCity()` 控件才出现** | v3.0 **没有** `setTilt`（那是 JS API GL 的能力），所以用特性探测，探测不到就不显示按钮 |

### 页面与前端集成

三套 SDK 用 **iframe 隔离**（同源，postMessage 通信），互不污染。壳页面与 iframe 之间的消息：`photonmap:pending-click`（iframe 里点了「?」标记 → 壳页面打开确认浮层）、`photonmap:open-photo`（预留，当前壳页面不再发送；侧栏点照片改成发 `photonmap:goto` 直接跳到该照片在地图上的位置）、`photonmap:goto`（壳页面搜索到地址 → iframe 跳过去）、`photonmap:move-photos`（iframe 胶卷里多选照片 → 壳页面打开「移动照片位置」浮层）、`photonmap:drag-move`（iframe 里把单张照片标记拖动后点保存 → 壳页面签发令牌并写入）、`photonmap:move-result`（壳页面把写入结果回给 iframe，失败时恢复标记原位置）。

**侧边栏宽度可拖、单击吸边**：中间的分隔条（`#splitter`）拖动改 `--side-w`，宽度记在 localStorage；**单击分割条**（位移 < 3px 视为单击）把 `body.side-collapsed` 打开，`--side-w` 归零、地图铺满，再单击恢复上次宽度。注意 `#mapframe` 是替换元素，宽度必须自己算（`width: calc(100% - var(--side-w) - 6px)`），给 `width:auto` 会退回 300px 的固有尺寸、给 `width:100%` 又会叠上 `left` 造成横向溢出。

**三个地图模板同时支持静态导出与 live 模式**，靠 `options.live` 分支。模板变量：`asset_prefix`（静态导出 `files`、服务 `/files`，因为页面在 `/map/xxx` 下相对路径会解析错）、`coord_system`、`tiles`、`pending`、`version`、`poll_ms`。

`server.py` 的要点：

- **`PhotoLibrary`**：按 `(mtime, size, 旁车文件 mtime)` 判断文件是否变化——**旁车文件也要算进去**，用户确认位置后只写旁车文件、原图没动，不算的话缓存不会失效。只有新增/修改的文件才重新解析 EXIF、重新生成派生资源。派生资源（缩略图、标记图）用**路径哈希做文件名**（`asset_key()`），不能用序号——照片会插进插出，序号命名会让已有资源的路径漂移。解析结果另存一份 `<workdir>/index.json`（同样以 stamp 判定失效），第二次打开同一目录直接复用，不再逐张读 EXIF。**需要重新生成缩略图的照片走线程池**（Pillow 的解码/编码在 C 层，能真吃满多核，48 张 HEIC 冷启动 42s → 4s）；`asset_key()` 有进程内缓存（`resolve()` 在云盘目录上是一次真实系统调用）。
- **照片分两类**：**已定位**（EXIF 或旁车文件里有坐标）与**待确认**（两者都没有）。`--location` / `--locations` 给了兜底坐标时，那些照片直接算已定位；没给才进待确认队列。
- **大图不落盘**：`/files/full/<key>` 由服务端按需转码成 JPEG（最长边 2000px，带 EXIF 方向校正），带一个内存缓存：**32 项 + 64MB 总字节双封顶**（只数条目不数体积的话，32 张 2000px 大图能吃几十 MB）。HEIC 也能显示。
- **原图走接口**：`/api/original/<key>` 直接回传原文件。浏览器不允许 http 页面跳 `file://`，所以服务模式下不能用 `file:` URI。
- **静态资源**：`photo_map.js` / `photo_map.css` / `vendor/`（Leaflet）复制到工作目录，默认在 `%LOCALAPPDATA%\photonmap\<hash>`（Linux/macOS 是 `~/.cache/photonmap/<hash>`），可用 `--workdir` 改。
- **目录穿越防护**：静态文件必须解析后仍位于资源目录内，否则 404。
- **Host / Origin / Sec-Fetch-Site 校验**（`_request_allowed()`，防 DNS rebinding 与跨站提交）：`Sec-Fetch-Site: cross-site` 一律拒（不管有没有 `Origin`）；`Host` 只认 `127.0.0.1 / localhost / [::1]`，`--host 0.0.0.0` 暴露到局域网时额外允许 **IP 字面量**（rebinding 用的是域名，所以域名一律拒）；带 `Origin` 的请求必须同源（同样只认本机或 IP 字面量、端口要一致）。非回环监听会打印警告。
- **POST 只收 `application/json`**：`_read_json()` 其他类型回 415，挡掉跨站「简单请求」。`/api/ai/locate` 也补上了 `session` 校验。
- **安全响应头**：所有响应带 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options`；壳页面 `frame-ancestors 'none'`，地图页 `frame-ancestors 'self'`（要能被壳页面嵌）。
- **不向不透明 origin 发敏感消息**：iframe 统一用 `postToParent()` 发消息——父窗口就是自己（静态导出直接打开）或页面是 `file://`（origin 为不透明）时**直接不发**，免得坐标/照片 key 落到任意嵌入者手里；静态导出也不响应 `edit-start/move/end`（函数开头 `if (!LIVE) return;`）。
- **定位会话的坐标基准固定**：打开浮层时把 `overlayEngine` 定死；iframe 回传的 `edit-point` 若带着不同的 `system`（底图中途换层，Google 各层基准不同），用 `PhotoMap.coord.convert()` 换算到会话基准，而不是把同一串数字按新基准重新解释。
- **页面间消息只认同源**：壳页面与 iframe 的 `message` 监听都先校验 `event.origin === window.location.origin`（壳页面还要求 `event.source === frame.contentWindow`），`postMessage` 的目标 origin 用 `window.location.origin`（静态导出走 `file://` 时退回 `'*'`）。
- **响应按需 gzip**：`_send_bytes()` 只在体积 ≥1KB **且是文本类**（json/html/js/css/svg）时压缩。`/api/photos` 在 4000 张照片时约 2.2MB，压缩后只要 200KB 左右；JPEG/PNG 本身已压过，再压是白烧 CPU。
- **listen backlog 128**：`LiveServer.request_queue_size`。默认的 5 在页面上千张图同时请求时会直接拒掉新连接（浏览器报 `ERR_CONNECTION_REFUSED`）。
- **分组结果按版本缓存**：`groups()` 里 `build_groups`+`infer` 在几千张照片时要好几秒，版本号没变就直接复用（副本返回，外部改动不影响缓存）。
- **坐标基准随引擎变**：`/api/photos?system=` 按查询参数换算坐标（百度 BD-09、Google 与高德 GCJ-02、Leaflet WGS-84）。候选位置（`candidates` / `candidate`）也一起换算。

### 坐标系：WGS-84 是唯一真相

`PhotoRecord.lat/lon` **永远存 WGS-84**。GCJ-02 / BD-09 的转换只发生在展示时（`PhotoRecord.coordinates(system)` → `photonmap/coords.py`）：导出器用 `coord_system` 类属性，`serve` 用 `--coord-system`。新增导出器时不要在读取阶段做偏移，也不要把偏移结果写回 `PhotoRecord`。

**手机照片的 GPS 是 WGS-84**：iPhone 相机写进 EXIF 的是原始 GNSS 坐标；安卓卫星定位同样是 WGS-84，只有走网络/基站定位（国内 ROM 通常已返回 GCJ-02）或某些第三方相机 App 才可能写入加密坐标。所以默认底图应当选 WGS-84 基准的（OSM 瓦片、离线瓦片、GeoJSON、GPS 轨迹图、正射影像），照片零转换即可对齐；接高德/腾讯/百度瓦片时才需要 `--coord-system gcj02` / `bd09`。

`coords.py` 的要点：
- 三套基准：`wgs84` / `gcj02` / `bd09`，`convert(lat, lon, target, source="wgs84")` 是统一入口，内部是一张 3×3 的 `(source, target)` 分派表。
- GCJ-02 加密只在中国境内生效。判定用 `_CN_LON` / `_CN_LAT` 包围盒，**境外坐标原样返回**。`gcj02_to_bd09` / `bd09_to_gcj02` 同样有境外短路，否则 BD-09 会给东京这种境外坐标加上偏移，且 WGS-84↔BD-09 不再可逆。
- GCJ-02 → WGS-84 没有解析逆解，用 30 步迭代逼近。
- BD-09 的偏移常量不能互换：正向 `bdLat = z·sinθ + 0.006`、`bdLon = z·cosθ + 0.0065`，逆向从 lon 减 `0.0065`、从 lat 减 `0.006`。写反会让正逆变换不对称，往返误差可达几十米。

### EXIF 解析

`exif_reader.py`：优先 Pillow，失败回退 `piexif`；`pillow_heif.register_heif_opener()` 支持 iPhone 的 HEIC。

- DMS 是三元组，每项是 `(分子, 分母)` 的有理数对，需要**相除**后再按 度/60/3600 累加。N/S/E/W 决定符号。
- `read_exif()` 不抛异常，坏文件返回空结果；`read_photo()` 无 GPS 且没有 fallback 时返回 `None`。
- 拍摄时间优先 `DateTimeOriginal`，缺失时回退 `GPSDateStamp` + `GPSTimeStamp`，再回退 `filename_timestamp()`（从文件名里的 `2026-10-01 100701` / `20261001_120000` 还原拍摄时间——相册重新导出会丢掉 EXIF 时间）。
- `read_photo(path, fallback=(lat, lon))`：EXIF 没有 GPS 时用 fallback 顶上。

### 各家相机的容器格式

`imaging.py` 提供统一的打开入口 `open_image(path)`，上层（EXIF 读取、缩略图、大图）都走它，别再直接 `Image.open()`：

- `.jpg` / `.heic` 等：原样交给 Pillow。
- **`.livp`**（vivo 的 Live Photo）：本质是 zip 容器，里面是 `XXX.HEIC.heic` + `XXX.HEIC.mov`。`extract_livp_image()` 取出内嵌的静态图读进内存再打开——静态图自带 EXIF，所以 GPS 能正常读出来。
- **iPhone 的 `.heic` 其实是 MPO**（JPEG 基底的 Live Photo），Pillow 报 `format=MPO`。判断格式要看内容不能看扩展名。

派生资源的写入是**原子的**（先写 `.part` 再改名，`thumbnail._save_atomic()`）：云盘同步可能留下半截文件，若直接写目标路径，坏文件会被当成成品永久缓存。服务端遇到解码失败的照片直接跳过，不显示破图。

**写文件要能扛住同步软件的锁定**：OneDrive / 百度云会在写入或改名时短暂占住文件，Windows 上报 `PermissionError`。`fsutil.replace_with_retry()` / `copy_with_retry()` 会退避重试几次。用户的照片目录往往就在 OneDrive 里，导出目录也可能在那儿——没有重试的话导出会随机失败。

### 导出器

详见 `photonmap/exporters/CLAUDE.md`（处理该目录下的文件时自动加载）——`BaseExporter` 契约、KML/GeoJSON/HTML 三种产物各自的坑都在那边。

### HTML 前端：地图标记 + 聚合 + 胶卷 + 灯箱

详见 `photonmap/templates/CLAUDE.md`（处理该目录下的文件时自动加载）——像素聚类、三套地图 adapter 契约、灯箱/胶卷的 Esc 分层、动态替换照片列表的约束都在那边。

### 缩略图

目录扫描在 `scanner.py`，用 `os.scandir` 手动遍历（`Path.glob("**/*")` 在上万个目录时要慢 3 倍以上，而且每个条目还要额外 `is_file()` stat 一次）。

`thumbnail.ensure_thumbnails(photos, assets_dir)` 统一生成缩略图并回写 `PhotoRecord.thumbnail`，文件名为 `{序号:04d}_{安全文件名}.jpg`。HTML / KML / KMZ 都复用这个目录，靠相对路径互链。

HTML 导出器另有 `files/markers/`（地图标记用的小图）和 `files/full/`（灯箱大图），`PhotoRecord.marker` 记录标记图路径。

`serve` 模式不走这套序号命名：照片会随时增删，序号一变已有资源的路径就漂移了。`server.asset_key()` 用**路径哈希**做前缀，目录里少了一张照片不会影响其它照片的资源名。

### CLI

`photonmap/cli.py` 用 argparse，四个子命令 `scan` / `export` / `serve` / `locate`。`--format` 可重复指定，默认 `kml`。可选值：`kml`、`geojson`、`leaflet`、`google`、`baidu`。

密钥不写进命令行，走环境变量：`GOOGLE_MAPS_API_KEY`、`BAIDU_MAP_AK`（也可用 `--google-key` / `--baidu-ak` 覆盖）。

没有 GPS 的照片用 `--location 纬度,经度` 统一补一个点，或 `--locations coords.csv` 逐张指定（格式 `文件名,纬度,经度`，表头会被跳过）。优先级：EXIF GPS > CSV 按文件名 > `--location`。三个参数 `scan` / `export` / `serve` 都支持。实现在 `photonmap/locations.py`。

`locate` 子命令把「规模化定位置」做成可脚本化的（`--apply-candidates` / `--set` / `--clear`，`--files` 按文件名匹配、`--all` 是全部照片、`--dry-run` 只打印）：同样只写旁车文件，原图不动。

`serve` 的底图完全由参数决定：`--tile 名称=URL` 可重复指定多套底图（页面右上角出现切换控件），`--tiles URL` 是单套的简写，不给就用默认的 OpenStreetMap 瓦片。瓦片模板沿用 Leaflet 语法（`{z}` / `{x}` / `{y}` / `{s}`），服务商若要 key 直接写在 URL 里。其余选项（`--tile-max-zoom`、`--attribution`、`--host`、`--port`、轮询间隔、`--workdir`、`--coord-system`、`--no-recursive`、`--no-open`、`--write-exif`、密钥覆盖等）见 `--help`。

Google / 百度引擎在 `serve` 里读 `GOOGLE_MAPS_API_KEY` / `BAIDU_MAP_AK` 环境变量（或 `--google-key` / `--baidu-ak`）。**serve 模式的 Google 引擎已改用官方瓦片，不需要密钥**（`GOOGLE_MAPS_API_KEY` 只影响静态导出）；百度仍然要 AK，缺 AK 时壳页面上的百度 Tab 置灰，在右上角齿轮里填也行。Leaflet 与高德始终可用。

退出码：`0` 成功，`1` 没找到带 GPS 的照片，`2` 路径不存在。

## 测试

`tests/` 用 pytest，`testpaths = ["tests"]`。`tests/conftest.py` 提供两个东西：

- `write_gps_photo(path, lat, lon, taken_at=None, altitude=None)`：用 piexif 写一张带 GPS EXIF 的 JPEG，测试样例照片都靠它生成。
- `photo_dir` fixture：一个含 3 张带 GPS 照片 + 1 张无 GPS 照片的目录。

写新测试时注意 DMS 编码：秒要用 `round()` 取整并写成有理数对 `(分子, 分母)`，浮点截断会产生 1e-5 量级的误差让断言失败。

`conftest.py` 提供 `write_gps_photo`（带 GPS）和 `write_timed_photo`（只有时间没有 GPS），可以直接 `from conftest import write_gps_photo`（`tests/` 不是包，用相对导入会失败）。
