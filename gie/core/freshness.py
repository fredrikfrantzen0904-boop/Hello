"""Freshness rules (requirements S00-ASG-23, G03-12 "reject stale signals", G06-04).

Two forms of freshness exist and both are **fail closed at the boundary**:

* *Expiry*: a record carrying ``valid_until_ns`` is usable iff
  ``now_ns < valid_until_ns``.  At the expiry instant it is already expired.
* *Age*: data with a reference instant is fresh iff
  ``0 <= now_ns - reference_ns < max_age_ns``.

A reference instant later than ``now_ns`` is never "very fresh": it is either
clock skew or look-ahead leakage and raises :class:`FutureDataError`.

Freshness is always evaluated against the injected :class:`~gie.core.clock.Clock`
so that replay evaluates it against virtual time.
"""

from __future__ import annotations

from gie.core.errors import ContractValidationError, FutureDataError, StaleDataError


def _require_int(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{what} must be an int of nanoseconds", value=repr(value)[:64])


def age_ns(*, now_ns: int, reference_ns: int, future_tolerance_ns: int = 0) -> int:
    """Age of data whose reference instant is *reference_ns*.

    ``future_tolerance_ns`` is the largest amount by which the reference may lie
    in the future (cross-host clock skew in paper mode).  It must stay ``0`` in
    replay.  Within the tolerance the age is reported as ``0``.
    """
    _require_int(now_ns, "now_ns")
    _require_int(reference_ns, "reference_ns")
    _require_int(future_tolerance_ns, "future_tolerance_ns")
    if future_tolerance_ns < 0:
        raise ContractValidationError("future_tolerance_ns must be >= 0", value=future_tolerance_ns)
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
    _require_int(now_ns, "now_ns")
    _require_int(valid_until_ns, "valid_until_ns")
    return now_ns >= valid_until_ns


def require_not_expired(*, now_ns: int, valid_until_ns: int, what: str) -> int:
    """Return the remaining validity in ns, or raise :class:`StaleDataError` if expired."""
    if is_expired(now_ns=now_ns, valid_until_ns=valid_until_ns):
        raise StaleDataError(f"{what} has expired", what=what, now_ns=now_ns, valid_until_ns=valid_until_ns)
    return valid_until_ns - now_ns
