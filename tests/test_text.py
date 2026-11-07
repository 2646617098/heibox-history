from datetime import datetime

from heibox.models import Coverage
from heibox.ranges import TZ
from heibox.text import clip, coverage_line, empty_reason, human_ago


def _ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=TZ).timestamp())


def test_human_ago_without_sync():
    assert human_ago(None) == "从未同步"
    assert human_ago(0) == "从未同步"


def test_human_ago_buckets():
    now = 1_700_000_000
    assert human_ago(now - 10, now) == "刚刚同步"
    assert human_ago(now - 120, now) == "2 分钟前同步"
    assert human_ago(now - 7200, now) == "2 小时前同步"
    assert human_ago(now - 3 * 86400, now) == "3 天前同步"


def test_clip_collapses_whitespace_and_truncates():
    assert clip("  a \n\t b  ", 20) == "a b"
    assert clip("abcdef", 4) == "abc…"
    assert clip("abcdef", 6) == "abcdef"


def test_coverage_line_handles_empty_cache():
    coverage = Coverage(
        oldest=None, newest=None, reached_end=False, total=0, last_sync_at=None
    )
    assert coverage_line(coverage) == "本地还没有缓存"


def test_coverage_line_reports_bounds_and_backfill_state():
    coverage = Coverage(
        oldest=_ts(2021, 3, 5),
        newest=_ts(2025, 11, 1),
        reached_end=False,
        total=1284,
        last_sync_at=1_700_000_000,
    )
    line = coverage_line(coverage)
    assert "缓存 1,284 条" in line
    assert "2021-03-05 ~ 2025-11-01" in line
    assert "可继续回填" in line


def test_empty_reason_distinguishes_three_cases():
    empty = Coverage(oldest=None, newest=None, reached_end=False, total=0, last_sync_at=None)
    assert "还没有缓存" in empty_reason(empty, 0, 1_700_000_000)

    cached = Coverage(
        oldest=_ts(2022, 1, 1),
        newest=_ts(2024, 1, 1),
        reached_end=False,
        total=10,
        last_sync_at=None,
    )
    too_early = empty_reason(cached, _ts(2019, 1, 1), _ts(2019, 12, 31))
    assert "早于本地最早缓存" in too_early
    assert "全量同步" in too_early

    too_late = empty_reason(cached, _ts(2025, 1, 1), _ts(2025, 12, 31))
    assert "晚于最新一条" in too_late

    gap = empty_reason(cached, _ts(2023, 1, 1), _ts(2023, 2, 1))
    assert gap == "这个时间段内没有帖子。"
