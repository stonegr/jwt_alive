"""配置加载与默认值合并测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jwt_alive.config import ConfigError, expand_env, interpolate_env, load_config, parse_config


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


def test_parse_config_auth_dict() -> None:
    """auth 表：key 是标识，value 是允许的 token 值。"""
    config = parse_config(
        {
            'auth': {'alice': 'token-a', 'bob': 'token-b'},
            'sites': [_minimal_site()],
        }
    )
    assert config.auth == {'alice': 'token-a', 'bob': 'token-b'}
    assert config.verify_token('token-a') == 'alice'
    assert config.verify_token('token-b') == 'bob'
    assert config.verify_token('wrong') is None
    assert config.verify_token(None) is None
    assert config.verify_token('') is None


def test_verify_token_returns_first_match() -> None:
    config = parse_config(
        {
            'auth': {'a': 'same', 'b': 'same'},
            'sites': [_minimal_site()],
        }
    )
    assert config.verify_token('same') == 'a'


def test_parse_config_api_defaults() -> None:
    config = parse_config({'sites': [_minimal_site()]})
    assert config.api.enabled is True
    assert config.api.host == '127.0.0.1'
    assert config.api.port == 8080


def test_parse_config_api_section() -> None:
    config = parse_config(
        {
            'api': {'enabled': False, 'host': '0.0.0.0', 'port': 9000},
            'sites': [_minimal_site()],
        }
    )
    assert config.api.enabled is False
    assert config.api.host == '0.0.0.0'
    assert config.api.port == 9000


def test_parse_config_api_prefix_normalized() -> None:
    config = parse_config({'api': {'prefix': 'jwt'}, 'sites': [_minimal_site()]})
    assert config.api.prefix == '/jwt'

    config2 = parse_config({'api': {'prefix': '/v1/api/'}, 'sites': [_minimal_site()]})
    assert config2.api.prefix == '/v1/api'

    config3 = parse_config({'api': {'prefix': '/'}, 'sites': [_minimal_site()]})
    assert config3.api.prefix == ''

    config4 = parse_config({'api': {'prefix': ''}, 'sites': [_minimal_site()]})
    assert config4.api.prefix == ''


def test_parse_config_api_prefix_default() -> None:
    config = parse_config({'sites': [_minimal_site()]})
    assert config.api.prefix == '/api'


def test_parse_config_rejects_invalid_api_port() -> None:
    with pytest.raises(ConfigError):
        parse_config({'api': {'port': 99999}, 'sites': [_minimal_site()]})


def test_parse_config_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ConfigError):
        parse_config({'nope': 1, 'sites': [_minimal_site()]})


def test_interpolate_env_expands_dict_keys() -> None:
    data = {'auth': {'${USER_NAME}': '${PWD}'}}
    result = interpolate_env(data, {'USER_NAME': 'alice', 'PWD': 'secret'})
    assert result == {'auth': {'alice': 'secret'}}


def test_load_config_auth_with_env(tmp_path: Path) -> None:
    config_file = tmp_path / 'sites.yaml'
    config_file.write_text(
        """
auth:
  "${API_USER}": "${API_PASSWORD}"
sites:
  - name: demo
    token:
      access_token: "a"
    refresh:
      url: "https://example.com/refresh"
""",
        encoding='utf-8',
    )
    config = load_config(config_file, environ={'API_USER': 'admin', 'API_PASSWORD': 'p@ss'})
    assert config.auth == {'admin': 'p@ss'}
    assert config.verify_token('p@ss') == 'admin'
