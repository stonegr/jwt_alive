"""SSL/网络错误的处理：日志分级、错误描述、prefer_ipv4。"""

from __future__ import annotations

import logging
import ssl
from typing import Any

import httpx
import pytest

from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.config import parse_config
from jwt_alive.http_client import build_client
from jwt_alive.utils import describe_error

from .conftest import make_token


PROFILE_URL = 'https://example.com/api/user/profile'


# -- describe_error ----------------------------------------------------------------------


def test_describe_error_ssl() -> None:
    cause = ssl.SSLError('[SSL] unknown error')
    exc = httpx.ConnectError('[SSL] unknown error')
    exc.__cause__ = cause
    detail = describe_error(exc)
    assert 'ConnectError' in detail
    assert 'SSL 错误' in detail
    assert 'SSLError' in detail


def test_describe_error_finds_inner_ssl_error() -> None:
    """真实异常链：httpx.ConnectError → httpcore.ConnectError → ssl.SSLError。

    SSLError 在最内层的 __context__ 里，只看一层会误判成"连接失败"。
    """
    ssl_error = ssl.SSLError('[SSL] unknown error (_ssl.c:1011)')
    inner = httpx.ConnectError('[SSL] unknown error (_ssl.c:1011)')
    inner.__context__ = ssl_error
    outer = httpx.ConnectError('[SSL] unknown error (_ssl.c:1011)]')
    outer.__cause__ = inner

    detail = describe_error(outer)
    assert 'SSL 错误' in detail
    assert '原因: SSLError' in detail
    assert '连接失败' not in detail


def test_describe_error_no_self_reference() -> None:
    exc = httpx.ConnectError('boom')
    exc.__cause__ = exc
    detail = describe_error(exc)
    assert '原因' not in detail
    assert '连接失败' in detail


def test_describe_error_timeout() -> None:
    detail = describe_error(httpx.ReadTimeout('too slow'))
    assert '超时' in detail


def test_describe_error_connect() -> None:
    detail = describe_error(httpx.ConnectError('connection refused'))
    assert '连接失败' in detail


def test_describe_error_plain() -> None:
    detail = describe_error(ValueError('boom'))
    assert 'ValueError' in detail
    assert 'boom' in detail


# -- prefer_ipv4 -------------------------------------------------------------------------


def _site(**overrides: Any) -> Any:
    data: dict[str, Any] = {
        'name': 'demo',
        'token': {'access_token': make_token()},
        'refresh': {'url': 'https://example.com/refresh'},
        'login_check': {'url': PROFILE_URL, 'method': 'GET'},
    }
    data.update(overrides)
    config = parse_config({'sites': [data]})
    return config.sites[0]


def test_prefer_ipv4_uses_bound_transport() -> None:
    site = _site(prefer_ipv4=True)
    client = build_client(site)
    try:
        transport = client._transport  # noqa: SLF001
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._local_address == '0.0.0.0'  # noqa: SLF001
    finally:
        client.close()


def test_default_client_has_no_bound_transport() -> None:
    site = _site()
    client = build_client(site)
    try:
        transport = client._transport  # noqa: SLF001
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._local_address is None  # noqa: SLF001
    finally:
        client.close()


def test_prefer_ipv4_still_sends_requests(httpx_mock: Any) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'ok': True})
    site = _site(prefer_ipv4=True)
    client = build_client(site)
    try:
        response = client.get(PROFILE_URL)
        assert response.status_code == 200
    finally:
        client.close()


# -- 日志分级 ----------------------------------------------------------------------------


def _adapter(site: Any) -> HttpSiteAdapter:
    return HttpSiteAdapter(site=site, client=build_client(site), logger=logging.getLogger('test'))


def test_retry_success_logs_info(httpx_mock: Any, caplog: pytest.LogCaptureFixture) -> None:
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=PROFILE_URL)
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'ok': True})
    site = _site(retry_attempts=3, retry_backoff_seconds=0)
    adapter = _adapter(site)

    caplog.set_level(logging.INFO, logger='test')
    result = adapter.check_login(adapter.site.token and _state(site))

    assert result.logged_in is True
    records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any('重试中' in r.getMessage() for r in records)
    assert any('重试后成功' in r.getMessage() for r in records)
    # 不应有 WARNING
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_all_attempts_fail_logs_warning(httpx_mock: Any, caplog: pytest.LogCaptureFixture) -> None:
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=PROFILE_URL)
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=PROFILE_URL)
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=PROFILE_URL)
    site = _site(retry_attempts=3, retry_backoff_seconds=0)
    adapter = _adapter(site)

    caplog.set_level(logging.INFO, logger='test')
    result = adapter.check_login(_state(site))

    assert result.logged_in is None
    assert result.detail and '连接失败' in result.detail
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert '3 次尝试后' in warnings[-1].getMessage()


def _state(site: Any) -> Any:
    from jwt_alive.models import SiteState

    return SiteState(name=site.name, access_token=site.token.access_token)
