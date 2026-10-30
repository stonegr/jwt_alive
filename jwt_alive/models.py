"""数据模型：配置模型（pydantic）与运行期模型（dataclass）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hmac
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------------------


class ExpiryFormat(StrEnum):
    """JWT 过期时间字段的取值格式。"""

    AUTO = 'auto'
    """自动推断：epoch 秒 / epoch 毫秒 / ISO8601 字符串。"""

    EPOCH_S = 'epoch_s'
    """Unix 时间戳（秒）。"""

    EPOCH_MS = 'epoch_ms'
    """Unix 时间戳（毫秒）。"""

    ISO8601 = 'iso8601'
    """ISO8601 字符串，例如 2026-01-01T00:00:00Z。"""

    DURATION_S = 'duration_s'
    """相对时长（秒），例如 expires_in=3600，配合 iat 计算绝对到期时间。"""


class TokenSource(StrEnum):
    """刷新接口下发新 token 的位置。"""

    BODY = 'body'
    SET_COOKIE = 'cookie'


class RefreshMode(StrEnum):
    """刷新触发方式。"""

    ACCESS = 'access'
    """按 access token 临期刷新（默认，= 现状）。"""

    REFRESH = 'refresh'
    """按 refresh token 临期刷新（低频保活，access token 靠按需刷新）。"""

    HYBRID = 'hybrid'
    """任一 token 临期就刷新。"""


class SuccessType(StrEnum):
    """登录态校验的成功判定方式。"""

    STATUS = 'status'
    """HTTP 状态码判定，默认 200。"""

    JSON = 'json'
    """JSON 字段判定，例如 {"type": "json", "path": "code", "equals": 0}。"""

    COOKIE = 'cookie'
    """响应中是否存在指定 Cookie。"""


# --------------------------------------------------------------------------------------
# 配置模型
# --------------------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra='forbid')


class JwtConfig(_Base):
    """JWT 过期时间的解构规则。"""

    expiry_field: str = 'exp'
    """解构后取哪个字段作为过期时间，支持点路径（如 data.expireTime）。"""

    expiry_format: ExpiryFormat = ExpiryFormat.AUTO
    """过期时间字段的格式，auto 表示自动推断。"""

    leeway_seconds: int = 30
    """时钟偏移容忍（秒），用于判断是否已过期。"""

    iat_field: str = 'iat'
    """相对时长格式下用于计算起点的字段（iat）。"""

    refresh_expiry_field: str | None = None
    """refresh token 的过期字段名，None 表示沿用 expiry_field。"""


class TokenConfig(_Base):
    """站点初始 token。"""

    access_token: str = ''
    refresh_token: str = ''
    variables: dict[str, str] = Field(default_factory=dict)
    """额外模板变量，可在 header/body 中用 {name} 引用。"""


class RefreshConfig(_Base):
    """刷新接口定义。"""

    url: str
    method: str = 'POST'
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    """请求体模板，支持 {access_token} / {refresh_token} 占位。"""

    token_source: TokenSource = TokenSource.BODY
    access_token_path: str | None = 'access_token'
    """从响应体取新 access_token 的点路径。"""

    refresh_token_path: str | None = 'refresh_token'
    """从响应体取新 refresh_token 的点路径，None 表示不更新。"""

    cookie_name: str | None = None
    """从 Set-Cookie 取 token 时的 Cookie 名。"""

    refresh_cookie_name: str | None = None
    """从 Set-Cookie 取新 refresh_token 时的 Cookie 名。"""

    cookie_domain: str | None = None
    cookie_path: str | None = None
    follow_redirects: bool = True
    timeout_seconds: float | None = None

    @field_validator('method')
    @classmethod
    def _upper_method(cls, v: str) -> str:
        return v.upper()


class SuccessCriteria(_Base):
    """登录态校验的成功判定。"""

    type: SuccessType = SuccessType.STATUS
    expected: int = 200
    """status 类型的期望状态码。"""

    path: str | None = None
    """json 类型的点路径。"""

    equals: Any = None
    """json 类型的期望值。"""

    name: str | None = None
    """cookie 类型的 Cookie 名。"""


class LoginCheckConfig(_Base):
    """登录态校验接口定义。"""

    url: str
    method: str = 'GET'
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    success: SuccessCriteria = Field(default_factory=SuccessCriteria)
    timeout_seconds: float | None = None

    @field_validator('method')
    @classmethod
    def _upper_method(cls, v: str) -> str:
        return v.upper()


class ScheduleConfig(_Base):
    """站点调度参数。"""

    interval_seconds: int | None = None
    """扫描间隔，None 表示使用全局默认值。"""

    refresh_before_seconds: int | None = None
    """access token 剩余寿命小于该值时触发刷新，None 表示使用全局默认值。"""

    refresh_token_before_seconds: int | None = None
    """refresh token 剩余寿命小于该值时触发刷新（mode=refresh/hybrid 生效），None 表示用默认值。"""

    mode: RefreshMode = RefreshMode.ACCESS
    """刷新触发方式：access（默认）/ refresh / hybrid。"""

    cron: str | None = None
    """可选的额外 cron 表达式，到点强制刷新。"""


class PersistConfig(_Base):
    """持久化策略。"""

    write_back_to_config: bool = True
    """刷新成功后是否把新 token 回写配置文件。"""


class SiteConfig(_Base):
    """单个站点配置。"""

    name: str
    enabled: bool = True
    adapter: str = 'jwt_alive.adapters.http.HttpSiteAdapter'
    """适配器路径，形如 module.path:ClassName，用于覆写站点特有逻辑。"""

    token: TokenConfig = Field(default_factory=TokenConfig)
    jwt: JwtConfig = Field(default_factory=JwtConfig)
    refresh: RefreshConfig
    login_check: LoginCheckConfig | None = None
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    persist: PersistConfig = Field(default_factory=PersistConfig)

    headers: dict[str, str] = Field(default_factory=dict)
    """站点级默认请求头，会与 refresh/login_check 的 headers 合并。"""

    cookies: dict[str, str] = Field(default_factory=dict)
    """站点级默认 Cookie。"""

    timeout_seconds: float = 15.0
    verify_tls: bool = True
    user_agent: str | None = None
    trust_env: bool = True
    """是否读取系统代理等环境变量（HTTP_PROXY / ALL_PROXY 等）。"""

    proxy: str | None = None
    """显式代理地址，如 http://127.0.0.1:1080。"""

    prefer_ipv4: bool = False
    """强制走 IPv4（NAS 上 IPv6 半通导致 TLS 握手失败时用）。"""

    retry_attempts: int = 3
    """刷新请求失败时的重试次数。"""

    retry_backoff_seconds: float = 1.0
    """重试退避基数（秒），实际等待为 backoff * 第几次尝试。"""

    @field_validator('name')
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('站点 name 不能为空')
        return v.strip()


