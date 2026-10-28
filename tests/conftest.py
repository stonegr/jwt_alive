"""测试公共夹具。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest


TEST_SECRET = 'test-secret-key-that-is-long-enough-for-hs256' * 2


def make_token(payload: dict[str, Any] | None = None, expires_in: int = 3600) -> str:
    """生成一个测试用 JWT。"""
    now = datetime.now(UTC)
    data: dict[str, Any] = {'sub': 'tester', 'iat': int(now.timestamp())}
    if expires_in is not None:
        data['exp'] = int((now + timedelta(seconds=expires_in)).timestamp())
    if payload:
        data.update(payload)
    return jwt.encode(data, TEST_SECRET, algorithm='HS256')


def write_config(path: Path, data: dict[str, Any]) -> Path:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return path


@pytest.fixture
def token_factory() -> Any:
    return make_token
