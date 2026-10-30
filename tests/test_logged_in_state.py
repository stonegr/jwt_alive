"""刷新成功/失败后 state.logged_in 的语义测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import httpx

from jwt_alive.api import create_app
from jwt_alive.config import parse_config
from jwt_alive.service import JwtAliveService

from .conftest import make_token


REFRESH_URL = 'https://example.com/api/auth/refresh'


def _site(access_expires_in: int = 3600) -> dict[str, Any]:
    return {
        'name': 'demo',
        'token': {'access_token': make_token(expires_in=access_expires_in), 'refresh_token': 'rt'},
        'jwt': {'expiry_field': 'exp'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'access_token_path': 'data.access_token',
        },
    }


def _service(tmp_path: Path, site: dict[str, Any]) -> JwtAliveService:
    config = parse_config({'sites': [site]})
    return JwtAliveService(config=config, state_path=tmp_path / 'state.json')


def test_refresh_success_sets_logged_in_true(httpx_mock: Any, tmp_path: Path) -> None:
    """刷新前 logged_in=False，刷新成功后应立即变 True。"""
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site())
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.logged_in = False
        outcome = service.refresh_site('demo')
        assert outcome.ok
        assert runtime.state.logged_in is True
    finally:
        service.close()


def test_refresh_success_keeps_logged_in_true(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site())
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.logged_in = True
        service.refresh_site('demo')
        assert runtime.state.logged_in is True
    finally:
        service.close()


def test_refresh_rejected_401_sets_logged_in_false(httpx_mock: Any, tmp_path: Path) -> None:
    """服务端 401 → 会话失效 → logged_in=False。"""
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={})
    service = _service(tmp_path, _site())
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.logged_in = True
        outcome = service.refresh_site('demo')
        assert not outcome.ok
        assert runtime.state.logged_in is False
    finally:
        service.close()


def test_refresh_network_error_keeps_logged_in(httpx_mock: Any, tmp_path: Path) -> None:
    """网络异常不代表会话失效 → logged_in 保持原值。"""
    httpx_mock.add_exception(httpx.ConnectError('boom'), url=REFRESH_URL)
    site = _site()
    site['retry_attempts'] = 1
    service = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.logged_in = True
        outcome = service.refresh_site('demo')
        assert not outcome.ok
        assert runtime.state.logged_in is True  # 保持原值
    finally:
        service.close()


def test_refresh_does_not_touch_last_check(httpx_mock: Any, tmp_path: Path) -> None:
    """刷新不是"检查"，不应改写 last_check。"""
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site())
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.last_check = None
        service.refresh_site('demo')
        assert runtime.state.last_check is None
        assert runtime.state.last_refresh is not None
    finally:
        service.close()


def test_api_list_shows_logged_in_after_on_demand_refresh(httpx_mock: Any, tmp_path: Path) -> None:
    """API 按需刷新成功后，站点列表的 logged_in 应为 true。"""
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    config = parse_config({'auth': {'cli': 'tok'}, 'sites': [_site(access_expires_in=-60)]})
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    client = TestClient(create_app(service, config))

    resp = client.get('/api/sites/demo/token', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 200
    assert resp.json()['refreshed'] is True

    listing = client.get('/api/sites', headers={'Authorization': 'Bearer tok'})
    assert listing.status_code == 200
    assert listing.json()[0]['logged_in'] is True


def test_api_list_shows_logged_out_after_rejected_refresh(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={})
    config = parse_config({'auth': {'cli': 'tok'}, 'sites': [_site(access_expires_in=-60)]})
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    client = TestClient(create_app(service, config))

    resp = client.get('/api/sites/demo/token', headers={'Authorization': 'Bearer tok'})
    assert resp.status_code == 502

    listing = client.get('/api/sites', headers={'Authorization': 'Bearer tok'})
    assert listing.json()[0]['logged_in'] is False
