from datetime import datetime

import pytest

from heibox.ranges import TZ, parse_period, parse_range


def dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, micro: int = 0):
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=TZ)


def test_day_is_closed_interval():
    start, end = parse_period("2024-03-05")
    assert start == dt(2024, 3, 5)
    assert end == dt(2024, 3, 5, 23, 59, 59, 999999)


def test_month_handles_leap_year():
    start, end = parse_period("2024-02")
    assert start == dt(2024, 2, 1)
    assert end == dt(2024, 2, 29, 23, 59, 59, 999999)


def test_month_december_rolls_to_next_year():
    start, end = parse_period("2024-12")
    assert start == dt(2024, 12, 1)
    assert end == dt(2024, 12, 31, 23, 59, 59, 999999)


def test_year():
    start, end = parse_period("2023")
    assert start == dt(2023, 1, 1)
    assert end == dt(2023, 12, 31, 23, 59, 59, 999999)


def test_range_between_days():
    start, end = parse_range("2024-01-01", "2024-03-31", now=dt(2026, 1, 1))
    assert start == int(dt(2024, 1, 1).timestamp())
    assert end == int(dt(2024, 3, 31, 23, 59, 59).timestamp())


def test_only_from_uses_now():
    now = dt(2024, 6, 1, 12, 0, 0)
    start, end = parse_range("2024-05-01", None, now=now)
    assert start == int(dt(2024, 5, 1).timestamp())
    assert end == int(now.timestamp())


def test_only_to_starts_from_epoch():
    start, end = parse_range(None, "2024-01-31", now=dt(2026, 1, 1))
    assert start == int(dt(1970, 1, 1).timestamp())
    assert end == int(dt(2024, 1, 31, 23, 59, 59).timestamp())


def test_future_is_clamped_to_now():
    now = dt(2024, 1, 1, 12, 0, 0)
    start, end = parse_range("2030-01-01", None, now=now)
    assert start == end == int(now.timestamp())


def test_from_after_to_is_rejected():
    with pytest.raises(ValueError):
        parse_range("2024-05-01", "2024-04-01")


def test_invalid_text_is_rejected():
    for text in ("2024-13", "2024-02-30", "昨天", "2024/01/01"):
        with pytest.raises(ValueError):
            parse_period(text)
