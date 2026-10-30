"""定时任务：APScheduler 集成，按站点间隔扫描 + 可选 cron 强制刷新。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import os
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from tzlocal import get_localzone

from jwt_alive.models import CheckKind, RefreshMode, SiteConfig
from jwt_alive.service import JwtAliveService
from jwt_alive.utils import now_utc


def resolve_timezone(logger: logging.Logger | None = None) -> Any:
    """解析本地时区：优先 TZ 环境变量，其次系统本地时区，最后回退 UTC。

    缺少 tzdata 时 tzlocal 会退化成 UTC 并打印警告，导致 cron 触发时间偏移，
    这里显式解析并给出清晰提示。
    """
    name = os.environ.get('TZ')
    if name:
        try:
            return ZoneInfo(name)
        except Exception as exc:
            if logger is not None:
                logger.warning('TZ=%s 无法加载时区（系统缺少 tzdata？），回退 UTC: %s', name, exc)
            return UTC
    try:
        return get_localzone()
    except Exception:
        return UTC


class KeepAliveScheduler:
    """保活调度器。

    - 全局扫描任务：按 ``scan_interval_seconds`` 周期扫描所有站点
    - 站点级间隔：站点 ``schedule.interval_seconds`` 决定该站点多久被扫描一次
    - 站点级 cron：``schedule.cron`` 到点强制刷新该站点
    """

    def __init__(
        self,
        service: JwtAliveService,
        interval_seconds: int = 60,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.interval_seconds = max(1, interval_seconds)
        self.log = logger or logging.getLogger(__name__)
        self.timezone = resolve_timezone(self.log)
        self.scheduler = BackgroundScheduler(timezone=self.timezone)
        self._last_scan: dict[str, datetime] = {}

    # -- 生命周期 -----------------------------------------------------------------

    def start(self) -> None:
        self.scheduler.add_job(
            self._scan,
            trigger=IntervalTrigger(seconds=self.interval_seconds),
            id='scan',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        for site in self.service.config.enabled_sites():
            if site.schedule.cron:
                self.scheduler.add_job(
                    self._force_refresh,
                    trigger=CronTrigger.from_crontab(site.schedule.cron, timezone=self.timezone),
                    args=[site.name],
                    id=f'cron-{site.name}',
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=60,
                )
        self.scheduler.start()
        self.log.info(
            '调度器已启动，扫描间隔 %ds，站点 %d 个',
            self.interval_seconds,
            len(self.service.runtimes),
        )

    def shutdown(self, wait: bool = False) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
            self.log.info('调度器已停止')

    # -- 任务 ---------------------------------------------------------------------

    def _scan(self) -> None:
        now = now_utc()
        self.service.store.last_scan = now
        for name, runtime in list(self.service.runtimes.items()):
            site: SiteConfig = runtime.site
            interval = site.schedule.interval_seconds or self.interval_seconds
            last = self._last_scan.get(name)
            if last is not None and now - last < timedelta(seconds=interval):
                continue
            self._last_scan[name] = now
            try:
                self._process_site(name)
            except Exception:
                self.log.exception('站点 %s 处理异常', name)
        self.service.store.save()

    def _process_site(self, name: str) -> None:
        runtime = self.service.get_runtime(name)
        if runtime is None:
            return

        result = runtime.check()
        mode = runtime.site.schedule.mode

        if result.kind == CheckKind.UNKNOWN:
            # 网络异常/超时：无法判断登录态，不刷新，等下一轮扫描
            self.log.warning(
                '站点 %s 登录态未知(%s)：%s，本轮跳过',
                name,
                result.reason,
                result.detail or '',
            )
            return

        if result.kind == CheckKind.LOGGED_OUT:
            # 服务端明确拒绝 → 会话已失效，必须刷新
            self.log.info('站点 %s 登录态异常(%s)，触发刷新', name, result.reason)
            self.service.refresh_site(name, force=True)
            return

        if result.kind == CheckKind.TOKEN_EXPIRED and mode == RefreshMode.REFRESH:
            # 低频保活模式：access token 过期是正常状态，等 refresh token 临期或用户按需取时再刷
            self.log.debug('站点 %s 的 access token 已过期（refresh 模式不触发刷新）', name)
            self.service.store.upsert(runtime.state)
            self.service.store.save()
            return

        if runtime.needs_refresh(mode=mode):
            remaining = runtime.remaining_seconds()
            refresh_remaining = runtime.refresh_token_remaining()
            if mode == RefreshMode.ACCESS:
                self.log.info('站点 %s 即将过期，触发刷新（剩余 %s 秒）', name, remaining)
            else:
                self.log.info(
                    '站点 %s 触发刷新（access 剩余 %s 秒，refresh 剩余 %s 秒）',
                    name,
                    remaining,
                    refresh_remaining,
                )
            self.service.refresh_site(name)
            return

        self.service.store.upsert(runtime.state)
        self.service.store.save()

    def _force_refresh(self, name: str) -> None:
        self.log.info('cron 触发强制刷新：站点 %s', name)
        self.service.refresh_site(name, force=True)
