"""HTTP 客户端工厂。"""

from __future__ import annotations

import httpx

from jwt_alive.models import SiteConfig


def build_client(site: SiteConfig) -> httpx.Client:
    """为站点创建独立的 httpx 客户端。"""
    headers: dict[str, str] = {}
    if site.user_agent:
        headers['User-Agent'] = site.user_agent
    client = httpx.Client(
        verify=site.verify_tls,
        timeout=site.timeout_seconds,
        follow_redirects=True,
        headers=headers,
        trust_env=site.trust_env,
        proxy=site.proxy,
    )
    for name, value in site.cookies.items():
        client.cookies.set(name, value)
    return client
