"""把刷新后的 token 回写配置文件（保留 YAML 注释与排版）。"""

from __future__ import annotations

import errno
import logging
from pathlib import Path
import shutil

from ruamel.yaml import YAML


class WriteBackError(Exception):
    """回写配置失败。"""


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def write_back_tokens(
    config_path: str | Path,
    site_name: str,
    access_token: str,
    refresh_token: str | None = None,
) -> bool:
    """把新 token 写回配置文件的对应站点。

    返回是否实际写入。站点不存在或字段缺失时返回 False，不抛异常。
    """
    path = Path(config_path).expanduser()
    if not path.is_file():
        return False

    yaml = _yaml()
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = yaml.load(fh)
    except OSError:
        raise
    except Exception as exc:  # ruamel 解析错误
        raise WriteBackError(f'配置文件解析失败: {exc}') from exc

    if not isinstance(data, dict):
        return False
    sites = data.get('sites')
    if not isinstance(sites, list):
        return False

    for entry in sites:
        if not isinstance(entry, dict):
            continue
        if entry.get('name') != site_name:
            continue
        token = entry.get('token')
        if not isinstance(token, dict):
            token = {}
            entry['token'] = token
        token['access_token'] = access_token
        if refresh_token:
            token['refresh_token'] = refresh_token
        break
    else:
        return False

    tmp_path = path.with_suffix(path.suffix + '.tmp')
    try:
        with tmp_path.open('w', encoding='utf-8') as fh:
            yaml.dump(data, fh)
        try:
            tmp_path.replace(path)
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES):
                raise
            # 绑定挂载的文件不能被 rename 覆盖（EBUSY），退化为原地写入
            shutil.copyfile(tmp_path, path)
            tmp_path.unlink(missing_ok=True)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise WriteBackError(f'配置文件写入失败: {exc}') from exc
    return True


def write_back_tokens_safe(
    config_path: str | Path,
    site_name: str,
    access_token: str,
    refresh_token: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """回写配置的容错版本，失败只记日志。"""
    log = logger or logging.getLogger(__name__)
    try:
        written = write_back_tokens(config_path, site_name, access_token, refresh_token)
    except (WriteBackError, OSError) as exc:
        log.warning('回写配置失败（站点 %s）: %s', site_name, exc)
        return False
    if written:
        log.info('已回写配置：站点 %s 的 token', site_name)
    return written
