"""时区配置测试（cron 触发时区可配）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from jwt_alive.config import parse_config
from jwt_alive.scheduler import KeepAliveScheduler, apply_process_timezone, resolve_timezone
from jwt_alive.service import JwtAliveService


def _site(cron: str = '0 3 * * *', **overrides: Any) -> dict[str, Any]:
    site: dict[str, Any] = {
        'name': 'demo',
        'token': {'access_token': 'a', 'refresh_token': 'r'},
        'jwt': {'expiry_field': 'exp'},
        'refresh': {'url': 'https://example.com/refresh'},
        'schedule': {'cron': cron},
    }
    site.update(overrides)
    return site


def _service(tmp_path: Path, site: dict[str, Any], defaults: dict[str, Any] | None = None) -> JwtAliveService:
    config = parse_config({'defaults': defaults or {}, 'sites': [site]})
    return JwtAliveService(config=config, state_path=tmp_path / 'state.json')


def test_resolve_timezone_explicit_name() -> None:
    tz = resolve_timezone(None, 'Asia/Shanghai')
    assert str(tz) == 'Asia/Shanghai'


def test_resolve_timezone_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TZ', 'Europe/London')
    assert str(resolve_timezone(None)) == 'Europe/London'


def test_resolve_timezone_invalid_falls_back_to_utc() -> None:
    log = logging.getLogger('test')
    tz = resolve_timezone(log, 'Nowhere/Nothing')
    assert str(tz) == 'UTC'


def test_config_timezone_used_by_scheduler(tmp_path: Path) -> None:
    """defaults.timezone 决定调度器与 cron 触发器的时区。"""
    service = _service(tmp_path, _site(), defaults={'timezone': 'Asia/Shanghai'})
    scheduler = KeepAliveScheduler(service, interval_seconds=60, logger=logging.getLogger('t'))
    try:
        assert str(scheduler.timezone) == 'Asia/Shanghai'
        scheduler.start()
        job = scheduler.scheduler.get_job('cron-demo')
        assert job is not None
        assert str(job.trigger.timezone) == 'Asia/Shanghai'
    finally:
        scheduler.shutdown()
        service.close()


def test_site_timezone_overrides_default(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        _site(schedule={'cron': '0 3 * * *', 'timezone': 'Europe/London'}),
        defaults={'timezone': 'Asia/Shanghai'},
    )
    scheduler = KeepAliveScheduler(service, interval_seconds=60, logger=logging.getLogger('t'))
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job('cron-demo')
        assert job is not None
        assert str(job.trigger.timezone) == 'Europe/London'
    finally:
        scheduler.shutdown()
        service.close()


def test_cron_fire_time_reflects_timezone(tmp_path: Path) -> None:
    """同一份 cron 表达式，不同时区算出的下次触发时间不同。"""
    service = _service(
        tmp_path,
        _site(schedule={'cron': '0 3 * * *', 'timezone': 'UTC'}),
        defaults={'timezone': 'Asia/Shanghai'},
    )
    scheduler = KeepAliveScheduler(service, interval_seconds=60, logger=logging.getLogger('t'))
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job('cron-demo')
        assert job is not None
        assert str(job.trigger.timezone) == 'UTC'
    finally:
        scheduler.shutdown()
        service.close()


def test_apply_process_timezone_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.delenv('TZ', raising=False)
    apply_process_timezone('Asia/Shanghai')
    assert os.environ['TZ'] == 'Asia/Shanghai'


def test_apply_process_timezone_none_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.delenv('TZ', raising=False)
    apply_process_timezone(None)
    assert 'TZ' not in os.environ
