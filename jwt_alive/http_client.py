"""HTTP 客户端工厂。"""

from __future__ import annotations

import httpx

from jwt_alive.models import SiteConfig


def build_client(site: SiteConfig) -> httpx.Client:
    """为站点创建独立的 httpx 客户端。"""
    headers: dict[str, str] = {}
    if site.user_agent:
        headers['User-Agent'] = site.user_agent

    transport: httpx.HTTPTransport | None = None
    if site.prefer_ipv4:
        # 绑 v4 源地址：httpcore 用 socket.create_connection(source_address=...)，
        # 绑 v4 后 v6 的 addrinfo 会因地址族不匹配被跳过 → 实际只走 IPv4
        transport = httpx.HTTPTransport(
            verify=site.verify_tls,
            trust_env=site.trust_env,
            proxy=site.proxy,
            local_address='0.0.0.0',
            retries=1,  # 连接级错误在 httpcore 层重试一次
        )

    client = httpx.Client(
        transport=transport,
        timeout=site.timeout_seconds,
        follow_redirects=True,
        headers=headers,
        **(
            {}
            if transport is not None
            else {
                'verify': site.verify_tls,
                'trust_env': site.trust_env,
                'proxy': site.proxy,
            }
        ),
    )
    for name, value in site.cookies.items():
        client.cookies.set(name, value)
    return client
