"""常驻服务：盯着一个照片目录，用自选的瓦片底图把照片实时显示在地图上。

与导出器共用同一套前端逻辑（聚类 / 胶卷 / 灯箱），差别只在于
照片列表由服务端按需推送，而不是内嵌在 HTML 里。

底图只是一条瓦片 URL 模板，不调用任何地图服务商的 JS API；
照片坐标与底图同为 WGS-84，直接对齐。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import json
import logging
import os
import shutil
import threading
import time
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from PIL import Image, ImageOps
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import coords
from .exif_reader import filename_timestamp, read_exif, read_photo
from .exporters.html_base import COORD_LABELS, TEMPLATE_DIR, photo_row
from .fsutil import copy_with_retry, write_text_atomic
from .geocode import Geocoder
from .geo import prepare_timeline, suggest_sorted
from .groups import build_groups, infer
from .imaging import open_image
from .locate import (
    read_sidecar,
    remove_sidecar,
    restore_backup,
    sidecar_path,
    write_exif_gps,
    write_sidecar,
)
from .models import PhotoRecord, finite_or_none
from .scanner import scan_directory
from .thumbnail import make_square_thumbnail, make_thumbnail, safe_stem

log = logging.getLogger(__name__)

# 灯箱大图的最长边（像素）；原图另存一份，浏览器不必下载几十 MB
FULL_MAX_SIZE = 2000
FULL_CACHE_SIZE = 32
# 大图缓存再按总字节数封顶：一张 2000px 的 JPEG 可能有大几百 KB，
# 只数条目不数体积的话，32 张就能吃掉几十 MB
FULL_CACHE_BYTES = 64 * 1024 * 1024
# 小于这个体积就不值得压缩了（gzip 头本身也有开销）
GZIP_MIN_SIZE = 1024

# 页面里引用静态资源时用的前缀（页面在 /map/xxx 下，相对路径会解析错）
ASSET_PREFIX = "/files"

# 免密钥底图。三个引擎都直接吃瓦片，不需要任何厂商的 JS API，
# 所以换引擎不用密钥，页面也能完全离线跑。
#
# Google 官方瓦片：lyrs=m 路网 / s 卫星 / y 混合。
#
# 三层的坐标基准并不一样（境内）：
#   路网是 GCJ-02（国家要求的加密坐标，和高德/百度一致）；
#   卫星与混合是 WGS-84（卫星影像没有做偏移，实测与高德卫星正好差一个 GCJ-02 的量）。
# 用同一套偏移去画三层，卫星底图上的照片就会偏出几百米，
# 所以每片瓦片各自带一个 coord_system，切换底图时按层换算。
GOOGLE_TILES = [
    {
        "name": "路网",
        "url": "https://mt1.google.com/vt/lyrs=m&hl=zh-CN&x={x}&y={y}&z={z}",
        "attribution": "© Google",
        "maxZoom": 20,
        "coord_system": "gcj02",
    },
    {
        "name": "卫星",
        "url": "https://mt1.google.com/vt/lyrs=s&hl=zh-CN&x={x}&y={y}&z={z}",
        "attribution": "© Google",
        "maxZoom": 20,
        "coord_system": "wgs84",
    },
    {
        "name": "混合",
        "url": "https://mt1.google.com/vt/lyrs=y&hl=zh-CN&x={x}&y={y}&z={z}",
        "attribution": "© Google",
        "maxZoom": 20,
        # 混合图的格网跟着卫星影像走（WGS-84）；它叠的路网注记本身是 GCJ-02，
        # 那是 Google 自带的错位，用这些公开瓦片消不掉。
        "coord_system": "wgs84",
    },
]

# 高德瓦片：webrd 是路网（style=8），webst 是卫星影像（style=6）。
# 高德的数据全是 GCJ-02 基准，三层一致。
# 混合图 = 卫星影像 + 一层透明底的路网与中文标注（webst style=8），
# 两层同为 GCJ-02，叠加后自洽，照片按 GCJ-02 换算就贴得住影像。
AMAP_TILES = [
    # 混合放在第一个：页面默认打开的就是这一层
    {
        "name": "混合",
        "url": "https://webst0{s}.is.autonavi.com/appmaptile"
               "?style=6&x={x}&y={y}&z={z}",
        "overlay": {
            "url": "https://webst0{s}.is.autonavi.com/appmaptile"
                   "?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scale=1&style=8",
            "maxZoom": 18,
            "subdomains": ["1", "2", "3", "4"],
        },
        "attribution": "© 高德地图",
        "maxZoom": 18,
        "subdomains": ["1", "2", "3", "4"],
        "coord_system": "gcj02",
    },
    {
        "name": "路网",
        "url": "https://webrd0{s}.is.autonavi.com/appmaptile"
               "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        "attribution": "© 高德地图",
        "maxZoom": 18,
        "subdomains": ["1", "2", "3", "4"],
        "coord_system": "gcj02",
    },
    {
        "name": "卫星",
        "url": "https://webst0{s}.is.autonavi.com/appmaptile"
               "?style=6&x={x}&y={y}&z={z}",
        "attribution": "© 高德地图",
        "maxZoom": 18,
        "subdomains": ["1", "2", "3", "4"],
        "coord_system": "gcj02",
    },
]

# 可选的地图引擎。coord_system 是这个引擎的**默认**坐标基准
# （也就是首层底图的基准）；各层底图自己的基准在 tiles 里逐层声明。
# 百度是 BD-09；Google 与高德在国内是 GCJ-02；Leaflet 默认 WGS-84。
# 本机地址（防 DNS rebinding：攻击者域名解析到 127.0.0.1）
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_ip_literal(hostname: str) -> bool:
    """是不是 IP 字面量（这决定「局域网访问」允不允许）。"""
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return True
    except ValueError:
        return False

ENGINES = {
    "leaflet": {
        "label": "Leaflet",
        "template": "leaflet.html",
        "coord_system": "wgs84",
        "option": None,  # Leaflet 用自带的瓦片，不需要密钥
        "tiles": None,
    },
    "google": {
        "label": "Google Maps",
        "template": "leaflet.html",
        "coord_system": "gcj02",
        "option": None,  # 走官方瓦片，不需要密钥
        "tiles": GOOGLE_TILES,
    },
    "amap": {
        "label": "高德地图",
        "template": "leaflet.html",
        "coord_system": "gcj02",
        "option": None,  # 走公开瓦片，不需要密钥
        "tiles": AMAP_TILES,
    },
    "baidu": {
        "label": "百度地图",
        "template": "baidu_maps.html",
        "coord_system": "bd09",
        "option": "ak",
        "tiles": None,
    },
}

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


_ASSET_KEY_CACHE: dict[str, str] = {}


def asset_key(path: Path) -> str:
    """按文件绝对路径生成稳定的资源名。

    照片会随目录内容增删，序号命名会让已有资源的文件名漂移，
    所以这里用路径哈希做前缀，保证同一张照片永远对应同一个文件。

    结果按路径缓存：resolve() 在云盘目录上是一次真实的文件系统调用，
    一次重建要调好几百次，不缓存的话光是取 key 就要一秒多。
    """
    cache_key = str(path)
    cached = _ASSET_KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    key = f"{digest}_{safe_stem(path)}"
    _ASSET_KEY_CACHE[cache_key] = key
    return key


def default_workdir(root) -> Path:
    """派生资源（缩略图、标记图）的默认存放目录，按源目录区分。

    root 可以是单个目录，也可以是目录列表（多个照片文件夹共用一个工作目录）。
    """
    roots = root if isinstance(root, (list, tuple)) else [root]
    resolved_parts = []
    for item in roots:
        try:
            resolved_parts.append(str(Path(item).resolve()))
        except OSError:
            resolved_parts.append(str(item))
    resolved = "\n".join(sorted(resolved_parts))
    tag = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "photonmap" / tag


def _file_stamp(path: Path) -> tuple | None:
    """文件的变化标记。

    旁车文件的改动也要算进来——用户确认位置后只写旁车文件，
    原图本身没动，不把它算进去的话缓存不会失效。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        sidecar_stamp = sidecar_path(path).stat().st_mtime
    except OSError:
        sidecar_stamp = 0.0
    return (stat.st_mtime, stat.st_size, sidecar_stamp)


