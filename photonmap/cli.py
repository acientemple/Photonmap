"""命令行入口。

    photonmap scan   <目录>
    photonmap export <目录> -o 输出目录 [--format kml geojson leaflet google baidu]
    photonmap serve  <目录> [--tile 名称=URL]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__, coords
from .exif_reader import filename_timestamp, read_exif, read_photo
from .geo import prepare_timeline, suggest_sorted
from .locate import read_sidecar, remove_sidecar, write_sidecar
from .locations import load_locations, parse_location
from .models import PhotoRecord
from .scanner import scan_directory

log = logging.getLogger("photonmap")

FORMATS = ("kml", "geojson", "leaflet", "google", "baidu")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def collect_photos(
    src: Path,
    recursive: bool = True,
    default_location: tuple[float, float] | None = None,
    location_table: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[PhotoRecord], int]:
    """扫描目录并解析照片，返回 (照片列表, 跳过数量)。

    EXIF 没有 GPS 的照片，优先用 location_table 里按文件名给的坐标，
    其次用 default_location；都没有则跳过。
    """
    files = scan_directory(src, recursive=recursive)
    photos: list[PhotoRecord] = []
    skipped = 0
    for path in files:
        fallback = (location_table or {}).get(path.name) or default_location
        try:
            record = read_photo(path, fallback=fallback)
        except Exception as exc:
            log.warning("读取失败 %s: %s", path, exc)
            skipped += 1
            continue
        if record is None:
            skipped += 1
            continue
        photos.append(record)
    return photos, skipped


def _resolve_locations(args) -> tuple[tuple[float, float] | None, dict]:
    """把 --location / --locations 解析成 (默认坐标, 按文件名的坐标表)。"""
    default_location = None
    if getattr(args, "location", None):
        default_location = parse_location(args.location)
    table = load_locations(args.locations) if getattr(args, "locations", None) else {}
    return default_location, table


def cmd_scan(args) -> int:
    default_location, table = _resolve_locations(args)
    photos, skipped = collect_photos(
        Path(args.source), not args.no_recursive, default_location, table
    )
    if not photos:
        print(f"没有找到带 GPS 信息的照片（共检查 {skipped} 个文件）")
        print("提示：照片被相册重新导出后可能丢失定位，可用 --location 纬度,经度 手动指定位置")
        return 1

    print(f"找到 {len(photos)} 张带 GPS 的照片，跳过 {skipped} 个")
    for photo in photos:
        lat, lon = photo.coordinates(args.coord_system)
        extra = f"  {photo.taken_at:%Y-%m-%d %H:%M:%S}" if photo.taken_at else ""
        print(f"  {photo.name:<32} {lat:10.6f}, {lon:11.6f}{extra}")
    return 0


def _build_exporters(args) -> list:
    from .exporters import (
        BaiduMapExporter,
        GeoJsonExporter,
        GoogleMapsExporter,
        KmlExporter,
        LeafletExporter,
    )

    exporters = []
    for fmt in args.format:
        if fmt == "kml":
            exporters.append(
                KmlExporter(group_by_date=not args.no_group_by_date, with_route=not args.no_route)
            )
        elif fmt == "geojson":
            exporters.append(GeoJsonExporter(embed_thumbnails=args.embed_thumbnails))
        elif fmt == "leaflet":
            exporters.append(LeafletExporter())
        elif fmt == "google":
            exporters.append(GoogleMapsExporter(api_key=args.google_key))
        elif fmt == "baidu":
            exporters.append(BaiduMapExporter(ak=args.baidu_ak))
    return exporters


def cmd_export(args) -> int:
    src = Path(args.source)
    out_dir = Path(args.output)
    default_location, table = _resolve_locations(args)
    photos, skipped = collect_photos(src, not args.no_recursive, default_location, table)
    if not photos:
        print(f"没有找到带 GPS 信息的照片（共检查 {skipped} 个文件）", file=sys.stderr)
        print(
            "提示：照片被相册重新导出后可能丢失定位，可用 --location 纬度,经度 手动指定位置",
            file=sys.stderr,
        )
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for exporter in _build_exporters(args):
        try:
            path = exporter.export(photos, out_dir)
        except Exception as exc:
            log.error("导出 %s 失败: %s", exporter.name, exc)
            continue
        print(f"  {exporter.name:<12} → {path}")

    suffix = f"，跳过 {skipped} 个无位置的文件" if skipped else ""
    print(f"共导出 {len(photos)} 张照片{suffix}")
    return 0


# 不带 --tile 时的默认底图
DEFAULT_TILE = {
    "name": "OpenStreetMap",
    "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": "&copy; OpenStreetMap contributors",
    "coord_system": "wgs84",
}


def _split_system(url: str) -> tuple[str, str]:
    """从 URL 末尾拆出可选的坐标基准：--tile "名称=URL@gcj02"。

    只认 wgs84/gcj02/bd09 三个值，免得把 URL 里本来就有的 @ 误当成基准。
    """
    head, sep, tail = url.rpartition("@")
    if sep and tail in coords.SYSTEMS:
        return head, tail
    return url, "wgs84"


def _build_tiles(args) -> list[dict]:
    """把 --tile / --tiles 解析成底图配置列表。"""
    tiles: list[dict] = []
    for spec in getattr(args, "tile", None) or []:
        name, sep, url = spec.partition("=")
        if not sep:
            name, url = "底图", name
        url, system = _split_system(url.strip())
        tiles.append(
            {
                "name": name.strip() or "底图",
                "url": url,
                "attribution": args.attribution or "",
                "maxZoom": args.tile_max_zoom,
                "coord_system": system,
            }
        )

    if getattr(args, "tiles", None):
        url, system = _split_system(args.tiles.strip())
        tiles.append(
            {
                "name": "底图",
                "url": url,
                "attribution": args.attribution or "",
                "maxZoom": args.tile_max_zoom,
                "coord_system": system,
            }
        )

    if not tiles:
        default = dict(DEFAULT_TILE)
        default["maxZoom"] = args.tile_max_zoom
        tiles.append(default)
    return tiles


def cmd_locate(args) -> int:
    """批量确定或修改照片位置。

    三种动作：
    - ``--apply-candidates``：按各自的最佳候选给没有 GPS 的照片补位置；
    - ``--set 纬度,经度``：把选中的照片钉在指定的点；
    - ``--clear``：删掉选中的照片的人工定位（退回相机 GPS 或待确认）。

    写的都是旁车文件，原图一个字节不动。
    """
    src = Path(args.source)
    if not src.exists():
        print(f"错误: 目录不存在: {src}", file=sys.stderr)
        return 2

    files = scan_directory(src, recursive=not args.no_recursive)
    if not files:
        print("目录里没有照片")
        return 1

    dry_run = bool(args.dry_run)

    if not (args.apply_candidates or args.set or args.clear):
        print(
            "错误: 至少要指定一种动作：--apply-candidates / --set / --clear",
            file=sys.stderr,
        )
        return 2

    if args.apply_candidates:
        # --apply-candidates 不指定范围时，默认处理目录里的全部照片
        selected = _select_files(files, args.files, args.all or not args.files)
        if not selected:
            print("没有匹配到照片", file=sys.stderr)
            return 1
        # 推断的**参照**要用全库的已定位照片——只选一张照片不代表
        # 其它照片就不该拿来当锚点；--files 只限制写哪些照片。
        located, pending = _split_records(files)
        wanted = {path for path in selected}
        timeline = prepare_timeline(located)
        written = 0
        for photo in pending:
            if photo.path not in wanted:
                continue
            candidates = suggest_sorted(timeline, photo)
            if not candidates:
                print(f"  {photo.name:<32} 没有可推断的候选，跳过")
                continue
            best = candidates[0]
            if not dry_run:
                write_sidecar(photo.path, best.lat, best.lon)
            print(
                f"  {photo.name:<32} → {best.lat:10.6f}, {best.lon:11.6f}"
                f"（{best.confidence}）{'  [试运行]' if dry_run else ''}"
            )
            written += 1
        if not written:
            print("没有可以补上位置的照片")
            return 1
        print(f"共处理 {written} 张" + ("（试运行，没有写盘）" if dry_run else ""))
        return 0

    selected = _select_files(files, args.files, args.all)
    if not selected:
        print("没有匹配到照片", file=sys.stderr)
        return 1

    if args.clear:
        removed = 0
        for path in selected:
            if dry_run:
                # 试运行只看不写：真删了文件，用户却以为没动
                print(f"  将清除位置：{path.name}")
                continue
            if remove_sidecar(path):
                removed += 1
                print(f"  已清除位置：{path.name}")
            else:
                print(f"  {path.name:<32} 本来就没有人工定位")
        if dry_run:
            print("（试运行，没有写盘）")
        else:
            print(f"共清除 {removed} 张的人工定位")
        return 0

    lat, lon = parse_location(args.set)
    for path in selected:
        if not dry_run:
            write_sidecar(path, lat, lon)
        print(f"  {path.name:<32} → {lat:10.6f}, {lon:11.6f}")
    print(f"共处理 {len(selected)} 张" + ("（试运行，没有写盘）" if dry_run else ""))
    return 0


def _split_records(files: list[Path]) -> tuple[list[PhotoRecord], list[PhotoRecord]]:
    """把照片分成已定位与待确认两堆。

    判定口径要和 `server.PhotoLibrary._read_record()` 一致：
    **旁车文件优先于 EXIF**——人工标定过的位置不能被当成「没有位置」，
    否则 --apply-candidates 会把人工标定的位置覆盖成推断值。
    """
    located: list[PhotoRecord] = []
    pending: list[PhotoRecord] = []
    for path in files:
        sidecar = read_sidecar(path)
        if sidecar is not None:
            exif = read_exif(path)
            located.append(
                PhotoRecord(
                    path=path,
                    lat=sidecar[0],
                    lon=sidecar[1],
                    altitude=exif.altitude,
                    taken_at=exif.taken_at or filename_timestamp(path),
                    source="sidecar",
                )
            )
            continue
        record = read_photo(path)
        if record is not None:
            located.append(record)
            continue
        exif = read_exif(path)
        pending.append(
            PhotoRecord(
                path=path,
                lat=0.0,
                lon=0.0,
                altitude=exif.altitude,
                taken_at=exif.taken_at or filename_timestamp(path),
                source="none",
            )
        )
    return located, pending


def _select_files(files: list[Path], names: str | None, everything: bool) -> list[Path]:
    """按 --files（逗号分隔的文件名）或 --all 选出照片。"""
    if everything:
        return list(files)
    wanted = {part.strip() for part in (names or "").replace("，", ",").split(",") if part.strip()}
    if not wanted:
        return []
    return [path for path in files if path.name in wanted]


def cmd_serve(args) -> int:
    if not args.tile_max_zoom:
        args.tile_max_zoom = 18
    default_location, table = _resolve_locations(args)
    tiles = _build_tiles(args)

    from .server import serve

    try:
        serve(
            Path(args.source),
            tiles=tiles,
            workdir=Path(args.workdir) if args.workdir else None,
            host=args.host,
            port=args.port,
            coord_system=args.coord_system,
            recursive=not args.no_recursive,
            default_location=default_location,
            location_table=table,
            poll_ms=int(args.interval * 1000),
            api_key=args.google_key or os.environ.get("GOOGLE_MAPS_API_KEY"),
            baidu_ak=args.baidu_ak or os.environ.get("BAIDU_MAP_AK"),
            amap_key=args.amap_key or os.environ.get("AMAP_API_KEY"),
            write_exif=args.write_exif,
            open_browser=not args.no_open,
        )
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photonmap",
        description="把带 GPS 信息的照片导出成 Google Earth / Google Maps / 百度地图能看的文件",
    )
    parser.add_argument("--version", action="version", version=f"photonmap {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描目录，列出带 GPS 的照片及其坐标")
    p_scan.add_argument("source", help="照片目录")
    p_scan.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p_scan.add_argument(
        "--coord-system",
        default="wgs84",
        choices=list(coords.SYSTEMS),
        help="显示时使用的坐标系（默认 wgs84）",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_export = sub.add_parser("export", help="导出为地图可读的文件")
    p_export.add_argument("source", help="照片目录")
    p_export.add_argument("-o", "--output", required=True, help="输出目录")
    p_export.add_argument(
        "-f",
        "--format",
        action="append",
        choices=list(FORMATS),
        help="导出格式，可重复指定（默认 kml）",
    )
    p_export.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p_export.add_argument("--no-group-by-date", action="store_true", help="KML 不按日期分组")
    p_export.add_argument("--no-route", action="store_true", help="KML 不画拍摄轨迹")
    p_export.add_argument(
        "--embed-thumbnails", action="store_true", help="GeoJSON 内嵌缩略图（文件会变大）"
    )
    p_export.add_argument("--google-key", default=None, help="Google Maps API Key（默认读环境变量）")
    p_export.add_argument("--baidu-ak", default=None, help="百度地图 AK（默认读环境变量）")
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser("serve", help="启动本地服务，实时显示目录里的照片")
    p_serve.add_argument("source", help="照片目录")
    p_serve.add_argument(
        "--tile",
        action="append",
        metavar="名称=URL",
        help=(
            "瓦片底图，形如 名称=https://.../{z}/{x}/{y}.png，可重复指定；"
            "URL 末尾可加 @wgs84/@gcj02/@bd09 指定该底图的坐标基准（默认 wgs84）"
        ),
    )
    p_serve.add_argument(
        "--tiles",
        metavar="URL",
        help="单个瓦片 URL 模板（等价于 --tile 底图=URL），末尾同样可加 @基准",
    )
    p_serve.add_argument(
        "--tile-max-zoom", type=int, default=18, help="瓦片的最大缩放层级（默认 18）"
    )
    p_serve.add_argument("--attribution", default="", help="底图版权署名")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p_serve.add_argument(
        "--interval", type=float, default=2.0, help="目录轮询间隔秒数（默认 2）"
    )
    p_serve.add_argument("--workdir", default=None, help="缩略图等派生资源的存放目录")
    p_serve.add_argument(
        "--coord-system",
        default="wgs84",
        choices=list(coords.SYSTEMS),
        help="底图使用的坐标系（默认 wgs84）",
    )
    p_serve.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p_serve.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    p_serve.add_argument(
        "--write-exif",
        action="store_true",
        help="确认位置时改写原文件的 EXIF（先备份到工作目录的 backups/）",
    )
    p_serve.add_argument("--google-key", default=None, help="Google Maps API Key（默认读环境变量）")
    p_serve.add_argument("--baidu-ak", default=None, help="百度地图 AK（默认读环境变量）")
    p_serve.add_argument(
        "--amap-key",
        default=None,
        help="高德 Web 服务 key（默认读环境变量 AMAP_API_KEY）；给了它地址搜索在境内更准",
    )
    for p in (p_scan, p_export, p_serve):
        p.add_argument(
            "--location",
            default=None,
            metavar="LAT,LON",
            help="给没有 GPS 的照片使用的默认坐标，例如 31.2304,121.4737",
        )
        p.add_argument(
            "--locations",
            default=None,
            metavar="CSV",
            help="逐张指定坐标的 CSV（文件名,纬度,经度），优先于 --location",
        )
    p_serve.set_defaults(func=cmd_serve)

    p_locate = sub.add_parser("locate", help="批量确定或修改照片位置")
    p_locate.add_argument("source", help="照片目录")
    p_locate.add_argument(
        "--apply-candidates",
        action="store_true",
        help="按各自的最佳候选给没有 GPS 的照片补上位置",
    )
    p_locate.add_argument(
        "--set",
        dest="set",
        default=None,
        metavar="LAT,LON",
        help="把选中的照片钉在指定的点，例如 --set 30.1,120.2",
    )
    p_locate.add_argument(
        "--clear",
        action="store_true",
        help="清除选中的照片的人工定位（退回相机 GPS 或待确认）",
    )
    p_locate.add_argument(
        "--files",
        default=None,
        metavar="A.JPG,B.JPG",
        help="逗号分隔的文件名，指定要处理哪些照片",
    )
    p_locate.add_argument("--all", action="store_true", help="处理目录里的所有照片")
    p_locate.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要做什么，不写盘",
    )
    p_locate.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p_locate.set_defaults(func=cmd_locate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if getattr(args, "format", None) is None:
        args.format = ["kml"]
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
