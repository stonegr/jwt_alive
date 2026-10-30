# jwt-alive

可扩展的 **JWT 保活** 框架：把 token 从配置文件里读进来，按站点配置「过期时间字段 / 刷新接口 / 登录态校验接口」，由后台定时任务自动续期。

- 多站点：一份 YAML 管所有站点，每个站点独立配置
- 过期字段可选：不同网站把过期时间放在 `exp` / `expireTime` / `expires_in` / 嵌套字段里都能解析
- 刷新接口自定义：URL、HTTP 方法、请求头、请求体都能配，新 token 支持从 **响应体** 或 **Set-Cookie** 提取
- 登录态校验：先用 JWT 自身解出的过期时间判断，再调用站点接口二次确认
- 定时刷新：全局扫描 + 每站阈值（临期刷新），可选每站 cron
- 纯 CLI 守护进程，无 Web 服务；状态落盘 + 结构化日志
- 可扩展：站点特有逻辑写成适配器子类，配置里一行 `adapter:` 指定

---

## 快速开始

```bash
# 1. 安装依赖（uv）
uv sync

# 2. 生成配置模板
uv run jwt-alive init

# 3. 编辑 config/sites.yaml，填入各站点的 token 与接口

# 4. 先做一次检查
uv run jwt-alive check

# 5. 启动保活守护进程
uv run jwt-alive run
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `jwt-alive run` | 启动守护进程：定时检查 + 临期自动刷新（前台运行，Ctrl-C 退出） |
| `jwt-alive check` | 检查所有站点登录态，不刷新（有站点未登录时退出码为 1） |
| `jwt-alive refresh <site>` | 手动刷新指定站点（`--all` 刷新全部，`--force` 忽略到期判断） |
| `jwt-alive status` | 查看本地状态文件，不发起网络请求 |
| `jwt-alive health` | 健康检查：判断守护进程是否还在正常扫描（退出码 0=健康） |
| `jwt-alive validate-config` | 校验配置并打印摘要 |
| `jwt-alive init` | 生成配置模板 |

全局选项：`--data-dir/-d` 运行根目录、`--config/-c` 配置文件、`--state/-s` 状态文件、
`--log-level/-l` 日志级别、`--log-file` 日志文件、`--plugins` 自定义适配器目录、
`--quiet` 关闭控制台日志。

> 路径也可用环境变量指定（命令行优先）：
> `JWT_ALIVE_DATA_DIR`、`JWT_ALIVE_CONFIG`、`JWT_ALIVE_STATE`、`JWT_ALIVE_LOG_FILE`、
> `JWT_ALIVE_PLUGINS`、`JWT_ALIVE_LOG_LEVEL`。

## 运行根目录（--data-dir）

默认情况下 `config/`、`state/`、`logs/`、`plugins/` 都建在**当前工作目录**下。
用 `--data-dir` 可以把它们统一挪到别处（NAS / systemd / 容器里特别有用）：

```bash
jwt-alive --data-dir /opt/jwt-alive run
# → /opt/jwt-alive/config/sites.yaml
# → /opt/jwt-alive/state/state.json
# → /opt/jwt-alive/logs/jwt_alive.log
# → /opt/jwt-alive/plugins/
```

目录不存在会自动创建。优先级（每个路径独立判断，从高到低）：

| 路径 | 1 | 2 | 3 | 4 |
| --- | --- | --- | --- | --- |
| 配置 | `--config` | `JWT_ALIVE_CONFIG` | `<data-dir>/config/sites.yaml` | `CWD/config/sites.yaml` |
| 状态 | `--state` | `JWT_ALIVE_STATE` | `<data-dir>/state/state.json` | `CWD/state/state.json` |
| 日志 | `--log-file` | `JWT_ALIVE_LOG_FILE` | `<data-dir>/logs/jwt_alive.log` | `CWD/logs/jwt_alive.log` |
| 适配器 | `--plugins` | `JWT_ALIVE_PLUGINS` | `<data-dir>/plugins` | `CWD/plugins` |

启动时会打印实际使用的四个路径（排查"是不是同一份文件"用）：

```
运行路径
  配置    /opt/jwt-alive/config/sites.yaml
  状态    /opt/jwt-alive/state/state.json
  日志    /opt/jwt-alive/logs/jwt_alive.log
  适配器  /opt/jwt-alive/plugins