def _sort_key(record: PhotoRecord) -> tuple:
    """按拍摄时间排序；没有时间的排最后。

    EXIF 里的时间有的是带时区的、有的是不带，直接比较会抛异常，
    所以统一换成时间戳再比。
    """
    taken = record.taken_at
    if taken is None:
        return (1, 0.0, str(record.path))
    try:
        stamp = taken.timestamp()
    except (OverflowError, OSError, ValueError):
        stamp = 0.0
    return (0, stamp, str(record.path))


_INDEX_VERSION = 1


def _record_to_json(record: PhotoRecord) -> dict:
    return {
        "path": str(record.path),
        "lat": record.lat,
        "lon": record.lon,
        "altitude": finite_or_none(record.altitude),
        "taken_at": record.taken_at.isoformat() if record.taken_at else None,
        "source": record.source,
        "thumbnail": str(record.thumbnail) if record.thumbnail else None,
        "marker": str(record.marker) if record.marker else None,
    }


def _entry_to_json(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "stamp": list(entry["stamp"]),
        "pending": bool(entry["pending"]),
        "record": _record_to_json(entry["record"]),
    }


def _entry_from_json(raw) -> dict | None:
    """还原一条索引缓存；格式不对就丢掉（宁可重扫，也不能用坏数据）。"""
    if not isinstance(raw, dict):
        return None
    record_raw = raw.get("record")
    stamp = raw.get("stamp")
    if not isinstance(record_raw, dict) or not isinstance(stamp, list) or len(stamp) != 3:
        return None
    try:
        taken_at = record_raw.get("taken_at")
        record = PhotoRecord(
            path=Path(record_raw["path"]),
            lat=float(record_raw["lat"]),
            lon=float(record_raw["lon"]),
            altitude=record_raw.get("altitude"),
            taken_at=datetime.fromisoformat(taken_at) if taken_at else None,
            thumbnail=Path(record_raw["thumbnail"]) if record_raw.get("thumbnail") else None,
            marker=Path(record_raw["marker"]) if record_raw.get("marker") else None,
            source=record_raw.get("source", "exif"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "name": raw.get("name") or asset_key(record.path),
        "record": record,
        "stamp": tuple(stamp),
        "pending": bool(raw.get("pending")),
    }


class PhotoLibrary:
    """跟踪一个目录里的照片，并维护它们的派生资源。

    照片分两类：**已定位**（EXIF 或旁车文件里有坐标）与
    **待确认**（两者都没有）。待确认的会按同目录其它照片的拍摄时间
    推断出候选位置，交给用户在地图上确认。

    只在文件真正变化（增删改）时才重新解析 EXIF、重新生成缩略图，
    其余情况直接复用，因此轮询的开销很小。
    """

    def __init__(
        self,
        root: Path | list[Path],
        assets_dir: Path,
        coord_system: str = "wgs84",
        recursive: bool = True,
        default_location: tuple[float, float] | None = None,
        location_table: dict[str, tuple[float, float]] | None = None,
        write_exif: bool = False,
        backup_dir: Path | None = None,
    ):
        roots = [Path(item) for item in (root if isinstance(root, (list, tuple)) else [root])]
        if not roots:
            raise FileNotFoundError("至少要有一个照片目录")
        self.roots = roots
        # 多个根目录同名时（比如两个 photos 文件夹）只靠 basename 会撞车：
        # 侧栏按文件夹合并时会把不相干照片并到一起，批处理就改错文件了
        names = [item.name for item in roots]
        self._root_labels = {
            item: (f"{item.name}#{index + 1}" if names.count(item.name) > 1 else item.name)
            for index, item in enumerate(roots)
        }
        # self.root 保留给只关心单个目录的老代码（导出器、CLI）
        self.root = roots[0]
        self.assets_dir = Path(assets_dir)
        self.coord_system = coord_system
        self.recursive = recursive
        self.default_location = default_location
        self.location_table = location_table or {}
        self.write_exif = write_exif
        self.backup_dir = backup_dir

        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._stamps: dict[str, tuple[float, int]] = {}
        self._rows: list[dict] = []
        self._pending: list[dict] = []
        self._sources: dict[str, Path] = {}
        # 原始记录（坐标一律 WGS-84），簇分组与位置读写都要用
        self._located_records: list[PhotoRecord] = []
        self._pending_records: list[PhotoRecord] = []
        self._records_by_key: dict[str, PhotoRecord] = {}
        self._version = 0
        self._index_loaded = False
        # 分组推断结果按版本缓存：库变了（version+1）才重算
        self._groups_cache: dict[str, tuple[int, list[dict]]] = {}

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def _index_file(self) -> Path:
        return self.assets_dir.parent / "index.json"

    def _load_index(self) -> None:
        """载入上次扫描的结果，第二次打开同一目录就不用再读 EXIF。"""
        if self._index_loaded:
            return
        self._index_loaded = True
        try:
            data = json.loads(self._index_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
            return
        stored = data.get("roots")
        if isinstance(stored, list):
            if [str(item) for item in self.roots] != stored:
                return
        elif len(self.roots) != 1 or data.get("root") != str(self.root):
            # 老格式只记了一个 root
            return
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, dict):
            return
        restored: dict[str, dict] = {}
        for key, raw in raw_entries.items():
            entry = _entry_from_json(raw)
            if entry is None:
                continue
            record = entry["record"]
            # 缩略图或标记图被删了就得重新生成，否则页面上是破图
            if not (record.thumbnail and record.thumbnail.exists()):
                continue
            if not (record.marker and record.marker.exists()):
                continue
            restored[key] = entry
        self._entries = restored

    def _save_index(self) -> None:
        payload = {
            "version": _INDEX_VERSION,
            "root": str(self.root),
            "roots": [str(item) for item in self.roots],
            "entries": {key: _entry_to_json(entry) for key, entry in self._entries.items()},
        }
        try:
            write_text_atomic(self._index_file(), json.dumps(payload, ensure_ascii=False))
        except OSError as exc:  # 缓存写不进去不影响正常使用
            log.warning("索引缓存写入失败: %s", exc)

    def refresh(self, progress=None) -> bool:
        """重新扫描目录；内容有变化时返回 True。

        progress(done, total) 每处理完一张照片回调一次，供启动器显示进度。
        """
        self._load_index()
        files: list[Path] = []
        seen: set[str] = set()
        for base in self.roots:
            try:
                found = scan_directory(base, recursive=self.recursive)
            except FileNotFoundError:
                log.warning("照片目录不存在，跳过：%s", base)
                continue
            for path in found:
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
        files.sort()
        stamps: dict[str, tuple] = {}
        for path in files:
            stamp = _file_stamp(path)
            if stamp is not None:
                stamps[str(path)] = stamp

        with self._lock:
            if stamps == self._stamps:
                return False
            self._rebuild(files, stamps, progress)
            self._stamps = stamps
            self._version += 1
            self._save_index()
            return True

    def apply_sidecars(self, paths) -> int:
        """写完（或删掉）旁车文件后，只更新这几张照片，不重新遍历目录。

        确认一次位置如果重扫几千张照片的目录要好几秒，页面上就像卡住了。
        返回实际更新的张数；一张都没更新时调用方可以退回整目录 refresh。
        """
        targets = [Path(path) for path in paths]
        with self._lock:
            keys = {str(path) for path in targets}
            located = [r for r in self._located_records if str(r.path) not in keys]
            pending = [
                (r, asset_key(r.path))
                for r in self._pending_records
                if str(r.path) not in keys
            ]
            changed = 0
            for path in targets:
                key = str(path)
                entry = self._entries.get(key)
                if entry is None:
                    continue
                record, waiting = self._read_record(path)
                source = record if record is not None else waiting
                if source is None:
                    continue
                prepared = self._prepare(source, entry["name"])
                if prepared is None:
                    continue
                entry["record"] = prepared
                entry["pending"] = record is None
                stamp = _file_stamp(path)
                if stamp is not None:
                    # 同步更新 stamp，免得下一次轮询又当成长了变化
                    entry["stamp"] = stamp
                    self._stamps[key] = stamp
                if record is not None:
                    located.append(prepared)
                else:
                    pending.append((prepared, entry["name"]))
                changed += 1
            if not changed:
                return 0
            self._reindex(
                located, pending, {k: e["name"] for k, e in self._entries.items()}
            )
            self._version += 1
            self._save_index()
            return changed

    def _read_record(self, path: Path) -> tuple[PhotoRecord | None, PhotoRecord | None]:
        """读一张照片，返回 (已定位的记录, 待确认的记录)，两者至多一个非空。

        优先级：旁车文件（人工标定过的位置）> EXIF GPS > --locations/--location 兜底。
        旁车排在最前，是因为人工标定的位置必须能盖过相机 GPS——
        否则「随时改一下位置」对已经有 GPS 的照片根本不生效。
        """
        fallback = self.location_table.get(path.name) or self.default_location

        # 人工确认过的位置优先
        sidecar = read_sidecar(path)
        if sidecar is not None:
            exif = read_exif(path)
            return (
                PhotoRecord(
                    path=path,
                    lat=sidecar[0],
                    lon=sidecar[1],
                    altitude=exif.altitude,
                    taken_at=exif.taken_at or filename_timestamp(path),
                    source="sidecar",
                ),
                None,
            )

        record = read_photo(path, fallback=fallback)
        if record is not None:
            exif = read_exif(path)
            return replace(record, source="exif" if exif.has_gps else "fallback"), None

        if fallback is None:
            # 既没有坐标也没法推断 → 进入待确认队列
            exif = read_exif(path)
            return None, PhotoRecord(
                path=path,
                lat=0.0,
                lon=0.0,
                altitude=exif.altitude,
                taken_at=exif.taken_at or filename_timestamp(path),
                source="none",
            )
        return None, None

    def _rebuild(
        self,
        files: list[Path],
        stamps: dict[str, tuple[float, int]],
        progress=None,
    ) -> None:
        entries: dict[str, dict] = {}
        located: list[PhotoRecord] = []
        pending: list[tuple[PhotoRecord, str]] = []

        total = len(files)
        done = 0
        # 先把需要重新解析/生成缩略图的挑出来；解码与编码在 Pillow 的 C 层，
        # 放线程池里能真吃满多核。上百张照片的目录首次扫描会快好几倍。
        todo: list[tuple[str, str, PhotoRecord, tuple, bool]] = []
        for path in files:
            key = str(path)
            stamp = stamps[key]
            cached = self._entries.get(key)
            if cached and cached["stamp"] == stamp:
                entries[key] = cached
                if cached["pending"]:
                    pending.append((cached["record"], cached["name"]))
                else:
                    located.append(cached["record"])
                done += 1
                if progress is not None:
                    progress(done, total)
                continue

            record, waiting = self._read_record(path)
            source = record if record is not None else waiting
            if source is None:
                done += 1
                if progress is not None:
                    progress(done, total)
                continue
            todo.append((key, asset_key(path), source, stamp, record is None))

        if todo:
            workers = min(8, os.cpu_count() or 4)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                prepared_list = list(
                    pool.map(lambda item: self._prepare(item[2], item[1]), todo)
                )
            for (key, name, _source, stamp, is_pending), prepared in zip(todo, prepared_list):
                done += 1
                if progress is not None:
                    progress(done, total)
                if prepared is None:
                    # 图片还没同步完（解码失败），先不显示，免得地图上出现破图
                    continue
                entries[key] = {
                    "name": name,
                    "record": prepared,
                    "stamp": stamp,
                    "pending": is_pending,
                }
                if is_pending:
                    pending.append((prepared, name))
                else:
                    located.append(prepared)

        self._entries = entries
        self._reindex(located, pending, {k: e["name"] for k, e in entries.items()})

    def _prepare(self, record: PhotoRecord, name: str) -> PhotoRecord | None:
        """生成缩略图与标记图；已存在则复用。解码不出来就返回 None。"""
        thumbs = self.assets_dir / "thumbs"
        markers = self.assets_dir / "markers"
        thumbs.mkdir(parents=True, exist_ok=True)
        markers.mkdir(parents=True, exist_ok=True)

        thumb = thumbs / f"{name}.jpg"
        marker = markers / f"{name}.jpg"
        if not thumb.exists():
            make_thumbnail(record.path, thumb)
        if not marker.exists() and thumb.exists():
            # 标记图从缩略图裁，避免再解码一次原图（首次扫描能快近一倍）
            make_square_thumbnail(thumb, marker)
        if not (thumb.exists() and marker.exists()):
            return None
        return replace(record, thumbnail=thumb, marker=marker)

    def _reindex(
        self,
        located: list[PhotoRecord],
        pending: list[tuple[PhotoRecord, str]],
        names: dict[str, str] | None = None,
    ) -> None:
        """整理出前端要的行数据，并给待确认的照片算出候选位置。

        names 是「路径 → 资源名」的现成映射（entries 里就有）：
        不用再对每张照片跑一次 resolve() 去算哈希。
        """
        names = names or {}
        located.sort(key=_sort_key)
        rows: list[dict] = []
        sources: dict[str, Path] = {}
        for record in located:
            key = str(record.path)
            name = names.get(key) or asset_key(record.path)
            sources[name] = record.path
            row = photo_row(
                record,
                self.assets_dir,
                coord_system=self.coord_system,
                original="",
                prefix=ASSET_PREFIX,
            )
            row["key"] = name
            # 大图由服务端按需转码，不落盘
            row["full"] = f"{ASSET_PREFIX}/full/{name}"
            row["original"] = f"api/original/{name}"
            rows.append(row)

        # 已定位照片按时间排一次序，给所有待确认照片复用，
        # 否则每张都要重排一遍，几千张的目录会卡上几分钟
        timeline = prepare_timeline(located)
        pending_rows: list[dict] = []
        for record, name in sorted(pending, key=lambda item: _sort_key(item[0])):
            sources[name] = record.path
            candidates = suggest_sorted(timeline, record)
            row = photo_row(
                record,
                self.assets_dir,
                coord_system=self.coord_system,
                original="",
                prefix=ASSET_PREFIX,
            )
            row["key"] = name
            row["status"] = "pending"
            row["full"] = f"{ASSET_PREFIX}/full/{name}"
            row["original"] = f"api/original/{name}"
            # 候选给的是 WGS-84，跟同一行的 lat/lon 一样要换算到库基准，
            # 否则照片坐标和候选坐标会落在两个基准上。
            row["candidates"] = [
                _shift_candidate(c.to_dict(), self.coord_system) for c in candidates
            ]
            row["candidate"] = row["candidates"][0] if row["candidates"] else None
            pending_rows.append(row)

        self._rows = rows
        self._pending = pending_rows
        self._sources = sources
        self._located_records = list(located)
        self._pending_records = [record for record, _ in pending]
        self._records_by_key = {
            asset_key(record.path): record
            for record in list(located) + [record for record, _ in pending]
        }

    def rows(self, coord_system: str | None = None) -> dict:
        """返回前端要的照片数据。

        坐标基准不同要重新换算（百度是 BD-09，其余是 WGS-84）。
        """
        system = coord_system or self.coord_system
        with self._lock:
            if system == self.coord_system:
                return {"photos": list(self._rows), "pending": list(self._pending)}
            return {
                "photos": [_shift_row(row, system, self.coord_system) for row in self._rows],
                "pending": [_shift_pending(row, system, self.coord_system) for row in self._pending],
            }

    def source_of(self, name: str) -> Path | None:
        with self._lock:
            return self._sources.get(name)

    def position_of(self, name: str) -> tuple[float, float] | None:
        """某张照片当前的位置（WGS-84）；没有位置时返回 None。

        待确认的照片在记录里的坐标是占位的 (0, 0)，不能当成真的位置，
        否则「整簇平移」会把它们写到赤道附近去。
        """
        with self._lock:
            record = self._records_by_key.get(name)
        if record is None or record.source == "none":
            return None
        return record.lat, record.lon

    def groups(self, coord_system: str | None = None) -> list[dict]:
        """按拍摄时间把照片聚成簇，并给出簇级别的位置推断。

        这是「规模化确定位置」的基础：一次出行里的照片本来就成簇，
        簇内前后夹着的已定位照片可以插值出一个更准的点。
        """
        system = coord_system or self.coord_system
        with self._lock:
            cached = self._groups_cache.get(system)
            if cached is not None and cached[0] == self._version:
                return list(cached[1])
            version = self._version
            located = list(self._located_records)
            pending = list(self._pending_records)

        result = []
        for group in build_groups(located, pending):
            guess = infer(group, located_all=located)
            item = {
                "id": group.id,
                "start": group.start.strftime("%Y-%m-%d %H:%M:%S") if group.start else "",
                "end": group.end.strftime("%Y-%m-%d %H:%M:%S") if group.end else "",
                "count": group.count,
                "pending_count": group.pending_count,
                "members": [asset_key(photo.path) for photo in group.photos],
                "located_members": [asset_key(photo.path) for photo in group.located],
                "pending_members": [asset_key(photo.path) for photo in group.pending],
                "thumb": self._thumb_url(group.photos[0]) if group.photos else "",
                # 照片所在文件夹：待确认照片大多没有坐标，文件夹是最可靠的
                # 「同一地点（相册）」信号，侧栏按它合并分组
                "folder": self._folder_label(group.photos[0]) if group.photos else "",
                "guess": None,
            }
            if guess is not None:
                lat, lon = coords.convert(
                    guess.lat, guess.lon, system, source=coords.WGS84
                )
                data = guess.to_dict()
                data["lat"] = round(lat, 7)
                data["lon"] = round(lon, 7)
                item["guess"] = data
            result.append(item)

        with self._lock:
            # 推断要好几秒，库没变就直接复用（版本号变了才重算）
            if version == self._version:
                self._groups_cache[system] = (version, result)
        return list(result)

    def _folder_label(self, record: PhotoRecord) -> str:
        """照片所在文件夹（相对照片根目录），根目录本身返回空串。

        多个照片目录时前缀根目录名，免得两个目录下的同名子文件夹混在一起。
        """
        for root in self.roots:
            try:
                relative = record.path.parent.relative_to(root)
            except ValueError:
                continue
            text = "" if str(relative) == "." else str(relative)
            if len(self.roots) == 1:
                return text
            label = self._root_labels.get(root, root.name)
            return f"{label}/{text}" if text else label
        return record.path.parent.name

    def _thumb_url(self, record: PhotoRecord) -> str:
        """缩略图在页面上的 URL。"""
        if not record.thumbnail:
            return ""
        try:
            relative = Path(record.thumbnail).relative_to(self.assets_dir.parent).as_posix()
        except ValueError:
            return Path(record.thumbnail).as_posix()
        _, _, rest = relative.partition("/")
        return f"{ASSET_PREFIX}/{rest}" if rest else ""


def _shift_candidate(candidate: dict, target: str, source: str = coords.WGS84) -> dict:
    """把一个候选位置的坐标换算到目标基准。"""
    moved = dict(candidate)
    lat, lon = coords.convert(candidate["lat"], candidate["lon"], target, source=source)
    moved["lat"] = round(lat, 7)
    moved["lon"] = round(lon, 7)
    return moved


def _shift_row(row: dict, system: str, source: str = coords.WGS84) -> dict:
    """把一行照片的坐标换算到另一个基准。

    行里的坐标已经是库基准（self.coord_system）了，所以要带上 source，
    否则会把已经偏移过的值当成 WGS-84 再偏一次。
    """
    moved = dict(row)
    lat, lon = coords.convert(row["lat"], row["lon"], system, source=source)
    moved["lat"] = round(lat, 7)
    moved["lon"] = round(lon, 7)
    return moved


def _shift_pending(row: dict, system: str, source: str = coords.WGS84) -> dict:
    """把待确认照片的候选坐标换算到另一个基准。"""
    moved = dict(row)
    candidates = []
    for candidate in row.get("candidates") or []:
        item = dict(candidate)
        lat, lon = coords.convert(item["lat"], item["lon"], system, source=source)
        item["lat"] = round(lat, 7)
        item["lon"] = round(lon, 7)
        candidates.append(item)
    moved["candidates"] = candidates
    moved["candidate"] = candidates[0] if candidates else None
    return moved


class _FullCache:
    """大图的内存缓存，转码一次就够看很久了。"""

    def __init__(self, maxsize: int = FULL_CACHE_SIZE, max_bytes: int = FULL_CACHE_BYTES):
        self.maxsize = maxsize
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
            return data

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = data
            self._size += len(data)
            while self._items and (
                len(self._items) > self.maxsize or self._size > self.max_bytes
            ):
                _, dropped = self._items.popitem(last=False)
                self._size -= len(dropped)


def encode_full_image(path: Path, max_size: int = FULL_MAX_SIZE) -> bytes:
    """把原图转成浏览器能显示的 JPEG（含 EXIF 方向校正）。"""
    with open_image(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", quality=88, optimize=True)
        return buffer.getvalue()


def make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def render_shell(
    engines: list[dict],
    tiles: list[dict],
    poll_ms: int,
    session: str,
    keys: dict,
    ai_available: bool = False,
) -> bytes:
    """壳页面：引擎切换、侧边栏、定位确认浮层。"""
    template = make_env().get_template("shell.html")
    html = template.render(
        engines=engines,
        tiles=tiles,
        amap_tiles=AMAP_TILES,
        poll_ms=poll_ms,
        session=session,
        keys=keys,
        ai_available=ai_available,
        asset_prefix=ASSET_PREFIX,
    )
    return html.encode("utf-8")


def default_tile_system(page_tiles: list[dict] | None, fallback: str) -> str:
    """初始底图的坐标基准：取第一片瓦片自己声明的，没有就用引擎的默认值。"""
    for tile in page_tiles or []:
        value = tile.get("coord_system")
        if value in coords.SYSTEMS:
            return value
    return fallback


def render_engine_page(
    engine: str,
    library: PhotoLibrary,
    options: dict,
    tiles: list[dict],
    poll_ms: int,
) -> bytes:
    """渲染某个地图引擎的页面（live 模式）。"""
    spec = ENGINES[engine]
    # 引擎自带底图时用它，--tile 配的底图只留给 Leaflet
    page_tiles = spec.get("tiles") or tiles
    system = default_tile_system(page_tiles, spec["coord_system"])
    data = library.rows(system)
    template = make_env().get_template(spec["template"])
    engine_options = dict(options)
    engine_options["live"] = True
    html = template.render(
        photos=data["photos"],
        pending=data["pending"],
        version=library.version,
        options=engine_options,
        coord_label=COORD_LABELS.get(system, system),
        coord_system=system,
        asset_prefix=ASSET_PREFIX,
        tiles=page_tiles,
        poll_ms=poll_ms,
    )
    return html.encode("utf-8")


def install_static_files(assets_dir: Path) -> None:
    """把前端资源复制到工作目录，供 HTTP 服务直接提供。"""
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in ("photo_map.js", "photo_map.css"):
        source = TEMPLATE_DIR / name
        if source.exists():
            copy_with_retry(source, assets_dir / name)
    vendor = TEMPLATE_DIR / "vendor"
    if vendor.is_dir():
        shutil.copytree(vendor, assets_dir / "vendor", dirs_exist_ok=True)


class LiveHandler(BaseHTTPRequestHandler):
    """处理页面、照片列表与静态资源。"""

    server_version = "photonmap"

    def _request_allowed(self) -> bool:
        """挡掉 DNS rebinding 和跨站提交。

        服务默认只监听本机，但恶意网页可以把自己的域名解析到 127.0.0.1：
        浏览器发来的请求 Host 是攻击者的域名、目标却是本服务。规则：
        - 浏览器标成 `Sec-Fetch-Site: cross-site` 的一律拒（不管有没有 Origin）；
        - Host 可以是本机地址；用 `--host 0.0.0.0` 暴露到局域网时，还允许
          **IP 字面量**（局域网里用 IP 访问），但域名一律拒——rebinding 用的
          正是域名；
        - 带 Origin 的请求要同源，同样只认本机地址 / IP 字面量。
        """
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return False

        port = getattr(self.server, "listen_port", None)
        exposed = bool(getattr(self.server, "exposed", False))

        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin:
            parts = urlsplit(origin)
            if parts.scheme != "http":
                return False
            if port is not None and parts.port not in (None, port):
                return False
            origin_host = (parts.hostname or "").strip().lower()
            if origin_host not in LOOPBACK_HOSTS:
                if not (exposed and _is_ip_literal(origin_host)):
                    return False

        host = (self.headers.get("Host") or "").strip().lower().rstrip(".")
        if not host:
            return True  # HTTP/1.0 客户端不带 Host；本服务自己的页面不会这么发
        if host.startswith("["):
            hostname = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        if hostname in LOOPBACK_HOSTS:
            return True
        if exposed and _is_ip_literal(hostname):
            return True
        bind_host = (getattr(self.server, "bind_host", "") or "").strip().lower()
        return bool(bind_host) and hostname == bind_host

    def _touch(self) -> None:
        self.server.last_activity = time.monotonic()
        self.server.bye_at = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._touch()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not self._request_allowed():
            self.send_error(403, "Forbidden")
            return
        try:
            if path in ("/", "/index.html"):
                self._send_bytes(
                    self.server.shell, "text/html; charset=utf-8", frame_ancestors="none"
                )
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif path.startswith("/map/"):
                self._send_engine_page(path[len("/map/"):])
            elif path == "/api/photos":
                self._send_json(self._photos_payload(parsed))
            elif path == "/api/folders":
                self._send_json(self._folders_payload())
            elif path == "/api/geocode":
                self._send_json(self._geocode_payload(parsed))
            elif path == "/api/groups":
                self._send_json(self._groups_payload(parsed))
            elif path.startswith("/files/full/"):
                self._send_full(path[len("/files/full/"):])
            elif path.startswith("/files/"):
                self._send_file(path[len("/files/"):])
            elif path.startswith("/api/original/"):
                self._send_original(path[len("/api/original/"):])
            else:
                self.send_error(404, "Not Found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("处理 %s 失败", path)
            # HTTP 状态行只能放 latin-1 字符，提示语一律用英文
            self.send_error(500, "Internal Server Error")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._touch()
        path = unquote(urlparse(self.path).path)
        if not self._request_allowed():
            self.send_error(403, "Forbidden")
            return
        try:
            if path == "/api/locate":
                self._handle_locate()
            elif path == "/api/locate/open":
                self._handle_locate_open()
            elif path == "/api/locate/batch":
                self._handle_locate_batch()
            elif path == "/api/locate/clear":
                self._handle_locate_clear()
            elif path == "/api/ai/locate":
                self._handle_ai_locate()
            elif path == "/api/bye":
                self._handle_bye()
            elif path == "/api/folders":
                self._handle_folders()
            elif path == "/api/browse":
                self._handle_browse()
            else:
                self.send_error(404, "Not Found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("处理 %s 失败", path)
            self.send_error(500, "Internal Server Error")

    # -- 各个响应 ---------------------------------------------------------

    def _geocode_payload(self, parsed) -> dict:
        """地址搜索。坐标一律 WGS-84，由前端按当前底图的基准换算。"""
        query = parse_qs(parsed.query).get("q", [""])[0].strip()
        if not query:
            return {"ok": False, "error": "缺少搜索词"}
        limit = parse_qs(parsed.query).get("limit", ["5"])[0]
        try:
            limit = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            limit = 5
        # 搜索跟着当前选中的地图引擎走：高德/百度用国内 POI，其余用 OSM
        engine = parse_qs(parsed.query).get("engine", [""])[0].strip().lower()
        if engine not in ENGINES:
            engine = None
        results = self.server.geocoder.search(query, limit, engine=engine)
        return {
            "ok": True,
            "query": query,
            "engine": engine or "",
            "results": results,
            "source": results[0]["source"] if results else "",
        }

    def _groups_payload(self, parsed) -> dict:
        """时间簇列表；推断出的坐标按请求的基准返回。"""
        system = parse_qs(parsed.query).get("system", [None])[0]
        if system and system not in coords.SYSTEMS:
            system = None
        return {
            "version": self.server.library.version,
            "groups": self.server.library.groups(system),
        }

    def _photos_payload(self, parsed) -> dict:
        """照片列表；坐标基准随查询参数变化（百度是 BD-09）。"""
        system = parse_qs(parsed.query).get("system", [None])[0]
        if system and system not in coords.SYSTEMS:
            system = None
        data = self.server.library.rows(system)
        return {
            "version": self.server.library.version,
            "session": self.server.session,
            # 实际使用的坐标基准，前端据此判断响应是不是过期的
            "system": system or self.server.library.coord_system,
            "photos": data["photos"],
            "pending": data["pending"],
        }

    def _send_engine_page(self, engine: str) -> None:
        if engine not in ENGINES:
            self.send_error(404, "Not Found")
            return
        spec = ENGINES[engine]
        # 密钥可以从 URL 传进来（页面上临时填的），优先于服务启动时配置的
        options = dict(self.server.engine_options)
        query = parse_qs(urlparse(self.path).query)
        option = spec.get("option")
        if option:
            supplied = query.get(option, [None])[0]
            if supplied:
                options[option] = supplied
        page = render_engine_page(
            engine,
            self.server.library,
            options,
            self.server.tiles,
            self.server.poll_ms,
        )
        self._send_bytes(page, "text/html; charset=utf-8", frame_ancestors="self")

    def _handle_locate_open(self) -> None:
        """用户打开了确认浮层 → 签发一次性令牌。

        带 group 时按簇签发：簇里每张待确认照片各一个，
        批量写入时每张照片都要有自己的令牌，不能一个令牌覆盖多张。
        """
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return

        group_id = payload.get("group")
        members = payload.get("members")
        if members or group_id:
            tokens: dict[str, str] = {}
            if group_id:
                for group in self.server.library.groups():
                    if group["id"] != group_id:
                        continue
                    for key in group["pending_members"]:
                        tokens[key] = self.server.tokens.issue(key)
                    break
                else:
                    self.send_error(404, "Not Found")
                    return
            else:
                for key in members:
                    if not isinstance(key, str):
                        continue
                    if self.server.library.source_of(key) is None:
                        continue
                    tokens[key] = self.server.tokens.issue(key)
            print(
                f"[{time.strftime('%H:%M:%S')}] 签发批量定位令牌：{len(tokens)} 张",
                flush=True,
            )
            self._send_json({"ok": True, "tokens": tokens})
            return

        key = payload.get("key")
        if not key:
            self.send_error(400, "Bad Request")
            return
        if self.server.library.source_of(key) is None:
            self.send_error(404, "Not Found")
            return
        token = self.server.tokens.issue(key)
        print(f"[{time.strftime('%H:%M:%S')}] 签发定位令牌：{key}", flush=True)
        self._send_json({"ok": True, "token": token})

    def _handle_locate(self) -> None:
        """写入用户确认过的位置。"""
        payload = self._read_json()
        if payload is None:
            return

        key = payload.get("key")
        lat = payload.get("lat")
        lon = payload.get("lon")
        if not key or lat is None or lon is None:
            self.send_error(400, "Bad Request")
            return
        # 会话标识：上一次运行留下的旧页面拿着旧标识来提交，直接拒掉
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return
        # 一次性令牌：浮层没打开过的页面拿不到，提交一律拒掉
        token = payload.get("token") or ""
        if not self.server.tokens.consume(token, key):
            self.send_error(409, "No Token")
            return
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            self.send_error(400, "Bad Request")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self.send_error(400, "Bad Request")
            return

        library = self.server.library
        source = library.source_of(key)
        if source is None:
            self.send_error(404, "Not Found")
            return

        # 页面上的地图可能是百度（BD-09），写盘前统一换算回 WGS-84
        system = payload.get("system") or "wgs84"
        if system not in coords.SYSTEMS:
            system = "wgs84"
        lat, lon = coords.convert(lat, lon, "wgs84", source=system)

        write_sidecar(source, lat, lon)
        if library.write_exif:
            write_exif_gps(source, lat, lon, backup_dir=library.backup_dir)
        if not library.apply_sidecars([source]):
            library.refresh()
        log.info("已记录位置：%s -> %.6f, %.6f", source.name, lat, lon)
        print(f"[{time.strftime('%H:%M:%S')}] 已记录位置：{source.name} -> {lat:.6f}, {lon:.6f}", flush=True)
        self._send_json({"ok": True, "key": key, "lat": lat, "lon": lon})

    def _handle_locate_batch(self) -> None:
        """批量写入位置。

        三种模式：
        - ``assign``：每张照片用它自己请求里带的坐标（按各自候选填）；
        - ``same``：整簇钉在同一个点；
        - ``shift``：整簇平移，保留组内相对位置。

        写盘只写旁车文件（原图一个字节不动）；要连 EXIF 一起改写的话
        用单张的 /api/locate——批量改 EXIF 又慢又容易出错。
        """
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return

        mode = payload.get("mode") or "assign"
        if mode not in ("assign", "same", "shift"):
            self.send_error(400, "Bad Request")
            return
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            self.send_error(400, "Bad Request")
            return

        system = payload.get("system") or "wgs84"
        if system not in coords.SYSTEMS:
            system = "wgs84"

        # 先把每一项的令牌都验一遍：任何一张不合法就整批拒掉，
        # 不能写了一半才失败。check() 只看不销，用完再 consume()。
        seen_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                self.send_error(400, "Bad Request")
                return
            key = item.get("key")
            token = item.get("token") or ""
            if not key or not self.server.tokens.check(token, key):
                self.send_error(409, "No Token")
                return
            if key in seen_keys:
                # 同一张照片出现两次：一个令牌不能写两遍
                self.send_error(400, "Bad Request")
                return
            seen_keys.add(key)

        def to_wgs(value, fallback=None) -> tuple[float, float]:
            lat, lon = float(value), float(fallback)
            return coords.convert(lat, lon, coords.WGS84, source=system)

        target = payload.get("target") or {}
        target_point = None
        shift_delta = None
        if mode in ("same", "shift"):
            try:
                target_point = to_wgs(target.get("lat"), target.get("lon"))
            except (TypeError, ValueError):
                self.send_error(400, "Bad Request")
                return
        if mode == "shift":
            anchor_key = payload.get("anchor_key")
            anchor = self.server.library.position_of(anchor_key) if anchor_key else None
            if anchor is None:
                self.send_error(400, "Bad Request")
                return
            shift_delta = (target_point[0] - anchor[0], target_point[1] - anchor[1])

        library = self.server.library
        # 令牌在写盘**之前**统一作废：写失败的项也不能在 5 分钟内
        # 被拿来重复提交（一次性令牌得真的是一次性的）。
        for item in items:
            self.server.tokens.consume(item.get("token") or "", item.get("key"))

        written: list[dict] = []
        written_paths: list[Path] = []
        errors: list[dict] = []
        for item in items:
            key = item.get("key")
            try:
                if mode == "assign":
                    lat, lon = to_wgs(item.get("lat"), item.get("lon"))
                elif mode == "same":
                    lat, lon = target_point
                else:
                    current = library.position_of(key)
                    if current is None:
                        # 待确认的照片本身没有位置，用请求里带的候选位置
                        # 当作平移起点（前端会把候选位置一起发过来）
                        try:
                            current = to_wgs(item.get("lat"), item.get("lon"))
                        except (TypeError, ValueError):
                            errors.append({"key": key, "error": "这张照片还没有位置，无法平移"})
                            continue
                    lat = current[0] + shift_delta[0]
                    lon = current[1] + shift_delta[1]
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    # 越界的坐标写进旁车，读的时候会被判为无效——
                    # 位置静默不生效，旁车还留在目录里，不如当场拒掉
                    errors.append({"key": key, "error": "坐标超出范围"})
                    continue
                source = library.source_of(key)
                if source is None:
                    errors.append({"key": key, "error": "找不到这张照片"})
                    continue
                write_sidecar(source, lat, lon)
                written_paths.append(source)
                written.append({"key": key, "lat": round(lat, 7), "lon": round(lon, 7)})
            except (TypeError, ValueError) as exc:
                errors.append({"key": key, "error": str(exc)})
            except OSError as exc:
                # 一张写盘失败（文件被同步软件占住等）不该把整批打断：
                # 已成功的要如实报出来，失败的进 errors。
                errors.append({"key": key, "error": f"写盘失败：{exc}"})

        if written_paths and not library.apply_sidecars(written_paths):
            library.refresh()
        print(
            f"[{time.strftime('%H:%M:%S')}] 批量写入位置：{len(written)} 张（{mode}）",
            flush=True,
        )
        self._send_json({"ok": True, "written": written, "errors": errors})

    def _handle_locate_clear(self) -> None:
        """清除人工标定的位置。

        删掉旁车文件，照片就退回相机 GPS（没有 GPS 的退回待确认）。
        若服务是以 --write-exif 启动的，EXIF 里也可能被改写过坐标，
        这时要从备份把原文件还原回来，否则人工坐标还赖在 EXIF 里。
        """
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return
        key = payload.get("key")
        token = payload.get("token") or ""
        if not key or not self.server.tokens.consume(token, key):
            self.send_error(409, "No Token")
            return

        library = self.server.library
        source = library.source_of(key)
        if source is None:
            self.send_error(404, "Not Found")
            return

        restored = False
        if library.write_exif:
            restored = restore_backup(source, library.backup_dir)
        removed = remove_sidecar(source)
        if not library.apply_sidecars([source]):
            library.refresh()
        print(f"[{time.strftime('%H:%M:%S')}] 已清除位置：{source.name}", flush=True)
        self._send_json(
            {
                "ok": True,
                "key": key,
                "sidecar_removed": removed,
                "exif_restored": restored,
            }
        )

    def _folders_payload(self) -> dict:
        """当前正在看的照片目录列表。"""
        return {"ok": True, "roots": [str(item) for item in self.server.library.roots]}

    def _browse_payload(self, payload: dict) -> dict:
        """列目录，给「添加文件夹」用。只列目录，不列文件。"""
        raw = str(payload.get("path") or "").strip()
        if not raw:
            if os.name == "nt":
                drives = []
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
                    candidate = Path(f"{letter}:\\")
                    if candidate.exists():
                        drives.append({"name": f"{letter}:", "path": str(candidate)})
                return {"ok": True, "path": "", "parent": None, "dirs": drives}
            raw = str(Path.home())
        target = Path(raw)
        if os.name == "nt":
            text = str(target).replace("/", "\\")
            # UNC（\server\share）和设备路径（\.\ / \?\）会触发网络/底层访问，
            # 目录浏览用不到，直接拒掉，免得被拿来探测内网共享
            if text.startswith("\\\\") or text.startswith("\\.\\"):
                return {"ok": False, "error": "不支持网络路径"}
        if not target.is_dir():
            return {"ok": False, "error": "目录不存在"}
        dirs = []
        try:
            with os.scandir(target) as entries:
                # 边扫边收，够 500 个就停：不要先把整目录（可能几十万项）排序再截断
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    dirs.append({"name": entry.name, "path": entry.path})
                    if len(dirs) >= 500:
                        break
        except OSError as exc:
            return {"ok": False, "error": f"无法读取：{exc}"}
        dirs.sort(key=lambda item: item["name"].lower())
        parent = str(target.parent) if target.parent != target else None
        return {"ok": True, "path": str(target), "parent": parent, "dirs": dirs}

    def _handle_browse(self) -> None:
        """列目录。要带 session：目录列表不该被任意页面拿到。"""
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return
        self._send_json(self._browse_payload(payload))

    def _handle_folders(self) -> None:
        """换一组照片目录（设置里的文件夹管理）。"""
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return
        items = payload.get("folders")
        if not isinstance(items, list) or not items:
            self.send_error(400, "Bad Request")
            return
        roots = []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                self.send_error(400, "Bad Request")
                return
            path = Path(item.strip())
            if not path.is_dir():
                self.send_error(400, "Bad Request")
                return
            if path not in roots:
                roots.append(path)
        if not roots:
            self.send_error(400, "Bad Request")
            return
        self.server.switch_roots(roots)
        print(
            f"[{time.strftime('%H:%M:%S')}] 更换照片目录：{'、'.join(str(r) for r in roots)}",
            flush=True,
        )
        self._send_json({"ok": True, "roots": [str(item) for item in roots]})

    def _handle_bye(self) -> None:
        """页面关了（sendBeacon）：给启动器一个关停信号。

        先等几秒看看有没有新请求（刷新页面会重新连上），
        没等到就由启动器把服务停掉。

        要带页面里的 session 当 CSRF 令牌：否则别的站点随手 POST 一下
        就能把本地服务关掉（一种廉价的拒绝服务）。
        """
        session = parse_qs(urlparse(self.path).query).get("session", [""])[0]
        if session != self.server.session:
            self.send_error(403, "Forbidden")
            return
        self.server.bye_at = time.monotonic()
        self.send_response(204)
        self.end_headers()

    def _handle_ai_locate(self) -> None:
        """用视觉模型给一张照片猜地名。

        模型只出地名，坐标由 /api/geocode 反查——
        模型给的坐标一定是编的，不能直接写进旁车文件。
        """
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("session") != self.server.session:
            self.send_error(409, "Stale Session")
            return
        key = payload.get("key")
        if not key:
            self.send_error(400, "Bad Request")
            return
        source = self.server.library.source_of(key)
        if source is None:
            self.send_error(404, "Not Found")
            return

        from . import ai

        ok, reason = ai.available()
        if not ok:
            self.send_error(501, reason.encode("latin-1", "replace")[:200])
            return

        try:
            image = encode_full_image(source)
        except Exception as exc:
            log.warning("准备图片失败 %s: %s", source.name, exc)
            self.send_error(500, "Internal Server Error")
            return

        context = ""
        row = next(
            (r for r in self.server.library.rows()["photos"] if r["key"] == key), None
        )
        if row and row.get("taken_at"):
            context = f"拍摄时间 {row['taken_at']}，文件名 {row['name']}"

        try:
            suggestions = ai.suggest_places(image, context=context)
        except Exception as exc:
            log.warning("AI 识别失败: %s", exc)
            self.send_error(502, f"AI request failed: {exc}".encode("latin-1", "replace")[:200])
            return

        print(
            f"[{time.strftime('%H:%M:%S')}] AI 识别：{source.name} → {suggestions}",
            flush=True,
        )
        self._send_json({"ok": True, "key": key, "suggestions": suggestions})

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_bytes(
        self, data: bytes, content_type: str, frame_ancestors: str | None = None
    ) -> None:
        # 照片列表动辄 2MB，gzip 之后只剩几百 KB，页面加载快得多。
        # JPEG/PNG 本身已经压过，再压一遍只是白烧 CPU（一页几千张图时非常明显）。
        wants_gzip = (
            len(data) >= GZIP_MIN_SIZE
            and content_type.startswith(("text/", "application/json", "application/javascript", "image/svg"))
            and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        )
        if wants_gzip:
            data = gzip.compress(data, compresslevel=6)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if wants_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if frame_ancestors == "none":
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        else:
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            if frame_ancestors:
                self.send_header(
                    "Content-Security-Policy", f"frame-ancestors '{frame_ancestors}'"
                )
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict) -> None:
        self._send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), MIME_TYPES[".json"])

    def _read_json(self) -> dict | None:
        """读请求体里的 JSON；解析失败时已经回过 400。"""
        # 只接受 application/json：跨站的「简单请求」发不了这个类型，
        # 挡掉一类不经过预检的 CSRF
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.send_error(415, "Unsupported Media Type")
            return None
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_error(400, "Bad Request")
            return None
        return payload if isinstance(payload, dict) else None

    def _send_file(self, relative: str) -> None:
        root = self.server.assets_dir.resolve()
        target = (root / relative).resolve()
        # 目录穿越防护：解析后的路径必须仍在资源目录内
        if not target.is_relative_to(root) or not target.is_file():
            self.send_error(404, "Not Found")
            return
        content_type = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send_bytes(target.read_bytes(), content_type)

    def _send_full(self, name: str) -> None:
        source = self.server.library.source_of(name)
        if source is None or not source.is_file():
            self.send_error(404, "Not Found")
            return
        cached = self.server.full_cache.get(name)
        if cached is None:
            cached = encode_full_image(source)
            self.server.full_cache.put(name, cached)
        self._send_bytes(cached, MIME_TYPES[".jpg"])

    def _send_original(self, name: str) -> None:
        source = self.server.library.source_of(name)
        if source is None or not source.is_file():
            self.send_error(404, "Not Found")
            return
        data = source.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{source.name}"'
        )
        self.end_headers()
        self.wfile.write(data)


