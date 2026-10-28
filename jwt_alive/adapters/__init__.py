"""站点适配器体系。"""

from jwt_alive.adapters.base import RequestSpec, SiteAdapter, render_template
from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.adapters.loader import AdapterError, create_adapter, ensure_import_path, load_adapter_class


__all__ = [
    'AdapterError',
    'HttpSiteAdapter',
    'RequestSpec',
    'SiteAdapter',
    'create_adapter',
    'ensure_import_path',
    'load_adapter_class',
    'render_template',
]
