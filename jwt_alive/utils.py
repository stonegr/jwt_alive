"""通用小工具。"""

from __future__ import annotations

from datetime import UTC, datetime
import ssl

import httpx


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


def describe_error(exc: BaseException) -> str:
    """把网络异常描述成便于排查的字符串。

    典型输出：
        ConnectError: [SSL] unknown error | 原因: SSLError: [SSL] unknown error | SSL 错误（常见于 IPv6 半通 / 中间设备干扰 / 服务端抖动）
    """
    parts = [f'{type(exc).__name__}: {exc}']

    cause = exc.__cause__
    if cause is not None and cause is not exc:
        parts.append(f'原因: {type(cause).__name__}: {cause}')
        errno = getattr(cause, 'errno', None)
        if errno:
            parts.append(f'errno={errno}')

    if isinstance(cause, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        parts.append('SSL 错误（常见于 IPv6 半通 / 中间设备干扰 / 服务端抖动）')
    elif isinstance(exc, httpx.TimeoutException):
        parts.append('超时')
    elif isinstance(exc, httpx.ConnectError):
        parts.append('连接失败')
    elif isinstance(exc, httpx.HTTPStatusError):
        parts.append('HTTP 状态错误')

    return ' | '.join(parts)
