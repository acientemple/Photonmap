"""给缺少 GPS 的照片补坐标。

两种来源：
- 命令行给一个默认点（``--location 纬度,经度``），适用于所有照片在同一处的情况；
- CSV 逐张指定（``--locations coords.csv``），格式为 ``文件名,纬度,经度``。
"""

from __future__ import annotations

import csv
from pathlib import Path


def parse_location(text: str) -> tuple[float, float]:
    """解析 "31.2304,121.4737" 形式的坐标，返回 (纬度, 经度)。"""
    parts = [p.strip() for p in str(text).replace("，", ",").split(",")]
    if len(parts) != 2:
        raise ValueError(f"坐标格式应为 '纬度,经度'，收到: {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError(f"坐标必须是数字，收到: {text!r}") from None
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"纬度超出 [-90, 90]: {text!r}")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"经度超出 [-180, 180]: {text!r}")
    return lat, lon


def load_locations(path: str | Path) -> dict[str, tuple[float, float]]:
    """读 CSV，返回 {文件名: (纬度, 经度)}；表头行会被跳过。"""
    table: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            if row[0].strip().lower() in ("filename", "name", "file", "文件", "文件名"):
                continue
            if len(row) < 3:
                raise ValueError(f"CSV 每行需要 3 列（文件名,纬度,经度），收到: {row!r}")
            table[row[0].strip()] = parse_location(f"{row[1]},{row[2]}")
    return table