class TokenStore:
    """定位确认的一次性令牌。

    只有真正打开过确认浮层的页面才拿得到令牌，写入坐标时必须带上它，
    这样任何「浮层没打开却提交了坐标」的路径都会被拒掉。
    """

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, key: str) -> str:
        token = uuid4().hex
        now = time.time()
        with self._lock:
            for old in [t for t, (_, exp) in self._tokens.items() if exp < now]:
                del self._tokens[old]
            self._tokens[token] = (key, now + self.ttl)
        return token

    def consume(self, token: str, key: str) -> bool:
        """校验并作废令牌；不存在、过期或绑定的不是这张照片都算失败。"""
        with self._lock:
            entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        bound_key, expires = entry
        return bound_key == key and expires >= time.time()

    def check(self, token: str, key: str) -> bool:
        """只校验，不作废。

        批量写入时先整体验一遍：任何一张不合法就整批拒掉，
        不能写了一半才失败。
        """
        with self._lock:
            entry = self._tokens.get(token)
        if entry is None:
            return False
        bound_key, expires = entry
        return bound_key == key and expires >= time.time()


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True
    # 默认 backlog 只有 5：页面上千张图同时请求时，新连接会被直接拒掉
    # （浏览器报 ERR_CONNECTION_REFUSED）
    request_queue_size = 128

    def __init__(self, address, handler, library, assets_dir, shell, full_cache):
        super().__init__(address, handler)
        # 最近一次请求时间 + 页面关闭信号（启动器靠这两个判断网页还在不在）
        self.last_activity = time.monotonic()
        self.bye_at: float | None = None
        self.library = library
        self.assets_dir = Path(assets_dir)
        self.shell = shell
        self.full_cache = full_cache
        self.tiles: list[dict] = []
        self.engine_options: dict = {}
        self.poll_ms = 2000
        self.session = ""
        self.tokens = TokenStore()
        self.geocoder = Geocoder()

    def switch_roots(self, roots: list[Path]) -> None:
        """换一组照片目录：重建库，端口和页面都不用变。"""
        library = self.library
        new_library = PhotoLibrary(
            roots,
            library.assets_dir,
            coord_system=library.coord_system,
            recursive=library.recursive,
            default_location=library.default_location,
            location_table=library.location_table,
            write_exif=library.write_exif,
            backup_dir=library.backup_dir,
        )
        new_library.refresh()
        watcher = getattr(self, "watcher", None)
        if watcher is not None:
            watcher.stop()
            watcher.join(timeout=5)
        self.library = new_library
        if watcher is not None:
            replacement = Watcher(new_library, watcher.interval)
            replacement.start()
            self.watcher = replacement



