# PhotonMap

把带 GPS 信息的照片变成可以在地图上浏览的东西：一次导出成 KML / KMZ / GeoJSON / HTML，
或者起一个本地服务实时盯着一个照片目录。没有 GPS 的照片会按同目录其它照片的拍摄时间
推断出候选位置，人工确认后写回坐标。

纯 Python 命令行工具，不依赖数据库；地图底图只用到瓦片 URL，不调用任何地图服务商的 JS API（百度除外，需要自己的 AK）。

## 功能

- **导出**：KML / KMZ / GeoJSON / Leaflet 网页 / Google Maps 网页 / 百度地图网页
- **本地服务**（`serve`）：浏览器里实时看照片目录，往目录里丢照片会自动出现
- **补录定位**：按拍摄时间推断候选位置，支持逐张确认、批量定位、时间簇推断、拖动标记改位置、地图上多选后整批移动
- **多地图引擎**：Leaflet / Google Maps / 高德（免密钥） / 百度（需 AK），支持底图切换与「照片标记」显隐
- **多格式**：JPEG / PNG / HEIC / WebP / TIFF / vivo `.livp`（Live Photo 容器）

## 安装

需要 Python 3.10+（开发用的版本是 3.14）。

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# 或：.venv/bin/python -m pip install -e ".[dev]"      # Linux / macOS
```

可选：`pip install -e ".[ai]"` 装上 AI 识别地名（用视觉模型猜照片大概在哪儿，只是提供候选地名）。

## 快速开始

```bash
# 扫一眼目录里有多少带 GPS 的照片
python -m photonmap.cli scan <照片目录>

# 导出成网页地图
python -m photonmap.cli export <照片目录> -o out -f leaflet -f kml

# 起本地服务，浏览器里实时查看
python -m photonmap.cli serve <照片目录> --port 8765

# 批量补位置：按各自候选 / 钉到指定点 / 清除
python -m photonmap.cli locate <照片目录> --apply-candidates
python -m photonmap.cli locate <照片目录> --set 30.1,120.2 --files a.jpg,b.jpg
python -m photonmap.cli locate <照片目录> --clear --all --dry-run
```

## 桌面启动器（可选）

```bash
# 双击版入口：不给目录就弹文件夹选择框，起服务后自动打开浏览器
python -m photonmap.launcher [照片目录]

# 打包成单文件 exe（Windows，需要 PyInstaller）
./build_exe.ps1      # 产出 dist/PhotonMap.exe
```

## 使用小贴士

- **默认打开高德混合底图**，右上角图层控件可以切路网 / 卫星 / 混合，也能勾掉「照片标记」只看底图
- 侧栏「按时间分组」里，**待确认 0 张的簇会自动隐藏**；可以「按地点合并」（按文件夹归并）、「全选」、「合并所选」、「所选同点」
- 点分组行可以展开缩略图看照片，点缩略图看大图，用来判断推断位置准不准
- 地图上**单张照片的标记可以拖动**，松手确认；聚在一起的点放大到 15 级以上会自动散开
- 侧栏「照片」列表点一行 = 地图跳到那张照片的位置

## 目录结构

```
photonmap/          核心包（扫描、EXIF、坐标、定位推断、服务、导出器）
photonmap/templates 前端页面（壳页面 + 三套地图模板 + Leaflet vendor）
tests/              pytest 测试
packaging/          PyInstaller 打包配置
build_exe.ps1       Windows 一键打包脚本
```

## 测试

```bash
python -m pytest -q
```

## 说明

- 照片坐标一律以 **WGS-84** 存储，只在展示时按底图基准换算成 GCJ-02 / BD-09
- 人工补录默认只写旁车文件 `<照片名>.photonmap.json`，原图不动；加 `--write-exif` 才会改写 EXIF（改前自动备份）
- 仓库暂未附带开源许可证，如需引用或分发请先联系作者
