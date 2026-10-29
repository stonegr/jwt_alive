"""服务编排：管理所有站点的运行时，提供检查/刷新/状态查询。"""

from __future__ import annotations

import contextlib
from datetime import datetime
import logging
from pathlib import Path
import threading

from jwt_alive.adapters.loader import create_adapter, ensure_import_path
from jwt_alive.http_client import build_client
from jwt_alive.jwt_tools import is_newer_token
from jwt_alive.models import AppConfig, CheckResult, SiteConfig, SiteState
from jwt_alive.runtime import RefreshOutcome, SiteRuntime, SiteStatus
from jwt_alive.state import StateStore
from jwt_alive.utils import now_utc


class JwtAliveService:
    """jwt-alive 的核心服务。"""

    def __init__(
        self,
        config: AppConfig,
        state_path: str | Path,
        config_path: str | Path | None = None,
        plugin_path: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.config = config
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.store = StateStore(state_path, self.log)
        self.runtimes: dict[str, SiteRuntime] = {}
        # API 线程与调度器线程可能同时操作同一站点，用锁串行化
        self._lock = threading.RLock()

        if plugin_path:
            ensure_import_path(plugin_path)

        for site in self.config.enabled_sites():
            self.runtimes[site.name] = self._build_runtime(site)

    # -- 构建 ---------------------------------------------------------------------

    def _build_runtime(self, site: SiteConfig) -> SiteRuntime:
        client = build_client(site)
        adapter = create_adapter(site.adapter, site, client, self.log)
        state = self.store.ensure(site.name, site.token.access_token, site.token.refresh_token)
        if state.expiry_field is None:
            state.expiry_field = site.jwt.expiry_field
        runtime = SiteRuntime(
            site=site,
            adapter=adapter,
            state=state,
            config_path=self.config_path,
            logger=self.log,
        )
        self._prefer_newer_token(site, state)
        runtime.ensure_expiry()
        self.store.upsert(state)
        return runtime

    def _prefer_newer_token(self, site: SiteConfig, state: SiteState) -> None:
        """配置与状态各有一份 token 时，按 JWT 自身时间戳选新的。

        避免「某份陈旧的 state 副本」一直压过更新的配置，导致老 token 一直生效。
        """
        config_token = site.token.access_token
        state_token = state.access_token
        if not config_token or not state_token or config_token == state_token:
            return
        if not is_newer_token(config_token, state_token):
            return
        state.access_token = config_token
        if site.token.refresh_token:
            state.refresh_token = site.token.refresh_token
        self.log.info(
            '站点 %s：配置里的 token 更新，已改用配置版本（避免陈旧状态压过新 token）',
            site.name,
        )

    # -- 查询 ---------------------------------------------------------------------

    def get_runtime(self, name: str) -> SiteRuntime | None:
        return self.runtimes.get(name)

    def check_all(self) -> dict[str, CheckResult]:
        """检查所有站点的登录态。"""
        results: dict[str, CheckResult] = {}
        with self._lock:
            for name, runtime in self.runtimes.items():
                try:
                    results[name] = runtime.check()
                except Exception as exc:  # 单站异常不影响其它站点
                    self.log.exception('站点 %s 检查异常', name)
                    results[name] = CheckResult(None, f'检查异常: {exc}', str(exc))
            self.store.save()
        return results

    def status_all(self) -> list[SiteStatus]:
        with self._lock:
            return [runtime.status() for runtime in self.runtimes.values()]

    # -- 刷新 ---------------------------------------------------------------------

    def refresh_site(self, name: str, force: bool = False) -> RefreshOutcome:
        runtime = self.runtimes.get(name)
        if runtime is None:
            return RefreshOutcome(site=name, ok=False, reason=f'未知站点: {name}')
        with self._lock:
            outcome = runtime.refresh(force=force)
            self.store.upsert(runtime.state)
            self.store.save()
        return outcome

    def refresh_all(self, force: bool = False) -> list[RefreshOutcome]:
        return [self.refresh_site(name, force=force) for name in self.runtimes]

    def refresh_due(self, now: datetime | None = None) -> list[RefreshOutcome]:
        """只刷新已到期/临近到期的站点。"""
        reference = now or now_utc()
        outcomes: list[RefreshOutcome] = []
        for name, runtime in self.runtimes.items():
            try:
                due = runtime.needs_refresh(reference)
            except Exception:
                self.log.exception('站点 %s 计算刷新时机异常', name)
                due = False
            if due:
                outcomes.append(self.refresh_site(name))
        return outcomes

    # -- 生命周期 -----------------------------------------------------------------

    def close(self) -> None:
        for runtime in self.runtimes.values():
            client = getattr(runtime.adapter, 'client', None)
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
        try:
            self.store.save()
        except OSError as exc:
            self.log.warning('状态保存失败: %s', exc)

    def __enter__(self) -> JwtAliveService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
