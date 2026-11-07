"""终端展示：Rich 表格、超链接、JSON。"""

from __future__ import annotations

from typing import Iterable

from rich.box import SIMPLE_HEAD
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .models import Coverage, Post
from .ranges import format_date, format_ts
from .text import clip, human_ago  # noqa: F401 - human_ago 由 cli 从这里导入

SNIPPET_LIMIT = 48


def render_posts(
    console: Console,
    *,
    posts: Iterable[Post],
    total: int,
    coverage: Coverage,
    page: int,
    limit: int,
    start: int,
    end: int,
) -> None:
    if total == 0:
        console.print(_empty_reason(coverage, start, end))
        return

    console.print(_header(total, coverage, start, end))

    table = Table(box=SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
    table.add_column("时间", style="cyan", no_wrap=True, width=16)
    table.add_column("标题", width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("摘要", overflow="fold")

    for post in posts:
        title = escape(post.title) or "[dim](无标题)[/dim]"
        if post.url:
            title = f"[link={post.url}]{title}[/link]"
        table.add_row(
            format_ts(post.create_at), title, escape(clip(post.description, SNIPPET_LIMIT))
        )


    console.print(table)
    pages = max(1, (total + limit - 1) // limit)
    console.print(f"[dim]第 {page}/{pages} 页 · 每页 {limit} 条 · 共 {total} 条[/dim]")


def posts_as_json(posts: Iterable[Post]) -> list[dict]:
    return [
        {
            "linkid": post.linkid,
            "time": format_ts(post.create_at),
            "timestamp": post.create_at,
            "title": post.title,
            "description": post.description,
            "url": post.url,
            "topics": list(post.topics),
            "click": post.click,
            "like_num": post.like_num,
            "comment_num": post.comment_num,
        }
        for post in posts
    ]


def _header(total: int, coverage: Coverage, start: int, end: int) -> str:
    if coverage.oldest and coverage.newest:
        cached = f"{format_date(coverage.oldest)} ~ {format_date(coverage.newest)}"
    else:
        cached = "空"
    return (
        f"[bold]{_range_label(start, end)}[/bold] · 命中 [bold]{total}[/bold] 条"
        f" · 缓存覆盖 {cached} · {human_ago(coverage.last_sync_at)}"
    )


def _empty_reason(coverage: Coverage, start: int, end: int) -> str:
    if coverage.total == 0:
        return "本地还没有缓存，先运行 [bold]heibox sync --full[/bold]"
    if coverage.oldest is not None and end < coverage.oldest:
        return f"该范围早于本地最早缓存（{format_date(coverage.oldest)}），可运行 heibox sync --full 继续回填"
    if coverage.newest is not None and start > coverage.newest:
        return f"该范围晚于最新一条（{format_date(coverage.newest)}），这个时间段内没有帖子"
    return "这个时间段内没有帖子"


def _range_label(start: int, end: int) -> str:
    start_date = format_date(start)
    end_date = format_date(end)
    return start_date if start_date == end_date else f"{start_date} ~ {end_date}"
