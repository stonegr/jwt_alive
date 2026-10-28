"""站点适配器基类。

不同网站的刷新接口、token 位置、登录态判定方式差异很大，
统一抽象成 5 个可覆写的钩子，站点特有逻辑放在 plugins/ 下的子类中。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
import re
from typing import Any

import httpx

from jwt_alive.jwt_tools import decode_payload, resolve_expiry
from jwt_alive.models import CheckResult, ExpiryInfo, SiteConfig, SiteState, TokenBundle


_PLACEHOLDER = re.compile(r'\{(\w+)\}')


@dataclass(slots=True)
class RequestSpec:
    """一次 HTTP 请求的描述。"""

    method: str = 'GET'
    url: str = ''
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    json_body: Any | None = None
    follow_redirects: bool = True
    timeout: float | None = None


def render_template(value: Any, context: dict[str, str]) -> Any:
    """递归替换 {key} 占位符；未知的占位符保持原样。"""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: str(context.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {key: render_template(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    return value


class SiteAdapter(ABC):
    """站点适配器。

    子类必须实现 ``build_refresh_request`` 与 ``extract_tokens``，
    其余钩子可选覆写，覆写后无需修改框架代码。
    """

    def __init__(self, site: SiteConfig, client: httpx.Client, logger: logging.Logger) -> None:
        self.site = site
        self.client = client
        self.log = logger

    # -- 便捷属性 ----------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.site.name

    def template_context(self, state: SiteState) -> dict[str, str]:
        """模板变量：access_token / refresh_token / 站点自定义变量。"""
        context: dict[str, str] = {
            'site_name': self.site.name,
            'access_token': state.access_token,
            'refresh_token': state.refresh_token,
        }
        context.update(self.site.token.variables)
        return context

    def apply_cookies(self, cookies: dict[str, str]) -> None:
        """把 Cookie 写入客户端。

        httpx 已废弃按请求传 cookies（行为有歧义），统一在客户端上设置。
        """
        for name, value in cookies.items():
            self.client.cookies.set(name, value)

    # -- 可覆写钩子 --------------------------------------------------------------

    def resolve_expiry(self, token: str) -> ExpiryInfo:
        """解析 token 的过期时间（默认按 jwt.expiry_field / expiry_format）。"""
        payload = decode_payload(token)
        return resolve_expiry(payload, self.site.jwt)

    @abstractmethod
    def build_refresh_request(self, state: SiteState) -> RequestSpec:
        """构造刷新请求。"""
        raise NotImplementedError

    @abstractmethod
    def extract_tokens(self, response: httpx.Response) -> TokenBundle:
        """从刷新响应中提取新 token。"""
        raise NotImplementedError

    def check_login(self, state: SiteState) -> CheckResult:
        """通过接口校验登录态。"""
        if self.site.login_check is None:
            return CheckResult(True, '未配置登录态校验接口')
        raise NotImplementedError

    def on_refreshed(self, previous: SiteState, current: SiteState) -> None:
        """刷新成功后的钩子（写额外数据、发通知等）。"""
        return None
