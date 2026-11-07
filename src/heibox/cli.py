"""命令行入口。"""

from __future__ import annotations

import json
import os
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import config as cfgmod
from .api import HeyboxClient
from .errors import CaptchaRequired, ConfigError, HeiboxError, LoginError
from .images import ImageCache
from .login import QRLogin, extract_userid, render_qr
from .ranges import format_date, format_ts, parse_range
from .render import human_ago, posts_as_json, render_posts
from .store import Store
from .sync import Syncer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="把小黑盒个人主页的帖子缓存到本地，按时间范围查询。",
)
console = Console()
err_console = Console(stderr=True)


@dataclass
class AppContext:
    data_dir: Path


def _log(message: str) -> None:
    err_console.print(f"[dim]{message}[/dim]")


def _fail(exc: Exception, code: int = 1) -> None:
    if isinstance(exc, CaptchaRequired):
        err_console.print(f"[yellow]接口要求人机验证：{exc}[/yellow]")
        err_console.print(
            "[yellow]请先用浏览器打开 xiaoheihe.cn 完成验证，或等待几分钟后再试。"
            "已抓取的部分已保存，重新运行会从断点继续；也可以加 --no-sync 只查本地缓存。[/yellow]"
        )
        raise typer.Exit(4)
    err_console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code)


def _require_config(ctx: typer.Context) -> cfgmod.Config:
    config = cfgmod.load_config(ctx.obj.data_dir)
    if config is None:
        raise ConfigError("还没有配置，请先运行：heibox init --userid <你的 userid>")
    return config


def _open_store(ctx: typer.Context, config: cfgmod.Config) -> Store:
    store = Store(cfgmod.db_path(ctx.obj.data_dir))
    store.ensure_user(config.userid)
    return store


def _client(config: cfgmod.Config) -> HeyboxClient:
    cookie = os.environ.get("HEIBOX_COOKIE") or config.cookie
    return HeyboxClient(interval=config.request_interval, cookie=cookie)


