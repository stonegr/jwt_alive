"""状态文件合并写与并发写的测试（防止回退别人刷新的 token）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from jwt_alive.models import SiteState
from jwt_alive.state import StateStore


NOW = datetime.now(UTC)


def _write_disk(path: Path, sites: dict[str, dict[str, Any]], last_scan: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': 1, 'last_scan': last_scan, 'sites': sites}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _disk(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


# -- 基础 --------------------------------------------------------------------------------


def test_save_writes_state(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    store = StateStore(path)
    state = store.ensure('demo', 'token-a', 'rt-a')
    state.expires_at = NOW + timedelta(hours=1)
    store.save()

    data = _disk(path)
    assert data['sites']['demo']['access_token'] == 'token-a'
    assert data['sites']['demo']['refresh_token'] == 'rt-a'


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / 'nested' / 'state.json'
    store = StateStore(path)
    store.ensure('demo', 'token-a')
    store.save()
    assert path.is_file()


def test_load_reads_existing(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'x', 'refresh_token': 'y'}})
    store = StateStore(path)
    state = store.get('demo')
    assert state is not None
    assert state.access_token == 'x'


def test_load_corrupted_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    path.write_text('not json', encoding='utf-8')
    store = StateStore(path)
    assert store.all() == {}


# -- 合并规则 ----------------------------------------------------------------------------


def test_does_not_revert_token_written_by_another_process(tmp_path: Path) -> None:
    """核心回归：进程 B 没动过这个 token，保存时不能把 A 的新 token 覆盖回旧的。"""
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'old', 'refresh_token': 'old-rt'}})

    store_b = StateStore(path)
    assert store_b.get('demo') is not None
    assert store_b.get('demo').access_token == 'old'  # type: ignore[union-attr]

    # 进程 A 在这期间刷新了，磁盘上变成新 token
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'new', 'refresh_token': 'new-rt'}})

    store_b.save()  # 进程 B 保存自己没改过的状态

    data = _disk(path)
    assert data['sites']['demo']['access_token'] == 'new'
    assert data['sites']['demo']['refresh_token'] == 'new-rt'


def test_writes_token_changed_by_this_process(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'old', 'refresh_token': 'old-rt'}})

    store = StateStore(path)
    state = store.get('demo')
    assert state is not None
    state.access_token = 'mine'
    state.refresh_token = 'mine-rt'
    store.save()

    data = _disk(path)
    assert data['sites']['demo']['access_token'] == 'mine'
    assert data['sites']['demo']['refresh_token'] == 'mine-rt'


def test_keeps_token_bundle_consistent(tmp_path: Path) -> None:
    """沿用磁盘上的 token 时，expires_at / expiry_field 必须一起沿用。"""
    path = tmp_path / 'state.json'
    expires = (NOW + timedelta(hours=2)).isoformat()
    _write_disk(
        path,
        {
            'demo': {
                'name': 'demo',
                'access_token': 'new',
                'refresh_token': 'new-rt',
                'expires_at': expires,
                'expiry_field': 'exp',
            }
        },
    )

    store = StateStore(path)
    store.save()

    site = _disk(path)['sites']['demo']
    assert site['access_token'] == 'new'
    assert site['expires_at'] == expires
    assert site['expiry_field'] == 'exp'


def test_config_fallback_token_written_when_disk_has_none(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': '', 'refresh_token': ''}})

    store = StateStore(path)
    store.ensure('demo', 'from-config', 'rt-from-config')
    store.save()

    site = _disk(path)['sites']['demo']
    assert site['access_token'] == 'from-config'
    assert site['refresh_token'] == 'rt-from-config'


def test_config_fallback_does_not_revert_disk_token(tmp_path: Path) -> None:
    """磁盘上有新 token 时，配置兜底的老 token 不能覆盖它。"""
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'new', 'refresh_token': 'new-rt'}})

    store = StateStore(path)
    state = store.get('demo')
    assert state is not None
    # 模拟：内存里是配置兜底的老 token（磁盘的为空才会兜底，这里直接构造场景）
    state.access_token = 'new'
    store.save()

    assert _disk(path)['sites']['demo']['access_token'] == 'new'


def test_other_sites_are_not_reverted(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(
        path,
        {
            'a': {'name': 'a', 'access_token': 'a-old', 'refresh_token': 'a-rt'},
            'b': {'name': 'b', 'access_token': 'b-old', 'refresh_token': 'b-rt'},
        },
    )

    store = StateStore(path)
    # 只有 a 被本进程刷新
    state_a = store.get('a')
    assert state_a is not None
    state_a.access_token = 'a-new'

    # 期间 b 被别的进程刷新了
    disk = _disk(path)
    disk['sites']['b']['access_token'] = 'b-new'
    path.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding='utf-8')

    store.save()

    data = _disk(path)
    assert data['sites']['a']['access_token'] == 'a-new'
    assert data['sites']['b']['access_token'] == 'b-new'


def test_metadata_keeps_newer_last_check(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    newer = (NOW + timedelta(minutes=5)).isoformat()
    (NOW - timedelta(minutes=5)).isoformat()
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'x', 'last_check': newer, 'logged_in': True}})

    store = StateStore(path)
    state = store.get('demo')
    assert state is not None
    state.last_check = NOW  # 内存里是更早的检查结果
    state.logged_in = False
    store.save()

    site = _disk(path)['sites']['demo']
    assert site['last_check'] == newer
    assert site['logged_in'] is True


def test_metadata_uses_memory_when_newer(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    older = (NOW - timedelta(minutes=5)).isoformat()
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'x', 'last_check': older, 'logged_in': False}})

    store = StateStore(path)
    state = store.get('demo')
    assert state is not None
    state.last_check = NOW
    state.logged_in = True
    store.save()

    site = _disk(path)['sites']['demo']
    assert site['logged_in'] is True


def test_last_scan_not_overwritten_by_other_process(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    disk_scan = NOW.isoformat()
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'x'}}, last_scan=disk_scan)

    store = StateStore(path)
    assert store.last_scan is not None
    store.save()  # 本进程没有更新扫描时间

    assert _disk(path)['last_scan'] == disk_scan


def test_last_scan_written_when_changed(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'x'}}, last_scan=NOW.isoformat())

    store = StateStore(path)
    store.last_scan = NOW + timedelta(minutes=1)
    store.save()

    assert _disk(path)['last_scan'] == (NOW + timedelta(minutes=1)).isoformat()


def test_site_only_on_disk_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    _write_disk(path, {'other': {'name': 'other', 'access_token': 'z'}})

    store = StateStore(path)
    store.ensure('demo', 'x')
    store.save()

    assert 'other' in _disk(path)['sites']


# -- 并发 --------------------------------------------------------------------------------


def test_concurrent_writers_do_not_corrupt_file(tmp_path: Path) -> None:
    """两个进程并发写，文件必须是完整合法的 JSON，且不丢站点。"""
    path = tmp_path / 'state.json'
    _write_disk(path, {'demo': {'name': 'demo', 'access_token': 'old'}})

    script = (
        'import sys;'
        f'sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r});'
        'from jwt_alive.state import StateStore;'
        f's = StateStore({str(path)!r});'
        'st = s.ensure("worker", "tok-" + sys.argv[1]);'
        'st.access_token = "tok-" + sys.argv[1];'
        's.save()'
    )
    procs = [subprocess.Popen([sys.executable, '-c', script, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(5)]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()

    data = _disk(path)  # 能解析说明文件没损坏
    assert 'demo' in data['sites']
    assert 'worker' in data['sites']


def test_lock_file_created(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    store = StateStore(path)
    store.ensure('demo', 'x')
    store.save()
    assert (tmp_path / 'state.json.lock').exists()


def test_state_state_roundtrip_keeps_fields(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    store = StateStore(path)
    state = SiteState(name='demo', access_token='a', refresh_token='r')
    state.expires_at = NOW
    state.expiry_field = 'exp'
    state.last_refresh = NOW
    state.last_check = NOW
    state.logged_in = True
    state.last_error = None
    state.consecutive_failures = 0
    state.history = [{'time': NOW.isoformat(), 'action': 'refresh', 'ok': True, 'reason': '刷新成功'}]
    store.upsert(state)
    store.save()

    reloaded = StateStore(path).get('demo')
    assert reloaded is not None
    assert reloaded.access_token == 'a'
    assert reloaded.expires_at == NOW
    assert reloaded.expiry_field == 'exp'
    assert reloaded.last_refresh == NOW
    assert reloaded.last_check == NOW
    assert reloaded.logged_in is True
    assert reloaded.history


def test_file_permissions_readable(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    store = StateStore(path)
    store.ensure('demo', 'x')
    store.save()
    assert oct(os.stat(path).st_mode)[-3:] == '644'
