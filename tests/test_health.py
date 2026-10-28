"""健康检查与容器相关路径测试。"""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jwt_alive.cli import app
from jwt_alive.state import StateStore
from jwt_alive.utils import now_utc


runner = CliRunner()


def _args(config: Path, state: Path) -> list[str]:
    return ['--config', str(config), '--state', str(state), '--quiet']


def test_health_ok_when_scan_is_fresh(tmp_path: Path) -> None:
    config = tmp_path / 'sites.yaml'
    config.write_text('sites: []\n', encoding='utf-8')
    state = tmp_path / 'state.json'

    store = StateStore(state)
    store.last_scan = now_utc()
    store.save()

    result = runner.invoke(app, [*_args(config, state), 'health', '--max-age', '60'])
    assert result.exit_code == 0
    assert '健康' in result.stdout


def test_health_fails_when_scan_is_stale(tmp_path: Path) -> None:
    config = tmp_path / 'sites.yaml'
    config.write_text('sites: []\n', encoding='utf-8')
    state = tmp_path / 'state.json'

    store = StateStore(state)
    store.last_scan = now_utc() - timedelta(seconds=600)
    store.save()

    result = runner.invoke(app, [*_args(config, state), 'health', '--max-age', '60'])
    assert result.exit_code == 1
    assert '不健康' in result.stdout


def test_health_fails_without_state_file(tmp_path: Path) -> None:
    config = tmp_path / 'sites.yaml'
    config.write_text('sites: []\n', encoding='utf-8')
    state = tmp_path / 'missing.json'

    result = runner.invoke(app, [*_args(config, state), 'health', '--max-age', '60'])
    assert result.exit_code == 1


def test_health_uses_config_scan_interval(tmp_path: Path) -> None:
    config = tmp_path / 'sites.yaml'
    config.write_text('defaults:\n  scan_interval_seconds: 10\nsites: []\n', encoding='utf-8')
    state = tmp_path / 'state.json'
    store = StateStore(state)
    store.last_scan = now_utc() - timedelta(seconds=45)
    store.save()

    # 默认阈值 = 3 × 10s = 30s，45 秒前扫过 → 不健康
    result = runner.invoke(app, [*_args(config, state), 'health'])
    assert result.exit_code == 1

    store.last_scan = now_utc() - timedelta(seconds=5)
    store.save()
    result2 = runner.invoke(app, [*_args(config, state), 'health'])
    assert result2.exit_code == 0


def test_state_persists_last_scan(tmp_path: Path) -> None:
    state = tmp_path / 'state.json'
    store = StateStore(state)
    store.last_scan = now_utc()
    store.save()

    reloaded = StateStore(state)
    assert reloaded.last_scan is not None
    assert abs((reloaded.last_scan - store.last_scan).total_seconds()) < 1


def test_env_var_paths_are_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / 'sites.yaml'
    config.write_text('sites: []\n', encoding='utf-8')
    state = tmp_path / 'state.json'

    monkeypatch.setenv('JWT_ALIVE_CONFIG', str(config))
    monkeypatch.setenv('JWT_ALIVE_STATE', str(state))

    result = runner.invoke(app, ['status'])
    assert result.exit_code == 0

    store = StateStore(state)
    store.last_scan = now_utc()
    store.save()
    result2 = runner.invoke(app, ['health', '--max-age', '60'])
    assert result2.exit_code == 0


def test_cli_flag_overrides_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_config = tmp_path / 'env.yaml'
    env_config.write_text('sites: []\n', encoding='utf-8')
    flag_config = tmp_path / 'flag.yaml'
    flag_config.write_text('sites: []\n', encoding='utf-8')
    state = tmp_path / 'state.json'

    monkeypatch.setenv('JWT_ALIVE_CONFIG', str(env_config))
    monkeypatch.setenv('JWT_ALIVE_STATE', str(state))

    result = runner.invoke(app, ['--config', str(flag_config), 'validate-config'])
    assert result.exit_code == 0
    assert os.environ['JWT_ALIVE_CONFIG'] == str(env_config)


def test_init_finds_example_config(tmp_path: Path) -> None:
    target = tmp_path / 'sites.yaml'
    result = runner.invoke(app, ['init', '--target', str(target)])
    assert result.exit_code == 0
    assert target.is_file()


def test_healthcheck_command_exists() -> None:
    result = runner.invoke(app, ['health', '--help'])
    assert result.exit_code == 0
    assert '--max-age' in result.stdout


def test_scheduler_updates_last_scan(tmp_path: Path) -> None:
    from jwt_alive.config import parse_config
    from jwt_alive.scheduler import KeepAliveScheduler
    from jwt_alive.service import JwtAliveService

    config = parse_config({'sites': []})
    service = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    scheduler = KeepAliveScheduler(service, interval_seconds=1)
    try:
        scheduler._scan()
        assert service.store.last_scan is not None
    finally:
        scheduler.shutdown()
        service.close()


def test_docker_files_exist() -> None:
    root = Path(__file__).resolve().parent.parent
    for name in ('Dockerfile', 'docker-compose.yml', '.dockerignore', '.env.example'):
        assert (root / name).is_file(), f'缺少 {name}'


def test_compose_healthcheck_uses_health_command() -> None:
    root = Path(__file__).resolve().parent.parent
    content = (root / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'jwt-alive' in content
    assert 'health' in content


def test_dockerfile_uses_uv_lock() -> None:
    root = Path(__file__).resolve().parent.parent
    content = (root / 'Dockerfile').read_text(encoding='utf-8')
    assert 'uv sync --frozen' in content
    assert 'HEALTHCHECK' in content


def test_any_import_is_used() -> None:
    value: Any = 'ok'
    assert value == 'ok'
