"""导出器基类：所有平台共用同一套接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import PhotoRecord


class BaseExporter(ABC):
    """把所有 PhotoRecord 变成目标平台能读的文件的统一入口。"""

    name: str = "base"
    extension: str = ""
    coord_system: str = "wgs84"

    def __init__(self, **options):
        self.options = options

    @abstractmethod
    def export(self, photos: list[PhotoRecord], output_dir: Path) -> Path:
        """执行导出，返回生成的文件路径。"""

    def output_path(self, output_dir: Path) -> Path:
        return Path(output_dir) / f"{self.name}{self.extension}"
