"""写文件时对付同步软件的临时锁定。

OneDrive / 百度云这类同步客户端会在文件写入或改名时短暂占住文件，
Windows 上表现为 PermissionError。重试几次通常就过去了。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 6
DEFAULT_DELAY = 0.2


def replace_with_retry(
    src: Path,
    dst: Path,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> None:
    """把 src 改名成 dst，遇到临时锁定就退避重试。"""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def copy_with_retry(
    src: Path,
    dst: Path,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> None:
    """复制文件，遇到临时锁定就退避重试。"""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            shutil.copyfile(src, dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def discard(path: Path) -> None:
    """删掉临时文件；删不掉就算了，不该因此中断流程。"""
    try:
        path.unlink()
    except OSError:
        pass


def write_text_atomic(
    path: Path,
    text: str,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> None:
    """原子地写一个文本文件：先写 .part，再改名过去。

    改名走 replace_with_retry，所以同步软件的临时锁定会被退避重试扛过去。
    半截文件不会留在目标路径上——旁车文件要是写坏了，
    读的时候会被当成没有位置，照片莫名其妙地回到待确认。
    """
    path = Path(path)
    temp = path.with_name(path.name + ".part")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            temp.write_text(text, encoding="utf-8")
            replace_with_retry(temp, path, attempts=attempts, delay=delay)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (attempt + 1))
    discard(temp)
    raise last  # type: ignore[misc]
