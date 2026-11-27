"""Independent tests for ``gie.core.timeutil`` (requirements S00-ASG-20/21, G05-10).

Expected values are derived from ``specifications/SHARED_CONTRACTS.md`` sections 2 and 3.4 and
from the civil-time rules of the Czech Republic, not from the implementation:

* instants are ``int`` nanoseconds since 1970-01-01T00:00:00Z in the range ``0 .. 2**63-1``;
* the trading day is the calendar date in ``Europe/Prague``; a day is the half-open interval
  from Prague midnight to the next Prague midnight;
* since 1996 Prague follows the EU summer-time rule: UTC+2 from 01:00 UTC on the last Sunday of
  March until 01:00 UTC on the last Sunday of October, UTC+1 otherwise.  In 2026 the changes
  are on 2026-03-29 and 2026-10-25.

The oracles below use only ``calendar`` and plain integer arithmetic in UTC (no time-zone
database), so they are independent of the code under test.  The 2026 boundary instants were
additionally checked once with the operating system's ``date`` command.

Revision 2 of the contracts (after the independent review) documents the limits of the range:
the first (1970-01-01) and the last (2262-04-12) representable trading day begin respectively
end outside ``0 .. 2**63-1``, so ``trading_day_bounds_ns`` clamps those two bounds to the range
and every day returned by ``trading_day`` has bounds.  Days outside the range are not days of any
instant; section 5 ("never repair input ... never clamped") applies to them.

All instants are synthetic fixtures; none is market data.  Nothing here reads the wall clock.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import ContractValidationError, GieError
from gie.core.timeutil import (
    MAX_TIME_NS,
    NS_PER_HOUR,
    NS_PER_MINUTE,
    NS_PER_MS,
    NS_PER_SECOND,
    NS_PER_US,
    TRADING_DAY_PATTERN,
    TRADING_DAY_TZ_NAME,
    iso_utc,
    parse_trading_day,
    time_ns_from_datetime,
    trading_day,
    trading_day_bounds_ns,
    utc_datetime,
)

PROPERTY = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

SECOND = 1_000_000_000
HOUR = 3600 * SECOND
INT64_MAX = 2**63 - 1
ONE_DAY = dt.timedelta(days=1)
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


# =========================================================================== independent oracles
def _utc_ns(*fields: int) -> int:
    """``_utc_ns(year, month, day[, hour[, minute[, second[, nanosecond]]]])`` as an instant.

    Nanoseconds since the epoch of a UTC civil time, by integer arithmetic only.
    """
    assert 3 <= len(fields) <= 7
    year, month, day, hour, minute, second, nanosecond = (*fields, 0, 0, 0, 0)[:7]
    return calendar.timegm((year, month, day, hour, minute, second)) * SECOND + nanosecond


def _last_sunday(year: int, month: int) -> int:
    """Day of month of the last Sunday of *month*."""
    last_day = calendar.monthrange(year, month)[1]
    weekday = calendar.weekday(year, month, last_day)  # Monday == 0 ... Sunday == 6
    return last_day - (weekday + 1) % 7


def _prague_utc_offset_hours(utc_seconds: int) -> int:
    """EU summer-time rule (valid for 1996 and later)."""
    year = (EPOCH + dt.timedelta(seconds=utc_seconds)).year
    summer_start = calendar.timegm((year, 3, _last_sunday(year, 3), 1, 0, 0))
    summer_end = calendar.timegm((year, 10, _last_sunday(year, 10), 1, 0, 0))
    return 2 if summer_start <= utc_seconds < summer_end else 1


def _expected_trading_day(time_ns: int) -> str:
    seconds = time_ns // SECOND
    local = EPOCH + dt.timedelta(seconds=seconds + 3600 * _prague_utc_offset_hours(seconds))
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def _expected_day_hours(date: dt.date) -> int:
    if date.month == 3 and date.day == _last_sunday(date.year, 3):
        return 23
    if date.month == 10 and date.day == _last_sunday(date.year, 10):
        return 25
    return 24


def _expected_start_ns(date: dt.date) -> int:
    """Prague midnight of *date* (1996 or later) as a UTC instant."""
    spring = dt.date(date.year, 3, _last_sunday(date.year, 3))
    autumn = dt.date(date.year, 10, _last_sunday(date.year, 10))
    # at 00:00 local time the change of that day (02:00/03:00 local) has not happened yet
    offset_hours = 2 if spring < date <= autumn else 1
    return _utc_ns(date.year, date.month, date.day) - offset_hours * HOUR


@pytest.mark.requirement("G05-10")
def test_oracle_self_check_for_2026() -> None:
    """The independent oracle agrees with the published 2026 change dates."""
    assert _last_sunday(2026, 3) == 29
    assert _last_sunday(2026, 10) == 25
    assert _expected_day_hours(dt.date(2026, 3, 29)) == 23
    assert _expected_day_hours(dt.date(2026, 10, 25)) == 25
    assert _expected_start_ns(dt.date(2026, 3, 29)) == _utc_ns(2026, 3, 28, 23)
    assert _expected_start_ns(dt.date(2026, 3, 30)) == _utc_ns(2026, 3, 29, 22)
    assert _expected_start_ns(dt.date(2026, 10, 25)) == _utc_ns(2026, 10, 24, 22)
    assert _expected_start_ns(dt.date(2026, 10, 26)) == _utc_ns(2026, 10, 25, 23)
    assert _utc_ns(2026, 11, 27, 9, 30, 15) == 1795771815 * SECOND  # checked with `date -u -d ... +%s`


# =========================================================================== units
@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
def test_unit_constants() -> None:
    assert NS_PER_US == 1_000
    assert NS_PER_MS == 1_000_000
    assert NS_PER_SECOND == 1_000_000_000
    assert NS_PER_MINUTE == 60_000_000_000
    assert NS_PER_HOUR == 3_600_000_000_000
    assert MAX_TIME_NS == 9223372036854775807
    assert TRADING_DAY_TZ_NAME == "Europe/Prague"
    for constant in (NS_PER_US, NS_PER_MS, NS_PER_SECOND, NS_PER_MINUTE, NS_PER_HOUR, MAX_TIME_NS):
        assert type(constant) is int


# =========================================================================== trading_day: fixed instants
#: (instant, expected trading day, what the case shows)
TRADING_DAY_CASES = [
    # winter time, UTC+1: the day changes at 23:00 UTC
    (_utc_ns(2026, 1, 14, 22, 59, 59, 999_999_999), "2026-01-14", "winter: last ns before midnight"),
    (_utc_ns(2026, 1, 14, 23), "2026-01-15", "winter: exactly Prague midnight"),
    (_utc_ns(2026, 1, 14, 23, 0, 0, 1), "2026-01-15", "winter: first ns after midnight"),
    (_utc_ns(2026, 1, 15), "2026-01-15", "winter: UTC midnight"),
    (_utc_ns(2026, 1, 15, 12), "2026-01-15", "winter: noon"),
    (_utc_ns(2026, 1, 15, 22, 59, 59, 999_999_999), "2026-01-15", "winter: last ns of the day"),
    (_utc_ns(2026, 1, 15, 23), "2026-01-16", "winter: next midnight"),
    (_utc_ns(2026, 1, 15, 22, 30), "2026-01-15", "winter: 23:30 local, UTC+2 would be wrong"),
    # summer time, UTC+2: the day changes at 22:00 UTC
    (_utc_ns(2026, 7, 14, 21, 59, 59, 999_999_999), "2026-07-14", "summer: last ns before midnight"),
    (_utc_ns(2026, 7, 14, 22), "2026-07-15", "summer: exactly Prague midnight"),
    (_utc_ns(2026, 7, 14, 22, 0, 0, 1), "2026-07-15", "summer: first ns after midnight"),
    (_utc_ns(2026, 7, 14, 22, 30), "2026-07-15", "summer: 00:30 local, UTC+1 would be wrong"),
    (_utc_ns(2026, 7, 14, 23, 30), "2026-07-15", "summer: UTC date is still the 14th"),
    (_utc_ns(2026, 7, 15, 21, 59, 59, 999_999_999), "2026-07-15", "summer: last ns of the day"),
    (_utc_ns(2026, 7, 15, 22), "2026-07-16", "summer: next midnight"),
    # 2026-03-29: clocks go from 02:00 CET to 03:00 CEST at 01:00 UTC; the day has 23 hours
    (_utc_ns(2026, 3, 28, 22, 59, 59, 999_999_999), "2026-03-28", "spring: last ns of the 28th"),
    (_utc_ns(2026, 3, 28, 23), "2026-03-29", "spring: midnight is still UTC+1"),
    (_utc_ns(2026, 3, 29, 0, 59, 59, 999_999_999), "2026-03-29", "spring: last ns of CET"),
    (_utc_ns(2026, 3, 29, 1), "2026-03-29", "spring: first instant of CEST"),
    (_utc_ns(2026, 3, 29, 21, 59, 59, 999_999_999), "2026-03-29", "spring: last ns of the short day"),
    (_utc_ns(2026, 3, 29, 22), "2026-03-30", "spring: next midnight is UTC+2"),
    (_utc_ns(2026, 3, 29, 22, 30), "2026-03-30", "spring: 00:30 CEST, UTC+1 would be wrong"),
    # 2026-10-25: clocks go from 03:00 CEST back to 02:00 CET at 01:00 UTC; the day has 25 hours
    (_utc_ns(2026, 10, 24, 21, 59, 59, 999_999_999), "2026-10-24", "autumn: last ns of the 24th"),
    (_utc_ns(2026, 10, 24, 22), "2026-10-25", "autumn: midnight is still UTC+2"),
    (_utc_ns(2026, 10, 25, 0, 30), "2026-10-25", "autumn: first 02:30 (CEST)"),
    (_utc_ns(2026, 10, 25, 1, 30), "2026-10-25", "autumn: second 02:30 (CET)"),
    (_utc_ns(2026, 10, 25, 22), "2026-10-25", "autumn: 23:00 CET, UTC+2 would be wrong"),
    (_utc_ns(2026, 10, 25, 22, 59, 59, 999_999_999), "2026-10-25", "autumn: last ns of the long day"),
    (_utc_ns(2026, 10, 25, 23), "2026-10-26", "autumn: next midnight is UTC+1"),
    # year change and the limits of the instant range
    (_utc_ns(2026, 12, 31, 22, 59, 59, 999_999_999), "2026-12-31", "new year: last ns of 2026"),
    (_utc_ns(2026, 12, 31, 23), "2027-01-01", "new year: Prague midnight"),
    (0, "1970-01-01", "epoch: 01:00 in Prague"),
    (INT64_MAX, "2262-04-12", "largest instant: 2262-04-11T23:47:16Z is 01:47 CEST"),
]


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@pytest.mark.parametrize(
    ("time_ns", "expected", "why"), TRADING_DAY_CASES, ids=[case[2] for case in TRADING_DAY_CASES]
)
def test_trading_day_of_fixed_instants(time_ns: int, expected: str, why: str) -> None:
    result = trading_day(time_ns)
    assert result == expected, why
    assert type(result) is str


# =========================================================================== bounds: fixed days
#: (day, start instant, end instant, length in hours)
BOUNDS_CASES = [
    ("2026-01-15", _utc_ns(2026, 1, 14, 23), _utc_ns(2026, 1, 15, 23), 24),
    ("2026-07-15", _utc_ns(2026, 7, 14, 22), _utc_ns(2026, 7, 15, 22), 24),
    ("2026-03-28", _utc_ns(2026, 3, 27, 23), _utc_ns(2026, 3, 28, 23), 24),
    ("2026-03-29", _utc_ns(2026, 3, 28, 23), _utc_ns(2026, 3, 29, 22), 23),
    ("2026-03-30", _utc_ns(2026, 3, 29, 22), _utc_ns(2026, 3, 30, 22), 24),
    ("2026-10-24", _utc_ns(2026, 10, 23, 22), _utc_ns(2026, 10, 24, 22), 24),
    ("2026-10-25", _utc_ns(2026, 10, 24, 22), _utc_ns(2026, 10, 25, 23), 25),
    ("2026-10-26", _utc_ns(2026, 10, 25, 23), _utc_ns(2026, 10, 26, 23), 24),
    ("2026-11-27", _utc_ns(2026, 11, 26, 23), _utc_ns(2026, 11, 27, 23), 24),
    ("2024-02-29", _utc_ns(2024, 2, 28, 23), _utc_ns(2024, 2, 29, 23), 24),
    ("2026-12-31", _utc_ns(2026, 12, 30, 23), _utc_ns(2026, 12, 31, 23), 24),
]


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@pytest.mark.parametrize(
    ("day", "start", "end", "hours"), BOUNDS_CASES, ids=[case[0] for case in BOUNDS_CASES]
)
def test_trading_day_bounds_of_fixed_days(day: str, start: int, end: int, hours: int) -> None:
    bounds = trading_day_bounds_ns(day)
    assert bounds == (start, end)
    assert type(bounds[0]) is int
    assert type(bounds[1]) is int
    assert bounds[1] - bounds[0] == hours * HOUR
    # half-open interval: the start belongs to the day, the end to the next one
    assert trading_day(start) == day
    assert trading_day(end - 1) == day
    assert trading_day(end) != day
    assert trading_day(start - 1) != day


@pytest.mark.requirement("G05-10")
def test_exact_instants_of_the_2026_dst_days() -> None:
    assert trading_day_bounds_ns("2026-03-29") == (1774738800 * SECOND, 1774821600 * SECOND)
    assert trading_day_bounds_ns("2026-10-25") == (1792879200 * SECOND, 1792969200 * SECOND)
    assert (1774821600 - 1774738800) == 23 * 3600
    assert (1792969200 - 1792879200) == 25 * 3600


@pytest.mark.requirement("G05-10")
@pytest.mark.parametrize("year", [2025, 2026, 2027, 2028])
def test_days_of_a_year_are_contiguous_and_have_23_24_or_25_hours(year: int) -> None:
    day = dt.date(year, 1, 1)
    lengths: Counter[int] = Counter()
    short_days: list[str] = []
    long_days: list[str] = []
    first_start: int | None = None
    previous_end: int | None = None
    while day.year == year:
        label = day.isoformat()
        start, end = trading_day_bounds_ns(label)
        assert start < end
        if previous_end is None:
            first_start = start
        else:
            assert start == previous_end, label  # no gap and no overlap between consecutive days
        hours, remainder = divmod(end - start, HOUR)
        assert remainder == 0
        lengths[hours] += 1
        if hours == 23:
            short_days.append(label)
        if hours == 25:
            long_days.append(label)
        previous_end = end
        day += ONE_DAY
    days_in_year = 366 if calendar.isleap(year) else 365
    assert lengths == {24: days_in_year - 2, 23: 1, 25: 1}
    assert short_days == [dt.date(year, 3, _last_sunday(year, 3)).isoformat()]
    assert long_days == [dt.date(year, 10, _last_sunday(year, 10)).isoformat()]
    # 1 January is in winter time on both sides, so the year spans a whole number of 24 h days
    assert first_start == _utc_ns(year - 1, 12, 31, 23)
    assert previous_end == _utc_ns(year, 12, 31, 23)


# =========================================================================== properties
_EU_RULE_DAYS = st.dates(min_value=dt.date(1996, 1, 1), max_value=dt.date(2262, 4, 11))


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@PROPERTY
@example(dt.date(2026, 3, 29))
@example(dt.date(2026, 10, 25))
@example(dt.date(2026, 3, 28))
@example(dt.date(2026, 10, 26))
@example(dt.date(1996, 1, 1))
@example(dt.date(2262, 4, 10))
@example(dt.date(2262, 4, 11))  # the last day whose end is still an instant
@example(dt.date(2038, 1, 19))
@given(_EU_RULE_DAYS)
def test_bounds_and_trading_day_agree_for_any_day(date: dt.date) -> None:
    day = date.isoformat()
    next_day = (date + ONE_DAY).isoformat()
    previous_day = (date - ONE_DAY).isoformat()
    start, end = trading_day_bounds_ns(day)

    assert start == _expected_start_ns(date)
    assert end - start == _expected_day_hours(date) * HOUR
    assert end == _expected_start_ns(date + ONE_DAY)

    assert trading_day(start) == day
    assert trading_day(end - 1) == day
    assert trading_day((start + end) // 2) == day
    assert trading_day(end) == next_day
    assert trading_day(start - 1) == previous_day

    # contiguous with both neighbours
    assert trading_day_bounds_ns(next_day)[0] == end
    assert trading_day_bounds_ns(previous_day)[1] == start


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@PROPERTY
@given(st.dates(min_value=dt.date(1970, 1, 2), max_value=dt.date(1995, 12, 31)))
def test_bounds_and_trading_day_agree_before_the_eu_rule(date: dt.date) -> None:
    """1970-1995 (other summer-time rules): only rule-independent facts are asserted."""
    day = date.isoformat()
    start, end = trading_day_bounds_ns(day)
    assert (end - start) in (23 * HOUR, 24 * HOUR, 25 * HOUR)
    assert trading_day(start) == day
    assert trading_day(end - 1) == day
    assert trading_day(end) == (date + ONE_DAY).isoformat()
    assert trading_day_bounds_ns((date + ONE_DAY).isoformat())[0] == end


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@PROPERTY
@example(_utc_ns(2026, 3, 29, 1) - 1)
@example(_utc_ns(2026, 3, 29, 1))
@example(_utc_ns(2026, 10, 25, 1) - 1)
@example(_utc_ns(2026, 10, 25, 1))
@given(st.integers(min_value=_utc_ns(1996, 1, 1), max_value=_utc_ns(2262, 4, 10)))
def test_trading_day_matches_the_eu_rule_oracle(time_ns: int) -> None:
    day = trading_day(time_ns)
    assert day == _expected_trading_day(time_ns)
    assert re.fullmatch(TRADING_DAY_PATTERN, day)
    start, end = trading_day_bounds_ns(day)
    assert start <= time_ns < end
    assert parse_trading_day(day).isoformat() == day


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@PROPERTY
@given(
    st.integers(min_value=_utc_ns(1970, 1, 2), max_value=_utc_ns(2262, 4, 10)),
    st.integers(min_value=0, max_value=3 * 24 * HOUR),
)
def test_trading_day_never_goes_backwards(time_ns: int, delta: int) -> None:
    later = min(time_ns + delta, INT64_MAX)
    earlier_day = trading_day(time_ns)
    later_day = trading_day(later)
    assert earlier_day <= later_day  # ISO dates order like the dates themselves
    start, end = trading_day_bounds_ns(earlier_day)
    assert start <= time_ns < end
    assert (later < end) == (later_day == earlier_day)


# =========================================================================== parse_trading_day
@pytest.mark.requirement("G05-10", "S00-ASG-20")
@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("2026-11-27", dt.date(2026, 11, 27)),
        ("2026-03-29", dt.date(2026, 3, 29)),
        ("2024-02-29", dt.date(2024, 2, 29)),
        ("2000-02-29", dt.date(2000, 2, 29)),
        ("1970-01-01", dt.date(1970, 1, 1)),
        ("2026-12-31", dt.date(2026, 12, 31)),
    ],
)
def test_parse_trading_day_accepts_real_dates(label: str, expected: dt.date) -> None:
    parsed = parse_trading_day(label)
    assert parsed == expected
    assert type(parsed) is dt.date
    assert re.fullmatch(TRADING_DAY_PATTERN, label)


_BAD_DAY_LABELS: list[Any] = [
    "2026-02-30",
    "2026-02-29",  # 2026 is not a leap year
    "2025-02-29",
    "1900-02-29",  # century rule
    "2026-04-31",
    "2026-13-01",
    "2026-00-10",
    "2026-01-00",
    "2026-01-32",
    "0000-01-01",
    "2026-1-5",
    "2026-01-5",
    "26-01-05",
    "20260105",
    "2026/01/05",
    "2026.01.05",
    "05-01-2026",
    "2026-01-05T00:00:00",
    "2026-01-05Z",
    "2026-01-05+01:00",
    "2026-W01-1",
    "2026-005",
    "+2026-01-05",
    "-2026-01-05",
    " 2026-01-05",
    "2026-01-05 ",
    "2026-01-05\n",
    "\n2026-01-05",
    "2026-01-05\t",
    "",
    "today",
    "abcd-ef-gh",
    "\uff12\uff10\uff12\uff16-01-05",  # full-width digits
    "2026-01-0\u0665",  # Arabic-Indic digit
    None,
    20260105,
    2026.0105,
    True,
    dt.date(2026, 1, 5),
    dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
    b"2026-01-05",
    ["2026-01-05"],
]


@pytest.mark.requirement("G05-10", "S00-ASG-24")
@pytest.mark.parametrize("label", _BAD_DAY_LABELS, ids=repr)
def test_parse_trading_day_rejects_everything_else(label: Any) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        parse_trading_day(label)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"
    with pytest.raises(ContractValidationError):
        trading_day_bounds_ns(label)


@pytest.mark.requirement("G05-10", "S00-ASG-20")
def test_trading_day_pattern_describes_the_label_format() -> None:
    for label in ("2026-11-27", "1970-01-01", "2262-04-11", "2026-10-31", "2026-02-28"):
        assert re.fullmatch(TRADING_DAY_PATTERN, label), label
    for label in (
        "2026-13-01",
        "2026-00-10",
        "2026-01-00",
        "2026-01-32",
        "2026-1-1",
        "20260101",
        "2026-01-01 ",
    ):
        assert re.fullmatch(TRADING_DAY_PATTERN, label) is None, label


# =========================================================================== datetime conversion
@pytest.mark.requirement("S00-ASG-21")
def test_utc_datetime_of_known_instants() -> None:
    assert utc_datetime(0) == EPOCH
    moment = utc_datetime(_utc_ns(2026, 11, 27, 9, 30, 15, 123_456_789))
    assert moment == dt.datetime(2026, 11, 27, 9, 30, 15, 123_456, tzinfo=dt.UTC)
    assert moment.tzinfo is not None
    assert moment.utcoffset() == dt.timedelta(0)
    assert (moment.year, moment.month, moment.day, moment.hour) == (2026, 11, 27, 9)  # UTC fields, not local
    assert utc_datetime(INT64_MAX) == dt.datetime(2262, 4, 11, 23, 47, 16, 854_775, tzinfo=dt.UTC)


@pytest.mark.requirement("S00-ASG-21")
@pytest.mark.parametrize(
    ("time_ns", "microsecond", "second"),
    [
        (0, 0, 0),
        (999, 0, 0),
        (1_000, 1, 0),
        (1_999, 1, 0),
        (999_999_000, 999_999, 0),
        (999_999_999, 999_999, 0),  # truncated, never rounded up into the next second
        (1_000_000_000, 0, 1),
        (59_999_999_999, 999_999, 59),
    ],
)
def test_utc_datetime_drops_the_sub_microsecond_part(time_ns: int, microsecond: int, second: int) -> None:
    moment = utc_datetime(time_ns)
    assert moment.microsecond == microsecond
    assert moment.second == second
    assert (moment.year, moment.month, moment.day, moment.hour, moment.minute) == (1970, 1, 1, 0, 0)


@pytest.mark.requirement("S00-ASG-21")
def test_time_ns_from_datetime_of_known_moments() -> None:
    assert time_ns_from_datetime(EPOCH) == 0
    moment = dt.datetime(2026, 11, 27, 9, 30, 15, 123_456, tzinfo=dt.UTC)
    assert time_ns_from_datetime(moment) == _utc_ns(2026, 11, 27, 9, 30, 15, 123_456_000)
    assert type(time_ns_from_datetime(moment)) is int
    last = dt.datetime(2262, 4, 11, 23, 47, 16, 854_775, tzinfo=dt.UTC)
    assert time_ns_from_datetime(last) == 9223372036854775000


@pytest.mark.requirement("S00-ASG-21", "G05-10")
def test_time_ns_from_datetime_honours_the_utc_offset() -> None:
    plus_two = dt.timezone(dt.timedelta(hours=2))
    minus_five = dt.timezone(dt.timedelta(hours=-5))
    odd = dt.timezone(dt.timedelta(hours=5, minutes=45))
    assert time_ns_from_datetime(dt.datetime(2026, 7, 15, 0, 0, tzinfo=plus_two)) == _utc_ns(2026, 7, 14, 22)
    assert time_ns_from_datetime(dt.datetime(2026, 7, 14, 17, 0, tzinfo=minus_five)) == _utc_ns(
        2026, 7, 14, 22
    )
    assert time_ns_from_datetime(dt.datetime(2026, 7, 15, 3, 45, tzinfo=odd)) == _utc_ns(2026, 7, 14, 22)

    prague = ZoneInfo("Europe/Prague")
    assert time_ns_from_datetime(dt.datetime(2026, 1, 15, 0, 0, tzinfo=prague)) == _utc_ns(2026, 1, 14, 23)
    assert time_ns_from_datetime(dt.datetime(2026, 7, 15, 0, 0, tzinfo=prague)) == _utc_ns(2026, 7, 14, 22)
    # the repeated hour of 2026-10-25: 02:30 happens twice and ``fold`` selects the instant
    first = dt.datetime(2026, 10, 25, 2, 30, tzinfo=prague, fold=0)
    second = dt.datetime(2026, 10, 25, 2, 30, tzinfo=prague, fold=1)
    assert time_ns_from_datetime(first) == _utc_ns(2026, 10, 25, 0, 30)
    assert time_ns_from_datetime(second) == _utc_ns(2026, 10, 25, 1, 30)


class _NoOffset(dt.tzinfo):
    """Synthetic tzinfo that declines to give an offset: such a datetime is naive by definition."""

    def utcoffset(self, moment: dt.datetime | None) -> dt.timedelta | None:
        return None

    def dst(self, moment: dt.datetime | None) -> dt.timedelta | None:
        return None

    def tzname(self, moment: dt.datetime | None) -> str | None:
        return "synthetic-no-offset"


@pytest.mark.requirement("S00-ASG-21", "S00-ASG-24")
@pytest.mark.parametrize(
    "moment",
    [
        dt.datetime(2026, 11, 27, 9, 30),
        dt.datetime(1970, 1, 1),
        dt.datetime(2026, 11, 27, 9, 30, tzinfo=_NoOffset()),
    ],
    ids=["naive", "naive-epoch", "tzinfo-without-offset"],
)
def test_naive_datetime_is_rejected(moment: dt.datetime) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        time_ns_from_datetime(moment)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21", "S00-ASG-24")
@pytest.mark.parametrize(
    "moment",
    [
        dt.datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=dt.UTC),
        dt.datetime(1970, 1, 1, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        dt.datetime(2262, 4, 11, 23, 47, 17, tzinfo=dt.UTC),
    ],
    ids=["last-microsecond-of-1969", "before-epoch-by-offset", "first-second-after-max"],
)
def test_time_ns_from_datetime_rejects_moments_outside_the_instant_range(moment: dt.datetime) -> None:
    """An instant is an int in ``0 .. 2**63-1``; a moment outside it has no valid instant."""
    with pytest.raises(ContractValidationError):
        time_ns_from_datetime(moment)


@pytest.mark.requirement("S00-ASG-21")
@PROPERTY
@example(0)
@example(INT64_MAX)
@example(999)
@example(1_000)
@given(st.integers(min_value=0, max_value=INT64_MAX))
def test_instant_to_datetime_round_trip_at_microsecond_precision(time_ns: int) -> None:
    moment = utc_datetime(time_ns)
    assert moment.utcoffset() == dt.timedelta(0)
    assert time_ns_from_datetime(moment) == time_ns - time_ns % 1_000
    # independent decomposition of the instant
    seconds, remainder = divmod(time_ns, SECOND)
    assert moment == EPOCH + dt.timedelta(seconds=seconds, microseconds=remainder // 1_000)


_AWARE_DATETIMES = st.datetimes(
    min_value=dt.datetime(1970, 1, 2),
    max_value=dt.datetime(2262, 4, 10),
    timezones=st.sampled_from(
        [
            dt.UTC,
            dt.timezone(dt.timedelta(hours=1)),
            dt.timezone(dt.timedelta(hours=-11)),
            dt.timezone(dt.timedelta(hours=5, minutes=45)),
        ]
    ),
)


@pytest.mark.requirement("S00-ASG-21")
@PROPERTY
@given(_AWARE_DATETIMES)
def test_datetime_to_instant_round_trip(moment: dt.datetime) -> None:
    time_ns = time_ns_from_datetime(moment)
    assert type(time_ns) is int
    assert time_ns % 1_000 == 0
    assert utc_datetime(time_ns) == moment  # the same instant (aware datetimes compare by instant)
    assert time_ns == (moment - EPOCH) // dt.timedelta(microseconds=1) * 1_000


# =========================================================================== iso_utc
@pytest.mark.requirement("S00-ASG-21")
@pytest.mark.parametrize(
    ("time_ns", "expected"),
    [
        (0, "1970-01-01T00:00:00.000000000Z"),
        (1, "1970-01-01T00:00:00.000000001Z"),
        (999_999_999, "1970-01-01T00:00:00.999999999Z"),
        (1_000_000_000, "1970-01-01T00:00:01.000000000Z"),
        (_utc_ns(2026, 11, 27, 9, 30, 15, 123_456_789), "2026-11-27T09:30:15.123456789Z"),
        (_utc_ns(2026, 11, 27, 9, 30, 15, 100), "2026-11-27T09:30:15.000000100Z"),
        (_utc_ns(2026, 3, 29, 1), "2026-03-29T01:00:00.000000000Z"),  # UTC, unaffected by Prague DST
        (_utc_ns(2026, 7, 14, 22), "2026-07-14T22:00:00.000000000Z"),
        (INT64_MAX, "2262-04-11T23:47:16.854775807Z"),
    ],
)
def test_iso_utc_keeps_all_nine_fractional_digits(time_ns: int, expected: str) -> None:
    assert iso_utc(time_ns) == expected


_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{9})Z")


@pytest.mark.requirement("S00-ASG-21")
@PROPERTY
@given(st.integers(min_value=0, max_value=INT64_MAX))
def test_iso_utc_is_lossless(time_ns: int) -> None:
    text = iso_utc(time_ns)
    match = _ISO_RE.fullmatch(text)
    assert match is not None, text
    year, month, day, hour, minute, second, fraction = (int(group) for group in match.groups())
    assert _utc_ns(year, month, day, hour, minute, second, fraction) == time_ns


# =========================================================================== range and type checks
_TIME_FUNCTIONS: list[Callable[[Any], Any]] = [utc_datetime, iso_utc, trading_day]

_BAD_INSTANTS: list[Any] = [
    -1,
    -(10**18),
    2**63,
    2**63 + 1,
    2**64,
    10**30,
    True,
    False,
    0.0,
    1.0,
    1.5,
    1e18,
    float("nan"),
    "0",
    "1795771815000000000",
    None,
    Decimal("5"),
    b"5",
    [0],
    dt.datetime(2026, 11, 27, tzinfo=dt.UTC),
]


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21", "S00-ASG-24")
@pytest.mark.parametrize("function", _TIME_FUNCTIONS, ids=lambda function: function.__name__)
@pytest.mark.parametrize("value", _BAD_INSTANTS, ids=repr)
def test_invalid_instants_are_rejected(function: Callable[[Any], Any], value: Any) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        function(value)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"
    assert isinstance(excinfo.value, GieError)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
@pytest.mark.parametrize("function", _TIME_FUNCTIONS, ids=lambda function: function.__name__)
@pytest.mark.parametrize("value", [0, 1, 2**53 + 1, INT64_MAX - 1, INT64_MAX])
def test_the_whole_instant_range_is_accepted(function: Callable[[Any], Any], value: int) -> None:
    assert function(value) is not None


@pytest.mark.requirement("S00-ASG-21", "S00-ASG-24")
def test_range_error_can_be_journaled_in_canonical_form() -> None:
    """Errors are data: ``to_dict()`` is the journal form and journals are canonical JSON."""
    for function in _TIME_FUNCTIONS:
        for value in (-1, 2**63, 2**70, -(2**70)):
            with pytest.raises(ContractValidationError) as excinfo:
                function(value)
            journal_form = excinfo.value.to_dict()
            assert strict_loads(canonical_dumps(journal_form)) == journal_form, (function.__name__, value)


# =========================================================================== limits of the instant range
#: The first and the last trading day that contain an instant (``0`` and ``2**63-1``).
FIRST_DAY = "1970-01-01"
LAST_DAY = "2262-04-12"
#: Prague midnight of the last day: 2262-04-12T00:00 CEST = 2262-04-11T22:00Z (summer time).
LAST_DAY_START = _utc_ns(2262, 4, 11, 22)
#: Prague midnight after the first day: 1970-01-02T00:00 CET = 1970-01-01T23:00Z (winter time).
FIRST_DAY_END = _utc_ns(1970, 1, 1, 23)


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_oracle_self_check_for_the_limits_of_the_range() -> None:
    """Independent of the code under test: where ``0`` and ``2**63-1`` fall in Prague civil time."""
    assert FIRST_DAY_END == 82_800 * SECOND
    # 1970-01-01T00:00Z is 01:00 in Prague (CET): the first day began an hour before the epoch
    assert _utc_ns(1970, 1, 1) - 1 * HOUR < 0 == _utc_ns(1970, 1, 1) < FIRST_DAY_END
    # 2**63-1 ns is 2262-04-11T23:47:16.854775807Z, i.e. 01:47 on 2262-04-12 in Prague (CEST)
    assert _utc_ns(2262, 4, 11, 23, 47, 16, 854_775_807) == INT64_MAX == MAX_TIME_NS
    assert _expected_start_ns(dt.date(2262, 4, 12)) == LAST_DAY_START
    assert LAST_DAY_START < INT64_MAX < LAST_DAY_START + 24 * HOUR
    assert INT64_MAX - LAST_DAY_START == 1 * HOUR + 47 * 60 * SECOND + 16 * SECOND + 854_775_807


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_the_first_and_the_last_instant_have_a_trading_day() -> None:
    assert trading_day(0) == FIRST_DAY
    assert trading_day(1) == FIRST_DAY
    assert trading_day(FIRST_DAY_END - 1) == FIRST_DAY
    assert trading_day(FIRST_DAY_END) == "1970-01-02"
    assert trading_day(LAST_DAY_START - 1) == "2262-04-11"
    assert trading_day(LAST_DAY_START) == LAST_DAY
    assert trading_day(MAX_TIME_NS - 1) == LAST_DAY
    assert trading_day(MAX_TIME_NS) == LAST_DAY


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_bounds_of_the_first_representable_day_are_clamped_to_the_epoch() -> None:
    """Prague midnight of 1970-01-01 is 1969-12-31T23:00Z, before the first instant."""
    bounds = trading_day_bounds_ns(FIRST_DAY)
    assert bounds == (0, FIRST_DAY_END)
    assert type(bounds) is tuple
    assert type(bounds[0]) is int
    assert type(bounds[1]) is int
    # only the start is clamped: the end is the real Prague midnight and the next day begins there
    assert trading_day_bounds_ns("1970-01-02") == (FIRST_DAY_END, FIRST_DAY_END + 24 * HOUR)
    assert trading_day(bounds[0]) == FIRST_DAY
    assert trading_day(bounds[1] - 1) == FIRST_DAY
    assert trading_day(bounds[1]) == "1970-01-02"


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_bounds_of_the_last_representable_day_are_clamped_to_the_largest_instant() -> None:
    """Prague midnight after 2262-04-12 is 2262-04-12T22:00Z, after the last instant."""
    bounds = trading_day_bounds_ns(LAST_DAY)
    assert bounds == (LAST_DAY_START, MAX_TIME_NS)
    assert type(bounds[0]) is int
    assert type(bounds[1]) is int
    # only the end is clamped: the start is the real Prague midnight and the previous day ends there
    assert trading_day_bounds_ns("2262-04-11") == (LAST_DAY_START - 24 * HOUR, LAST_DAY_START)
    assert trading_day(bounds[0]) == LAST_DAY
    assert trading_day(bounds[0] - 1) == "2262-04-11"
    assert trading_day(bounds[1] - 1) == LAST_DAY


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@pytest.mark.parametrize(
    "time_ns",
    [
        0,
        1,
        FIRST_DAY_END - 1,
        FIRST_DAY_END,
        LAST_DAY_START - 1,
        LAST_DAY_START,
        MAX_TIME_NS - 1,
        MAX_TIME_NS,
    ],
    ids=["0", "1", "first-day-end-1", "first-day-end", "last-day-start-1", "last-day-start", "max-1", "max"],
)
def test_every_day_returned_by_trading_day_has_bounds_that_are_instants(time_ns: int) -> None:
    """The documented purpose of the clamp: no instant has a trading day without bounds."""
    day = trading_day(time_ns)
    start, end = trading_day_bounds_ns(day)
    assert 0 <= start < end <= MAX_TIME_NS
    assert start <= time_ns <= end
    # both bounds are themselves valid instants for every other function of the module
    assert iso_utc(start) <= iso_utc(end)
    assert utc_datetime(start) <= utc_datetime(end)


@pytest.mark.requirement("G05-10", "S00-ASG-21")
@PROPERTY
@example(0)
@example(1)
@example(FIRST_DAY_END - 1)
@example(FIRST_DAY_END)
@example(LAST_DAY_START - 1)
@example(LAST_DAY_START)
@example(MAX_TIME_NS - 1)
@given(st.integers(min_value=0, max_value=MAX_TIME_NS - 1))
def test_every_instant_lies_within_the_half_open_bounds_of_its_trading_day(time_ns: int) -> None:
    """Also on the two clamped days (the very last instant is checked separately below)."""
    day = trading_day(time_ns)
    start, end = trading_day_bounds_ns(day)
    assert 0 <= start <= time_ns < end <= MAX_TIME_NS
    assert trading_day(start) == day
    assert trading_day(end - 1) == day
    if start > 0:
        assert trading_day(start - 1) < day
    if end < MAX_TIME_NS:
        assert trading_day(end) > day
    if start > 0 and end < MAX_TIME_NS:  # a day that is not clamped has its full length
        assert end - start in (23 * HOUR, 24 * HOUR, 25 * HOUR)


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_the_last_instant_belongs_to_the_last_day_whose_upper_bound_is_inclusive() -> None:
    """The one documented exception to the half-open rule.

    ``trading_day(2**63-1)`` is 2262-04-12.  Its bounds stay instants, so its end is
    ``MAX_TIME_NS`` itself, and the documentation states that for this day alone the upper
    bound is inclusive: the last instant belongs to it and no instant is without a trading day.
    """
    day = trading_day(MAX_TIME_NS)
    assert day == LAST_DAY
    start, end = trading_day_bounds_ns(day)
    assert end == MAX_TIME_NS
    assert start <= MAX_TIME_NS <= end
    # every other day keeps the half-open rule: its end instant belongs to the next day
    _, previous_end = trading_day_bounds_ns("2262-04-11")
    assert previous_end == start
    assert trading_day(previous_end) == LAST_DAY
    assert trading_day(previous_end - 1) == "2262-04-11"


@pytest.mark.requirement("G05-10", "S00-ASG-21")
def test_days_next_to_the_limits_are_ordinary_days() -> None:
    """The clamp affects the first and the last day only."""
    for day, start in (
        ("1970-01-02", _utc_ns(1970, 1, 1, 23)),
        ("1970-01-03", _utc_ns(1970, 1, 2, 23)),
        ("2262-04-10", _utc_ns(2262, 4, 9, 22)),
        ("2262-04-11", _utc_ns(2262, 4, 10, 22)),
    ):
        assert trading_day_bounds_ns(day) == (start, start + 24 * HOUR)
        assert trading_day(start) == day
        assert trading_day(start + 24 * HOUR - 1) == day


@pytest.mark.requirement("G05-10", "S00-ASG-21", "S00-ASG-24")
@pytest.mark.parametrize(
    "day",
    [
        "1969-12-31",  # the day before the first representable day
        "1900-01-01",
        "0001-01-01",
        "2262-04-13",  # the day after the last representable day
        "2300-01-01",
        "9999-12-30",
        "9999-12-31",
    ],
)
def test_a_day_outside_the_instant_range_has_no_bounds(day: str) -> None:
    """Such a label is a real calendar date but contains no instant: it is refused, not clamped.

    Clamping "affects only the first and the last representable trading day"; for any other day
    it would return an empty interval (``start == end``) as if it were a day - invalid input
    repaired silently, which section 5 forbids ("never clamped, defaulted ...").
    """
    assert parse_trading_day(day).isoformat() == day  # positive control: the label itself is well formed
    with pytest.raises(ContractValidationError) as excinfo:
        trading_day_bounds_ns(day)
    error = excinfo.value
    assert error.code == "GIE.CONTRACT.INVALID"
    assert error.details["value"] == day
    assert strict_loads(canonical_dumps(error.to_dict())) == error.to_dict()


@pytest.mark.requirement("G05-10", "S00-ASG-24")
@pytest.mark.parametrize("label", ["2262-02-30", "1970-00-01", "1970-1-1", "19700101", "", None, 0, 2262])
def test_bounds_of_a_label_that_is_not_a_date_are_refused(label: Any) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        trading_day_bounds_ns(label)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"
