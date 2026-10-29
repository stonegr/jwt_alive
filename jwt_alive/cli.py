"""命令行入口。"""

from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Annotated, Any

from rich.console import Console
from rich.table import Table
import typer

from jwt_alive.api_server import ApiServer
from jwt_alive.config import ConfigError, load_config
from jwt_alive.logging_setup import setup_logging
from jwt_alive.models import AppConfig
from jwt_alive.scheduler import KeepAliveScheduler
from jwt_alive.service import JwtAliveService
from jwt_alive.state import StateStore
from jwt_alive.utils import now_utc, to_local


app = typer.Typer(
    name='jwt-alive',
    help='可扩展的 JWT 保活工具：按站点配置过期字段、刷新接口与登录态校验。',
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

ConfigOpt = Annotated[
    Path | None,
    typer.Option('--config', '-c', help='配置文件路径，默认 <data-dir>/config/sites.yaml'),
]
StateOpt = Annotated[Path | None, typer.Option('--state', '-s', help='状态文件路径，默认 <data-dir>/state/state.json')]
LevelOpt = Annotated[str, typer.Option('--log-level', '-l', help='日志级别 DEBUG/INFO/WARN/ERROR')]
LogFileOpt = Annotated[Path | None, typer.Option('--log-file', help='日志文件路径')]
PluginOpt = Annotated[Path | None, typer.Option('--plugins', help='自定义适配器目录，默认 <data-dir>/plugins')]
QuietOpt = Annotated[bool, typer.Option('--quiet', help='不输出控制台日志')]
DataDirOpt = Annotated[
    Path | None,
    typer.Option('--data-dir', '-d', help='运行根目录：config/ state/ logs/ plugins/ 的父目录'),
]

ENV_CONFIG = 'JWT_ALIVE_CONFIG'
ENV_STATE = 'JWT_ALIVE_STATE'
ENV_LOG_FILE = 'JWT_ALIVE_LOG_FILE'
ENV_PLUGINS = 'JWT_ALIVE_PLUGINS'
ENV_LOG_LEVEL = 'JWT_ALIVE_LOG_LEVEL'
ENV_API_HOST = 'JWT_ALIVE_API_HOST'
ENV_API_PORT = 'JWT_ALIVE_API_PORT'
ENV_DATA_DIR = 'JWT_ALIVE_DATA_DIR'


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def _data_dir(value: Path | None) -> Path | None:
    """运行根目录：只来自命令行/环境变量（不从配置文件读，避免循环依赖）。"""
    if value is not None:
        return value.expanduser()
    return _env_path(ENV_DATA_DIR)


def _config_path(value: Path | None, data_dir: Path | None = None) -> Path:
    if value is not None:
        return value.expanduser()
    env = _env_path(ENV_CONFIG)
    if env is not None:
        return env
    base = data_dir if data_dir is not None else Path.cwd()
    return base / 'config' / 'sites.yaml'


def _state_path(value: Path | None, data_dir: Path | None = None) -> Path:
    if value is not None:
        return value.expanduser()
    env = _env_path(ENV_STATE)
    if env is not None:
        return env
    base = data_dir if data_dir is not None else Path.cwd()
    return base / 'state' / 'state.json'


def _log_file_path(value: Path | None, data_dir: Path | None = None) -> Path:
    if value is not None:
        return value.expanduser()
    env = _env_path(ENV_LOG_FILE)
    if env is not None:
        return env
    base = data_dir if data_dir is not None else Path.cwd()
    return base / 'logs' / 'jwt_alive.log'


def _plugin_path(value: Path | None, data_dir: Path | None = None) -> Path:
    if value is not None:
        return value.expanduser()
    env = _env_path(ENV_PLUGINS)
    if env is not None:
        return env
    base = data_dir if data_dir is not None else Path.cwd()
    return base / 'plugins'


EXAMPLE_CONFIG_NAME = 'sites.example.yaml'
EXAMPLE_CONFIG_DIRS = (
    Path('/usr/share/jwt-alive'),  # 容器镜像内的位置
    Path(__file__).resolve().parent.parent / 'config',  # 仓库 / 可编辑安装
)


def find_example_config(data_dir: Path | None = None) -> Path | None:
    """查找示例配置模板（容器里可能不在包目录旁边）。"""
    candidates = list(EXAMPLE_CONFIG_DIRS)
    if data_dir is not None:
        candidates.append(data_dir / 'config')
    candidates.append(Path.cwd() / 'config')
    for directory in candidates:
        candidate = directory / EXAMPLE_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _log_level(value: str) -> str:
    return value or os.environ.get(ENV_LOG_LEVEL, 'INFO')


def _api_host(value: str | None) -> str | None:
    if value is not None:
        return value
    return os.environ.get(ENV_API_HOST) or None


def _api_port(value: int | None) -> int | None:
    if value is not None:
        return value
    raw = os.environ.get(ENV_API_PORT)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@app.callback()
def root(
    ctx: typer.Context,
    config: ConfigOpt = None,
    state: StateOpt = None,
    log_level: LevelOpt = 'INFO',
    log_file: LogFileOpt = None,
    plugins: PluginOpt = None,
    quiet: QuietOpt = False,
    data_dir: DataDirOpt = None,
) -> None:
    """全局选项。"""
    base = _data_dir(data_dir)
    ctx.obj = {
        'data_dir': base,
        'config_path': _config_path(config, base),
        'state_path': _state_path(state, base),
        'log_path': _log_file_path(log_file, base),
        'plugin_path': _plugin_path(plugins, base),
        'log_level': _log_level(log_level),
        'quiet': quiet,
    }


def _bootstrap(ctx: typer.Context) -> tuple[logging.Logger, AppConfig]:
    opts: dict[str, Any] = ctx.obj or {}
    setup_logging(
        level=str(opts.get('log_level', 'INFO')),
        log_file=opts.get('log_path'),
        console=not opts.get('quiet', False),
    )
    logger = logging.getLogger('jwt_alive')
    try:
        config = load_config(opts['config_path'])
    except ConfigError as exc:
        err_console.print(f'[red]配置错误：{exc}[/red]')
        raise typer.Exit(code=2) from exc
    return logger, config


def _build_service(ctx: typer.Context, config: AppConfig, logger: logging.Logger) -> JwtAliveService:
    opts: dict[str, Any] = ctx.obj or {}
    return JwtAliveService(
        config=config,
        state_path=opts['state_path'],
        config_path=opts['config_path'],
        plugin_path=opts['plugin_path'],
        logger=logger,
    )


def _fmt_remaining(seconds: float | None) -> str:
    if seconds is None:
        return '-'
    if seconds <= 0:
        return '[red]已过期[/red]'
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h{minutes}m'
    return f'{minutes}m{sec}s'


def _fmt_time(value: object) -> str:
    if value is None:
        return '-'
    if isinstance(value, datetime):
        local = to_local(value)
        return local.strftime('%Y-%m-%d %H:%M:%S') if local else '-'
    return str(value).replace('T', ' ')[:19]


def _render_paths(ctx: typer.Context) -> None:
    """打印实际使用的四个路径（排查"是不是同一份文件"用）。"""
    opts: dict[str, Any] = ctx.obj or {}
    rows = [
        ('配置', opts.get('config_path')),
        ('状态', opts.get('state_path')),
        ('日志', opts.get('log_path')),
        ('适配器', opts.get('plugin_path')),
    ]
    table = Table(title='运行路径', show_header=False, box=None)
    table.add_column()
    table.add_column()
    for label, value in rows:
        table.add_row(label, str(value))
    console.print(table)


@app.command()
def run(
    ctx: typer.Context,
    api_host: Annotated[str | None, typer.Option('--api-host', help='API 监听地址，覆盖配置')] = None,
    api_port: Annotated[int | None, typer.Option('--api-port', help='API 监听端口，覆盖配置')] = None,
    no_api: Annotated[bool, typer.Option('--no-api', help='不启动对外 API')] = False,
) -> None:
    """启动保活守护进程（前台运行，Ctrl-C 退出）。"""
    logger, config = _bootstrap(ctx)
    service = _build_service(ctx, config, logger)
    scheduler = KeepAliveScheduler(service, interval_seconds=config.defaults.scan_interval_seconds, logger=logger)
    api_server: ApiServer | None = None
    if not no_api:
        api_server = ApiServer(
            service,
            config,
            host=_api_host(api_host),
            port=_api_port(api_port),
            logger=logger,
        )
    console.print(f'[bold green]jwt-alive[/bold green] 已启动，站点 {len(service.runtimes)} 个')
    _render_paths(ctx)
    try:
        console.print('首次检查：')
        _render_check_results(service.check_all())
        service.store.last_scan = now_utc()
        service.store.save()
        scheduler.start()
        if api_server is not None:
            api_server.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        console.print('\n[yellow]正在退出...[/yellow]')
    finally:
        if api_server is not None:
            api_server.stop()
        scheduler.shutdown()
        service.close()
        console.print('[green]已退出[/green]')


@app.command()
def health(
    ctx: typer.Context,
    max_age: Annotated[
        int | None,
        typer.Option('--max-age', help='允许的最大扫描间隔（秒），默认 3 倍扫描间隔'),
    ] = None,
) -> None:
    """健康检查：判断守护进程是否还在正常扫描（供容器 HEALTHCHECK 使用）。

    退出码 0 表示健康，1 表示不健康。
    """
    opts: dict[str, Any] = ctx.obj or {}
    limit = max_age
    if limit is None:
        limit = 180
        try:
            config = load_config(opts['config_path'])
            limit = max(30, config.defaults.scan_interval_seconds * 3)
        except ConfigError:
            pass

    store = StateStore(opts['state_path'])
    last_scan = store.last_scan
    if last_scan is None:
        console.print('[red]不健康：状态文件里没有扫描记录[/red]')
        raise typer.Exit(code=1)

    age = (now_utc() - last_scan).total_seconds()
    if age > limit:
        console.print(f'[red]不健康：最后一次扫描在 {age:.0f} 秒前（阈值 {limit} 秒）[/red]')
        raise typer.Exit(code=1)

    console.print(f'[green]健康：最后一次扫描在 {age:.0f} 秒前[/green]')


@app.command()
def check(ctx: typer.Context) -> None:
    """检查所有站点的登录态（不刷新）。"""
    logger, config = _bootstrap(ctx)
    with _build_service(ctx, config, logger) as service:
        results = service.check_all()
        _render_check_results(results)
        if any(not r.logged_in for r in results.values()):
            raise typer.Exit(code=1)


@app.command()
def refresh(
    ctx: typer.Context,
    site: Annotated[str | None, typer.Argument(help='站点名，省略则配合 --all 刷新全部')] = None,
    all_sites: Annotated[bool, typer.Option('--all', '-a', help='刷新所有站点')] = False,
    force: Annotated[bool, typer.Option('--force', '-f', help='忽略到期判断，强制刷新')] = False,
) -> None:
    """手动刷新 token。"""
    logger, config = _bootstrap(ctx)
    with _build_service(ctx, config, logger) as service:
        if all_sites or not site:
            if not all_sites:
                err_console.print('[red]请指定站点名，或使用 --all[/red]')
                raise typer.Exit(code=2)
            outcomes = service.refresh_all(force=force)
        else:
            if service.get_runtime(site) is None:
                err_console.print(f'[red]未知站点：{site}[/red]')
                raise typer.Exit(code=2)
            outcomes = [service.refresh_site(site, force=force)]
        _render_refresh_outcomes(outcomes)
        if any(not o.ok for o in outcomes):
            raise typer.Exit(code=1)


@app.command()
def status(ctx: typer.Context) -> None:
    """查看本地状态文件（不发起网络请求）。"""
    opts: dict[str, Any] = ctx.obj or {}
    setup_logging(level=str(opts.get('log_level', 'INFO')), log_file=None, console=False)
    store = StateStore(opts['state_path'])
    states = store.all()
    console.print(f'[dim]state 文件：{opts["state_path"]}[/dim]', soft_wrap=True)
    if not states:
        console.print('[yellow]暂无状态记录，先运行 jwt-alive check[/yellow]')
        return

    table = Table(title='jwt-alive 状态', show_lines=False)
    for col in ('站点', '登录态', '到期时间', '剩余', '最后刷新', '最后错误'):
        table.add_column(col)
    for state in states.values():
        remaining = None
        if state.expires_at is not None:
            remaining = (state.expires_at - now_utc()).total_seconds()
        if state.logged_in is None:
            logged = '未知'
        elif state.logged_in:
            logged = '[green]是[/green]'
        else:
            logged = '[red]否[/red]'
        table.add_row(
            state.name,
            logged,
            _fmt_time(state.expires_at),
            _fmt_remaining(remaining),
            _fmt_time(state.last_refresh),
            (state.last_error or '-')[:60],
        )
    console.print(table)


@app.command('validate-config')
def validate_config(ctx: typer.Context) -> None:
    """校验配置文件并输出摘要。"""
    logger, config = _bootstrap(ctx)
    table = Table(title='配置摘要')
    for col in ('站点', '启用', '适配器', '刷新接口', '过期字段', '校验接口'):
        table.add_column(col)
    for site in config.sites:
        adapter_name = site.adapter.rsplit(':', 1)[-1].rsplit('.', 1)[-1]
        table.add_row(
            site.name,
            '是' if site.enabled else '否',
            adapter_name,
            f'{site.refresh.method} {site.refresh.url}',
            f'{site.jwt.expiry_field}({site.jwt.expiry_format.value})',
            site.login_check.url if site.login_check else '-',
        )
    console.print(table)
    console.print(f'[green]配置校验通过，共 {len(config.sites)} 个站点[/green]')
    del logger


@app.command()
def init(
    ctx: typer.Context,
    target: Annotated[Path | None, typer.Option('--target', '-t', help='输出路径')] = None,
    force: Annotated[bool, typer.Option('--force', '-f', help='覆盖已存在的文件')] = False,
) -> None:
    """生成示例配置文件。"""
    opts: dict[str, Any] = ctx.obj or {}
    out_path = target.expanduser() if target else opts['config_path']
    example = find_example_config(opts.get('data_dir'))
    if example is None:
        err_console.print('[red]找不到示例配置模板[/red]')
        raise typer.Exit(code=2)
    if out_path.exists() and not force:
        err_console.print(f'[red]{out_path} 已存在，使用 --force 覆盖[/red]')
        raise typer.Exit(code=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, out_path)
    console.print(f'[green]已生成配置：{out_path}[/green]')


# ----------------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------------


def _render_check_results(results: dict[str, Any]) -> None:
    table = Table(title='登录态检查')
    for col in ('站点', '已登录', '说明'):
        table.add_column(col)
    for name, result in results.items():
        if result.logged_in is None:
            logged = '[yellow]未知[/yellow]'
        elif result.logged_in:
            logged = '[green]是[/green]'
        else:
            logged = '[red]否[/red]'
        reason = result.reason if result.detail is None else f'{result.reason}（{result.detail}）'
        table.add_row(name, logged, reason)
    console.print(table)


def _render_refresh_outcomes(outcomes: list[Any]) -> None:
    table = Table(title='刷新结果')
    for col in ('站点', '结果', '说明', '新到期时间', '已回写配置'):
        table.add_column(col)
    for outcome in outcomes:
        result = '[green]成功[/green]' if outcome.ok else '[red]失败[/red]'
        table.add_row(
            outcome.site,
            result,
            outcome.reason,
            _fmt_time(outcome.new_expiry),
            '是' if outcome.written_back else '否',
        )
    console.print(table)


def main() -> None:
    """console_scripts 入口。"""
    app()


if __name__ == '__main__':
    main()