class Watcher(threading.Thread):
    """后台定时扫描目录，照片有变动就更新库。"""

    def __init__(self, library: PhotoLibrary, interval: float):
        super().__init__(daemon=True)
        self.library = library
        self.interval = max(0.5, interval)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            started = time.monotonic()
            try:
                if self.library.refresh():
                    data = self.library.rows()
                    log.info(
                        "照片目录有变化，已定位 %d 张，待确认 %d 张",
                        len(data["photos"]),
                        len(data["pending"]),
                    )
            except Exception:  # pragma: no cover - 轮询不能因单次失败而退出
                log.exception("扫描目录失败")
            finally:
                self._pause(time.monotonic() - started)

    def _pause(self, spent: float) -> None:
        """一次扫描比轮询间隔还久时先歇一下。

        上万个目录扫一次要好几秒，而轮询间隔只有 2 秒——
        不歇的话就变成不间断扫盘，白白占满一个 CPU 核。
        """
        if spent >= self.interval:
            self._stop.wait(min(spent, self.interval * 3))

    def stop(self) -> None:
        self._stop.set()


def build_server(
    root: Path,
    tiles: list[dict],
    workdir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    coord_system: str = "wgs84",
    recursive: bool = True,
    default_location: tuple[float, float] | None = None,
    location_table: dict[str, tuple[float, float]] | None = None,
    poll_ms: int = 2000,
    api_key: str | None = None,
    baidu_ak: str | None = None,
    amap_key: str | None = None,
    write_exif: bool = False,
    progress=None,
) -> LiveServer:
    """搭好服务（不启动监听），供测试与 serve() 使用。"""
    roots = [Path(item) for item in (root if isinstance(root, (list, tuple)) else [root])]
    if not roots:
        raise FileNotFoundError("至少要有一个照片目录")
    missing = [item for item in roots if not item.exists()]
    for item in missing:
        log.warning("照片目录不存在，跳过：%s", item)
    roots = [item for item in roots if item.exists()]
    if not roots:
        raise FileNotFoundError(f"目录不存在: {missing[0] if missing else root}")

    base = Path(workdir) if workdir else default_workdir(roots)
    assets_dir = base / "files"
    install_static_files(assets_dir)

    library = PhotoLibrary(
        roots,
        assets_dir,
        coord_system=coord_system,
        recursive=recursive,
        default_location=default_location,
        location_table=location_table,
        write_exif=write_exif,
        backup_dir=base / "backups",
    )
    library.refresh(progress=progress)

    engine_options = {
        "api_key": api_key or "",
        "ak": baidu_ak or "",
    }
    engines = _engine_list(engine_options)
    session = uuid4().hex
    keys = {
        "google": engine_options.get("api_key") or "",
        "baidu": engine_options.get("ak") or "",
    }
    shell = render_shell(engines, tiles, poll_ms, session, keys, ai_available=_ai_available())

    server = LiveServer(
        (host, port),
        LiveHandler,
        library,
        assets_dir,
        shell,
        _FullCache(),
    )
    # 防 DNS rebinding：Host 必须是本机地址（含实际端口）
    server.bind_host = host
    server.listen_port = server.server_address[1]
    server.exposed = host not in LOOPBACK_HOSTS
    if server.exposed:
        log.warning("服务监听在 %s（非本机地址），局域网内其它设备可访问并改写定位", host)
        print(f"警告：服务监听在 {host}，不是本机地址，局域网内其它设备可以访问并改写定位", flush=True)

    server.tiles = tiles
    server.engine_options = engine_options
    server.poll_ms = poll_ms
    server.session = session
    server.workdir = base
    server.geocoder = Geocoder(amap_key=amap_key)
    return server


