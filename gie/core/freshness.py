"""Freshness rules (requirements S00-ASG-23, G03-12 "reject stale signals", G06-04).

Two forms of freshness exist and both are **fail closed at the boundary**:

* *Expiry*: a record carrying ``valid_until_ns`` is usable iff
  ``now_ns < valid_until_ns``.  At the expiry instant it is already expired.
* *Age*: data with a reference instant is fresh iff
  ``0 <= now_ns - reference_ns < max_age_ns``.

A reference instant later than ``now_ns`` is never "very fresh": it is either
clock skew or look-ahead leakage and raises :class:`FutureDataError`.

Freshness is always evaluated against the injected :class:`~gie.core.clock.Clock`
so that replay evaluates it against virtual time.  Components use the
``*_now`` functions, which take the clock: they refuse any future tolerance in
replay (where it would be look-ahead) and bound it in paper mode.  The plain
functions that take ``now_ns`` exist for code that already holds a reading.

Which instant is the reference is fixed per contract in
``specifications/SHARED_CONTRACTS.md`` section 3.3; a source event time is never
used as the reference.
"""

from __future__ import annotations

from typing import Final

from gie.core.clock import Clock, ClockMode
from gie.core.errors import ContractValidationError, FutureDataError, StaleDataError, describe
from gie.core.timeutil import MAX_TIME_NS, NS_PER_SECOND

#: Largest tolerance for a reference instant in the future (cross-host clock skew, paper only).
MAX_FUTURE_TOLERANCE_NS: Final[int] = NS_PER_SECOND


def _require_int(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{what} must be an int of nanoseconds", value=describe(value, 64))
    if not -MAX_TIME_NS <= value <= MAX_TIME_NS:
        raise ContractValidationError(f"{what} is outside the supported range", value=describe(value, 64))


def _require_instant(value: int, what: str) -> None:
    _require_int(value, what)
    if value < 0:
        raise ContractValidationError(f"{what} must be an instant (>= 0)", value=value)


def _require_tolerance(future_tolerance_ns: int) -> None:
    _require_int(future_tolerance_ns, "future_tolerance_ns")
    if not 0 <= future_tolerance_ns <= MAX_FUTURE_TOLERANCE_NS:
        raise ContractValidationError(
            "future_tolerance_ns must be within [0, MAX_FUTURE_TOLERANCE_NS]", value=future_tolerance_ns
        )


def age_ns(*, now_ns: int, reference_ns: int, future_tolerance_ns: int = 0) -> int:
    """Age of data whose reference instant is *reference_ns*.

    ``future_tolerance_ns`` is the largest amount by which the reference may lie
    in the future (cross-host clock skew in paper mode).  It must stay ``0`` in
    replay.  Within the tolerance the age is reported as ``0``.
    """
    _require_instant(now_ns, "now_ns")
    _require_instant(reference_ns, "reference_ns")
    _require_tolerance(future_tolerance_ns)
    age = now_ns - reference_ns
    if age < -future_tolerance_ns:
        raise FutureDataError(
            "reference instant lies in the future",
            now_ns=now_ns,
            reference_ns=reference_ns,
            future_tolerance_ns=future_tolerance_ns,
        )
    return max(age, 0)


def is_fresh(*, now_ns: int, reference_ns: int, max_age_ns: int, future_tolerance_ns: int = 0) -> bool:
    """True iff ``0 <= age < max_age_ns``.  Raises :class:`FutureDataError` on future data."""
    _require_int(max_age_ns, "max_age_ns")
    if max_age_ns <= 0:
        raise ContractValidationError("max_age_ns must be > 0", value=max_age_ns)
    return (
        age_ns(now_ns=now_ns, reference_ns=reference_ns, future_tolerance_ns=future_tolerance_ns) < max_age_ns
    )


def require_fresh(
    *,
    now_ns: int,
    reference_ns: int,
    max_age_ns: int,
    what: str,
    future_tolerance_ns: int = 0,
) -> int:
    """Return the age if fresh, else raise :class:`StaleDataError`."""
    _require_int(max_age_ns, "max_age_ns")
    if max_age_ns <= 0:
        raise ContractValidationError("max_age_ns must be > 0", value=max_age_ns)
    age = age_ns(now_ns=now_ns, reference_ns=reference_ns, future_tolerance_ns=future_tolerance_ns)
    if age >= max_age_ns:
        raise StaleDataError(f"{what} is stale", what=what, age_ns=age, max_age_ns=max_age_ns, now_ns=now_ns)
    return age


def is_expired(*, now_ns: int, valid_until_ns: int) -> bool:
    """True iff ``now_ns >= valid_until_ns`` (expired exactly at the expiry instant)."""
    _require_instant(now_ns, "now_ns")
    _require_instant(valid_until_ns, "valid_until_ns")
    return now_ns >= valid_until_ns


def require_not_expired(*, now_ns: int, valid_until_ns: int, what: str) -> int:
    """Return the remaining validity in ns, or raise :class:`StaleDataError` if expired."""
    if is_expired(now_ns=now_ns, valid_until_ns=valid_until_ns):
        raise StaleDataError(f"{what} has expired", what=what, now_ns=now_ns, valid_until_ns=valid_until_ns)
    return valid_until_ns - now_ns


# --------------------------------------------------------------------------- clock-based forms
def _clock_tolerance(clock: Clock, future_tolerance_ns: int) -> int:
    _require_tolerance(future_tolerance_ns)
    # Fail closed: only the genuine system clock of a paper run may be granted a tolerance.  In
    # replay it would be look-ahead, and a clock of any other or unknown kind gets none either.
    if future_tolerance_ns != 0 and clock.mode is not ClockMode.SYSTEM:
        raise ContractValidationError(
            "a future tolerance exists for the system clock only; in replay it is look-ahead and must be 0",
            value=future_tolerance_ns,
        )
    return future_tolerance_ns


def is_fresh_now(clock: Clock, *, reference_ns: int, max_age_ns: int, future_tolerance_ns: int = 0) -> bool:
    """:func:`is_fresh` against the injected clock; refuses a future tolerance in replay."""
    return is_fresh(
        now_ns=clock.now_ns(),
        reference_ns=reference_ns,
        max_age_ns=max_age_ns,
        future_tolerance_ns=_clock_tolerance(clock, future_tolerance_ns),
    )


def require_fresh_now(
    clock: Clock, *, reference_ns: int, max_age_ns: int, what: str, future_tolerance_ns: int = 0
) -> int:
    """:func:`require_fresh` against the injected clock; refuses a future tolerance in replay."""
    return require_fresh(
        now_ns=clock.now_ns(),
        reference_ns=reference_ns,
        max_age_ns=max_age_ns,
        what=what,
        future_tolerance_ns=_clock_tolerance(clock, future_tolerance_ns),
    )


def is_expired_now(clock: Clock, *, valid_until_ns: int) -> bool:
    """:func:`is_expired` against the injected clock."""
    return is_expired(now_ns=clock.now_ns(), valid_until_ns=valid_until_ns)


def require_not_expired_now(clock: Clock, *, valid_until_ns: int, what: str) -> int:
    """:func:`require_not_expired` against the injected clock."""
    return require_not_expired(now_ns=clock.now_ns(), valid_until_ns=valid_until_ns, what=what)
