"""与界面无关的文本/时间格式化。

终端（rich）和 tkinter 界面共用这几个函数，所以这里不能引入任何 UI 依赖：
打包 EXE 时少带一个 rich，也就少几 MB。
"""

from __future__ import annotations

import time

from .models import Coverage
from .ranges import format_date


def human_ago(ts: int | None, now: int | None = None) -> str:
    if not ts:
        return "从未同步"
    current = now if now is not None else int(time.time())
    delta = max(0, current - ts)
    if delta < 60:
        return "刚刚同步"
    if delta < 3600:
        return f"{delta // 60} 分钟前同步"
    if delta < 86400:
        return f"{delta // 3600} 小时前同步"
    return f"{delta // 86400} 天前同步"


def clip(text: str, limit: int) -> str:
    """压成一行的摘要，超长截断。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def coverage_line(coverage: Coverage) -> str:
    """一行说明本地缓存覆盖情况，塞在状态栏里。"""
    if not coverage.total:
        return "本地还没有缓存"
    oldest = format_date(coverage.oldest) if coverage.oldest else "—"
    newest = format_date(coverage.newest) if coverage.newest else "—"
    end_note = "已到最早" if coverage.reached_end else "可继续回填"
    return (
        f"缓存 {coverage.total:,} 条 · 覆盖 {oldest} ~ {newest}（{end_note}）"
        f" · {human_ago(coverage.last_sync_at)}"
    )


def empty_reason(coverage: Coverage, start: int, end: int) -> str:
    """没有结果时区分「没缓存 / 早于缓存 / 真没发帖」三种情况。"""
    if coverage.total == 0:
        return "本地还没有缓存，先点「同步最新」抓一次。"
    if coverage.oldest is not None and end < coverage.oldest:
        return (
            f"该范围早于本地最早缓存（{format_date(coverage.oldest)}），"
            "点「补全更早」继续往前回填。"
        )
    if coverage.newest is not None and start > coverage.newest:
        return f"该范围晚于最新一条（{format_date(coverage.newest)}），这段时间没有帖子。"
    return "这个时间段内没有帖子。"
