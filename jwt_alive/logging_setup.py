"""日志配置：rich 控制台 + 轮转文件。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler


LOG_FORMAT = '%(asctime)s %(levelname)-7s %(name)s | %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def setup_logging(
    level: str = 'INFO',
    log_file: str | Path | None = None,
    console: bool = True,
) -> Path | None:
    """初始化日志，返回日志文件路径（未配置时为 None）。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if console:
        rich_handler = RichHandler(rich_tracebacks=True, show_path=False, show_time=True)
        rich_handler.setFormatter(logging.Formatter('%(message)s', datefmt=DATE_FORMAT))
        root.addHandler(rich_handler)

    file_path: Path | None = None
    if log_file is not None:
        file_path = Path(log_file).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(file_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(file_handler)

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    return file_path
