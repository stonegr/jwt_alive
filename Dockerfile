# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------------------
# 构建阶段：用 uv 按 uv.lock 安装依赖（可复现）
# --------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ARG UV_VERSION=0.12.17
# 国内构建（make docker-build-cn）会覆盖成国内镜像源
ARG APT_MIRROR=deb.debian.org
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    PIP_INDEX_URL=${UV_DEFAULT_INDEX}

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

# 先只拷依赖清单，命中 Docker 层缓存
COPY pyproject.toml uv.lock README.md ./
COPY jwt_alive ./jwt_alive
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 再安装项目本体（非 editable，保证容器里是 site-packages 安装）
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------------------------
# 运行阶段
# --------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG APT_MIRROR=deb.debian.org

# ca-certificates: HTTPS 刷新接口；tzdata: TZ=Asia/Shanghai 生效
RUN if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    JWT_ALIVE_CONFIG=/app/config/sites.yaml \
    JWT_ALIVE_STATE=/app/state/state.json \
    JWT_ALIVE_LOG_FILE=/app/logs/jwt_alive.log \
    JWT_ALIVE_PLUGINS=/app/plugins \
    TZ=Asia/Shanghai

COPY --from=builder /app/.venv /app/.venv

WORKDIR /app

# 示例配置放在挂载目录之外，避免被 config/ 的 bind mount 遮住
COPY --from=builder /app/config/sites.example.yaml /usr/share/jwt-alive/sites.example.yaml
COPY --from=builder /app/plugins /app/plugins

# 目录权限放开，命名卷与任意 uid 都能写
RUN mkdir -p /app/config /app/state /app/logs /app/plugins \
    && chmod -R 0777 /app/config /app/state /app/logs /app/plugins \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

USER app

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["jwt-alive", "health"]

ENTRYPOINT ["jwt-alive"]
CMD ["run"]
