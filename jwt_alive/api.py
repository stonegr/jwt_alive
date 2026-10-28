"""对外 HTTP API：凭配置里的账号密码获取站点列表 / token / 触发刷新。

认证用 HTTP Basic：用户名查 auth 表，密码常量时间比对。
API 与保活守护进程同进程运行（uvicorn 后台线程），共享同一个 service 实例。
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .models import AppConfig
from .service import JwtAliveService
from .utils import now_utc


log = logging.getLogger(__name__)

REALM = 'jwt-alive'
security = HTTPBasic(auto_error=False)


def authenticate(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    """HTTP Basic 认证：用户名查表 + 密码常量时间比对。

    账号表放在 app.state.config 上，这里是模块级函数，便于 FastAPI 解析依赖。
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='需要认证（HTTP Basic）',
            headers={'WWW-Authenticate': f'Basic realm="{REALM}"'},
        )
    config: AppConfig = request.app.state.config
    if not config.verify_password(credentials.username, credentials.password):
        log.warning('API 认证失败：用户 %s', credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='用户名或密码错误',
            headers={'WWW-Authenticate': f'Basic realm="{REALM}"'},
        )
    return credentials.username


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


class SiteOut(BaseModel):
    """站点概览（不含 token）。"""

    name: str
    logged_in: bool | None = None
    expires_at: datetime | None = None
    remaining_seconds: float | None = None
    expiry_field: str | None = None
    last_check: datetime | None = None
    last_refresh: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class SiteDetailOut(SiteOut):
    """站点详情（含 token）。"""

    access_token: str
    refresh_token: str


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
    """当前认证用户。"""

    username: str


# --------------------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------------------


def create_app(service: JwtAliveService, config: AppConfig) -> FastAPI:
    """创建 FastAPI 应用。"""
    app = FastAPI(
        title='jwt-alive API',
        description='JWT 保活服务的对外接口：查询站点列表、获取 token、触发刷新。',
        version='0.1.0',
    )
    app.state.config = config
    app.state.service = service

    @app.get('/api/me', response_model=MeOut, summary='当前认证用户')
    def me(username: CurrentUser) -> MeOut:
        return MeOut(username=username)

    @app.get('/api/health', response_model=HealthOut, summary='健康状态')
    def health(username: CurrentUser) -> HealthOut:
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

    @app.get('/api/sites', response_model=list[SiteOut], summary='站点列表')
    def list_sites(username: CurrentUser) -> list[SiteOut]:
        return [_site_out(s) for s in service.status_all()]

    @app.get('/api/sites/{name}', response_model=SiteDetailOut, summary='站点详情（含 token）')
    def get_site(name: str, username: CurrentUser) -> SiteDetailOut:
        runtime = service.get_runtime(name)
        if runtime is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'未知站点：{name}')
        out = _site_out(runtime.status())
        return SiteDetailOut(
            **out.model_dump(),
            access_token=runtime.state.access_token,
            refresh_token=runtime.state.refresh_token,
        )

    @app.get('/api/sites/{name}/token', response_model=TokenOut, summary='获取站点 token')
    def get_token(name: str, username: CurrentUser) -> TokenOut:
        runtime = service.get_runtime(name)
        if runtime is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f'未知站点：{name}')
        return TokenOut(
            access_token=runtime.state.access_token,
            refresh_token=runtime.state.refresh_token,
            expires_at=runtime.state.expires_at,
            expiry_field=runtime.state.expiry_field,
        )

    @app.post('/api/sites/{name}/refresh', response_model=RefreshOut, summary='触发站点刷新')
    def refresh_site(name: str, username: CurrentUser) -> RefreshOut:
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

    @app.post('/api/refresh-all', response_model=list[RefreshOut], summary='刷新全部站点')
    def refresh_all(username: CurrentUser) -> list[RefreshOut]:
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

    return app


def _site_out(s: Any) -> SiteOut:
    return SiteOut(
        name=s.name,
        logged_in=s.logged_in,
        expires_at=s.expires_at,
        remaining_seconds=s.remaining_seconds,
        expiry_field=s.expiry_field,
        last_check=s.last_check,
        last_refresh=s.last_refresh,
        last_error=s.last_error,
        consecutive_failures=s.consecutive_failures,
    )
