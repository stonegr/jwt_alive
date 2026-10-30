"""对外 HTTP API：凭配置里的账号密码获取站点列表 / token / 触发刷新。

认证用 HTTP Basic：用户名查 auth 表，密码常量时间比对。
API 与保活守护进程同进程运行（uvicorn 后台线程），共享同一个 service 实例。
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from .models import AppConfig
from .service import JwtAliveService
from .utils import now_utc


log = logging.getLogger(__name__)

REALM = 'jwt-alive'


def extract_token(
    authorization: str | None,
    x_token: str | None,
    x_api_key: str | None,
) -> str | None:
    """从请求头里取 token（按优先级）。

    Authorization  支持 `Bearer <token>` 与不带前缀的裸 token；
    其次 X-Token、X-API-Key。
    """
    if authorization:
        value = authorization.strip()
        if value.lower().startswith('bearer '):
            value = value[7:].strip()
        if value:
            return value
    for candidate in (x_token, x_api_key):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def authenticate(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_token: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """用请求头里的 token 认证：值命中 auth 表中任意一项即通过，返回该项的 key（标识）。

    账号表放在 app.state.config 上，这里是模块级函数，便于 FastAPI 解析依赖。
    """
    token = extract_token(authorization, x_token, x_api_key)
    config: AppConfig = request.app.state.config
    identity = config.verify_token(token)
    if identity is None:
        # 不区分"标识不存在"与"token 不匹配"，避免泄露有效 token 的存在性；
        # 但日志里说明是"没带头"还是"带了没匹配"，且绝不记录 token 本身
        if token is None:
            log.warning('API 认证失败：请求未携带 token（Authorization / X-Token / X-API-Key）')
        else:
            log.warning(
                'API 认证失败：token 未命中 auth 表（长度 %d，auth 表 %d 条）',
                len(token),
                len(config.auth),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='token 无效',
            headers={'WWW-Authenticate': f'Bearer realm="{REALM}"'},
        )
    return identity


CurrentUser = Annotated[str, Depends(authenticate)]


# --------------------------------------------------------------------------------------
# 响应模型
# --------------------------------------------------------------------------------------


class TokenOut(BaseModel):
    """token 信息。"""

    access_token: str
    refresh_token: str
    expires_at: datetime | None = None
    expiry_field: str | None = None
    refreshed: bool = False
    """本次请求是否触发了一次刷新（access token 已过期时按需刷新）。"""


class SiteOut(BaseModel):
    """站点概览（不含 token）。"""

    name: str
    logged_in: bool | None = None
    expires_at: datetime | None = None
    remaining_seconds: float | None = None
    refresh_expires_at: datetime | None = None
    refresh_remaining_seconds: float | None = None
    expiry_field: str | None = None
    last_check: datetime | None = None
    last_refresh: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class SiteDetailOut(SiteOut):
    """站点详情（含 token）。"""

    access_token: str
    refresh_token: str
    refreshed: bool = False


class RefreshOut(BaseModel):
    """刷新结果。"""

    site: str
    ok: bool
    reason: str
    access_token: str | None = None
    expires_at: datetime | None = None
    written_back: bool = False


class HealthOut(BaseModel):
    """健康状态。"""

    status: str
    last_scan: datetime | None = None
    scan_age_seconds: float | None = None
    site_count: int
    logged_in_count: int
    sites: list[SiteOut] = Field(default_factory=list)


class MeOut(BaseModel):
    """当前认证身份（auth 表里命中的 key，仅作标识）。"""

    id: str


# --------------------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------------------


def create_app(
    service: JwtAliveService,
    config: AppConfig,
    prefix: str | None = None,
    docs: bool | None = None,
) -> FastAPI:
    """创建 FastAPI 应用。

    prefix 为 None 时取配置里的 api.prefix；所有路由（含 /docs、/openapi.json）都在该前缀下。
    docs 为 None 时取配置里的 api.docs；False 时不注册文档路由（访问 404，业务接口不受影响）。
    """
    app_prefix = config.api.prefix if prefix is None else prefix
    show_docs = config.api.docs if docs is None else docs

    def _url(path: str) -> str | None:
        if not show_docs:
            return None
        return f'{app_prefix}{path}' if app_prefix else path

    app = FastAPI(
        title='jwt-alive API',
        description='JWT 保活服务的对外接口：查询站点列表、获取 token、触发刷新。',
        version='0.1.0',
        # 文档也放在前缀下，前缀外不留任何端点；关闭文档时这几个路由不注册
        docs_url=_url('/docs'),
        redoc_url=_url('/redoc'),
        openapi_url=_url('/openapi.json'),
        swagger_ui_oauth2_redirect_url=_url('/docs/oauth2-redirect'),
    )
    router = APIRouter(prefix=app_prefix)
    app.state.config = config
    app.state.service = service

    @router.get('/me', response_model=MeOut, summary='当前认证用户')
    def me(client_id: CurrentUser) -> MeOut:
        return MeOut(id=client_id)

    @router.get('/health', response_model=HealthOut, summary='健康状态')
    def health(client_id: CurrentUser) -> HealthOut:
        statuses = service.status_all()
        last_scan = service.store.last_scan
        age = (now_utc() - last_scan).total_seconds() if last_scan else None
        return HealthOut(
            status='ok' if age is None or age < 300 else 'stale',
            last_scan=last_scan,
            scan_age_seconds=age,
            site_count=len(statuses),
            logged_in_count=sum(1 for s in statuses if s.logged_in),
            sites=[_site_out(s) for s in statuses],
        )

    @router.get('/sites', response_model=list[SiteOut], summary='站点列表')
    def list_sites(client_id: CurrentUser) -> list[SiteOut]:
        return [_site_out(s) for s in service.status_all()]

    @router.get('/sites/{name}', response_model=SiteDetailOut, summary='站点详情（含 token）')
    def get_site(name: str, client_id: CurrentUser) -> SiteDetailOut:
        runtime = service.get_runtime(name)
        if runtime is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'未知站点：{name}')
        refreshed = _ensure_fresh(service, name)
        out = _site_out(runtime.status())
        return SiteDetailOut(
            **out.model_dump(),
            access_token=runtime.state.access_token,
            refresh_token=runtime.state.refresh_token,
            refreshed=refreshed,
        )

    @router.get('/sites/{name}/token', response_model=TokenOut, summary='获取站点 token')
    def get_token(name: str, client_id: CurrentUser) -> TokenOut:
        runtime = service.get_runtime(name)
        if runtime is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'未知站点：{name}')
        refreshed = _ensure_fresh(service, name)
        return TokenOut(
            access_token=runtime.state.access_token,
            refresh_token=runtime.state.refresh_token,
            expires_at=runtime.state.expires_at,
            expiry_field=runtime.state.expiry_field,
            refreshed=refreshed,
        )

    @router.post('/sites/{name}/refresh', response_model=RefreshOut, summary='触发站点刷新')
    def refresh_site(name: str, client_id: CurrentUser) -> RefreshOut:
        runtime = service.get_runtime(name)
        if runtime is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'未知站点：{name}')
        outcome = service.refresh_site(name, force=True)
        return RefreshOut(
            site=outcome.site,
            ok=outcome.ok,
            reason=outcome.reason,
            access_token=outcome.new_access_token,
            expires_at=outcome.new_expiry,
            written_back=outcome.written_back,
        )

    @router.post('/refresh-all', response_model=list[RefreshOut], summary='刷新全部站点')
    def refresh_all(client_id: CurrentUser) -> list[RefreshOut]:
        return [
            RefreshOut(
                site=o.site,
                ok=o.ok,
                reason=o.reason,
                access_token=o.new_access_token,
                expires_at=o.new_expiry,
                written_back=o.written_back,
            )
            for o in service.refresh_all(force=True)
        ]

    app.include_router(router)
    return app


def _ensure_fresh(service: JwtAliveService, name: str) -> bool:
    """access token 已过期时按需刷新一次；刷新失败直接报错，不返回过期 token。"""
    outcome = service.ensure_fresh(name)
    if outcome is None:
        return False
    if not outcome.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'刷新失败：{outcome.reason}（可能需要重新登录）',
        )
    return True


def _site_out(s: Any) -> SiteOut:
    return SiteOut(
        name=s.name,
        logged_in=s.logged_in,
        expires_at=s.expires_at,
        remaining_seconds=s.remaining_seconds,
        refresh_expires_at=s.refresh_expires_at,
        refresh_remaining_seconds=s.refresh_remaining_seconds,
        expiry_field=s.expiry_field,
        last_check=s.last_check,
        last_refresh=s.last_refresh,
        last_error=s.last_error,
        consecutive_failures=s.consecutive_failures,
    )
