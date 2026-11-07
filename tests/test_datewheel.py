"""滚轮日期选择的纯逻辑：不创建 Tk 窗口。"""

from datetime import date

import pytest

from heibox import datewheel


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2024, 2, 29),
        (2023, 2, 28),
        (2024, 4, 30),
        (2024, 12, 31),
        (2100, 2, 28),
    ],
)
def test_days_in_month(year: int, month: int, expected: int) -> None:
    assert datewheel.days_in_month(year, month) == expected


@pytest.mark.parametrize(
    ("index", "count", "expected"),
    [
        (-3, 5, 0),
        (0, 5, 0),
        (2, 5, 2),
        (4, 5, 4),
        (9, 5, 4),
        (3, 0, 0),
    ],
)
def test_clamp_index(index: int, count: int, expected: int) -> None:
    assert datewheel.clamp_index(index, count) == expected


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0.0, 0), (0.4, 0), (0.5, 0), (0.6, 1), (-0.6, -1), (-1.4, -1)],
)
def test_snap_offset_rounds_to_nearest_row(offset: float, expected: int) -> None:
    assert datewheel.snap_offset(offset) == expected


def test_parse_iso_date_falls_back_on_blank_or_bad_text() -> None:
    fallback = date(2024, 1, 1)
    assert datewheel.parse_iso_date("", fallback) == fallback
    assert datewheel.parse_iso_date("2024/03/05", fallback) == fallback
    assert datewheel.parse_iso_date("2024-03-05", fallback) == date(2024, 3, 5)


def test_iso_date_is_zero_padded() -> None:
    assert datewheel.iso_date(date(2024, 3, 5)) == "2024-03-05"
