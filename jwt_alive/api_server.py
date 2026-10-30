"""在守护进程内以后台线程运行 API 服务。"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from .api import create_app
from .models import AppConfig
from .service import JwtAliveService


def port_available(host: str, port: int) -> bool:
    """检查端口是否可绑定（提前发现端口占用，避免 uvicorn 报错刷屏）。"""
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


class ApiServer:
    """API 服务包装：uvicorn 跑在后台线程，与守护进程共享 service 实例。"""

    def __init__(
        self,
        service: JwtAliveService,
        config: AppConfig,
        host: str | None = None,
        port: int | None = None,
        prefix: str | None = None,
        docs: bool | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.service = service
        self.config = config
        self.host = host or config.api.host
        self.port = port or config.api.port
        self.prefix = config.api.prefix if prefix is None else prefix
        self.docs = config.api.docs if docs is None else docs
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """启动 API 服务；失败只记录日志，不影响保活主流程。"""
        if not self.config.auth:
            self.log.info('未配置 auth 账号，API 未启动')
            return False
        if not self.config.api.enabled:
            self.log.info('api.enabled=false，API 未启动')
            return False
        if not port_available(self.host, self.port):
            self.log.error('API 端口 %s:%s 被占用，API 未启动', self.host, self.port)
            return False

        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            self.log.error('未安装 uvicorn，API 未启动: %s', exc)
            return False

        for issue in self.config.auth_issues():
            self.log.warning('auth 配置有问题：%s', issue)

        app = create_app(self.service, self.config, prefix=self.prefix, docs=self.docs)
        kwargs: dict[str, Any] = {
            'host': self.host,
            'port': self.port,
            'log_level': 'warning',
            'access_log': False,
        }
        if self.config.api.ssl_certfile and self.config.api.ssl_keyfile:
            kwargs['ssl_certfile'] = self.config.api.ssl_certfile
            kwargs['ssl_keyfile'] = self.config.api.ssl_keyfile

        self._server = uvicorn.Server(uvicorn.Config(app, **kwargs))
        self._thread = threading.Thread(target=self._server.run, name='jwt-alive-api', daemon=True)
        self._thread.start()
        self.log.info(
            'API 已启动: http://%s:%s%s（文档 %s/docs）',
            self.host,
            self.port,
            self.prefix,
            self.prefix,
        )
        return True

    def stop(self) -> None:
        """优雅关闭 API 服务。"""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
