"""token 提取逻辑测试。"""

from __future__ import annotations

import logging

import httpx

from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.config import parse_config
from jwt_alive.models import SiteConfig, SiteState, TokenSource


def _adapter(site_data: dict[str, object]) -> HttpSiteAdapter:
    config = parse_config({'sites': [site_data]})
    site: SiteConfig = config.sites[0]
    logger = logging.getLogger('test')
    return HttpSiteAdapter(site=site, client=httpx.Client(), logger=logger)


def _response(
    status: int = 200,
    json_body: object | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request('POST', 'https://example.com/refresh')
    header_items: list[tuple[str, str]] = list((headers or {}).items())
    for name, value in (cookies or {}).items():
        header_items.append(('set-cookie', f'{name}={value}; Path=/'))
    return httpx.Response(
        status,
        json=json_body,
        headers=header_items,
        request=request,
    )


def test_extract_tokens_from_nested_body() -> None:
    adapter = _adapter(
        {
            'name': 'demo',
            'token': {'access_token': 'old', 'refresh_token': 'old-r'},
            'refresh': {
                'url': 'https://example.com/refresh',
                'access_token_path': 'data.access_token',
                'refresh_token_path': 'data.refresh_token',
            },
        }
    )
    response = _response(json_body={'data': {'access_token': 'new', 'refresh_token': 'new-r'}})
    bundle = adapter.extract_tokens(response)
    assert bundle.access_token == 'new'
    assert bundle.refresh_token == 'new-r'


def test_extract_tokens_from_cookie() -> None:
    adapter = _adapter(
        {
            'name': 'demo',
            'token': {'access_token': 'old'},
            'refresh': {
                'url': 'https://example.com/refresh',
                'token_source': 'cookie',
                'cookie_name': 'SESSION',
                'refresh_cookie_name': 'SESSION_RT',
            },
        }
    )
    response = _response(cookies={'SESSION': 'cookie-token', 'SESSION_RT': 'cookie-rt'})
    bundle = adapter.extract_tokens(response)
    assert bundle.access_token == 'cookie-token'
    assert bundle.refresh_token == 'cookie-rt'


def test_extract_tokens_falls_back_to_cookie() -> None:
    adapter = _adapter(
        {
            'name': 'demo',
            'token': {'access_token': 'old'},
            'refresh': {
                'url': 'https://example.com/refresh',
                'token_source': 'body',
                'access_token_path': 'data.access_token',
                'cookie_name': 'SESSION',
            },
        }
    )
    response = _response(json_body={'data': {}}, cookies={'SESSION': 'cookie-token'})
    bundle = adapter.extract_tokens(response)
    assert bundle.access_token == 'cookie-token'


def test_extract_tokens_missing_returns_empty() -> None:
    adapter = _adapter(
        {
            'name': 'demo',
            'token': {'access_token': 'old'},
            'refresh': {'url': 'https://example.com/refresh', 'access_token_path': 'data.token'},
        }
    )
    bundle = adapter.extract_tokens(_response(json_body={'data': {}}))
    assert bundle.access_token is None
    assert not bundle


def test_build_refresh_request_renders_templates() -> None:
    adapter = _adapter(
        {
            'name': 'demo',
            'token': {'access_token': 'at', 'refresh_token': 'rt'},
            'refresh': {
                'url': 'https://example.com/refresh?rt={refresh_token}',
                'method': 'POST',
                'headers': {'Authorization': 'Bearer {access_token}'},
                'body': {'refresh_token': '{refresh_token}', 'site': '{site_name}'},
            },
        }
    )
    state = SiteState(name='demo', access_token='at', refresh_token='rt')
    request = adapter.build_refresh_request(state)
    assert request.url == 'https://example.com/refresh?rt=rt'
    assert request.headers['Authorization'] == 'Bearer at'
    assert request.json_body == {'refresh_token': 'rt', 'site': 'demo'}


def test_token_source_enum_is_used() -> None:
    assert TokenSource.BODY == 'body'
