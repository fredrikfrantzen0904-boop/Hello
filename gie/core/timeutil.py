"""Time units and the Prague trading day (requirements S00-ASG-20/21, G05-10).

* Every instant is an ``int`` count of **nanoseconds since the Unix epoch in
  UTC** and lives in a field whose name ends in ``_time_ns`` (or ``_until_ns``).
* Every duration is an ``int`` count of nanoseconds in a field ending ``_ns``
  that is documented as a duration.
* Local time zones never appear in contract fields.  The only calendar concept
  shared by all subsystems is the **trading day**, the calendar date in
  ``Europe/Prague`` (CET/CEST), because the account's daily limits reset at
  Prague midnight (G05, to be confirmed against the actual account rules by
  session 08).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Final
from zoneinfo import ZoneInfo

from gie.core.errors import ContractValidationError

NS_PER_US: Final[int] = 1_000
NS_PER_MS: Final[int] = 1_000_000
NS_PER_SECOND: Final[int] = 1_000_000_000
NS_PER_MINUTE: Final[int] = 60 * NS_PER_SECOND
NS_PER_HOUR: Final[int] = 60 * NS_PER_MINUTE

#: Largest representable instant (signed 64-bit nanoseconds, year 2262).
MAX_TIME_NS: Final[int] = 2**63 - 1

TRADING_DAY_TZ_NAME: Final[str] = "Europe/Prague"
_PRAGUE: Final[ZoneInfo] = ZoneInfo(TRADING_DAY_TZ_NAME)
_UTC: Final[_dt.timezone] = _dt.UTC
_DAY_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TRADING_DAY_PATTERN: Final[str] = r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"


def _require_time_ns(time_ns: int) -> None:
    if isinstance(time_ns, bool) or not isinstance(time_ns, int):
        raise ContractValidationError("time must be an int of nanoseconds", value=repr(time_ns)[:64])
    if not 0 <= time_ns <= MAX_TIME_NS:
        raise ContractValidationError("time outside the supported range", value=time_ns)


def utc_datetime(time_ns: int) -> _dt.datetime:
    """UTC ``datetime`` for an instant (microsecond precision, sub-microsecond part dropped)."""
    _require_time_ns(time_ns)
    seconds, remainder = divmod(time_ns, NS_PER_SECOND)
    base = _dt.datetime.fromtimestamp(seconds, tz=_UTC)
    return base.replace(microsecond=remainder // NS_PER_US)


def time_ns_from_datetime(moment: _dt.datetime) -> int:
    """Instant for a timezone-aware ``datetime``.  Naive datetimes are rejected."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ContractValidationError("naive datetime has no defined instant")
    delta = moment - _dt.datetime(1970, 1, 1, tzinfo=_UTC)
    time_ns = (delta.days * 86_400 + delta.seconds) * NS_PER_SECOND + delta.microseconds * NS_PER_US
    if not 0 <= time_ns <= MAX_TIME_NS:
        raise ContractValidationError(
            "datetime outside the supported instant range (1970 .. 2262)", value=moment.isoformat()
        )
    return time_ns


def iso_utc(time_ns: int) -> str:
    """Human-readable ``YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ`` (for logs and reports only)."""
    _require_time_ns(time_ns)
    seconds, remainder = divmod(time_ns, NS_PER_SECOND)
    base = _dt.datetime.fromtimestamp(seconds, tz=_UTC)
    return f"{base:%Y-%m-%dT%H:%M:%S}.{remainder:09d}Z"


def trading_day(time_ns: int) -> str:
    """Trading day (``YYYY-MM-DD``) of an instant: its calendar date in Europe/Prague."""
    _require_time_ns(time_ns)
    seconds = time_ns // NS_PER_SECOND
    return _dt.datetime.fromtimestamp(seconds, tz=_PRAGUE).date().isoformat()


def parse_trading_day(day: str) -> _dt.date:
    """Validate and parse a ``YYYY-MM-DD`` trading-day label."""
    if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
        raise ContractValidationError("trading day must be YYYY-MM-DD", value=repr(day)[:32])
    try:
        return _dt.date.fromisoformat(day)
    except ValueError as exc:
        raise ContractValidationError("trading day is not a real calendar date", value=day) from exc


def trading_day_bounds_ns(day: str) -> tuple[int, int]:
    """``(start_ns, end_ns)`` of a trading day: Prague midnight to the next Prague midnight.

    The interval is half open: an instant belongs to the day iff
    ``start_ns <= t < end_ns``.  Days are 23 h or 25 h long on DST changes.

    Both bounds are always instants.  Two days touch the limits of the instant
    range and are the only ones whose bounds are cut to it: the first
    (1970-01-01, whose Prague midnight lies before instant 0) starts at 0, and
    the last (2262-04-12) ends at ``MAX_TIME_NS``.  For that last day alone the
    upper bound is inclusive - ``MAX_TIME_NS`` itself belongs to it, because no
    later instant exists.  A day that lies entirely outside the instant range
    has no bounds and is refused.
    """
    date = parse_trading_day(day)
    try:
        start = _dt.datetime(date.year, date.month, date.day, tzinfo=_PRAGUE)
        next_date = date + _dt.timedelta(days=1)
        end = _dt.datetime(next_date.year, next_date.month, next_date.day, tzinfo=_PRAGUE)
    except (OverflowError, ValueError) as exc:
        raise ContractValidationError("trading day outside the supported range", value=day) from exc
    start_ns, end_ns = _epoch_ns(start), _epoch_ns(end)
    if end_ns <= 0 or start_ns > MAX_TIME_NS:
        raise ContractValidationError("trading day outside the supported range", value=day)
    return max(start_ns, 0), min(end_ns, MAX_TIME_NS)


def _epoch_ns(moment: _dt.datetime) -> int:
    """Nanoseconds from the Unix epoch to *moment* (may lie outside the instant range)."""
    delta = moment - _dt.datetime(1970, 1, 1, tzinfo=_UTC)
    return (delta.days * 86_400 + delta.seconds) * NS_PER_SECOND + delta.microseconds * NS_PER_US