@app.callback()
def root(
    ctx: typer.Context,
    data_dir: Path | None = typer.Option(None, "--data-dir", help="数据目录，默认 %LOCALAPPDATA%\\heibox-history"),
    version: bool = typer.Option(False, "--version", help="显示版本并退出"),
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()
    ctx.obj = AppContext(data_dir=data_dir or cfgmod.data_dir())


@app.command()
def init(
    ctx: typer.Context,
    userid: int = typer.Option(..., "--userid", prompt="小黑盒 userid（个人主页网址末尾的数字）"),
) -> None:
    """绑定账号，并向接口验证一次。"""
    try:
        if userid <= 0:
            raise ValueError("userid 必须是正整数")
        config = cfgmod.Config(userid=userid)
        with _client(config) as client:
            page = client.fetch_page(userid, 0)
        cfgmod.save_config(ctx.obj.data_dir, config)
        console.print(f"已绑定 userid={userid} · 接口报告共 [bold]{page.post_link_num}[/bold] 条帖子")
        console.print(f"配置目录：{ctx.obj.data_dir}")
        console.print("下一步：heibox sync --full")
    except ValueError as exc:
        _fail(exc, 2)
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command()
def login(
    ctx: typer.Context,
    timeout: int = typer.Option(180, "--timeout", min=30, help="二维码等待秒数"),
    interval: float = typer.Option(2.0, "--interval", min=1.0, help="轮询间隔（秒）"),
) -> None:
    """用小黑盒 App 扫码登录，自动绑定账号并保存 Cookie。"""
    config = cfgmod.load_config(ctx.obj.data_dir)
    qr_path = ctx.obj.data_dir / "qrcode.png"

    def on_qr(qr_url: str) -> None:
        console.print("请用小黑盒 App 扫描二维码（App 内右上角扫码）：")
        saved = render_qr(console, qr_url, qr_path)
        if saved is not None:
            console.print(f"[dim]二维码图片：{saved}[/dim]")

    last_state = {"value": ""}

    def on_state(state: str, message: str) -> None:
        if state and state != last_state["value"]:
            last_state["value"] = state
            err_console.print(f"[dim]登录状态：{state} {message}[/dim]")

    try:
        with QRLogin(timeout=timeout, interval=interval, on_qr=on_qr, on_state=on_state) as qr:
            cookie, nickname = qr.run()

        userid = extract_userid(cookie)
        if userid is None:
            raise LoginError(
                "登录成功，但 Cookie 里没有 user_heybox_id；请用 heibox init --userid 手动绑定"
            )

        if config is not None and config.userid != userid:
            raise ConfigError(
                f"当前绑定的是 userid={config.userid}，扫码账号是 {userid}；"
                "如需切换账号，请换一个 --data-dir 或先删除 config.toml"
            )

        if config is None:
            config = cfgmod.Config(userid=userid)
            console.print(f"已自动识别账号：userid={userid}")

        config.cookie = cookie
        cfgmod.save_config(ctx.obj.data_dir, config)
        suffix = f"（{nickname}）" if nickname else ""
        console.print(f"登录成功{suffix}，Cookie 已保存")

        try:
            with HeyboxClient(interval=0, cookie=cookie) as client:
                page = client.fetch_page(userid, 0)
        except CaptchaRequired as exc:
            err_console.print(
                f"[yellow]Cookie 已保存，但资料接口仍要求验证：{exc}；稍后再同步。[/yellow]"
            )
            raise typer.Exit(4)

        if page.profile_userid and page.profile_userid != userid:
            config.cookie = ""
            cfgmod.save_config(ctx.obj.data_dir, config)
            raise ConfigError(
                f"扫码登录的是 userid={page.profile_userid}，与识别出的 {userid} 不一致；已丢弃 Cookie"
            )

        console.print(f"验证通过 · 接口当前可见 {page.post_link_num} 条帖子")
        console.print("下一步：heibox sync --full")
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command()
def auth(
    ctx: typer.Context,
    file: Path | None = typer.Option(None, "--file", help="从文件读取 Cookie（单行，可带 Cookie: 前缀）"),
    clear: bool = typer.Option(False, "--clear", help="清除已保存的 Cookie"),
) -> None:
    """保存浏览器完成人机验证后的 Cookie（仅在被要求验证时需要）。"""
    try:
        config = _require_config(ctx)

        if clear:
            config.cookie = ""
            cfgmod.save_config(ctx.obj.data_dir, config)
            console.print("已清除保存的 Cookie")
            return

        if file is not None:
            raw = file.read_text(encoding="utf-8")
        else:
            url = f"https://www.xiaoheihe.cn/app/user/profile/{config.userid}"
            console.print(f"即将打开浏览器：{url}")
            webbrowser.open(url)
            console.print(
                "完成人机验证后，在 DevTools → Network 里复制任意 api.xiaoheihe.cn 请求的 Cookie 请求头。"
            )
            raw = typer.prompt("粘贴 Cookie（输入不回显）", hide_input=True)

        cookie = cfgmod.normalize_cookie(raw)
        if not cookie:
            raise ValueError("Cookie 为空")

        config.cookie = cookie
        cfgmod.save_config(ctx.obj.data_dir, config)

        try:
            with HeyboxClient(interval=0, cookie=cookie) as client:
                page = client.fetch_page(config.userid, 0)
        except CaptchaRequired as exc:
            err_console.print(
                f"[yellow]Cookie 已保存，但接口仍要求验证（{exc}）。"
                "可能是 IP 级冷却，等一段时间后再试；Cookie 已保留。[/yellow]"
            )
            raise typer.Exit(4)

        console.print(f"验证通过，Cookie 已保存 · 接口当前可见 {page.post_link_num} 条帖子")
        console.print("现在可以运行：heibox sync --full")
    except ValueError as exc:
        _fail(exc, 2)
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command()
def sync(
    ctx: typer.Context,
    full: bool = typer.Option(False, "--full", help="从断点一直翻到最早一页"),
    rewalk: bool = typer.Option(
        False, "--rewalk", help="从 offset 0 重扫全量，补齐被列表临时过滤掉的条目"
    ),
    images: str | None = typer.Option(
        None, "--images", help="图片档位：off / thumb / full（会写入配置）"
    ),
) -> None:
    """同步帖子到本地。"""
    try:
        config = _require_config(ctx)
        if images is not None:
            mode = images.strip().lower()
            if mode not in cfgmod.IMAGE_MODE_LABELS:
                raise ConfigError("--images 只能是 off / thumb / full")
            config.image_mode = mode
            cfgmod.save_config(ctx.obj.data_dir, config)
            console.print(f"图片下载：{cfgmod.IMAGE_MODE_LABELS[mode]}")
        with _open_store(ctx, config) as store, _client(config) as client:
            if rewalk:
                state = store.load_state(config.userid)
                state.next_offset = 0
                state.reached_end = False
                store.save_state(state)
            syncer = Syncer(
                store,
                client,
                config.userid,
                log=_log,
                image_mode=config.image_mode,
                images=ImageCache(ctx.obj.data_dir),
            )
            report = syncer.sync_full() if (full or rewalk) else syncer.sync_latest()
        suffix = " · 已到最早" if report.reached_end else ""
        image_note = ""
        if report.images or report.images_failed:
            image_note = f" · 图片 {report.images} 张"
            if report.images_failed:
                image_note += f"（失败 {report.images_failed}）"
        console.print(
            f"完成：{report.pages} 页 · 新收录 [bold]{report.inserted}[/bold] 条"
            f" · 更新 {report.updated} 条{image_note}{suffix}"
        )
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command("list")
def list_posts(
    ctx: typer.Context,
    from_: str | None = typer.Option(None, "--from", help="起始：2024 / 2024-03 / 2024-03-05"),
    to: str | None = typer.Option(None, "--to", help="结束：同上，含当天"),
    period: str | None = typer.Option(None, "--period", help="精确区间：2022-06 表示整个 6 月"),
    page: int = typer.Option(1, "--page", min=1),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON（stdout 只有 JSON）"),
    no_sync: bool = typer.Option(False, "--no-sync", help="只用本地缓存，不联网"),
) -> None:
    """按时间范围列出帖子。"""
    try:
        if period is not None:
            if from_ is not None or to is not None:
                raise ValueError("--period 不能与 --from/--to 同时使用")
            start, end = parse_range(period, period)
        else:
            start, end = parse_range(from_, to)
    except ValueError as exc:
        _fail(exc, 2)

    try:
        config = _require_config(ctx)
        with _open_store(ctx, config) as store:
            if not no_sync:
                try:
                    with _client(config) as client:
                        Syncer(
                            store,
                            client,
                            config.userid,
                            log=_log,
                            image_mode=config.image_mode,
                            images=ImageCache(ctx.obj.data_dir),
                        ).ensure_range(start, end)
                except CaptchaRequired as exc:
                    err_console.print(
                        f"[yellow]接口要求人机验证（{exc}），本次只展示本地已有缓存。[/yellow]"
                    )
            total = store.count(config.userid, start, end)
            posts = store.query(config.userid, start, end, limit, (page - 1) * limit)
            coverage = store.coverage(config.userid)

        if as_json:
            typer.echo(json.dumps(posts_as_json(posts), ensure_ascii=False, indent=2))
        else:
            render_posts(
                console,
                posts=posts,
                total=total,
                coverage=coverage,
                page=page,
                limit=limit,
                start=start,
                end=end,
            )
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command()
def show(ctx: typer.Context, linkid: int) -> None:
    """查看本地保存的单条帖子。"""
    try:
        config = _require_config(ctx)
        with _open_store(ctx, config) as store:
            post = store.get_post(linkid)
        if post is None:
            _fail(ConfigError(f"本地没有 linkid={linkid} 的帖子，先运行 heibox sync"))
        assert post is not None
        console.print(f"[bold]{post.title or '(无标题)'}[/bold]")
        likes = "—" if post.like_num is None else str(post.like_num)
        console.print(
            f"[dim]{format_ts(post.create_at)} · {post.click} 浏览"
            f" · {likes} 点赞 · {post.comment_num} 评论[/dim]"
        )
        if post.description:
            console.print(post.description)
        if post.url:
            console.print(f"[link={post.url}]{post.url}[/link]")
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command("open")
def open_post(ctx: typer.Context, linkid: int) -> None:
    """在浏览器中打开帖子。"""
    try:
        config = _require_config(ctx)
        with _open_store(ctx, config) as store:
            post = store.get_post(linkid)
        if post is None:
            _fail(ConfigError(f"本地没有 linkid={linkid} 的帖子，先运行 heibox sync"))
        assert post is not None
        if not post.url:
            _fail(ConfigError("这条帖子没有可用的网页地址"))
        webbrowser.open(post.url)
        console.print(f"已在浏览器打开：{post.url}")
    except (HeiboxError, OSError) as exc:
        _fail(exc)


@app.command()
def status(ctx: typer.Context) -> None:
    """查看缓存状态。"""
    try:
        config = _require_config(ctx)
        with _open_store(ctx, config) as store:
            coverage = store.coverage(config.userid)
            database = cfgmod.db_path(ctx.obj.data_dir)
        size_kb = database.stat().st_size / 1024 if database.exists() else 0

        oldest = format_date(coverage.oldest) if coverage.oldest else "—"
        newest = format_date(coverage.newest) if coverage.newest else "—"
        end_note = "已到最早" if coverage.reached_end else "可继续回填"
        console.print(f"userid    : {config.userid}")
        console.print(f"缓存条数  : {coverage.total}")
        has_cookie = bool(os.environ.get("HEIBOX_COOKIE") or config.cookie)
        console.print(f"登录态    : {'已配置 Cookie' if has_cookie else '未配置（匿名）'}")
        console.print(f"图片下载  : {cfgmod.IMAGE_MODE_LABELS[config.image_mode]}")
        console.print(f"时间覆盖  : {oldest} ~ {newest}（{end_note}）")
        console.print(f"最近同步  : {human_ago(coverage.last_sync_at)}")
        console.print(f"数据库    : {database}（{size_kb:.0f} KB）")
    except (HeiboxError, OSError) as exc:
        _fail(exc)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
