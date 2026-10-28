"""JWT 解码与过期时间解析测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jwt_alive.jwt_tools import (
    JwtDecodeError,
    decode_payload,
    get_by_path,
    is_expired,
    resolve_expiry,
    seconds_remaining,
)
from jwt_alive.models import ExpiryFormat, JwtConfig

from .conftest import make_token


def test_decode_payload_returns_claims() -> None:
    token = make_token({'role': 'admin'})
    payload = decode_payload(token)
    assert payload['sub'] == 'tester'
    assert payload['role'] == 'admin'


def test_decode_payload_rejects_garbage() -> None:
    with pytest.raises(JwtDecodeError):
        decode_payload('not-a-jwt')


def test_decode_payload_rejects_empty() -> None:
    with pytest.raises(JwtDecodeError):
        decode_payload('')


def test_resolve_expiry_epoch_seconds() -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    payload = {'exp': int(expires_at.timestamp())}
    info = resolve_expiry(payload, JwtConfig(expiry_field='exp'))
    assert info.resolved
    assert info.expires_at is not None
    assert abs((info.expires_at - expires_at).total_seconds()) < 1


def test_resolve_expiry_epoch_milliseconds() -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    payload = {'exp': int(expires_at.timestamp() * 1000)}
    info = resolve_expiry(payload, JwtConfig(expiry_field='exp'))
    assert info.resolved
    assert info.expires_at is not None
    assert abs((info.expires_at - expires_at).total_seconds()) < 1


def test_resolve_expiry_iso8601() -> None:
    expires_at = datetime.now(UTC) + timedelta(days=1)
    payload = {'expireTime': expires_at.isoformat().replace('+00:00', 'Z')}
    info = resolve_expiry(payload, JwtConfig(expiry_field='expireTime'))
    assert info.resolved
    assert info.expires_at is not None
    assert abs((info.expires_at - expires_at).total_seconds()) < 1


def test_resolve_expiry_custom_nested_field() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    payload = {'data': {'expireTime': int(expires_at.timestamp())}}
    info = resolve_expiry(payload, JwtConfig(expiry_field='data.expireTime'))
    assert info.resolved
    assert info.expires_at is not None


def test_resolve_expiry_duration_uses_iat() -> None:
    issued = datetime.now(UTC) - timedelta(minutes=5)
    payload = {'iat': int(issued.timestamp()), 'expires_in': 3600}
    info = resolve_expiry(payload, JwtConfig(expiry_field='expires_in'))
    assert info.resolved
    assert info.format == ExpiryFormat.DURATION_S
    assert info.expires_at is not None
    assert abs((info.expires_at - (issued + timedelta(hours=1))).total_seconds()) < 1


def test_resolve_expiry_duration_without_iat_uses_now() -> None:
    now = datetime.now(UTC)
    info = resolve_expiry({'expires_in': 60}, JwtConfig(expiry_field='expires_in'), now=now)
    assert info.resolved
    assert info.expires_at is not None
    assert abs((info.expires_at - (now + timedelta(seconds=60))).total_seconds()) < 1


def test_resolve_expiry_missing_field() -> None:
    info = resolve_expiry({'sub': 'x'}, JwtConfig(expiry_field='exp'))
    assert not info.resolved
    assert info.expires_at is None


def test_resolve_expiry_explicit_format_wins() -> None:
    payload = {'exp': '2026-01-01T00:00:00Z'}
    info = resolve_expiry(payload, JwtConfig(expiry_field='exp', expiry_format=ExpiryFormat.ISO8601))
    assert info.resolved
    assert info.expires_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_seconds_remaining_and_is_expired() -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    info = resolve_expiry({'exp': int(past.timestamp())})
    remaining = seconds_remaining(info)
    assert remaining is not None and remaining < 0
    assert is_expired(info)

    future = datetime.now(UTC) + timedelta(minutes=1)
    info2 = resolve_expiry({'exp': int(future.timestamp())})
    assert not is_expired(info2)
    assert not is_expired(info2, leeway=30)


def test_get_by_path_with_list_index() -> None:
    data = {'items': [{'id': 7}]}
    assert get_by_path(data, 'items.0.id') == 7
    assert get_by_path(data, 'items.1.id') is None
    assert get_by_path(data, 'missing.key') is None