```

> 以上路径也可用环境变量指定（命令行参数优先），容器里默认已设置：
> `JWT_ALIVE_CONFIG`、`JWT_ALIVE_STATE`、`JWT_ALIVE_LOG_FILE`、`JWT_ALIVE_PLUGINS`、`JWT_ALIVE_LOG_LEVEL`。

## 配置说明

完整示例见 [`config/sites.example.yaml`](config/sites.example.yaml)。

```yaml
auth:                            # 对外 API 账号表：用户名 -> 密码（不配则不启动 API）
  ${API_USER:-admin}: "${API_PASSWORD:-}"

api:                             # 对外 API 配置
  enabled: true
  host: "127.0.0.1"              # 容器内用 0.0.0.0
  port: 8080
  prefix: "/api"                 # 所有接口的前缀（"" 表示挂在根路径）

defaults:
  scan_interval_seconds: 60     # 全局扫描间隔
  refresh_before_seconds: 300   # 剩余寿命小于该值就刷新
  timeout_seconds: 15
  user_agent: "jwt-alive/0.1"

sites:
  - name: example
    enabled: true
    adapter: "jwt_alive.adapters.http:HttpSiteAdapter"   # 可指向自定义子类
    token:
      access_token: "${EXAMPLE_ACCESS_TOKEN}"     # 支持 ${ENV} 与 ${ENV:-默认值}
      refresh_token: "${EXAMPLE_REFRESH_TOKEN}"
    jwt:
      expiry_field: "exp"        # 解构 payload 后取哪个字段（支持点路径 data.expireTime）
      expiry_format: "auto"      # auto|epoch_s|epoch_ms|iso8601|duration_s
      leeway_seconds: 30
    refresh:
      url: "https://example.com/api/auth/refresh"
      method: "POST"
      headers:
        Authorization: "Bearer {access_token}"     # 支持占位符
    # 网络相关（可选）
    retry_attempts: 3          # 失败重试次数
    retry_backoff_seconds: 1.0 # 重试退避基数
    trust_env: true            # 读取系统代理环境变量
    proxy: null                # 显式代理
    prefer_ipv4: false         # 强制 IPv4（NAS IPv6 半通时开）
      body:
        refresh_token: "{refresh_token}"
      token_source: "body"      # body | cookie
      access_token_path: "data.access_token"      # 响应体点路径
      refresh_token_path: "data.refresh_token"
      cookie_name: "SESSION"    # token_source=cookie 时的 Cookie 名
    login_check:
      url: "https://example.com/api/user/profile"
      method: "GET"
      headers:
        Authorization: "Bearer {access_token}"
      success:
        type: "status"          # status | json | cookie
        expected: 200
    schedule:
      interval_seconds: 60
      refresh_before_seconds: 300
      cron: "0 3 * * *"          # 可选，到点强制刷新
    persist:
      write_back_to_config: true
    retry_attempts: 3            # 失败重试次数
    retry_backoff_seconds: 1.0   # 重试退避基数（秒）
    trust_env: true              # 是否使用系统代理环境变量
    proxy: null                  # 显式代理地址
```

### 过期时间解析

`jwt.expiry_field` 指定 JWT 解构后的字段名（支持 `data.expireTime` 这类点路径），
`jwt.expiry_format` 指定取值格式：

| 格式 | 说明 | 例子 |
| --- | --- | --- |
| `auto` | 自动区分秒/毫秒时间戳、ISO8601 字符串、相对时长 | 默认 |
| `epoch_s` | Unix 秒 | `1767225600` |
| `epoch_ms` | Unix 毫秒 | `1767225600000` |
| `iso8601` | ISO8601 字符串 | `2026-01-01T00:00:00Z` |
| `duration_s` | 相对时长（秒），有 `iat` 时以 `iat` 为起点 | `3600` |

### 登录态判定

1. **先看 JWT 自身**：解不出 / 已过期 → 直接判定未登录，不再请求接口；
2. **再调接口**：按 `login_check.success.type` 判定
   - `status`：HTTP 状态码等于 `expected`
   - `json`：`path` 取到的值等于 `equals`（不填 `equals` 时只要非空即通过）
   - `cookie`：响应里存在指定 Cookie

校验请求失败（网络异常/超时）会**自动重试** `retry_attempts` 次（默认 3 次，
覆盖"服务端回收空闲 keep-alive 连接"这类瞬时错误）；仍失败则判定为
**登录态未知**（不刷新，等下一轮扫描），避免一次网络抖动白白消耗 refresh_token。
只有明确的 401/403 或 JWT 过期才会触发刷新。

## 对外 API

配置里写了 `auth` 账号表后，`jwt-alive run` 会在**同一进程内**拉起一个 HTTP 服务
（uvicorn 后台线程，与保活调度器共享同一份状态，token 实时一致）。

### 认证

HTTP Basic：用户名查 `auth` 表，密码用常量时间比较（`hmac.compare_digest`）。

```bash
curl -u alice:s3cret http://127.0.0.1:8080/api/sites
```

未认证或密码错误 → `401` + `WWW-Authenticate: Basic realm="jwt-alive"`。

### 路径前缀

所有接口都在 `api.prefix` 前缀下（默认 `/api`），交互式文档同理：

```yaml
api:
  prefix: "/api"      # → /api/sites、/api/sites/{name}/token …
