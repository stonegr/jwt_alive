"""通用小工具。"""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def to_local(value: datetime | None) -> datetime | None:
    """转成当前系统本地时区（用于展示）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone()


def mask_token(token: str | None, keep: int = 4) -> str:
    """日志脱敏：只保留前后若干字符。"""
    if not token:
        return '<empty>'
    if len(token) <= keep * 2:
        return '*' * len(token)
    return f'{token[:keep]}...{token[-keep:]}'


def truncate(text: str | None, limit: int = 200) -> str:
    """截断长文本，避免日志刷屏。"""
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + f'...<truncated {len(text) - limit} chars>'