def _ai_available() -> bool:
    """AI 识别能不能用（装了库且有凭据）。"""
    try:
        from . import ai

        return ai.available()[0]
    except Exception:  # pragma: no cover - 兜底
        return False


def _engine_list(engine_options: dict) -> list[dict]:
    """列出可用的地图引擎，缺密钥的标记为不可用。"""
    result = []
    for engine_id, spec in ENGINES.items():
        option = spec.get("option")
        credential = engine_options.get(option) if option else ""
        result.append(
            {
                "id": engine_id,
                "label": spec["label"],
                "available": bool(credential) if option else True,
                "reason": "" if (credential or not option) else "缺少密钥",
            }
        )
    return result


def serve(
    root: Path,
    tiles: list[dict],
    workdir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    coord_system: str = "wgs84",
    recursive: bool = True,
    default_location: tuple[float, float] | None = None,
    location_table: dict[str, tuple[float, float]] | None = None,
    poll_ms: int = 2000,
    api_key: str | None = None,
    baidu_ak: str | None = None,
    amap_key: str | None = None,
    write_exif: bool = False,
    open_browser: bool = True,
) -> None:
    """启动服务并一直运行。"""
    server = build_server(
        root,
        tiles,
        workdir=workdir,
        host=host,
        port=port,
        coord_system=coord_system,
        recursive=recursive,
        default_location=default_location,
        location_table=location_table,
        poll_ms=poll_ms,
        api_key=api_key,
        baidu_ak=baidu_ak,
        amap_key=amap_key,
        write_exif=write_exif,
    )

    watcher = Watcher(server.library, poll_ms / 1000.0)
    watcher.start()
    server.watcher = watcher

    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"照片地图服务已启动：{url}")
    print(f"  照片目录：{Path(root).resolve()}")
    print(f"  工作目录：{server.workdir}")
    print(f"  底图：{'、'.join(t['name'] for t in tiles) or '无'}")
    print(f"  地图引擎：{'、'.join(e['label'] for e in _engine_list(server.engine_options))}")
    if write_exif:
        print("  确认位置后会同时改写原文件的 EXIF（原文件备份在工作目录的 backups/）")
    print("按 Ctrl+C 停止")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()
