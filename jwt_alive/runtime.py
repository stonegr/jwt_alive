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
from jwt_alive.jwt_tools import JwtDecodeError, decode_payload, resolve_expiry
from jwt_alive.models import (
    CheckKind,
    CheckResult,
    ExpiryInfo,
    RefreshMode,
    SiteConfig,
    SiteState,
)
from jwt_alive.persist import write_back_tokens_safe
from jwt_alive.utils import describe_error, mask_token, now_utc, truncate


DEFAULT_REFRESH_TOKEN_BEFORE_SECONDS = 86400


class RefreshError(Exception):
    """刷新失败。"""


class AuthRejectedError(RefreshError):
    """服务端明确拒绝（401/403）→ 会话已失效，登录态应置为未登录。"""


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
    refresh_expires_at: datetime | None = None
    refresh_remaining_seconds: float | None = None
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
        """解析当前 access token 的过期时间。"""
        return self.adapter.resolve_expiry(self.state.access_token)

    def refresh_expiry_info(self) -> ExpiryInfo:
        """解析 refresh token 的过期时间（字段名可单独配置）。

        解析不出来（非 JWT / 无过期字段）时返回空结果，由调用方退回按 access token 判断。
        """
        token = self.state.refresh_token
        if not token:
            return ExpiryInfo(expires_at=None)
        field = self.site.jwt.refresh_expiry_field
        try:
            if not field:
                return self.adapter.resolve_expiry(token)
            payload = decode_payload(token)
        except JwtDecodeError:
            return ExpiryInfo(expires_at=None)
        return resolve_expiry(payload, self.site.jwt.model_copy(update={'expiry_field': field}))

    def refresh_token_remaining(self, now: datetime | None = None) -> float | None:
        """refresh token 的剩余寿命（秒），解析不出来返回 None。"""
        info = self.refresh_expiry_info()
        if info.expires_at is None:
            return None
        reference = now or now_utc()
        return (info.expires_at - reference).total_seconds()

    def access_token_expired(self, now: datetime | None = None) -> bool:
        """access token 是否已过期（含 leeway 容忍）。"""
        try:
            info = self.expiry_info()
        except JwtDecodeError:
            return True
        if info.expires_at is None:
            return False
        reference = now or now_utc()
        remaining = (info.expires_at - reference).total_seconds() + self.site.jwt.leeway_seconds
        return remaining <= 0

    def ensure_expiry(self) -> None:
        """补齐状态里的到期时间（配置里的初始 token 也要算）。

        access 与 refresh 两个 token 的到期时间都会补齐，
        方便按 refresh token 触发刷新（mode=refresh）。
        """
        if self.state.expires_at is None and self.state.access_token:
            try:
                info = self.adapter.resolve_expiry(self.state.access_token)
            except JwtDecodeError as exc:
                self.log.debug('站点 %s 的初始 token 无法解析: %s', self.name, exc)
            else:
                self.state.expires_at = info.expires_at
                self.state.expiry_field = info.field
        self._sync_refresh_expiry(self.state)

    def remaining_seconds(self, now: datetime | None = None) -> float | None:
        try:
            info = self.expiry_info()
        except JwtDecodeError:
            # token 不是 JWT（不透明 token / 占位值）→ 没有到期时间概念
            return None
        if info.expires_at is None:
            return None
        reference = now or now_utc()
        return (info.expires_at - reference).total_seconds()

    def needs_refresh(self, now: datetime | None = None, mode: RefreshMode | None = None) -> bool:
        """是否到了需要刷新的时间点（按 schedule.mode 判定）。"""
        reference = now or now_utc()
        effective = mode or self.site.schedule.mode
        if effective == RefreshMode.REFRESH:
            return self._refresh_token_near_expiry(reference)
        access_due = self._access_near_expiry(reference)
        if effective == RefreshMode.HYBRID:
            return access_due or self._refresh_token_near_expiry(reference)
        return access_due

    def _access_near_expiry(self, reference: datetime) -> bool:
        try:
            info = self.expiry_info()
        except JwtDecodeError:
            return True
        if info.expires_at is None:
            return False
        remaining = (info.expires_at - reference).total_seconds()
        return remaining <= self.refresh_before_seconds

    def _refresh_token_near_expiry(self, reference: datetime) -> bool:
        """refresh token 是否临近到期；解析不出来时退回按 access token 判断。"""
        remaining = self.refresh_token_remaining(reference)
        if remaining is None:
            # refresh token 不是 JWT / 没有过期字段 → 退回 access 策略，避免不刷新
            self.log.debug('站点 %s 的 refresh token 无法解析过期时间，退回按 access token 判断', self.name)
            return self._access_near_expiry(reference)
        threshold = self.site.schedule.refresh_token_before_seconds
        if threshold is None:
            threshold = DEFAULT_REFRESH_TOKEN_BEFORE_SECONDS
        return remaining <= threshold

    # -- 登录态校验 ---------------------------------------------------------------

    def check(self) -> CheckResult:
        """先看 JWT 自身，再按需调用站点接口校验。

        mode=refresh 时不请求校验接口（那次真正的刷新本身就是对会话有效性的检测）。
        """
        token = self.state.access_token
        if not token:
            result = CheckResult(False, '没有 access_token', kind=CheckKind.LOGGED_OUT)
            self._record_check(result)
            return result

        try:
            decode_payload(token)
        except JwtDecodeError as exc:
            result = CheckResult(False, 'token 无法解析', str(exc), kind=CheckKind.LOGGED_OUT)
            self._record_check(result)
            return result

        try:
            info = self.expiry_info()
        except JwtDecodeError as exc:
            result = CheckResult(False, 'token 无法解析', str(exc), kind=CheckKind.LOGGED_OUT)
            self._record_check(result)
            return result

        if info.expires_at is not None:
            leeway = self.site.jwt.leeway_seconds
            remaining = (info.expires_at - now_utc()).total_seconds() + leeway
            if remaining <= 0:
                result = CheckResult(
                    False,
                    'token 已过期',
                    f'过期时间 {info.expires_at.isoformat()}',
                    kind=CheckKind.TOKEN_EXPIRED,
                )
                self._record_check(result)
                return result

        if self.site.schedule.mode == RefreshMode.REFRESH:
            # 低频保活模式：不做接口校验，等真正刷新时再验证会话是否还有效
            result = CheckResult(True, 'token 未过期（refresh 模式不做接口校验）')
            self._record_check(result)
            return result

        if self.site.login_check is None:
            result = CheckResult(True, 'token 未过期（未配置校验接口）')
            self._record_check(result)
            return result

        result = self.adapter.check_login(self.state)
        if result.logged_in is None:
            result.kind = CheckKind.UNKNOWN
        elif result.logged_in is False:
            result.kind = CheckKind.LOGGED_OUT
        else:
            result.kind = CheckKind.LOGGED_IN
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
        except AuthRejectedError as exc:
            # 服务端明确拒绝 → 会话已失效
            self._on_failure(str(exc))
            self.state.logged_in = False
            return RefreshOutcome(site=self.name, ok=False, reason=str(exc))
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
        new_state.last_check = self.state.last_check
        new_state.last_refresh = now_utc()
        new_state.consecutive_failures = 0
        new_state.last_error = None
        new_state.history = list(self.state.history[-19:])
        _append_history(new_state, True, '刷新成功')

        self.state = new_state
        self._sync_refresh_expiry(new_state)
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
                        raise AuthRejectedError(f'刷新接口返回 HTTP {response.status_code}: {truncate(response.text, 200)}')
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

    def _sync_refresh_expiry(self, state: SiteState) -> None:
        """刷新后同步 refresh token 的到期时间到状态里。"""
        try:
            info = self.refresh_expiry_info()
        except JwtDecodeError:
            info = ExpiryInfo(expires_at=None)
        state.refresh_expires_at = info.expires_at

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
        # 刷新成功即证明会话有效（服务端认了 refresh token 并签发了新 access token）
        self.state.logged_in = True
        self.state.consecutive_failures = 0
        self.state.last_error = None

    # -- 展示 ---------------------------------------------------------------------

    def status(self) -> SiteStatus:
        remaining = self.remaining_seconds()
        refresh_remaining = self.refresh_token_remaining()
        return SiteStatus(
            name=self.name,
            logged_in=self.state.logged_in,
            expires_at=self.state.expires_at,
            remaining_seconds=remaining,
            refresh_expires_at=self.state.refresh_expires_at,
            refresh_remaining_seconds=refresh_remaining,
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
