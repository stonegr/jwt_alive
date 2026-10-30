"""对外 API 测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from jwt_alive.api import create_app
from jwt_alive.config import parse_config
from jwt_alive.service import JwtAliveService

from .conftest import make_token


REFRESH_URL = 'https://example.com/api/auth/refresh'
PROFILE_URL = 'https://example.com/api/user/profile'
USER = 'alice'
PASSWORD = 's3cret-pwd'


def _site(token: str) -> dict[str, Any]:
    return {
        'name': 'demo',
        'token': {'access_token': token, 'refresh_token': 'rt'},
        'jwt': {'expiry_field': 'exp'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'access_token_path': 'data.access_token',
        },
        'login_check': {'url': PROFILE_URL, 'method': 'GET'},
    }


def _client(
    tmp_path: Path,
    auth: dict[str, str] | None = None,
    api: dict[str, Any] | None = None,
    prefix: str | None = None,
) -> TestClient:
    config = parse_config(
        {
            'auth': auth if auth is not None else {USER: PASSWORD},
            'api': api or {},
            'sites': [_site(make_token())],
        }
    )
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    app = create_app(service, config, prefix=prefix)
    return TestClient(app)


def _auth_header(token: str = PASSWORD) -> dict[str, str]:
    """默认用 Authorization: Bearer 头。"""
    return {'Authorization': f'Bearer {token}'}


# -- 认证 --------------------------------------------------------------------------------


def test_requires_authentication(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites')
    assert resp.status_code == 401
    assert 'WWW-Authenticate' in resp.headers


def test_rejects_wrong_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers=_auth_header(token='wrong-token'))
    assert resp.status_code == 401


def test_rejects_missing_token_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites')
    assert resp.status_code == 401


def test_rejects_empty_token_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers={'Authorization': 'Bearer '})
    assert resp.status_code == 401


def test_accepts_x_token_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers={'X-Token': PASSWORD})
    assert resp.status_code == 200


def test_accepts_x_api_key_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers={'X-API-Key': PASSWORD})
    assert resp.status_code == 200


def test_accepts_bare_token_without_bearer_prefix(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers={'Authorization': PASSWORD})
    assert resp.status_code == 200


def test_error_message_does_not_leak(tmp_path: Path) -> None:
    """token 不匹配与没有 token 的响应一致，不泄露有效 token 的存在性。"""
    client = _client(tmp_path)
    wrong = client.get('/api/sites', headers=_auth_header(token='wrong'))
    missing = client.get('/api/sites')
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


def test_me_returns_matched_identifier(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/me', headers=_auth_header())
    assert resp.status_code == 200
    assert resp.json()['id'] == USER


def test_multiple_identifiers_each_match_own_token(tmp_path: Path) -> None:
    client = _client(tmp_path, auth={'alice': 'token-a', 'bob': 'token-b'})
    resp_a = client.get('/api/me', headers=_auth_header(token='token-a'))
    resp_b = client.get('/api/me', headers=_auth_header(token='token-b'))
    assert resp_a.json()['id'] == 'alice'
    assert resp_b.json()['id'] == 'bob'
    assert client.get('/api/me', headers=_auth_header(token='token-c')).status_code == 401


def test_accepts_valid_credentials(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers=_auth_header())
    assert resp.status_code == 200


# -- 站点 --------------------------------------------------------------------------------


def test_list_sites(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['name'] == 'demo'
    assert 'access_token' not in data[0]


def test_get_site_detail_includes_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites/demo', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'demo'
    assert data['access_token'].startswith('eyJ')
    assert data['refresh_token'] == 'rt'


def test_get_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites/demo/token', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data['access_token'].startswith('eyJ')
    assert data['refresh_token'] == 'rt'
    assert data['expiry_field'] == 'exp'


def test_unknown_site_returns_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/sites/nope', headers=_auth_header())
    assert resp.status_code == 404
    resp2 = client.get('/api/sites/nope/token', headers=_auth_header())
    assert resp2.status_code == 404


def test_health_endpoint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/health', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data['status'] == 'ok'
    assert data['site_count'] == 1
    assert 'sites' in data


# -- 刷新 --------------------------------------------------------------------------------


def test_refresh_site(httpx_mock: Any, tmp_path: Path) -> None:
    new_token = make_token(expires_in=7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_token}})
    client = _client(tmp_path)
    resp = client.post('/api/sites/demo/refresh', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True
    assert data['access_token'] == new_token
    assert data['site'] == 'demo'


def test_refresh_unknown_site_returns_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.post('/api/sites/nope/refresh', headers=_auth_header())
    assert resp.status_code == 404


def test_refresh_all(httpx_mock: Any, tmp_path: Path) -> None:
    new_token = make_token(expires_in=7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_token}})
    client = _client(tmp_path)
    resp = client.post('/api/refresh-all', headers=_auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['ok'] is True


def test_refresh_failure_reports_business_error(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=401, json={})
    client = _client(tmp_path)
    resp = client.post('/api/sites/demo/refresh', headers=_auth_header())
    assert resp.status_code == 200
    assert resp.json()['ok'] is False


def test_openapi_docs_available(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get('/api/openapi.json')
    assert resp.status_code == 200
    paths = resp.json()['paths']
    assert '/api/sites' in paths
    assert '/api/sites/{name}/refresh' in paths
    # 文档也放在前缀下
    assert client.get('/api/docs').status_code == 200


# -- 路径前缀 ----------------------------------------------------------------------------


def test_default_prefix_is_api(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get('/api/sites', headers=_auth_header()).status_code == 200
    assert client.get('/sites', headers=_auth_header()).status_code == 404


def test_custom_prefix_from_config(tmp_path: Path) -> None:
    client = _client(tmp_path, api={'prefix': '/jwt'})
    assert client.get('/jwt/sites', headers=_auth_header()).status_code == 200
    assert client.get('/api/sites', headers=_auth_header()).status_code == 404
    assert client.get('/jwt/sites/demo/token', headers=_auth_header()).status_code == 200


def test_empty_prefix_mounts_on_root(tmp_path: Path) -> None:
    client = _client(tmp_path, api={'prefix': ''})
    assert client.get('/sites', headers=_auth_header()).status_code == 200
    assert client.get('/api/sites', headers=_auth_header()).status_code == 404


def test_prefix_argument_overrides_config(tmp_path: Path) -> None:
    client = _client(tmp_path, api={'prefix': '/api'}, prefix='/v1')
    assert client.get('/v1/sites', headers=_auth_header()).status_code == 200
    assert client.get('/api/sites', headers=_auth_header()).status_code == 404


def test_openapi_paths_use_custom_prefix(tmp_path: Path) -> None:
    client = _client(tmp_path, api={'prefix': '/jwt'})
    resp = client.get('/jwt/openapi.json')
    assert resp.status_code == 200
    paths = resp.json()['paths']
    assert '/jwt/sites' in paths
    assert '/jwt/refresh-all' in paths
    assert '/api/sites' not in paths
