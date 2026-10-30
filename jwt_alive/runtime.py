"""站点运行时：把配置、适配器、状态串成一个可执行的单元。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from pathlib import Path
import time
from typing import Any

import httpx

from jwt_alive.adapters.base import SiteAdapter
from jwt_alive.jwt_tools import JwtDecodeError, decode_payload
from jwt_alive.models import CheckResult, ExpiryInfo, SiteConfig, SiteState
from jwt_alive.persist import write_back_tokens_safe
from jwt_alive.utils import describe_error, mask_token, now_utc, truncate


class RefreshError(Exception):
    """刷新失败。"""


@dataclass(slots=True)
class RefreshOutcome:
    """刷新结果。"""

    site: str
    ok: bool
    reason: str
    previous_expiry: datetime | None = None
    new_expiry: datetime | None = None
    new_access_token: str | None = None
    written_back: bool = False
    detail: str | None = None


@dataclass(slots=True)
class SiteStatus:
    """站点状态快照（用于 CLI 展示）。"""

    name: str
    logged_in: bool | None = None
    expires_at: datetime | None = None
    remaining_seconds: float | None = None
    expiry_field: str | None = None
    last_check: datetime | None = None
    last_refresh: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class SiteRuntime:
    """单个站点的运行时封装。"""

    def __init__(
        self,
        site: SiteConfig,
        adapter: SiteAdapter,
        state: SiteState,
        config_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.site = site
        self.adapter = adapter
        self.state = state
        self.config_path = config_path
        self.log = logger or logging.getLogger(__name__)

    # -- 基础信息 ----------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.site.name

    @property
    def refresh_before_seconds(self) -> int:
        value = self.site.schedule.refresh_before_seconds
        return value if value is not None else 300

    def expiry_info(self) -> ExpiryInfo:
        """解析当前 token 的过期时间。"""
        return self.adapter.resolve_expiry(self.state.access_token)

    def ensure_expiry(self) -> None:
        """首次使用时补齐状态里的到期时间（配置里的初始 token 也要算）。"""
        if self.state.expires_at is not None or not self.state.access_token:
            return
        try:
            info = self.adapter.resolve_expiry(self.state.access_token)
        except JwtDecodeError as exc:
            self.log.debug('站点 %s 的初始 token 无法解析: %s', self.name, exc)
            return
        self.state.expires_at = info.expires_at
        self.state.expiry_field = info.field

    def remaining_seconds(self, now: datetime | None = None) -> float | None:
        info = self.expiry_info()
        if info.expires_at is None:
            return None
        reference = now or now_utc()
        return (info.expires_at - reference).total_seconds()

    def needs_refresh(self, now: datetime | None = None) -> bool:
        """是否到了需要刷新的时间点。"""
        reference = now or now_utc()
        try:
            info = self.expiry_info()
        except JwtDecodeError:
            return True
        if info.expires_at is None:
            return False
        remaining = (info.expires_at - reference).total_seconds()
        return remaining <= self.refresh_before_seconds

    # -- 登录态校验 ---------------------------------------------------------------

    def check(self) -> CheckResult:
        """先看 JWT 自身，再调用站点接口校验。"""
        token = self.state.access_token
        if not token:
            result = CheckResult(False, '没有 access_token')
            self._record_check(result)
            return result

        try:
            decode_payload(token)
        except JwtDecodeError as exc:
            result = CheckResult(False, 'token 无法解析', str(exc))
            self._record_check(result)
            return result

        try:
            info = self.expiry_info()
        except JwtDecodeError as exc:
            result = CheckResult(False, 'token 无法解析', str(exc))
            self._record_check(result)
            return result

        if info.expires_at is not None:
            leeway = self.site.jwt.leeway_seconds
            remaining = (info.expires_at - now_utc()).total_seconds() + leeway
            if remaining <= 0:
                result = CheckResult(False, 'token 已过期', f'过期时间 {info.expires_at.isoformat()}')
                self._record_check(result)
                return result

        if self.site.login_check is None:
            result = CheckResult(True, 'token 未过期（未配置校验接口）')
            self._record_check(result)
            return result

        result = self.adapter.check_login(self.state)
        self._record_check(result)
        return result

    def _record_check(self, result: CheckResult) -> None:
        self.state.logged_in = result.logged_in
        self.state.last_check = now_utc()
        if not result.logged_in:
            self.state.last_error = result.reason
        elif self.state.last_error is None:
            self.state.last_error = None

    # -- 刷新 ---------------------------------------------------------------------

    def refresh(self, force: bool = False) -> RefreshOutcome:
        """执行刷新流程。"""
        previous = self.state
        try:
            previous_info = self.expiry_info()
        except JwtDecodeError:
            previous_info = ExpiryInfo(expires_at=None)

        try:
            request = self.adapter.build_refresh_request(self.state)
            response = self._send(request)
            bundle = self.adapter.extract_tokens(response)
        except RefreshError as exc:
            self._on_failure(str(exc))
            return RefreshOutcome(site=self.name, ok=False, reason=str(exc))
        except httpx.HTTPError as exc:
            self._on_failure(f'请求失败: {exc}')
            return RefreshOutcome(site=self.name, ok=False, reason=f'请求失败: {exc}')

        if not bundle.access_token:
            detail = truncate(response.text, 200)
            reason = '刷新成功但未能提取到 access_token'
            self._on_failure(reason)
            return RefreshOutcome(site=self.name, ok=False, reason=reason, detail=detail or None)

        new_state = SiteState(
            name=self.name,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token or self.state.refresh_token,
        )
        new_info = self._update_expiry(new_state)
        new_state.logged_in = self.state.logged_in
        new_state.last_check = self.state.last_check
        new_state.last_refresh = now_utc()
        new_state.consecutive_failures = 0
        new_state.last_error = None
        new_state.history = list(self.state.history[-19:])
        _append_history(new_state, True, '刷新成功')

        self.state = new_state
        self._on_success()

        written = False
        if self.site.persist.write_back_to_config and self.config_path is not None:
            written = write_back_tokens_safe(
                self.config_path,
                self.name,
                new_state.access_token,
                new_state.refresh_token,
                self.log,
            )

        try:
            self.adapter.on_refreshed(previous, new_state)
        except Exception as exc:  # 钩子异常不影响主流程
            self.log.warning('站点 %s 的 on_refreshed 钩子异常: %s', self.name, exc)

        self.log.info(
            '站点 %s 刷新成功，新 token=%s',
            self.name,
            mask_token(new_state.access_token),
        )
        return RefreshOutcome(
            site=self.name,
            ok=True,
            reason='刷新成功',
            previous_expiry=previous_info.expires_at,
            new_expiry=new_info.expires_at,
            new_access_token=new_state.access_token,
            written_back=written,
        )

    def _send(self, request: Any) -> httpx.Response:
        last_error: Exception | None = None
        attempts = max(1, self.site.retry_attempts)
        backoff = self.site.retry_backoff_seconds
        for attempt in range(1, attempts + 1):
            try:
                response = self.adapter.client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    params=request.params,
                    json=request.json_body,
                    follow_redirects=request.follow_redirects,
                    timeout=request.timeout,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                detail = describe_error(exc)
                if attempt < attempts:
                    self.log.info(
                        '站点 %s 刷新请求异常（第 %d/%d 次）: %s，重试中',
                        self.name,
                        attempt,
                        attempts,
                        detail,
                    )
                else:
                    self.log.warning(
                        '站点 %s 刷新请求异常（第 %d/%d 次）: %s',
                        self.name,
                        attempt,
                        attempts,
                        detail,
                    )
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise RefreshError(f'刷新接口返回 HTTP {response.status_code}: {truncate(response.text, 200)}')
                    return response
                last_error = RefreshError(f'刷新接口返回 HTTP {response.status_code}')
                if attempt < attempts:
                    self.log.info(
                        '站点 %s 刷新接口返回 %d（第 %d/%d 次），重试中',
                        self.name,
                        response.status_code,
                        attempt,
                        attempts,
                    )
                else:
                    self.log.warning(
                        '站点 %s 刷新接口返回 %d（第 %d/%d 次）',
                        self.name,
                        response.status_code,
                        attempt,
                        attempts,
                    )
            if attempt < attempts and backoff > 0:
                _sleep(backoff * attempt)
        raise RefreshError(f'刷新失败: {last_error}')

    def _update_expiry(self, state: SiteState) -> ExpiryInfo:
        try:
            info = self.adapter.resolve_expiry(state.access_token)
        except JwtDecodeError as exc:
            self.log.warning('站点 %s 的新 token 无法解析: %s', self.name, exc)
            return ExpiryInfo(expires_at=None, resolved=False)
        state.expires_at = info.expires_at
        state.expiry_field = info.field
        return info

    def _on_failure(self, reason: str) -> None:
        self.state.consecutive_failures += 1
        self.state.last_error = reason
        self.state.last_check = now_utc()
        _append_history(self.state, False, reason)
        self.log.error('站点 %s 刷新失败: %s', self.name, reason)

    def _on_success(self) -> None:
        self.state.consecutive_failures = 0
        self.state.last_error = None
        self.state.last_check = now_utc()

    # -- 展示 ---------------------------------------------------------------------

    def status(self) -> SiteStatus:
        remaining = self.remaining_seconds()
        return SiteStatus(
            name=self.name,
            logged_in=self.state.logged_in,
            expires_at=self.state.expires_at,
            remaining_seconds=remaining,
            expiry_field=self.state.expiry_field or self.site.jwt.expiry_field,
            last_check=self.state.last_check,
            last_refresh=self.state.last_refresh,
            last_error=self.state.last_error,
            consecutive_failures=self.state.consecutive_failures,
        )


def _append_history(state: SiteState, ok: bool, reason: str) -> None:
    """记录一条刷新历史（最多保留 20 条）。"""
    entries = list(state.history)[-19:]
    entries.append(
        {
            'time': now_utc().isoformat(),
            'action': 'refresh',
            'ok': ok,
            'reason': reason,
        }
    )
    state.history = entries


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