```

| 配置 | 实际路径 |
| --- | --- |
| `prefix: "/api"` | `/api/sites` |
| `prefix: "/jwt"` | `/jwt/sites` |
| `prefix: ""` | `/sites`（挂在根路径） |

命令行 `--api-prefix`、环境变量 `JWT_ALIVE_API_PREFIX` 可覆盖配置（优先级：命令行 > 环境变量 > 配置）。
前缀会做归一化：`api` → `/api`，`/v1/api/` → `/v1/api`，`/` → 无前缀。

### 端点

（下列路径以默认前缀 `/api` 为例）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/me` | 当前认证用户名 |
| GET | `/api/health` | 守护进程健康：最后扫描时间、站点数、各站登录态 |
| GET | `/api/sites` | 站点列表（名称/登录态/到期时间/剩余/最后刷新/最后错误） |
| GET | `/api/sites/{name}` | 站点详情，**含 access_token / refresh_token 明文** |
| GET | `/api/sites/{name}/token` | 只返回 token 与到期时间 |
| POST | `/api/sites/{name}/refresh` | 触发该站点刷新，返回新 token |
| POST | `/api/refresh-all` | 刷新全部站点 |

交互式文档：`http://127.0.0.1:8080/docs`（FastAPI 自动生成）。

```bash
# 列表
curl -u alice:s3cret http://127.0.0.1:8080/api/sites

# 取 token
curl -u alice:s3cret http://127.0.0.1:8080/api/sites/clockify/token

# 触发刷新
curl -u alice:s3cret -X POST http://127.0.0.1:8080/api/sites/clockify/refresh
```

### 行为约定

- 未配置 `auth` 或 `api.enabled: false` → **不启动 API**，保活不受影响（`--no-api` 可显式关闭）
- API 端口被占用 / 启动失败 → 只记日志，**不影响保活主流程**
- 未知站点 → `404`；刷新业务失败 → `200` + `{"ok": false, "reason": "..."}`
- API 与调度器并发操作同一站点时由 service 层加锁串行化

### 安全提醒

- API 会返回 **token 明文**，默认只监听 `127.0.0.1`；容器内需监听 `0.0.0.0` 时**务必**前置 HTTPS 反代（或用 `api.ssl_certfile` / `api.ssl_keyfile` 自签）
- HTTP Basic 每次请求都带密码，务必只在 HTTPS 下使用
- 密码明文存配置（与 token 同风险模型），建议 `${ENV}` 注入，且 `config/` 已在 `.gitignore` 中
- Docker 部署时用 `ports` 暴露端口，并限制来源 IP

## 扩展：自定义站点适配器

在 `plugins/` 下写一个子类，配置里用 `adapter:` 指定即可：

```python
from jwt_alive.adapters.http import HttpSiteAdapter
from jwt_alive.models import CheckResult, SiteState, TokenBundle


class MySiteAdapter(HttpSiteAdapter):
    def extract_tokens(self, response):  # 新 token 在自定义响应头里
        bundle = TokenBundle()
        bundle.access_token = response.headers.get('X-Token')
        return bundle

    def check_login(self, state):  # 自定义登录态判定
        return CheckResult(True, '自定义校验通过')
```