class DefaultsConfig(_Base):
    """全局默认参数。"""

    scan_interval_seconds: int = 60
    refresh_before_seconds: int = 300
    timeout_seconds: float = 15.0
    user_agent: str = 'jwt-alive/0.1'
    verify_tls: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)


class ApiConfig(_Base):
    """对外 HTTP API 配置。"""

    enabled: bool = True
    """是否启用 API（未配置 auth 时不会启动）。"""

    host: str = '127.0.0.1'
    """监听地址，容器内一般用 0.0.0.0。"""

    port: int = 8080
    """监听端口。"""

    ssl_certfile: str | None = None
    """HTTPS 证书路径（可选，建议走反向代理）。"""

    ssl_keyfile: str | None = None
    """HTTPS 私钥路径（可选）。"""

    prefix: str = '/api'
    """所有 API 路径的前缀（'' 表示直接挂在根路径）。"""

    @field_validator('port')
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError('api.port 必须在 1-65535 之间')
        return v

    @field_validator('prefix')
    @classmethod
    def _valid_prefix(cls, v: str) -> str:
        p = v.strip()
        if not p or p == '/':
            return ''
        if not p.startswith('/'):
            p = '/' + p
        return p.rstrip('/')


class AppConfig(_Base):
    """完整配置。"""

    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    sites: list[SiteConfig] = Field(default_factory=list)
    auth: dict[str, str] = Field(default_factory=dict)
    """对外 API 的账号表：用户名 -> 密码（明文，与 token 同风险模型）。"""

    api: ApiConfig = Field(default_factory=ApiConfig)
    """对外 API 配置。"""

    source_path: str | None = None
    """配置文件来源路径，写回时使用。"""

    def enabled_sites(self) -> list[SiteConfig]:
        return [s for s in self.sites if s.enabled]

    def get_site(self, name: str) -> SiteConfig | None:
        for site in self.sites:
            if site.name == name:
                return site
        return None

    def verify_token(self, token: str | None) -> str | None:
        """校验请求里的 token：值命中 auth 表中任意一项即通过，返回该项的 key（标识）。

        key 只是标识、不参与校验；所有条目都做一次常量时间比较，
        避免通过耗时/命中位置推断命中的是哪一个。
        """
        if not token:
            return None
        matched: str | None = None
        for name, expected in self.auth.items():
            if hmac.compare_digest(token, expected) and matched is None:
                matched = name
        return matched


