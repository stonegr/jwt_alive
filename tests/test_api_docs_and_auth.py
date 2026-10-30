"""接口文档开关与认证健壮性的测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from jwt_alive.api import create_app
from jwt_alive.cli import _api_docs
from jwt_alive.config import parse_config
from jwt_alive.models import AppConfig
from jwt_alive.service import JwtAliveService


TOKEN = 'super-secret-token'


def _config(auth: dict[str, str] | None = None, **api: Any) -> AppConfig:
    return parse_config(
        {
            'auth': auth if auth is not None else {'my-client': TOKEN},
            'api': api,
            'sites': [
                {
                    'name': 'demo',
                    'token': {'access_token': 'a'},
                    'refresh': {'url': 'https://example.com/refresh'},
                }
            ],
        }
    )


def _client(tmp_path: Path, config: AppConfig) -> TestClient:
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    return TestClient(create_app(service, config))


# -- 文档开关 -----------------------------------------------------------------------------


def test_docs_enabled_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path, _config())
    assert client.get('/api/docs').status_code == 200
    assert client.get('/api/openapi.json').status_code == 200


def test_docs_disabled_by_config(tmp_path: Path) -> None:
    client = _client(tmp_path, _config(docs=False))
    assert client.get('/api/docs').status_code == 404
    assert client.get('/api/redoc').status_code == 404
    assert client.get('/api/openapi.json').status_code == 404


def test_business_endpoints_still_work_when_docs_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path, _config(docs=False))
    resp = client.get('/api/sites', headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    assert resp.json()[0]['name'] == 'demo'


def test_docs_argument_overrides_config(tmp_path: Path) -> None:
    config = _config(docs=False)
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    client = TestClient(create_app(service, config, docs=True))
    assert client.get('/api/docs').status_code == 200


def test_api_docs_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('JWT_ALIVE_API_DOCS', raising=False)
    assert _api_docs(None) is None
    monkeypatch.setenv('JWT_ALIVE_API_DOCS', 'false')
    assert _api_docs(None) is False
    monkeypatch.setenv('JWT_ALIVE_API_DOCS', '0')
    assert _api_docs(None) is False
    monkeypatch.setenv('JWT_ALIVE_API_DOCS', 'true')
    assert _api_docs(None) is True
    assert _api_docs(False) is False  # 命令行优先


# -- 认证健壮性 -----------------------------------------------------------------------------


def test_token_with_trailing_newline_in_config_matches() -> None:
    """YAML 块标量写出的值带尾部换行，也应该能匹配上。"""
    config = parse_config({'auth': {'client': 'secret-token\n'}, 'sites': []})
    assert config.verify_token('secret-token') == 'client'
    assert config.verify_token('secret-token\n') == 'client'


def test_token_with_spaces_matches() -> None:
    config = parse_config({'auth': {'client': '  secret-token  '}, 'sites': []})
    assert config.verify_token('secret-token') == 'client'


def test_empty_auth_value_is_ignored() -> None:
    config = parse_config({'auth': {'client': ''}, 'sites': []})
    assert config.verify_token('anything') is None


def test_auth_issues_reports_empty_value() -> None:
    config = parse_config({'auth': {'client': '', 'other': 'x'}, 'sites': []})
    issues = config.auth_issues()
    assert any('为空' in i for i in issues)


def test_auth_issues_reports_duplicate_values() -> None:
    config = parse_config({'auth': {'a': 'same', 'b': 'same'}, 'sites': []})
    issues = config.auth_issues()
    assert any('相同' in i for i in issues)


def test_auth_issues_empty_when_ok() -> None:
    config = parse_config({'auth': {'a': 'x', 'b': 'y'}, 'sites': []})
    assert config.auth_issues() == []


def test_auth_failure_log_distinguishes_missing_header(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client = _client(tmp_path, _config())
    with caplog.at_level(logging.WARNING, logger='jwt_alive.api'):
        client.get('/api/sites')
    assert any('未携带 token' in r.getMessage() for r in caplog.records)


def test_auth_failure_log_reports_length(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client = _client(tmp_path, _config())
    with caplog.at_level(logging.WARNING, logger='jwt_alive.api'):
        client.get('/api/sites', headers={'Authorization': 'Bearer wrong-token'})
    assert any('未命中 auth 表' in r.getMessage() for r in caplog.records)
    # 日志里不能出现 token 本身
    assert all('wrong-token' not in r.getMessage() for r in caplog.records)
