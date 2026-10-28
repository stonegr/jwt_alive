"""自定义站点适配器示例。

用法：在 config/sites.yaml 中指定

    sites:
      - name: my-site
        adapter: "plugins.example_site:HeaderTokenAdapter"

或直接放在站点目录下：

    adapter: "example_site:HeaderTokenAdapter"

子类可覆写以下钩子（全部可选）：
    resolve_expiry(token)         解析过期时间
    build_refresh_request(state)  构造刷新请求
    extract_tokens(response)      提取新 token
    check_login(state)            校验登录态
    on_refreshed(old, new)        刷新成功后的钩子
"""

from __future__ import annotations

import httpx

from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.models import CheckResult, SiteState, TokenBundle


class HeaderTokenAdapter(HttpSiteAdapter):
    """刷新接口把新 token 放在响应头 X-Token 里的站点。

    登录态校验：请求用户信息接口，判断响应体里有没有 userId。
    """

    def extract_tokens(self, response: httpx.Response) -> TokenBundle:
        bundle = TokenBundle()
        access_token = response.headers.get('X-Token')
        if access_token:
            bundle.access_token = access_token
        refresh_token = response.headers.get('X-Refresh-Token')
        if refresh_token:
            bundle.refresh_token = refresh_token
        return bundle

    def check_login(self, state: SiteState) -> CheckResult:
        url = 'https://my-site.example.com/api/user/info'
        try:
            response = self.client.get(
                url,
                headers={'Authorization': f'Bearer {state.access_token}'},
                timeout=self.site.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return CheckResult(False, '用户信息接口请求失败', str(exc))

        if response.status_code != 200:
            return CheckResult(False, f'用户信息接口返回 {response.status_code}')

        try:
            data = response.json()
        except ValueError:
            return CheckResult(False, '用户信息接口返回非 JSON')

        user_id = (data or {}).get('userId')
        if not user_id:
            return CheckResult(False, '响应中没有 userId，视为未登录')
        return CheckResult(True, f'已登录 userId={user_id}')
