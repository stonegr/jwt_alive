"""刷新策略（schedule.mode）与按需刷新的测试。"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import jwt

from jwt_alive.api import create_app
from jwt_alive.config import parse_config
from jwt_alive.models import CheckKind
from jwt_alive.scheduler import KeepAliveScheduler
from jwt_alive.service import JwtAliveService


SECRET = 'x' * 40
PROFILE_URL = 'https://example.com/api/user/profile'
REFRESH_URL = 'https://example.com/api/auth/refresh'


def _token(expires_in: int) -> str:
    now = int(time.time())
    return jwt.encode({'sub': 'tester', 'iat': now, 'exp': now + expires_in}, SECRET, algorithm='HS256')


def _site(
    access_expires_in: int = 3600,
    refresh_expires_in: int = 14 * 24 * 3600,
    mode: str = 'access',
    **overrides: Any,
) -> dict[str, Any]:
    site: dict[str, Any] = {
        'name': 'demo',
        'token': {
            'access_token': _token(access_expires_in),
            'refresh_token': _token(refresh_expires_in),
        },
        'jwt': {'expiry_field': 'exp'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'access_token_path': 'data.access_token',
        },
        'login_check': {'url': PROFILE_URL, 'method': 'GET'},
        'schedule': {'mode': mode},
    }
    site.update(overrides)
    return site


def _service(tmp_path: Path, site: dict[str, Any]) -> JwtAliveService:
    config = parse_config({'sites': [site]})
    return JwtAliveService(config=config, state_path=tmp_path / 'state.json')


# -- needs_refresh 的三种模式 ------------------------------------------------------------


def test_access_mode_refreshes_when_access_near_expiry(tmp_path: Path) -> None:
    site = _site(access_expires_in=60, mode='access')
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.needs_refresh()
    finally:
        service.close()


def test_access_mode_no_refresh_when_access_fresh(tmp_path: Path) -> None:
    site = _site(access_expires_in=7200, mode='access')
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert not runtime.needs_refresh()
    finally:
        service.close()


def test_refresh_mode_refreshes_when_refresh_token_near_expiry(tmp_path: Path) -> None:
    """access 还很新，但 refresh token 快到期 → 刷。"""
    site = _site(access_expires_in=7200, refresh_expires_in=60, mode='refresh')
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.needs_refresh()
    finally:
        service.close()


def test_refresh_mode_no_refresh_when_refresh_token_fresh(tmp_path: Path) -> None:
    """access 已过期但 refresh token 还很新 → 不刷（低频保活）。"""
    site = _site(access_expires_in=60, refresh_expires_in=14 * 24 * 3600, mode='refresh')
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert not runtime.needs_refresh()
    finally:
        service.close()


def test_hybrid_mode_triggers_on_either(tmp_path: Path) -> None:
    site_a = _site(access_expires_in=60, refresh_expires_in=14 * 24 * 3600, mode='hybrid')
    site_b = _site(access_expires_in=7200, refresh_expires_in=60, mode='hybrid')
    s1 = _service(tmp_path, site_a)
    s2 = _service(tmp_path, site_b)
    try:
        r1 = s1.get_runtime('demo')
        r2 = s2.get_runtime('demo')
        assert r1 is not None and r2 is not None
        assert r1.needs_refresh()
        assert r2.needs_refresh()
    finally:
        s1.close()
        s2.close()


def test_refresh_mode_falls_back_to_access_when_refresh_not_jwt(tmp_path: Path) -> None:
    """refresh token 不是 JWT → 退回按 access token 判断。"""
    site = _site(access_expires_in=60, mode='refresh')
    site['token']['refresh_token'] = 'opaque-refresh-token'
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.refresh_token_remaining() is None
        assert runtime.needs_refresh()  # 退回 access 策略
    finally:
        service.close()


def test_refresh_expiry_is_persisted(tmp_path: Path) -> None:
    site = _site()
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.refresh_expires_at is not None
        service.store.save()
    finally:
        service.close()

    data = json.loads((tmp_path / 'state.json').read_text(encoding='utf-8'))
    assert data['sites']['demo']['refresh_expires_at'] is not None


# -- check() 的结论分类 -------------------------------------------------------------------


def test_check_kind_token_expired(tmp_path: Path) -> None:
    site = _site(access_expires_in=-60)
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        result = runtime.check()
        assert result.kind == CheckKind.TOKEN_EXPIRED
    finally:
        service.close()


def test_check_kind_logged_out(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', status_code=401, json={})
    site = _site(access_expires_in=3600)
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        result = runtime.check()
        assert result.kind == CheckKind.LOGGED_OUT
    finally:
        service.close()


def test_check_kind_unknown_on_network_error(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=PROFILE_URL)
    site = _site(access_expires_in=3600)
    site['retry_attempts'] = 1
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        result = runtime.check()
        assert result.kind == CheckKind.UNKNOWN
    finally:
        service.close()


def test_refresh_mode_skips_login_check(httpx_mock: Any, tmp_path: Path) -> None:
    """mode=refresh 时不调用登录态校验接口（省流量）。

    这里故意不注册 PROFILE_URL 的 mock：一旦发起请求，pytest-httpx 会直接报错。
    """
    site = _site(access_expires_in=3600, mode='refresh')
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        result = runtime.check()
        assert result.kind == CheckKind.LOGGED_IN
    finally:
        service.close()


# -- 调度器 --------------------------------------------------------------------------------


def test_scheduler_refresh_mode_skips_expired_access(httpx_mock: Any, tmp_path: Path) -> None:
    """mode=refresh 下 access 过期不触发刷新。"""
    import logging

    site = _site(access_expires_in=-60, refresh_expires_in=14 * 24 * 3600, mode='refresh')
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is None
    finally:
        scheduler.shutdown()
        service.close()


def test_scheduler_refresh_mode_refreshes_when_refresh_token_due(httpx_mock: Any, tmp_path: Path) -> None:
    import logging

    new_access = _token(7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_access}})
    site = _site(access_expires_in=7200, refresh_expires_in=60, mode='refresh')
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is not None
        assert runtime.state.access_token == new_access
    finally:
        scheduler.shutdown()
        service.close()


def test_scheduler_logged_out_refreshes_in_access_mode(httpx_mock: Any, tmp_path: Path) -> None:
    """mode=access 下服务端 401 → 触发刷新。"""
    import logging

    httpx_mock.add_response(url=PROFILE_URL, method='GET', status_code=401, json={})
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': _token(7200)}})
    site = _site(access_expires_in=3600, mode='access')
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is not None
    finally:
        scheduler.shutdown()
        service.close()


def test_refresh_mode_detects_revocation_at_refresh_time(httpx_mock: Any, tmp_path: Path) -> None:
    """mode=refresh 下会话被吊销 → 由真正刷新那一次发现（失败记错误）。"""
    import logging

    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={})
    site = _site(access_expires_in=7200, refresh_expires_in=60, mode='refresh')
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is None  # 刷新失败
        assert runtime.state.last_error  # 记录了错误（会话可能已失效）
        assert runtime.state.consecutive_failures == 1
    finally:
        scheduler.shutdown()
        service.close()


# -- API 按需刷新 ---------------------------------------------------------------------------


def _client(tmp_path: Path, site: dict[str, Any]) -> TestClient:
    config = parse_config({'auth': {'cli': 'tok'}, 'sites': [site]})
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    return TestClient(create_app(service, config))


def test_api_refreshes_on_demand_when_expired(httpx_mock: Any, tmp_path: Path) -> None:
    new_access = _token(7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_access}})
    client = _client(tmp_path, _site(access_expires_in=-60))
    resp = client.get('/api/sites/demo/token', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    data = resp.json()
    assert data['refreshed'] is True
    assert data['access_token'] == new_access


def test_api_does_not_refresh_when_token_valid(httpx_mock: Any, tmp_path: Path) -> None:
    client = _client(tmp_path, _site(access_expires_in=3600))
    resp = client.get('/api/sites/demo/token', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    assert resp.json()['refreshed'] is False


def test_api_returns_502_when_on_demand_refresh_fails(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={})
    client = _client(tmp_path, _site(access_expires_in=-60))
    resp = client.get('/api/sites/demo/token', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 502
    assert '刷新失败' in resp.json()['detail']


def test_api_site_detail_refreshes_on_demand(httpx_mock: Any, tmp_path: Path) -> None:
    new_access = _token(7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_access}})
    client = _client(tmp_path, _site(access_expires_in=-60))
    resp = client.get('/api/sites/demo', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    assert resp.json()['refreshed'] is True
    assert resp.json()['access_token'] == new_access


def test_api_site_list_does_not_refresh(httpx_mock: Any, tmp_path: Path) -> None:
    client = _client(tmp_path, _site(access_expires_in=-60))
    resp = client.get('/api/sites', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    assert resp.json()[0]['refresh_expires_at'] is not None
