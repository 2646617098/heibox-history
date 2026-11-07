"""日期文本 -> Unix 秒区间。

支持三种粒度：2024（整年）、2024-03（整月）、2024-03-05（整天）。
统一按 UTC+8 解释（中国无夏令时，固定偏移足够，也避免 Windows 缺 IANA 时区库）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")

_ONE_MICROSECOND = timedelta(microseconds=1)
_ONE_DAY = timedelta(days=1)


def parse_period(text: str) -> tuple[datetime, datetime]:
    """把 2024 / 2024-03 / 2024-03-05 解析成闭区间的起止时刻。"""
    text = text.strip()
    match = _DAY.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            start = datetime(year, month, day, tzinfo=TZ)
        except ValueError:
            raise ValueError(f"日期不存在：{text}") from None
        return start, start + _ONE_DAY - _ONE_MICROSECOND

    match = _MONTH.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"月份不存在：{text}")
        start = datetime(year, month, 1, tzinfo=TZ)
        if month == 12:
            next_start = datetime(year + 1, 1, 1, tzinfo=TZ)
        else:
            next_start = datetime(year, month + 1, 1, tzinfo=TZ)
        return start, next_start - _ONE_MICROSECOND

    match = _YEAR.match(text)
    if match:
        year = int(match.group(1))
        return datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ) - _ONE_MICROSECOND

    raise ValueError(f"无法识别的日期：{text}（示例：2024、2024-03、2024-03-05）")


def parse_range(
    from_text: str | None,
    to_text: str | None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """返回 [start, end] 两个 Unix 秒；缺省侧会自动补全，未来时间截断到 now。"""
    current = now or datetime.now(TZ)

    if from_text and to_text:
        start, _ = parse_period(from_text)
        _, end = parse_period(to_text)
        if start > end:
            raise ValueError(f"起始日期 {from_text} 晚于结束日期 {to_text}")
    elif from_text:
        start, _ = parse_period(from_text)
        end = current
    elif to_text:
        _, end = parse_period(to_text)
        start = datetime(1970, 1, 1, tzinfo=TZ)
    else:
        start = datetime(1970, 1, 1, tzinfo=TZ)
        end = current

    if start > current:
        start = current
    if end > current:
        end = current
    if start > end:
        start = end

    return int(start.timestamp()), int(end.timestamp())


def format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")


def format_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
