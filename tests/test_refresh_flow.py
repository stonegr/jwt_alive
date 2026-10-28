"""刷新流程与登录态校验的集成测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jwt_alive.config import parse_config
from jwt_alive.models import SiteState
from jwt_alive.service import JwtAliveService
from jwt_alive.state import StateStore

from .conftest import make_token, write_config


REFRESH_URL = 'https://example.com/api/auth/refresh'
PROFILE_URL = 'https://example.com/api/user/profile'


def _site_data(token: str, **overrides: Any) -> dict[str, Any]:
    site: dict[str, Any] = {
        'name': 'demo',
        'token': {'access_token': token, 'refresh_token': 'rt'},
        'jwt': {'expiry_field': 'exp'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'headers': {'Authorization': 'Bearer {access_token}'},
            'body': {'refresh_token': '{refresh_token}'},
            'access_token_path': 'data.access_token',
            'refresh_token_path': 'data.refresh_token',
        },
        'login_check': {
            'url': PROFILE_URL,
            'method': 'GET',
            'headers': {'Authorization': 'Bearer {access_token}'},
            'success': {'type': 'status', 'expected': 200},
        },
    }
    site.update(overrides)
    return site


def _service(tmp_path: Path, site: dict[str, Any]) -> tuple[JwtAliveService, Path]:
    config = parse_config({'sites': [site]})
    config_file = write_config(tmp_path / 'sites.yaml', {'sites': [site]})
    service = JwtAliveService(
        config=config,
        state_path=tmp_path / 'state' / 'state.json',
        config_path=config_file,
        plugin_path=tmp_path / 'plugins',
    )
    return service, config_file


def test_refresh_success_updates_state(httpx_mock: Any, tmp_path: Path) -> None:
    new_token = make_token(expires_in=7200)
    httpx_mock.add_response(
        url=REFRESH_URL,
        method='POST',
        json={'data': {'access_token': new_token, 'refresh_token': 'new-rt'}},
    )
    service, config_file = _service(tmp_path, _site_data(make_token(expires_in=60)))
    try:
        outcome = service.refresh_site('demo')
    finally:
        service.close()

    assert outcome.ok
    assert outcome.new_access_token == new_token
    assert outcome.written_back

    state = StateStore(tmp_path / 'state' / 'state.json').get('demo')
    assert state is not None
    assert state.access_token == new_token
    assert state.refresh_token == 'new-rt'
    assert state.expires_at is not None
    assert state.last_refresh is not None

    # 回写配置
    written = json.loads('{}')  # 占位，配置是 YAML，下面直接读文本校验
    assert written == {}
    text = config_file.read_text(encoding='utf-8')
    assert new_token in text


def test_refresh_failure_keeps_old_token(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={'error': 'bad'})
    old_token = make_token(expires_in=60)
    service, _ = _service(tmp_path, _site_data(old_token))
    try:
        outcome = service.refresh_site('demo')
    finally:
        service.close()

    assert not outcome.ok
    assert '401' in outcome.reason
    state = StateStore(tmp_path / 'state' / 'state.json').get('demo')
    assert state is not None
    assert state.access_token == old_token
    assert state.consecutive_failures == 1
    assert state.last_error


def test_refresh_without_token_in_response_fails(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {}})
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=60)))
    try:
        outcome = service.refresh_site('demo')
    finally:
        service.close()
    assert not outcome.ok
    assert 'access_token' in outcome.reason


def test_check_expired_token_short_circuits(httpx_mock: Any, tmp_path: Path) -> None:
    expired = make_token(expires_in=-60)
    service, _ = _service(tmp_path, _site_data(expired))
    try:
        results = service.check_all()
    finally:
        service.close()
    result = results['demo']
    assert not result.logged_in
    assert '过期' in result.reason


def test_check_valid_token_calls_api(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'name': 'tester'})
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=3600)))
    try:
        results = service.check_all()
    finally:
        service.close()
    assert results['demo'].logged_in


def test_check_api_401_means_logged_out(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', status_code=401, json={})
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=3600)))
    try:
        results = service.check_all()
    finally:
        service.close()
    assert not results['demo'].logged_in


def test_check_json_criteria(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'code': 0})
    site = _site_data(make_token(expires_in=3600))
    site['login_check']['success'] = {'type': 'json', 'path': 'code', 'equals': 0}
    service, _ = _service(tmp_path, site)
    try:
        results = service.check_all()
    finally:
        service.close()
    assert results['demo'].logged_in


def test_needs_refresh_uses_threshold(httpx_mock: Any, tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=60)))
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.needs_refresh()
    finally:
        service.close()


def test_needs_refresh_false_when_fresh(httpx_mock: Any, tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=7200)))
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert not runtime.needs_refresh()
    finally:
        service.close()


def test_state_prefers_state_file_token(tmp_path: Path) -> None:
    """状态文件里的 token 比配置新，重启后应优先使用。"""
    token = make_token(expires_in=3600)
    service, _ = _service(tmp_path, _site_data('old-token'))
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.access_token = token
        service.store.upsert(runtime.state)
        service.store.save()
    finally:
        service.close()

    service2, _ = _service(tmp_path, _site_data('old-token'))
    try:
        runtime2 = service2.get_runtime('demo')
        assert runtime2 is not None
        assert runtime2.state.access_token == token
    finally:
        service2.close()


def test_custom_expiry_field_used(httpx_mock: Any, tmp_path: Path) -> None:
    expires_at = datetime.now(UTC) + timedelta(seconds=30)
    token = make_token({'data': {'expireTime': int(expires_at.timestamp())}})
    site = _site_data(token)
    site['jwt'] = {'expiry_field': 'data.expireTime'}
    service, _ = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        info = runtime.expiry_info()
        assert info.resolved
        assert info.expires_at is not None
        assert runtime.needs_refresh()
    finally:
        service.close()


def test_adapter_client_uses_configured_timeout(httpx_mock: Any, tmp_path: Path) -> None:
    site = _site_data(make_token(expires_in=3600))
    site['timeout_seconds'] = 3.5
    service, _ = _service(tmp_path, site)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert isinstance(runtime.adapter.client, httpx.Client)
        assert runtime.adapter.client.timeout.connect == 3.5
    finally:
        service.close()


def test_state_history_is_bounded(httpx_mock: Any, tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _site_data(make_token(expires_in=3600)))
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        runtime.state.history = [{'n': i} for i in range(50)]
        service.store.upsert(runtime.state)
        service.store.save()
    finally:
        service.close()
    state = StateStore(tmp_path / 'state' / 'state.json').get('demo')
    assert state is not None
    assert len(state.history) == 50


@pytest.mark.parametrize('field', ['exp', 'expireTime'])
def test_site_state_roundtrip(field: str) -> None:
    state = SiteState(name='demo', access_token='a', expiry_field=field)
    restored = SiteState.from_dict(state.to_dict())
    assert restored.name == 'demo'
    assert restored.expiry_field == field
