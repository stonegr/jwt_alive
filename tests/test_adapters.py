"""自定义适配器（插件）机制测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from jwt_alive.adapters.base import SiteAdapter
from jwt_alive.adapters.loader import (
    AdapterError,
    create_adapter,
    ensure_import_path,
    load_adapter_class,
)
from jwt_alive.config import parse_config
from jwt_alive.models import SiteState


PLUGIN_SOURCE = '''
"""测试用插件。"""

from __future__ import annotations

import httpx

from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.models import CheckResult, SiteState, TokenBundle


class DemoAdapter(HttpSiteAdapter):
    """演示：覆写 token 提取与登录态校验。"""

    def extract_tokens(self, response: httpx.Response) -> TokenBundle:
        bundle = TokenBundle()
        token = response.headers.get("X-Token")
        if token:
            bundle.access_token = token
        return bundle

    def check_login(self, state: SiteState) -> CheckResult:
        return CheckResult(True, "自定义校验通过")
'''


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    plugins = tmp_path / 'plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / 'demo_site.py').write_text(PLUGIN_SOURCE, encoding='utf-8')
    (plugins / '__init__.py').write_text('', encoding='utf-8')
    return plugins


def test_load_adapter_class_builtin() -> None:
    adapter_cls = load_adapter_class('jwt_alive.adapters.http:HttpSiteAdapter')
    assert adapter_cls.__name__ == 'HttpSiteAdapter'
    assert issubclass(adapter_cls, SiteAdapter)


def test_load_adapter_class_dot_form() -> None:
    adapter_cls = load_adapter_class('jwt_alive.adapters.http.HttpSiteAdapter')
    assert adapter_cls.__name__ == 'HttpSiteAdapter'


def test_load_adapter_class_invalid() -> None:
    with pytest.raises(AdapterError):
        load_adapter_class('jwt_alive.adapters.http:NotExist')


def test_load_adapter_class_not_a_subclass() -> None:
    with pytest.raises(AdapterError):
        load_adapter_class('jwt_alive.models:AppConfig')


def test_load_adapter_class_bad_spec() -> None:
    with pytest.raises(AdapterError):
        load_adapter_class('nodots')


def test_custom_adapter_from_plugins_dir(plugin_dir: Path) -> None:
    ensure_import_path(plugin_dir)
    config = parse_config(
        {
            'sites': [
                {
                    'name': 'demo',
                    'token': {'access_token': 'old'},
                    'refresh': {'url': 'https://example.com/refresh'},
                    'adapter': 'demo_site:DemoAdapter',
                }
            ]
        }
    )
    site = config.sites[0]
    adapter = create_adapter(site.adapter, site, httpx.Client(), logging.getLogger('test'))
    assert type(adapter).__name__ == 'DemoAdapter'

    request = httpx.Request('POST', 'https://example.com/refresh')
    response = httpx.Response(200, headers={'X-Token': 'from-header'}, request=request)
    bundle = adapter.extract_tokens(response)
    assert bundle.access_token == 'from-header'

    result = adapter.check_login(SiteState(name='demo', access_token='x'))
    assert result.logged_in
    assert result.reason == '自定义校验通过'


def test_service_uses_plugin_adapter(plugin_dir: Path, tmp_path: Path) -> None:
    from jwt_alive.service import JwtAliveService

    ensure_import_path(plugin_dir)
    config = parse_config(
        {
            'sites': [
                {
                    'name': 'demo',
                    'token': {'access_token': 'old'},
                    'refresh': {'url': 'https://example.com/refresh'},
                    'adapter': 'demo_site:DemoAdapter',
                }
            ]
        }
    )
    service = JwtAliveService(
        config=config,
        state_path=tmp_path / 'state.json',
        config_path=None,
        plugin_path=plugin_dir,
    )
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert type(runtime.adapter).__name__ == 'DemoAdapter'
    finally:
        service.close()


def test_unknown_adapter_reports_error(tmp_path: Path) -> None:
    from jwt_alive.service import JwtAliveService

    config = parse_config(
        {
            'sites': [
                {
                    'name': 'demo',
                    'token': {'access_token': 'old'},
                    'refresh': {'url': 'https://example.com/refresh'},
                    'adapter': 'not_exist_module:DemoAdapter',
                }
            ]
        }
    )
    with pytest.raises(AdapterError):
        JwtAliveService(
            config=config,
            state_path=tmp_path / 'state.json',
            plugin_path=tmp_path / 'plugins',
        )


def test_ensure_import_path_is_idempotent(tmp_path: Path) -> None:
    import sys

    ensure_import_path(tmp_path)
    ensure_import_path(tmp_path)
    assert str(tmp_path.resolve()) in sys.path


def test_plugin_example_module_is_importable() -> None:
    """仓库里的示例插件应能被加载。"""
    repo_plugins = Path(__file__).resolve().parent.parent / 'plugins'
    ensure_import_path(repo_plugins)
    adapter_cls = load_adapter_class('example_site:HeaderTokenAdapter')
    assert issubclass(adapter_cls, SiteAdapter)


def test_any_typed_helper() -> None:
    assert isinstance({}, dict) is True
    value: Any = 1
    assert value == 1
