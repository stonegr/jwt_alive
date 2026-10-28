"""适配器动态加载：支持 module:Class 与 module.Class 两种写法。"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
import sys
from typing import Any

from jwt_alive.adapters.base import SiteAdapter
from jwt_alive.models import SiteConfig


class AdapterError(Exception):
    """适配器加载/实例化失败。"""


def ensure_import_path(path: str | Path) -> None:
    """把目录加入 sys.path，便于加载 plugins/ 下的自定义适配器。"""
    resolved = str(Path(path).expanduser().resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def load_adapter_class(spec: str) -> type[SiteAdapter]:
    """按路径加载适配器类。

    spec 形如 ``jwt_alive.adapters.http:HttpSiteAdapter`` 或
    ``plugins.example_site:ExampleSiteAdapter``。
    """
    if not spec or not spec.strip():
        raise AdapterError('adapter 配置为空')

    text = spec.strip()
    module_name, class_name = '', ''
    if ':' in text:
        module_name, _, class_name = text.partition(':')
    elif '.' in text:
        module_name, _, class_name = text.rpartition('.')

    if not module_name or not class_name:
        raise AdapterError(f'无法解析 adapter 路径: {spec!r}（应为 module:Class）')

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AdapterError(f'导入适配器模块失败 {module_name!r}: {exc}') from exc

    try:
        attr: Any = getattr(module, class_name)
    except AttributeError as exc:
        raise AdapterError(f'模块 {module_name!r} 中不存在 {class_name!r}') from exc

    if not (isinstance(attr, type) and issubclass(attr, SiteAdapter)):
        raise AdapterError(f'{spec!r} 不是 SiteAdapter 的子类')
    return attr


def create_adapter(
    spec: str,
    site: SiteConfig,
    client: Any,
    logger: logging.Logger,
) -> SiteAdapter:
    """实例化站点适配器。"""
    adapter_cls = load_adapter_class(spec)
    adapter = adapter_cls(site=site, client=client, logger=logger)
    logger.debug('站点 %s 使用适配器 %s', site.name, adapter_cls.__name__)
    return adapter


__all__ = [
    'AdapterError',
    'create_adapter',
    'ensure_import_path',
    'load_adapter_class',
]
