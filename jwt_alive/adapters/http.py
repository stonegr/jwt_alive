"""默认 HTTP 适配器：完全由配置驱动，无需写代码即可覆盖大部分站点。"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from jwt_alive.adapters.base import RequestSpec, SiteAdapter, render_template
from jwt_alive.jwt_tools import get_by_path
from jwt_alive.models import (
    CheckResult,
    LoginCheckConfig,
    RefreshConfig,
    SiteState,
    SuccessType,
    TokenBundle,
    TokenSource,
)


class HttpSiteAdapter(SiteAdapter):
    """配置驱动的站点适配器。

    - 刷新请求：url / method / headers / body 全部来自配置
    - token 提取：响应体点路径 或 Set-Cookie（可互相兜底）
    - 登录态校验：状态码 / JSON 字段 / Cookie 三种判定
    """

    # -- 刷新 --------------------------------------------------------------------

    def build_refresh_request(self, state: SiteState) -> RequestSpec:
        cfg: RefreshConfig = self.site.refresh
        context = self.template_context(state)
        headers = render_template(cfg.headers, context)
        headers.setdefault('User-Agent', self.site.user_agent or '')
        if not headers.get('User-Agent'):
            headers.pop('User-Agent')
        cookies = render_template(cfg.cookies, context)
        self.apply_cookies(cookies)
        return RequestSpec(
            method=cfg.method,
            url=render_template(cfg.url, context),
            headers=headers,
            params=render_template(cfg.params, context),
            cookies=cookies,
            json_body=render_template(cfg.body, context) if cfg.body is not None else None,
            follow_redirects=cfg.follow_redirects,
            timeout=cfg.timeout_seconds or self.site.timeout_seconds,
        )

    def extract_tokens(self, response: httpx.Response) -> TokenBundle:
        cfg = self.site.refresh
        bundle = TokenBundle()

        if cfg.token_source == TokenSource.BODY:
            self._extract_from_body(response, bundle)
        if not bundle.access_token and cfg.cookie_name:
            self._extract_from_cookie(response, bundle)

        if not bundle.access_token and cfg.token_source == TokenSource.SET_COOKIE:
            self._extract_from_cookie(response, bundle)
        return bundle

    def _extract_from_body(self, response: httpx.Response, bundle: TokenBundle) -> None:
        cfg = self.site.refresh
        data = _safe_json(response)
        if data is None:
            return
        if cfg.access_token_path:
            value = get_by_path(data, cfg.access_token_path)
            if isinstance(value, str) and value:
                bundle.access_token = value
            elif value is not None and not isinstance(value, str):
                bundle.access_token = json.dumps(value, ensure_ascii=False)
        if cfg.refresh_token_path:
            value = get_by_path(data, cfg.refresh_token_path)
            if isinstance(value, str) and value:
                bundle.refresh_token = value

    def _extract_from_cookie(self, response: httpx.Response, bundle: TokenBundle) -> None:
        cfg = self.site.refresh
        if cfg.cookie_name:
            value = _find_cookie(response, cfg.cookie_name, cfg.cookie_domain, cfg.cookie_path)
            if value:
                bundle.access_token = value
        if cfg.refresh_cookie_name:
            value = _find_cookie(response, cfg.refresh_cookie_name, cfg.cookie_domain, cfg.cookie_path)
            if value:
                bundle.refresh_token = value

    # -- 登录态校验 ---------------------------------------------------------------

    def check_login(self, state: SiteState) -> CheckResult:
        cfg = self.site.login_check
        if cfg is None:
            return CheckResult(True, '未配置登录态校验接口')

        request = self._build_check_request(cfg, state)
        attempts = max(1, self.site.retry_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    params=request.params,
                    json=request.json_body,
                    follow_redirects=request.follow_redirects,
                    timeout=request.timeout,
                )
            except httpx.HTTPError as exc:
                # 常见于服务端回收了空闲 keep-alive 连接，重试一次即可
                last_error = exc
                self.log.warning(
                    '站点 %s 登录态校验请求异常（第 %d/%d 次）: %s',
                    self.site.name,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                continue
            return self._evaluate(cfg, response)

        # 网络异常：无法判断登录态（不当作"未登录"，避免白白消耗 refresh_token）
        return CheckResult(None, '登录态校验请求失败', str(last_error))

    def _build_check_request(self, cfg: LoginCheckConfig, state: SiteState) -> RequestSpec:
        context = self.template_context(state)
        headers = render_template(cfg.headers, context)
        headers.setdefault('User-Agent', self.site.user_agent or '')
        if not headers.get('User-Agent'):
            headers.pop('User-Agent')
        cookies = render_template(cfg.cookies, context)
        self.apply_cookies(cookies)
        return RequestSpec(
            method=cfg.method,
            url=render_template(cfg.url, context),
            headers=headers,
            params=render_template(cfg.params, context),
            cookies=cookies,
            json_body=render_template(cfg.body, context) if cfg.body is not None else None,
            follow_redirects=True,
            timeout=cfg.timeout_seconds or self.site.timeout_seconds,
        )

    def _evaluate(self, cfg: LoginCheckConfig, response: httpx.Response) -> CheckResult:
        criteria = cfg.success
        if criteria.type == SuccessType.STATUS:
            ok = response.status_code == criteria.expected
            return CheckResult(
                ok,
                f'HTTP {response.status_code} (期望 {criteria.expected})',
                f'body={response.text[:200]}',
            )

        if criteria.type == SuccessType.COOKIE:
            name = criteria.name or self.site.refresh.cookie_name or ''
            value = _find_cookie(response, name) if name else None
            return CheckResult(bool(value), f'Cookie {name} {"存在" if value else "缺失"}')

        data = _safe_json(response)
        if data is None:
            return CheckResult(False, '响应不是合法 JSON', response.text[:200])
        if not criteria.path:
            return CheckResult(True, 'JSON 解析成功（未配置判定路径）')
        value = get_by_path(data, criteria.path)
        ok = value is not None and value != '' and value is not False if criteria.equals is None else _loose_equals(value, criteria.equals)
        return CheckResult(ok, f'{criteria.path}={value!r} (期望 {criteria.equals!r})')


def _safe_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _loose_equals(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    if isinstance(actual, (int, float)) and isinstance(expected, str):
        return str(actual) == expected
    if isinstance(actual, str) and isinstance(expected, (int, float)):
        return actual == str(expected)
    if isinstance(actual, str) and isinstance(expected, bool):
        return actual.lower() == str(expected).lower()
    return False


def _find_cookie(
    response: httpx.Response,
    name: str,
    domain: str | None = None,
    path: str | None = None,
) -> str | None:
    """在响应的 Set-Cookie 中查找指定 Cookie。"""
    if not name:
        return None
    for cookie in response.cookies.jar:
        if cookie.name != name:
            continue
        if domain and domain not in (cookie.domain or ''):
            continue
        if path and cookie.path and not path.startswith(cookie.path):
            continue
        return cookie.value or None
    return None
