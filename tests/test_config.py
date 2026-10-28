"""配置加载与默认值合并测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jwt_alive.config import ConfigError, expand_env, load_config, parse_config


def _minimal_site(**overrides: object) -> dict[str, object]:
    site: dict[str, object] = {
        'name': 'demo',
        'token': {'access_token': 'a', 'refresh_token': 'r'},
        'refresh': {'url': 'https://example.com/refresh', 'method': 'POST'},
    }
    site.update(overrides)
    return site


def test_expand_env_with_default() -> None:
    assert expand_env('${A:-fallback}', {}) == 'fallback'
    assert expand_env('${A}', {'A': 'value'}) == 'value'
    assert expand_env('token=${A};x', {'A': 'v'}) == 'token=v;x'


def test_expand_env_missing_raises() -> None:
    with pytest.raises(ConfigError):
        expand_env('${MISSING_VAR}', {})


def test_parse_config_merges_defaults() -> None:
    config = parse_config(
        {
            'defaults': {
                'scan_interval_seconds': 30,
                'refresh_before_seconds': 120,
                'timeout_seconds': 9.0,
                'user_agent': 'ua-from-defaults',
            },
            'sites': [_minimal_site()],
        }
    )
    site = config.sites[0]
    assert site.schedule.interval_seconds == 30
    assert site.schedule.refresh_before_seconds == 120
    assert site.timeout_seconds == 9.0
    assert site.user_agent == 'ua-from-defaults'
    assert site.refresh.timeout_seconds == 9.0


def test_parse_config_site_overrides_defaults() -> None:
    config = parse_config(
        {
            'defaults': {'scan_interval_seconds': 30, 'refresh_before_seconds': 120},
            'sites': [_minimal_site(schedule={'interval_seconds': 15, 'refresh_before_seconds': 60})],
        }
    )
    site = config.sites[0]
    assert site.schedule.interval_seconds == 15
    assert site.schedule.refresh_before_seconds == 60


def test_parse_config_rejects_duplicate_names() -> None:
    with pytest.raises(ConfigError):
        parse_config({'sites': [_minimal_site(), _minimal_site()]})


def test_parse_config_rejects_missing_refresh_url() -> None:
    with pytest.raises(ConfigError):
        parse_config({'sites': [{'name': 'demo', 'token': {'access_token': 'a'}}]})


def test_parse_config_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError):
        parse_config({'sites': [_minimal_site(unknown_key=1)]})


def test_load_config_interpolates_env(tmp_path: Path) -> None:
    config_file = tmp_path / 'sites.yaml'
    config_file.write_text(
        """
sites:
  - name: demo
    token:
      access_token: "${DEMO_TOKEN}"
      refresh_token: "rt"
    refresh:
      url: "https://example.com/refresh"
""",
        encoding='utf-8',
    )
    config = load_config(config_file, environ={'DEMO_TOKEN': 'from-env'})
    assert config.sites[0].token.access_token == 'from-env'
    assert config.source_path == str(config_file)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / 'nope.yaml')


def test_load_config_missing_env_var(tmp_path: Path) -> None:
    config_file = tmp_path / 'sites.yaml'
    config_file.write_text(
        """
sites:
  - name: demo
    token:
      access_token: "${NOPE_TOKEN}"
    refresh:
      url: "https://example.com/refresh"
""",
        encoding='utf-8',
    )
    with pytest.raises(ConfigError):
        load_config(config_file, environ={})
