"""CLI 命令测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
import yaml

from jwt_alive.cli import app

from .conftest import make_token


runner = CliRunner()

REFRESH_URL = 'https://example.com/api/auth/refresh'
PROFILE_URL = 'https://example.com/api/user/profile'


def _site(token: str) -> dict[str, Any]:
    return {
        'name': 'demo',
        'token': {'access_token': token, 'refresh_token': 'rt'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'body': {'refresh_token': '{refresh_token}'},
            'access_token_path': 'data.access_token',
        },
        'login_check': {'url': PROFILE_URL, 'method': 'GET'},
    }


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return path


def _args(config: Path, state: Path, log: Path) -> list[str]:
    return ['--config', str(config), '--state', str(state), '--log-file', str(log)]


def test_validate_config(tmp_path: Path) -> None:
    config_file = _write(tmp_path / 'sites.yaml', {'sites': [_site(make_token())]})
    result = runner.invoke(
        app,
        [*_args(config_file, tmp_path / 's.json', tmp_path / 'l.log'), 'validate-config'],
    )
    assert result.exit_code == 0
    assert '配置校验通过' in result.stdout


def test_validate_config_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            *_args(tmp_path / 'nope.yaml', tmp_path / 's.json', tmp_path / 'l.log'),
            'validate-config',
        ],
    )
    assert result.exit_code == 2


def test_check_command(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'ok': True})
    config_file = _write(tmp_path / 'sites.yaml', {'sites': [_site(make_token())]})
    result = runner.invoke(
        app,
        [*_args(config_file, tmp_path / 's.json', tmp_path / 'l.log'), 'check'],
    )
    assert result.exit_code == 0
    assert 'demo' in result.stdout


def test_refresh_all_command(httpx_mock: Any, tmp_path: Path) -> None:
    new_token = make_token(expires_in=7200)
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': new_token}})
    config_file = _write(tmp_path / 'sites.yaml', {'sites': [_site(make_token())]})
    state_file = tmp_path / 'state.json'
    result = runner.invoke(
        app,
        [*_args(config_file, state_file, tmp_path / 'l.log'), 'refresh', '--all'],
    )
    assert result.exit_code == 0
    assert '成功' in result.stdout
    assert state_file.is_file()
    assert new_token in state_file.read_text(encoding='utf-8')


def test_refresh_unknown_site(tmp_path: Path) -> None:
    config_file = _write(tmp_path / 'sites.yaml', {'sites': [_site(make_token())]})
    result = runner.invoke(
        app,
        [*_args(config_file, tmp_path / 's.json', tmp_path / 'l.log'), 'refresh', 'nope'],
    )
    assert result.exit_code == 2


def test_status_command_without_state(tmp_path: Path) -> None:
    config_file = _write(tmp_path / 'sites.yaml', {'sites': [_site(make_token())]})
    result = runner.invoke(
        app,
        [*_args(config_file, tmp_path / 's.json', tmp_path / 'l.log'), 'status'],
    )
    assert result.exit_code == 0


def test_init_command(tmp_path: Path) -> None:
    target = tmp_path / 'sites.yaml'
    result = runner.invoke(app, ['init', '--target', str(target)])
    assert result.exit_code == 0
    assert target.is_file()
    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    assert 'sites' in data


@pytest.mark.parametrize('command', ['check', 'refresh', 'status', 'validate-config'])
def test_help_available(command: str) -> None:
    result = runner.invoke(app, [command, '--help'])
    assert result.exit_code == 0
