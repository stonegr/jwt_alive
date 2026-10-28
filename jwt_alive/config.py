"""配置加载：YAML 解析、环境变量插值、默认值合并、模型校验。"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError
import yaml

from jwt_alive.models import AppConfig, DefaultsConfig, SiteConfig


_ENV_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


class ConfigError(Exception):
    """配置相关错误。"""


def expand_env(text: str, environ: dict[str, str] | None = None) -> str:
    """把字符串中的 ${VAR} / ${VAR:-default} 替换为环境变量值。

    未定义且未给默认值时报错，避免静默把 "${TOKEN}" 当成 token 用。
    """
    env = os.environ if environ is None else environ

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConfigError(f'环境变量 {name} 未定义（配置中引用了 ${{{name}}}）')

    return _ENV_PATTERN.sub(_replace, text)


def interpolate_env(data: Any, environ: dict[str, str] | None = None) -> Any:
    """递归地对配置中的所有字符串做环境变量插值（dict 的 key 也会插值）。"""
    if isinstance(data, str):
        return expand_env(data, environ)
    if isinstance(data, dict):
        return {expand_env(key, environ) if isinstance(key, str) else key: interpolate_env(value, environ) for key, value in data.items()}
    if isinstance(data, list):
        return [interpolate_env(item, environ) for item in data]
    return data


def _merge_mapping(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update({k: v for k, v in override.items() if v})
    return merged


def apply_defaults(site_raw: dict[str, Any], defaults: DefaultsConfig) -> dict[str, Any]:
    """把全局默认参数合并进单个站点配置（站点显式配置优先）。"""
    site = dict(site_raw)
    schedule = dict(site.get('schedule') or {})

    if not schedule.get('interval_seconds'):
        schedule['interval_seconds'] = defaults.scan_interval_seconds
    if not schedule.get('refresh_before_seconds'):
        schedule['refresh_before_seconds'] = defaults.refresh_before_seconds
    site['schedule'] = schedule

    if not site.get('timeout_seconds'):
        site['timeout_seconds'] = defaults.timeout_seconds
    if site.get('user_agent') is None:
        site['user_agent'] = defaults.user_agent
    if site.get('verify_tls') is None:
        site['verify_tls'] = defaults.verify_tls

    site['headers'] = _merge_mapping(dict(defaults.headers), dict(site.get('headers') or {}))
    site['cookies'] = _merge_mapping(dict(defaults.cookies), dict(site.get('cookies') or {}))

    for key in ('refresh', 'login_check'):
        section = site.get(key)
        if not section:
            continue
        section = dict(section)
        section['headers'] = _merge_mapping(dict(defaults.headers), dict(section.get('headers') or {}))
        section['cookies'] = _merge_mapping(dict(defaults.cookies), dict(section.get('cookies') or {}))
        if section.get('timeout_seconds') is None:
            section['timeout_seconds'] = defaults.timeout_seconds
        site[key] = section

    return site


def parse_config(data: dict[str, Any], source_path: Path | None = None) -> AppConfig:
    """把已解析的 YAML 字典转成 AppConfig（含默认值合并与校验）。"""
    defaults_raw = data.get('defaults') or {}
    try:
        defaults = DefaultsConfig.model_validate(defaults_raw)
    except ValidationError as exc:
        raise ConfigError(f'defaults 配置无效: {exc}') from exc

    sites_raw = data.get('sites') or []
    if not isinstance(sites_raw, list):
        raise ConfigError('sites 必须是列表')

    sites: list[SiteConfig] = []
    seen: set[str] = set()
    for index, site_raw in enumerate(sites_raw):
        if not isinstance(site_raw, dict):
            raise ConfigError(f'sites[{index}] 必须是字典')
        if 'name' not in site_raw:
            raise ConfigError(f'sites[{index}] 缺少 name 字段')
        merged = apply_defaults(site_raw, defaults)
        try:
            site = SiteConfig.model_validate(merged)
        except ValidationError as exc:
            raise ConfigError(f'站点 {site_raw.get("name", index)} 配置无效: {exc}') from exc
        if site.name in seen:
            raise ConfigError(f'站点名重复: {site.name}')
        seen.add(site.name)
        sites.append(site)

    # 顶层其余字段（auth / api 等）一并交给模型校验，未知字段会直接报错
    payload: dict[str, Any] = {k: v for k, v in data.items() if k != 'sites'}
    payload['sites'] = [s.model_dump() for s in sites]
    try:
        app = AppConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f'配置无效: {exc}') from exc

    app.sites = sites
    if source_path is not None:
        app.source_path = str(source_path)
    return app


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> AppConfig:
    """读取 YAML 配置文件并返回校验后的 AppConfig。"""
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f'配置文件不存在: {config_path}')
    try:
        raw = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f'配置文件解析失败: {exc}') from exc
    if not isinstance(raw, dict):
        raise ConfigError('配置文件根节点必须是字典')
    return parse_config(interpolate_env(raw, environ), config_path)


def default_config_path() -> Path:
    """默认配置文件位置：当前工作目录下的 config/sites.yaml。"""
    return Path.cwd() / 'config' / 'sites.yaml'
