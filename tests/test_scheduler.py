"""调度器测试。"""

from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
from typing import Any

from jwt_alive.config import parse_config
from jwt_alive.scheduler import KeepAliveScheduler
from jwt_alive.service import JwtAliveService
from jwt_alive.utils import now_utc

from .conftest import make_token


REFRESH_URL = 'https://example.com/api/auth/refresh'
PROFILE_URL = 'https://example.com/api/user/profile'


def _site(token: str, **overrides: Any) -> dict[str, Any]:
    site: dict[str, Any] = {
        'name': 'demo',
        'token': {'access_token': token, 'refresh_token': 'rt'},
        'refresh': {
            'url': REFRESH_URL,
            'method': 'POST',
            'access_token_path': 'data.access_token',
        },
        'login_check': {'url': PROFILE_URL, 'method': 'GET'},
    }
    site.update(overrides)
    return site


def _service(tmp_path: Path, site: dict[str, Any]) -> JwtAliveService:
    config = parse_config({'sites': [site]})
    return JwtAliveService(
        config=config,
        state_path=tmp_path / 'state.json',
        plugin_path=tmp_path / 'plugins',
    )


def test_scheduler_registers_jobs(tmp_path: Path) -> None:
    service = _service(tmp_path, _site(make_token()))
    scheduler = KeepAliveScheduler(service, interval_seconds=30, logger=logging.getLogger('t'))
    try:
        scheduler.start()
        job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
        assert 'scan' in job_ids
    finally:
        scheduler.shutdown()
        service.close()


def test_scheduler_registers_cron_job(tmp_path: Path) -> None:
    site = _site(make_token())
    site['schedule'] = {'cron': '0 3 * * *'}
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=30, logger=logging.getLogger('t'))
    try:
        scheduler.start()
        job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
        assert 'cron-demo' in job_ids
    finally:
        scheduler.shutdown()
        service.close()


def test_scan_refreshes_when_near_expiry(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'ok': True})
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site(make_token(expires_in=30)))
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is not None
    finally:
        scheduler.shutdown()
        service.close()


def test_scan_respects_site_interval(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', json={'ok': True})
    site = _site(make_token(expires_in=3600))
    site['schedule'] = {'interval_seconds': 600}
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        first = scheduler._last_scan['demo']
        scheduler._scan()
        assert scheduler._last_scan['demo'] == first
    finally:
        scheduler.shutdown()
        service.close()


def test_scan_triggers_refresh_when_logged_out(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=PROFILE_URL, method='GET', status_code=401, json={})
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site(make_token(expires_in=3600)))
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is not None
    finally:
        scheduler.shutdown()
        service.close()


def test_force_refresh_by_cron(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', json={'data': {'access_token': make_token(expires_in=7200)}})
    service = _service(tmp_path, _site(make_token(expires_in=3600)))
    scheduler = KeepAliveScheduler(service, interval_seconds=60, logger=logging.getLogger('t'))
    try:
        scheduler._force_refresh('demo')
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is not None
    finally:
        scheduler.shutdown()
        service.close()


def test_history_is_recorded(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url=REFRESH_URL, method='POST', status_code=500, json={})
    site = _site(make_token(expires_in=3600))
    site['retry_attempts'] = 1
    site['retry_backoff_seconds'] = 0
    service = _service(tmp_path, site)
    try:
        outcome = service.refresh_site('demo')
        assert not outcome.ok
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.history
        assert runtime.state.history[-1]['ok'] is False
    finally:
        service.close()


def test_now_utc_is_timezone_aware() -> None:
    value = now_utc()
    assert value.tzinfo is not None
    assert value + timedelta(seconds=1) > value