可覆写的钩子：

| 钩子 | 作用 |
| --- | --- |
| `resolve_expiry(token)` | 解析过期时间 |
| `build_refresh_request(state)` | 构造刷新请求 |
| `extract_tokens(response)` | 提取新 token |
| `check_login(state)` | 校验登录态 |
| `on_refreshed(old, new)` | 刷新成功后的钩子 |

配置示例：

```yaml
sites:
  - name: my-site
    adapter: "plugins.my_site:MySiteAdapter"    # 或 "my_site:MySiteAdapter"
```

## Token 存放

- **状态文件** `state/state.json`：运行时最新 token、到期时间、最后刷新/错误
- **回写配置文件**：`persist.write_back_to_config: true` 时，刷新成功会把新 token 写回 `config/sites.yaml`（保留 YAML 注释）

两处都有 token 时，**按 token 自身的时间戳取新的**（解 JWT 比 `exp`，相同再比 `iat`），
而不是"状态文件永远优先"——这样某一份陈旧的副本不会让老 token 一直生效，
下一次保存时状态文件也会被自愈成新 token。非 JWT（不透明 token）无法比较时维持"状态文件优先"。

> 状态文件是**运行时真相**，配置文件只是镜像/种子；多台机器共用同一份配置时，
> 各自的 state 文件互不可见，按上面的规则各取最新的那个。

> `config/sites.yaml`、`state/`、`logs/`、`.env` 都已在 `.gitignore` 中，token 不要提交到仓库。

## Docker 部署

镜像基于 `python:3.12-slim`，依赖用 `uv.lock` 锁定构建，非 root 运行，带健康检查。

### 首次部署

```bash
# 1. 准备配置与密钥
make init                             # 从模板生成 config/sites.yaml（已存在则跳过）
cp .env.example .env
vim .env                 # 填各站点 token
vim config/sites.yaml    # 改成真实站点，enabled: true

# 2. 构建并启动
make docker-build
make docker-up
make docker-logs
```

也可以直接用 compose：

```bash
docker compose up -d --build
docker compose logs -f
```

### 国内构建

网络慢时用清华源构建（PyPI + Debian 源都走国内镜像）：

```bash
make docker-build-cn
```

底层等价于：

```bash
docker compose build \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
```

> 基础镜像 `python:3.12-slim` 仍从 Docker Hub 拉取，需要加速的话配置 Docker daemon
> 的 registry mirror（`/etc/docker/daemon.json` 的 `registry-mirrors`）。

> **配置用软链接也没问题**：compose 按「单文件」挂载 `./config/sites.yaml`，
> Docker 会自动解析软链接指向真实文件（如 `self/prod.yaml`），
> 刷新回写直接落到真实文件，软链接本身不会被替换。
> 但宿主的 `config/sites.yaml` 必须先存在（`make init` 或手动创建），
> 否则 Docker 会把它当成目录创建，容器启动即报错。

### 常用操作

```bash
docker compose exec jwt-alive jwt-alive check       # 检查登录态
docker compose exec jwt-alive jwt-alive status      # 查看状态
docker compose run --rm jwt-alive jwt-alive refresh --all   # 手动刷新
docker compose run --rm jwt-alive jwt-alive init    # 在挂载目录生成配置模板
docker compose down                                 # 停止
docker compose down -v                              # 停止并清空状态/日志卷
```

### 挂载与权限

| 容器路径 | 来源 | 权限 | 说明 |
| --- | --- | --- | --- |
| `/app/config/sites.yaml` | 宿主 `./config/sites.yaml`（软链接会被解析） | 读写 | 刷新成功后回写新 token |
| `/app/plugins` | 宿主 `./plugins` | 只读 | 自定义适配器 |
| `/app/state` | 命名卷 `jwt-state` | 读写 | 最新 token、扫描时间 |
| `/app/logs` | 命名卷 `jwt-logs` | 读写 | 轮转日志文件 |

容器里 `JWT_ALIVE_DATA_DIR=/app`，四个路径都从它推导（`/app/config`、`/app/state`、
`/app/logs`、`/app/plugins`）。**改这个值要同步改上面的 volumes 挂载路径**，
否则 state/logs 会落到容器可写层，容器重建即丢。