# --------------------------------------------------------------------------------------
# 运行期模型
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ExpiryInfo:
    """过期时间解析结果。"""

    expires_at: datetime | None
    field: str | None = None
    raw: Any = None
    format: ExpiryFormat = ExpiryFormat.AUTO
    resolved: bool = False
    """是否成功解析出绝对到期时间。"""


@dataclass(slots=True)
class TokenBundle:
    """刷新后提取到的新 token。"""

    access_token: str | None = None
    refresh_token: str | None = None
    set_cookies: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.access_token or self.refresh_token)


class CheckKind(StrEnum):
    """登录态校验的结论类型。"""

    LOGGED_IN = 'logged_in'
    """已登录。"""

    TOKEN_EXPIRED = 'token_expired'
    """access token 已过期（mode=refresh 下属正常状态，不触发刷新）。"""

    LOGGED_OUT = 'logged_out'
    """服务端明确拒绝（401/403）→ 会话已失效，必须刷新。"""

    UNKNOWN = 'unknown'
    """网络异常/超时，无法判断 → 不刷新，等下一轮扫描。"""


@dataclass(slots=True)
class CheckResult:
    """登录态校验结果。

    logged_in:
        True  = 已登录
        False = 确定未登录（401/403、token 过期等）
        None  = 无法判断（网络异常/超时）→ 不刷新，等下一轮扫描

    kind 用于区分「access 过期」与「服务端拒绝」，两者是否触发刷新取决于 schedule.mode。
    """

    logged_in: bool | None
    reason: str
    detail: str | None = None
    kind: CheckKind = CheckKind.LOGGED_IN


@dataclass(slots=True)
class SiteState:
    """站点运行期状态（落盘内容）。"""

    name: str
    access_token: str = ''
    refresh_token: str = ''
    expires_at: datetime | None = None
    expiry_field: str | None = None
    refresh_expires_at: datetime | None = None
    """refresh token 的到期时间（用于按 refresh token 触发刷新）。"""
    logged_in: bool | None = None
    last_check: datetime | None = None
    last_refresh: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'expiry_field': self.expiry_field,
            'refresh_expires_at': self.refresh_expires_at.isoformat() if self.refresh_expires_at else None,
            'logged_in': self.logged_in,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'last_refresh': self.last_refresh.isoformat() if self.last_refresh else None,
            'last_error': self.last_error,
            'consecutive_failures': self.consecutive_failures,
            'history': self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteState:
        def _dt(key: str) -> datetime | None:
            raw = data.get(key)
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                return None

        return cls(
            name=str(data.get('name', '')),
            access_token=str(data.get('access_token', '') or ''),
            refresh_token=str(data.get('refresh_token', '') or ''),
            expires_at=_dt('expires_at'),
            expiry_field=data.get('expiry_field'),
            refresh_expires_at=_dt('refresh_expires_at'),
            logged_in=data.get('logged_in'),
            last_check=_dt('last_check'),
            last_refresh=_dt('last_refresh'),
            last_error=data.get('last_error'),
            consecutive_failures=int(data.get('consecutive_failures', 0) or 0),
            history=list(data.get('history', []) or []),
        )
