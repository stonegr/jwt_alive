"""状态持久化：state/state.json（原子写）。"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from jwt_alive.models import SiteState


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
        self.last_scan: datetime | None = None
        self.load()

    # -- 读写 --------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            self._states = {}
            self.last_scan = None
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning('状态文件读取失败，已忽略: %s', exc)
            self._states = {}
            self.last_scan = None
            return
        sites = raw.get('sites', {}) if isinstance(raw, dict) else {}
        self._states = {name: SiteState.from_dict(data) for name, data in sites.items() if isinstance(data, dict)}
        self.last_scan = _parse_dt(raw.get('last_scan')) if isinstance(raw, dict) else None
        self.log.debug('已加载 %d 个站点的状态', len(self._states))

    def save(self) -> None:
        payload: dict[str, Any] = {
            'version': 1,
            'last_scan': self.last_scan.isoformat() if self.last_scan else None,
            'sites': {name: state.to_dict() for name, state in self._states.items()},
        }
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
        else:
            if access_token and not state.access_token:
                state.access_token = access_token
            if refresh_token and not state.refresh_token:
                state.refresh_token = refresh_token
        return state
