"""状态持久化：state/state.json（合并写 + 文件锁）。

多个进程可能同时操作同一个状态文件（守护进程 + CLI 命令 + 容器内 exec），
直接整体覆盖会把别人刚刷新的 token 回退成旧值。
这里采用「基线比对 + 合并写 + 排他锁」：
    - 基线：进程启动时读到的磁盘内容，用来判断"这个 token 是不是我改的"
    - 合并：不是我改的 token 一律保留磁盘上的最新值
    - 锁：读盘 → 合并 → 写盘 全程持锁，避免两个进程交错写
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from jwt_alive.models import SiteState


try:  # pragma: no cover - Windows 没有 fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

TOKEN_FIELDS = ('access_token', 'refresh_token', 'expires_at', 'expiry_field', 'refresh_expires_at')
META_FIELDS = ('logged_in', 'last_check', 'last_refresh', 'last_error', 'consecutive_failures', 'history')


def _parse_dt(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class StateStore:
    """站点运行期状态存储。

    状态文件保存最新的 access_token / refresh_token / 到期时间等，
    重启后优先使用状态文件里的 token（比配置文件新）。
    """

    def __init__(self, path: str | Path, logger: logging.Logger | None = None) -> None:
        self.path = Path(path).expanduser()
        self.log = logger or logging.getLogger(__name__)
        self._states: dict[str, SiteState] = {}
        self._baseline: dict[str, dict[str, Any]] = {}
        self._baseline_scan: datetime | None = None
        self.last_scan: datetime | None = None
        self.load()

    # -- 读写 --------------------------------------------------------------------

    def load(self) -> None:
        raw = self._read_disk()
        if raw is None:
            self._states = {}
            self._baseline = {}
            self._baseline_scan = None
            self.last_scan = None
            return
        sites = raw.get('sites', {}) if isinstance(raw, dict) else {}
        self._states = {name: SiteState.from_dict(data) for name, data in sites.items() if isinstance(data, dict)}
        self._baseline = {name: dict(data) for name, data in sites.items() if isinstance(data, dict)}
        self.last_scan = _parse_dt(raw.get('last_scan')) if isinstance(raw, dict) else None
        self._baseline_scan = self.last_scan
        self.log.debug('已加载 %d 个站点的状态', len(self._states))

    def _read_disk(self) -> dict[str, Any] | None:
        """读取磁盘上的状态文件（不存在或损坏返回 None）。"""
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning('状态文件读取失败，已忽略: %s', exc)
            return None
        return raw if isinstance(raw, dict) else None

    def save(self) -> None:
        """把状态写回磁盘（合并写，不会回退别人刷新的 token）。"""
        with self._lock():
            disk = self._read_disk() or {}
            payload = self._merge(disk)
            self._write(payload)
            # 落盘内容成为新的基线
            self._baseline = {name: dict(data) for name, data in payload['sites'].items()}

    def _merge(self, disk: dict[str, Any]) -> dict[str, Any]:
        """把内存状态与磁盘状态合并成最终要写的内容。"""
        disk_sites = disk.get('sites') if isinstance(disk.get('sites'), dict) else {}
        merged_sites: dict[str, Any] = {}

        for name, state in self._states.items():
            disk_state = disk_sites.get(name) if isinstance(disk_sites.get(name), dict) else {}
            merged_sites[name] = self._merge_site(state, disk_state)

        # 磁盘上存在但内存里没有的站点（例如别的进程新增的）原样保留
        for name, data in disk_sites.items():
            if name not in merged_sites and isinstance(data, dict):
                merged_sites[name] = dict(data)

        last_scan = self.last_scan
        if last_scan is None or last_scan == self._baseline_scan:
            # 本进程没有更新过扫描时间 → 保留磁盘上的
            disk_scan = _parse_dt(disk.get('last_scan'))
            if disk_scan is not None:
                last_scan = disk_scan

        return {
            'version': 1,
            'last_scan': last_scan.isoformat() if last_scan else None,
            'sites': merged_sites,
        }

    def _merge_site(self, state: SiteState, disk_state: dict[str, Any]) -> dict[str, Any]:
        """合并单个站点：token 组看基线，元数据看时间新旧。"""
        base = self._baseline.get(state.name, {})
        disk_token = str(disk_state.get('access_token') or '')
        mem_token = state.access_token
        base_token = str(base.get('access_token') or '')
        mem_dict = state.to_dict()

        if mem_token != base_token:
            # 本进程确实改过这个 token（刷新成功）→ 用内存的
            token_source = mem_dict
        elif disk_token:
            # 本进程没动过，磁盘上可能有更新的 → 用磁盘的
            token_source = disk_state
        else:
            # 磁盘上没有 token（首次运行，用配置兜底）→ 用内存的
            token_source = mem_dict

        merged: dict[str, Any] = {key: token_source.get(key) for key in TOKEN_FIELDS}

        # 元数据：谁的 last_check 更新用谁
        mem_check = state.last_check
        disk_check = _parse_dt(disk_state.get('last_check'))
        if disk_check is not None and (mem_check is None or disk_check > mem_check):
            for key in META_FIELDS:
                merged[key] = disk_state.get(key)
        else:
            for key in META_FIELDS:
                merged[key] = mem_dict.get(key)

        merged['name'] = state.name
        return merged

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix='.state-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            # mkstemp 默认 0600，容器里换 uid 跑健康检查会读不到，这里放开读权限
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """跨进程排他锁（锁独立文件，避免 os.replace 换 inode 导致锁失效）。"""
        if fcntl is None:  # pragma: no cover - Windows
            yield
            return
        lock_path = self.path.with_name(self.path.name + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+') as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- 站点状态 ----------------------------------------------------------------

    def get(self, name: str) -> SiteState | None:
        return self._states.get(name)

    def all(self) -> dict[str, SiteState]:
        return dict(self._states)

    def upsert(self, state: SiteState) -> None:
        self._states[state.name] = state

    def ensure(self, name: str, access_token: str = '', refresh_token: str = '') -> SiteState:
        """返回该站点的状态，不存在则用给定初值创建。"""
        state = self._states.get(name)
        if state is None:
            state = SiteState(name=name, access_token=access_token, refresh_token=refresh_token)
            self._states[name] = state
            self._baseline.setdefault(name, {})['access_token'] = access_token
            self._baseline[name]['refresh_token'] = refresh_token
            return state
        if access_token and not state.access_token:
            state.access_token = access_token
            # 用配置兜底填充的 token 不算"本进程改过"，避免覆盖磁盘上的新值
            self._baseline.setdefault(name, {})['access_token'] = access_token
        if refresh_token and not state.refresh_token:
            state.refresh_token = refresh_token
            self._baseline.setdefault(name, {})['refresh_token'] = refresh_token
        return state
