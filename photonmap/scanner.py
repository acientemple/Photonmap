"""扫描目录，找出所有可能带 EXIF 的图片文件。"""

from __future__ import annotations

import os
from pathlib import Path

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".dng",
    ".livp",  # vivo 的 Live Photo 容器，内嵌静态图
}


def scan_directory(
    root: str | Path,
    recursive: bool = True,
    extensions: set[str] | None = None,
) -> list[Path]:
    """返回目录下所有图片路径，按路径排序。"""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {root}")
    if root.is_file():
        return [root] if root.suffix.lower() in (extensions or IMAGE_EXTENSIONS) else []

    exts = {e.lower() for e in (extensions or IMAGE_EXTENSIONS)}
    found: list[Path] = []

    def collect(directory: str) -> None:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            pending_dirs.append(entry.path)
                        continue
                    if os.path.splitext(entry.name)[1].lower() in exts:
                        found.append(Path(entry.path))
        except OSError:
            # 权限不足 / 同步软件临时锁住目录：跳过，别让整次扫描失败
            return

    # 用 os.scandir 而不是 Path.glob("**/*")：上万个目录时快 3 倍以上，
    # 而且顺手拿到了文件类型，不用再对每个条目 stat 一遍。
    pending_dirs = [str(root)]
    while pending_dirs:
        collect(pending_dirs.pop())
    return sorted(found)
