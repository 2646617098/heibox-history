"""GUI 的纯逻辑部分：不创建 Tk 窗口，只测能脱离界面验证的换算。"""

from datetime import datetime

import pytest

from heibox import gui
from heibox.ranges import TZ, parse_range


def test_parse_range_inputs_matches_cli_semantics():
    assert gui.parse_range_inputs("2024-03", "") == parse_range("2024-03", None)
    assert gui.parse_range_inputs("2024", "2024") == parse_range("2024", "2024")


def test_parse_range_inputs_treats_blank_as_unbounded():
    now = datetime(2024, 6, 1, 12, tzinfo=TZ)
    start, end = gui.parse_range_inputs("  ", "", now=now)
    assert start == int(datetime(1970, 1, 1, tzinfo=TZ).timestamp())
    assert end == int(now.timestamp())


def test_parse_range_inputs_rejects_unknown_text():
    with pytest.raises(ValueError):
        gui.parse_range_inputs("2024-13", "")


@pytest.mark.parametrize(
    ("total", "expected"),
    [(0, 1), (1, 1), (gui.PAGE_SIZE, 1), (gui.PAGE_SIZE + 1, 2), (gui.PAGE_SIZE * 3, 3)],
)
def test_page_count(total: int, expected: int) -> None:
    assert gui.page_count(total) == expected


def test_range_text_shows_unbounded_and_full_ranges() -> None:
    assert gui.range_text("", "") == "全部时间"
    assert gui.range_text(" ", "  ") == "全部时间"
    assert gui.range_text("2024-01-01", "2024-12-31") == "2024-01-01 ~ 2024-12-31"
    assert gui.range_text("2024-01-01", "") == "2024-01-01 ~ 不限"
    assert gui.range_text("", "2024-12-31") == "不限 ~ 2024-12-31"


def test_range_label_collapses_single_day():
    day = int(datetime(2024, 3, 5, tzinfo=TZ).timestamp())
    assert gui.range_label(day, day + 3600) == "2024-03-05"
    assert gui.range_label(day, day + 86400 * 3) == "2024-03-05 ~ 2024-03-08"


def test_image_mode_text_has_short_labels():
    assert gui.image_mode_text("off") == "不下载"
    assert gui.image_mode_text("thumb") == "缩略图"
    assert gui.image_mode_text("full") == "缩略+原图"
    assert gui.image_mode_text("unknown") == "不下载"
