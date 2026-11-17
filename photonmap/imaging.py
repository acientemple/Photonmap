"""按统一的入口打开照片图像。

系统相机各写各的容器格式，同一张照片可能是 JPEG、HEIC，
vivo 的 Live Photo 更是把静态图和短视频打包成 .livp（本质是 zip）。
这里把差异收敛掉，上层只管用 Pillow 的方式打开。
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

try:  # iPhone 的 HEIC 格式需要额外插件
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于环境
    HEIF_AVAILABLE = False

# .livp 容器里静态图的扩展名
_LIVP_IMAGE_SUFFIXES = (".heic", ".heif", ".jpg", ".jpeg")


def extract_livp_image(path: Path) -> bytes:
    """从 vivo 的 .livp 容器里取出内嵌的静态照片。"""
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.lower().endswith(_LIVP_IMAGE_SUFFIXES):
                return archive.read(name)
    raise ValueError(f"容器里没有静态照片: {path}")


@contextmanager
def open_image(path: Path):
    """打开照片图像，产出与 `PIL.Image.open` 同样的上下文管理器。"""
    path = Path(path)
    if path.suffix.lower() == ".livp":
        with Image.open(io.BytesIO(extract_livp_image(path))) as img:
            yield img
    else:
        with Image.open(path) as img:
            yield img
