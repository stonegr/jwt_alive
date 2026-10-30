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


def _exception_chain(exc: BaseException, max_depth: int = 5) -> list[BaseException]:
    """沿 __cause__ 与 __context__ 收集异常链（去重、限深）。

    httpx 的异常链形如：
        httpx.ConnectError → __cause__ → httpcore.ConnectError → __context__ → ssl.SSLError
    真正的根因（SSLError）在最内层，只看一层会误判。
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack and len(chain) < max_depth:
        current = stack.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for link in (current.__cause__, current.__context__):
            if link is not None and id(link) not in seen:
                stack.append(link)
    return chain


def describe_error(exc: BaseException) -> str:
    """把网络异常描述成便于排查的字符串。

    典型输出：
        ConnectError: [SSL] unknown error | 原因: SSLError: [SSL] unknown error | SSL 错误（常见于 IPv6 半通 / 中间设备干扰 / 服务端抖动）
    """
    parts = [f'{type(exc).__name__}: {exc}']

    chain = _exception_chain(exc)
    innermost = chain[-1] if len(chain) > 1 else None
    if innermost is not None:
        parts.append(f'原因: {type(innermost).__name__}: {innermost}')
        errno = getattr(innermost, 'errno', None)
        if errno:
            parts.append(f'errno={errno}')

    if any(isinstance(e, ssl.SSLError) for e in chain):
        parts.append('SSL 错误（常见于 IPv6 半通 / 中间设备干扰 / 服务端抖动）')
    elif isinstance(exc, httpx.TimeoutException):
        parts.append('超时')
    elif isinstance(exc, httpx.ConnectError):
        parts.append('连接失败')
    elif isinstance(exc, httpx.HTTPStatusError):
        parts.append('HTTP 状态错误')

    return ' | '.join(parts)
