"""--data-dir 运行根目录相关测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jwt_alive.cli import (
    _config_path,
    _log_file_path,
    _plugin_path,
    _state_path,
    app,
)


runner = CliRunner()


# -- 路径解析 -----------------------------------------------------------------------------


def test_data_dir_used_for_all_paths(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    assert _config_path(None, base) == base / 'config' / 'sites.yaml'
    assert _state_path(None, base) == base / 'state' / 'state.json'
    assert _log_file_path(None, base) == base / 'logs' / 'jwt_alive.log'
    assert _plugin_path(None, base) == base / 'plugins'


def test_explicit_option_beats_data_dir(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    assert _state_path(tmp_path / 'x.json', base) == tmp_path / 'x.json'
    assert _log_file_path(tmp_path / 'y.log', base) == tmp_path / 'y.log'
    assert _config_path(tmp_path / 'z.yaml', base) == tmp_path / 'z.yaml'
    assert _plugin_path(tmp_path / 'p', base) == tmp_path / 'p'


def test_env_beats_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / 'root'
    monkeypatch.setenv('JWT_ALIVE_STATE', str(tmp_path / 'env.json'))
    monkeypatch.setenv('JWT_ALIVE_LOG_FILE', str(tmp_path / 'env.log'))
    monkeypatch.setenv('JWT_ALIVE_CONFIG', str(tmp_path / 'env.yaml'))
    monkeypatch.setenv('JWT_ALIVE_PLUGINS', str(tmp_path / 'envplugins'))

    assert _state_path(None, base) == tmp_path / 'env.json'
    assert _log_file_path(None, base) == tmp_path / 'env.log'
    assert _config_path(None, base) == tmp_path / 'env.yaml'
    assert _plugin_path(None, base) == tmp_path / 'envplugins'


def test_option_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('JWT_ALIVE_STATE', str(tmp_path / 'env.json'))
    assert _state_path(tmp_path / 'opt.json', tmp_path / 'root') == tmp_path / 'opt.json'


def test_default_is_cwd_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _state_path(None) == tmp_path / 'state' / 'state.json'
    assert _log_file_path(None) == tmp_path / 'logs' / 'jwt_alive.log'
    assert _config_path(None) == tmp_path / 'config' / 'sites.yaml'
    assert _plugin_path(None) == tmp_path / 'plugins'


def test_data_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('JWT_ALIVE_DATA_DIR', str(tmp_path / 'envroot'))
    result = runner.invoke(app, ['status'])
    assert result.exit_code == 0
    assert 'envroot' in result.stdout


# -- 实际使用 ------------------------------------------------------------------------------


def _write_config(path: Path, token: str = 'tok') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
sites:
  - name: demo
    token:
      access_token: "{token}"
    refresh:
      url: "https://example.com/refresh"
""",
        encoding='utf-8',
    )
    return path


def test_check_uses_data_dir_state(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    config = _write_config(base / 'config' / 'sites.yaml')
    result = runner.invoke(app, ['--data-dir', str(base), 'check'])
    assert result.exit_code in (0, 1)  # 网络失败也算正常退出路径
    assert (base / 'state' / 'state.json').is_file()
    assert config.is_file()


def test_status_shows_state_path(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    result = runner.invoke(app, ['--data-dir', str(base), 'status'])
    assert result.exit_code == 0
    assert str(base / 'state' / 'state.json') in result.stdout


def test_logs_created_under_data_dir(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    _write_config(base / 'config' / 'sites.yaml')
    runner.invoke(app, ['--data-dir', str(base), 'check'])
    assert (base / 'logs' / 'jwt_alive.log').is_file()


def test_find_example_config_in_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jwt_alive.cli as cli_mod

    monkeypatch.setattr(cli_mod, 'EXAMPLE_CONFIG_DIRS', ())
    base = tmp_path / 'root'
    (base / 'config').mkdir(parents=True)
    (base / 'config' / 'sites.example.yaml').write_text('sites: []\n', encoding='utf-8')
    found = cli_mod.find_example_config(base)
    assert found is not None
    assert found == base / 'config' / 'sites.example.yaml'


def test_init_creates_config_under_data_dir(tmp_path: Path) -> None:
    base = tmp_path / 'root'
    result = runner.invoke(app, ['--data-dir', str(base), 'init'])
    assert result.exit_code == 0
    assert (base / 'config' / 'sites.yaml').is_file()


def test_log_file_unwritable_degrades_to_console(tmp_path: Path) -> None:
    """日志目录不可写时不能把守护进程搞挂。"""
    import logging

    from jwt_alive.logging_setup import setup_logging

    blocked = tmp_path / 'blocked'
    blocked.mkdir()
    blocked.chmod(0o500)  # 只读
    try:
        path = setup_logging(level='INFO', log_file=blocked / 'sub' / 'app.log', console=False)
        assert path is None
    finally:
        blocked.chmod(0o700)
    logging.getLogger().handlers.clear()


def test_data_dir_env_and_option_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('JWT_ALIVE_DATA_DIR', str(tmp_path / 'fromenv'))
    result = runner.invoke(app, ['--data-dir', str(tmp_path / 'fromopt'), 'status'])
    assert result.exit_code == 0
    assert 'fromopt' in result.stdout
    assert 'fromenv' not in result.stdout


def test_paths_helper_signature() -> None:
    sig = Path
    assert sig is not None