容器以 `${UID}:${GID}` 运行（默认 1000:1000），避免把宿主文件写成 root 属主。
**Linux 上如果 token 回写报权限错**，把宿主 uid/gid 写进 `.env`：

```bash
echo "UID=$(id -u)" >> .env
echo "GID=$(id -g)" >> .env
```

### 健康检查

容器 `HEALTHCHECK` 执行 `jwt-alive health`：读取状态文件里的 `last_scan`，
判断守护进程最近是否还在正常扫描。**进程假死（不再扫描）会被判定为 unhealthy**，
配合 `restart: unless-stopped` 可配合外部编排做自愈。

阈值默认取配置里 `scan_interval_seconds` 的 3 倍（最小 30 秒），也可显式指定：

```bash
make docker-health                       # 查看健康状态
docker compose exec jwt-alive jwt-alive health --max-age 300
```

### SSL / 网络错误排查

日志里出现 `[SSL] unknown error (_ssl.c:1011)`、`ConnectError`、`ReadTimeout` 等，属于
**TLS 握手/网络层**的瞬时失败（不是业务错误）。程序会自动重试
（`retry_attempts`，默认 3 次），重试成功只记 INFO，不影响保活。

按可能性排查：

1. **IPv6 半通**（NAS 最常见）：TCP 走 v6 连上了，但 TLS 握手数据被丢弃 →
   握手中途失败（`socket.create_connection` 只在 connect 失败时才回退地址）。
   → 站点配置加 `prefer_ipv4: true` 强制走 IPv4，或在 NAS 上关掉 IPv6
2. **代理环境变量**：本项目 `trust_env: true` 默认读取 `HTTP_PROXY`/`ALL_PROXY`，
   代理不稳也会造成握手失败 → `env | grep -i proxy` 确认，或显式配 `proxy:`
3. **系统时间**：时间不对一般是证书校验失败 → `date` + NTP
4. **CA 证书链**：Alpine 需 `apk add ca-certificates`，Debian 需 `apt install ca-certificates`
5. **OpenSSL 版本**：过老的 OpenSSL 与服务端协商失败（会持续失败而非偶发）

### 常见问题

- **代理**：宿主的 `HTTP_PROXY` / `ALL_PROXY` 不会进容器，需要在 `docker-compose.yml` 的
  `environment` 里显式传入，或用站点级 `proxy:` 配置
- **时区**：`TZ` 影响日志与状态展示，内部过期计算一律用 UTC；
  `schedule.cron` 按 `TZ` 指定的时区触发（没设 `TZ` 时用系统本地时区）。
  **缺少 tzdata 时会回退 UTC**（Alpine 需 `apk add tzdata`，Debian 需 `apt install tzdata`），
  此时 cron 会比预期偏移，回退时会打印告警
- **token 放哪**：推荐放在 `.env`（环境变量）里，配置中用 `${VAR}` 引用，避免明文落盘
- **更新镜像**：改完代码执行 `docker compose up -d --build` 重建并重启
- **调试**：`docker compose exec jwt-alive sh` 进入容器，或 `make docker-shell`

## 目录结构

```
jwt_alive/
├── cli.py            命令行入口
├── config.py         配置加载、环境变量插值、默认值合并
├── models.py         配置模型 + 运行期模型
├── jwt_tools.py      JWT 解码、过期时间解析
├── adapters/         站点适配器（base / http / loader）
├── api.py            对外 HTTP API（FastAPI）
├── api_server.py     API 后台线程启动器
├── runtime.py        单站点运行时（刷新 / 校验流程）
├── service.py        多站点编排
├── scheduler.py      APScheduler 定时任务
├── state.py          状态持久化
├── persist.py        回写配置文件
├── logging_setup.py  日志配置
└── http_client.py    HTTP 客户端
plugins/              自定义适配器
config/               配置（sites.yaml 本地，sites.example.yaml 模板）
tests/                pytest 测试
Dockerfile            多阶段构建（uv 构建 → python:3.12-slim 运行）
docker-compose.yml    编排：挂载、密钥、健康检查、自动重启
.env.example          token 环境变量模板
```

## 开发

```bash
make install   # uv sync
make dev       # 本地启动守护进程（DEBUG 日志）
make test      # pytest
make lint      # ruff check + ruff format --check
make fmt       # ruff format + ruff check --fix
make check     # lint + test
make help      # 查看全部命令
```
