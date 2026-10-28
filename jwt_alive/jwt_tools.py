"""JWT 解码与过期时间解析。

不同网站把过期时间放在不同字段（exp / expireTime / expires_in ...），
这里提供统一的解析入口，字段名与格式均可按站点配置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from jwt_alive.models import ExpiryFormat, ExpiryInfo, JwtConfig
from jwt_alive.utils import now_utc


_EPOCH_MS_THRESHOLD = 1e11


class JwtDecodeError(Exception):
    """JWT 解析失败。"""


def decode_payload(token: str, verify: bool = False, key: str = '') -> dict[str, Any]:
    """解构 JWT 拿到 payload。

    默认不校验签名（保活场景通常拿不到签名密钥），只关心声明内容。
    """
    if not token or not token.strip():
        raise JwtDecodeError('token 为空')
    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=['HS256', 'HS384', 'HS512', 'RS256'],
            options={
                'verify_signature': verify,
                'verify_exp': False,
                'verify_aud': False,
                'verify_iat': False,
                'verify_nbf': False,
            },
        )
    except jwt.PyJWTError as exc:
        raise JwtDecodeError(f'JWT 解析失败: {exc}') from exc
    if not isinstance(payload, dict):
        raise JwtDecodeError('JWT payload 不是对象')
    return payload


def get_by_path(data: Any, path: str) -> Any:
    """按点路径取值，支持列表下标，如 data.list.0.id。"""
    if not path:
        return data
    current = data
    for part in path.split('.'):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return None
            if -len(current) <= index < len(current):
                current = current[index]
                continue
            return None
        return None
    return current


def _parse_iso8601(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_epoch(value: float) -> datetime:
    """按数值大小自动区分秒 / 毫秒时间戳。"""
    if abs(value) >= _EPOCH_MS_THRESHOLD:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return datetime.fromtimestamp(value, tz=UTC)


def _detect_format(value: Any) -> ExpiryFormat | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return ExpiryFormat.EPOCH_MS if abs(float(value)) >= _EPOCH_MS_THRESHOLD else ExpiryFormat.EPOCH_S
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            return ExpiryFormat.ISO8601 if _parse_iso8601(text) else None
        return ExpiryFormat.EPOCH_MS if abs(numeric) >= _EPOCH_MS_THRESHOLD else ExpiryFormat.EPOCH_S
    return None


def _to_datetime(value: Any, fmt: ExpiryFormat) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if fmt in (ExpiryFormat.EPOCH_S, ExpiryFormat.EPOCH_MS):
        if isinstance(value, str):
            try:
                numeric = float(value.strip())
            except ValueError:
                return None
        elif isinstance(value, (int, float)):
            numeric = float(value)
        else:
            return None
        if fmt == ExpiryFormat.EPOCH_MS:
            return datetime.fromtimestamp(numeric / 1000, tz=UTC)
        return datetime.fromtimestamp(numeric, tz=UTC)
    if fmt == ExpiryFormat.ISO8601:
        if isinstance(value, (int, float)):
            return _parse_epoch(float(value))
        if isinstance(value, str):
            return _parse_iso8601(value)
        return None
    if fmt == ExpiryFormat.DURATION_S:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return now_utc() + timedelta(seconds=seconds)
    return None


def resolve_expiry(
    payload: dict[str, Any],
    jwt_config: JwtConfig | None = None,
    now: datetime | None = None,
) -> ExpiryInfo:
    """按配置解析 payload 中的过期时间。

    - expiry_field 支持点路径
    - expiry_format=auto 时自动区分 秒/毫秒/ISO8601
    - expiry_format=duration_s 时以 iat（若存在）为起点计算绝对到期时间
    """
    cfg = jwt_config or JwtConfig()
    reference = now or now_utc()
    raw = get_by_path(payload, cfg.expiry_field)

    if raw is None:
        return ExpiryInfo(expires_at=None, field=cfg.expiry_field, raw=None, resolved=False)

    fmt = cfg.expiry_format
    if fmt == ExpiryFormat.AUTO:
        fmt = _detect_format(raw) or ExpiryFormat.EPOCH_S
        # 数值明显是"时长"而不是时间戳时（如 expires_in=3600），按相对时长处理
        if fmt in (ExpiryFormat.EPOCH_S, ExpiryFormat.EPOCH_MS) and isinstance(raw, (int, float)) and not isinstance(raw, bool) and float(raw) < 1_000_000:
            fmt = ExpiryFormat.DURATION_S

    if fmt == ExpiryFormat.DURATION_S:
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return ExpiryInfo(expires_at=None, field=cfg.expiry_field, raw=raw, format=fmt, resolved=False)
        base = reference
        iat_raw = get_by_path(payload, cfg.iat_field)
        if isinstance(iat_raw, (int, float)) and not isinstance(iat_raw, bool):
            base = _parse_epoch(float(iat_raw))
        expires_at = base + timedelta(seconds=seconds)
        return ExpiryInfo(expires_at=expires_at, field=cfg.expiry_field, raw=raw, format=fmt, resolved=True)

    parsed = _to_datetime(raw, fmt)
    return ExpiryInfo(
        expires_at=parsed,
        field=cfg.expiry_field,
        raw=raw,
        format=fmt,
        resolved=parsed is not None,
    )


def seconds_remaining(info: ExpiryInfo, now: datetime | None = None, leeway: int = 0) -> float | None:
    """剩余寿命（秒）。无法解析时返回 None。"""
    if info.expires_at is None:
        return None
    reference = now or now_utc()
    return (info.expires_at - reference).total_seconds() + leeway


def is_expired(info: ExpiryInfo, now: datetime | None = None, leeway: int = 0) -> bool:
    """是否已过期（考虑时钟偏移容忍）。"""
    remaining = seconds_remaining(info, now=now, leeway=leeway)
    if remaining is None:
        return False
    return remaining <= 0


def decode_and_resolve(
    token: str,
    jwt_config: JwtConfig | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], ExpiryInfo]:
    """解构 JWT 并解析过期时间。"""
    payload = decode_payload(token)
    return payload, resolve_expiry(payload, jwt_config, now=now)
