"""token 选新（按 JWT 时间戳）与登录态结论分类的测试。"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import httpx
import jwt

from jwt_alive.config import parse_config
from jwt_alive.jwt_tools import is_newer_token, token_exp_iat
from jwt_alive.service import JwtAliveService


SECRET = 'x' * 40


def _token(expires_in: int, issued_at: int | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            'sub': 'tester',
            'iat': issued_at if issued_at is not None else now,
            'exp': now + expires_in,
        },
        SECRET,
        algorithm='HS256',
    )


def _site(token: str, refresh_token: str = 'rt') -> dict[str, Any]:
    return {
        'name': 'demo',
        'token': {'access_token': token, 'refresh_token': refresh_token},
        'jwt': {'expiry_field': 'exp'},
        'refresh': {
            'url': 'https://example.com/api/auth/refresh',
            'method': 'POST',
            'access_token_path': 'data.access_token',
        },
        'login_check': {'url': 'https://example.com/api/user/profile', 'method': 'GET'},
    }


def _service(tmp_path: Path, site: dict[str, Any], state_token: str | None = None) -> JwtAliveService:
    config = parse_config({'sites': [site]})
    state_path = tmp_path / 'state.json'
    if state_token is not None:
        # 模拟"状态文件里存着旧 token"（陈旧副本）
        state_path.write_text(
            json.dumps(
                {
                    'version': 1,
                    'last_scan': None,
                    'sites': {
                        'demo': {
                            'name': 'demo',
                            'access_token': state_token,
                            'refresh_token': 'state-rt',
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
    return JwtAliveService(config=config, state_path=state_path)


# -- is_newer_token -----------------------------------------------------------------------


def test_is_newer_token_by_exp() -> None:
    older = _token(3600)
    newer = _token(7200)
    assert is_newer_token(newer, older)
    assert not is_newer_token(older, newer)


def test_is_newer_token_same_exp_uses_iat() -> None:
    exp = int(time.time()) + 3600
    old_iat = _token(0, issued_at=1000)
    old_iat = jwt.encode({'exp': exp, 'iat': 1000}, SECRET, algorithm='HS256')
    new_iat = jwt.encode({'exp': exp, 'iat': 2000}, SECRET, algorithm='HS256')
    assert is_newer_token(new_iat, old_iat)
    assert not is_newer_token(old_iat, new_iat)


def test_is_newer_token_opaque_returns_false() -> None:
    assert not is_newer_token('opaque-token', _token(3600))
    assert not is_newer_token(_token(3600), 'opaque-token')


def test_token_exp_iat_of_opaque() -> None:
    assert token_exp_iat('not-a-jwt') == (None, None)
    assert token_exp_iat('') == (None, None)


# -- service 选新 --------------------------------------------------------------------------


def test_config_token_newer_than_state_wins(tmp_path: Path) -> None:
    """state 里的 token 陈旧时，改用配置里更新的那个。"""
    old = _token(3600)
    new = _token(7200)
    service = _service(tmp_path, _site(new, 'new-rt'), state_token=old)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.access_token == new
        assert runtime.state.refresh_token == 'new-rt'
    finally:
        service.close()


def test_state_token_newer_than_config_kept(tmp_path: Path) -> None:
    """state 里的 token 更新时，保持用它。"""
    old = _token(3600)
    new = _token(7200)
    service = _service(tmp_path, _site(old, 'old-rt'), state_token=new)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.access_token == new
    finally:
        service.close()


def test_same_token_no_change(tmp_path: Path) -> None:
    token = _token(3600)
    service = _service(tmp_path, _site(token), state_token=token)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.access_token == token
    finally:
        service.close()


def test_opaque_state_token_keeps_state_priority(tmp_path: Path) -> None:
    """state 里是不可解析的 token 时，维持现状（state 优先）。"""
    new = _token(7200)
    service = _service(tmp_path, _site(new), state_token='opaque')
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.access_token == 'opaque'
    finally:
        service.close()


def test_stale_state_is_self_healed_on_save(tmp_path: Path) -> None:
    """陈旧的 state 文件在下次保存时被更新成新 token（自愈）。"""
    old = _token(3600)
    new = _token(7200)
    config = parse_config({'sites': [_site(new, 'new-rt')]})
    state_path = tmp_path / 'state.json'
    state_path.write_text(
        json.dumps(
            {
                'version': 1,
                'last_scan': None,
                'sites': {'demo': {'name': 'demo', 'access_token': old, 'refresh_token': 'old-rt'}},
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    service = JwtAliveService(config=config, state_path=state_path)
    try:
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.access_token == new  # 已改用配置里的新 token
        service.store.save()
    finally:
        service.close()

    service2 = JwtAliveService(config=config, state_path=tmp_path / 'state.json')
    try:
        runtime2 = service2.get_runtime('demo')
        assert runtime2 is not None
        assert runtime2.state.access_token == new
    finally:
        service2.close()


# -- 登录态结论分类 ------------------------------------------------------------------------


def test_network_error_does_not_mark_logged_out(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_exception(httpx.ConnectError('boom'), url='https://example.com/api/user/profile')
    site = _site(_token(3600))
    site['retry_attempts'] = 1
    service = _service(tmp_path, site)
    try:
        results = service.check_all()
    finally:
        service.close()
    result = results['demo']
    assert result.logged_in is None  # 无法判断
    assert result.detail


def test_401_means_logged_out(httpx_mock: Any, tmp_path: Path) -> None:
    httpx_mock.add_response(url='https://example.com/api/user/profile', status_code=401, json={})
    service = _service(tmp_path, _site(_token(3600)))
    try:
        results = service.check_all()
    finally:
        service.close()
    assert results['demo'].logged_in is False


def test_login_check_retries_on_transient_error(httpx_mock: Any, tmp_path: Path) -> None:
    """第一次连接异常、第二次成功 → 不应判定为异常。"""
    httpx_mock.add_exception(httpx.ConnectError('boom'), url='https://example.com/api/user/profile')
    httpx_mock.add_response(url='https://example.com/api/user/profile', json={'ok': True})
    site = _site(_token(3600))
    site['retry_attempts'] = 3
    site['retry_backoff_seconds'] = 0
    service = _service(tmp_path, site)
    try:
        results = service.check_all()
    finally:
        service.close()
    assert results['demo'].logged_in is True


def test_scheduler_skips_refresh_on_unknown_login(httpx_mock: Any, tmp_path: Path) -> None:
    """网络异常时不触发刷新（避免白白消耗 refresh_token）。"""
    import logging

    from jwt_alive.scheduler import KeepAliveScheduler

    httpx_mock.add_exception(httpx.ConnectError('boom'), url='https://example.com/api/user/profile')
    site = _site(_token(3600))
    site['retry_attempts'] = 1
    service = _service(tmp_path, site)
    scheduler = KeepAliveScheduler(service, interval_seconds=1, logger=logging.getLogger('t'))
    try:
        scheduler._scan()
        runtime = service.get_runtime('demo')
        assert runtime is not None
        assert runtime.state.last_refresh is None  # 没刷新
        assert runtime.state.last_error  # 但记录了原因
    finally:
        scheduler.shutdown()
        service.close()
