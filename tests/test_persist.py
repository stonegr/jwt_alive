"""配置回写测试（含绑定挂载文件的退化写入路径）。"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from jwt_alive.persist import write_back_tokens, write_back_tokens_safe


def _config(path: Path) -> Path:
    path.write_text(
        """
sites:
  - name: demo
    token:
      access_token: "old"
      refresh_token: "old-rt"
    refresh:
      url: "https://example.com/refresh"
""",
        encoding='utf-8',
    )
    return path


def test_write_back_updates_tokens(tmp_path: Path) -> None:
    path = _config(tmp_path / 'sites.yaml')
    assert write_back_tokens(path, 'demo', 'new', 'new-rt') is True
    text = path.read_text(encoding='utf-8')
    assert 'new' in text
    assert 'old-rt' not in text


def test_write_back_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / 'sites.yaml'
    path.write_text(
        '# 顶部注释\nsites:\n  - name: demo  # 行内注释\n    token:\n      access_token: "old"\n',
        encoding='utf-8',
    )
    write_back_tokens(path, 'demo', 'new')
    text = path.read_text(encoding='utf-8')
    assert '# 顶部注释' in text
    assert '# 行内注释' in text


def test_write_back_unknown_site_returns_false(tmp_path: Path) -> None:
    path = _config(tmp_path / 'sites.yaml')
    assert write_back_tokens(path, 'nope', 'new') is False


def test_write_back_missing_file_returns_false(tmp_path: Path) -> None:
    assert write_back_tokens(tmp_path / 'nope.yaml', 'demo', 'new') is False


def test_write_back_falls_back_to_inplace_when_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """绑定挂载的文件无法 rename 覆盖，应退化为原地写入。"""
    path = _config(tmp_path / 'sites.yaml')
    original_replace = os.replace

    def _fake_replace(src: Any, dst: Any) -> None:
        raise OSError(errno.EBUSY, 'Device or resource busy')

    monkeypatch.setattr(os, 'replace', _fake_replace)
    assert write_back_tokens(path, 'demo', 'new', 'new-rt') is True
    monkeypatch.setattr(os, 'replace', original_replace)

    text = path.read_text(encoding='utf-8')
    assert 'new' in text
    assert not list(tmp_path.glob('*.tmp'))


def test_write_back_safe_logs_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _config(tmp_path / 'sites.yaml')

    def _boom(src: Any, dst: Any) -> None:
        raise OSError(errno.EBUSY, 'Device or resource busy')

    monkeypatch.setattr(os, 'replace', _boom)
    assert write_back_tokens_safe(path, 'demo', 'new') is True  # 退化路径仍成功


def test_write_back_ignores_non_file_target(tmp_path: Path) -> None:
    # 目标是目录 → 直接跳过，不抛异常
    target = tmp_path / 'sites.yaml'
    target.mkdir()
    assert write_back_tokens(target, 'demo', 'new') is False
