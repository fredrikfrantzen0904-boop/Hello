"""Independent tests for ``gie.core.freshness`` (requirements S00-ASG-23, G03-12).

The expected behaviour is the normative text of ``specifications/SHARED_CONTRACTS.md``
section 3.3 and decision D-010:

* *Expiry*: a record is usable iff ``now_ns < valid_until_ns``; at the expiry instant it is
  already expired.
* *Age*: data is fresh iff ``0 <= now_ns - reference_ns < max_age_ns``.
* *Future data*: a reference later than ``now_ns`` is never fresh; it raises
  ``FutureDataError``.  ``future_tolerance_ns`` is the largest amount by which the reference
  may lie in the future; within the tolerance the age is reported as ``0``.

Revision 2 of the contracts (after the independent review) hardened the module:

* every instant (``now_ns``, ``reference_ns``, ``valid_until_ns``) lies in ``0 .. 2**63-1``
  (section 2, "instant"); anything else is a ``ContractValidationError``;
* the future tolerance exists "for paper mode only" and is bounded to one second
  (``MAX_FUTURE_TOLERANCE_NS``);
* the clock-based helpers ``is_fresh_now`` / ``require_fresh_now`` / ``is_expired_now`` /
  ``require_not_expired_now`` evaluate against the injected ``Clock`` and refuse any non-zero
  tolerance when the clock is a REPLAY clock (it would be look-ahead).

All instants are synthetic fixtures.  Nothing here reads the wall clock: the clock-based helpers
are driven by a ``ReplayClock`` at synthetic instants and by a ``SystemClock`` whose time sources
are injected constants.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.clock import Clock, ClockMode, ReplayClock, SystemClock
from gie.core.errors import (
    ContractError,
    ContractValidationError,
    FutureDataError,
    GieError,
    StaleDataError,
    TimeError,
)
from gie.core.freshness import (
    MAX_FUTURE_TOLERANCE_NS,
    age_ns,
    is_expired,
    is_expired_now,
    is_fresh,
    is_fresh_now,
    require_fresh,
    require_fresh_now,
    require_not_expired,
    require_not_expired_now,
)
from gie.core.timeutil import MAX_TIME_NS, NS_PER_SECOND

PROPERTY = settings(
    max_examples=400,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: A nanosecond instant on 2026-11-27 (synthetic).  It is larger than 2**53, so any arithmetic
#: that passed through a binary float would lose the single nanoseconds these tests rely on.
NOW = 1795771815123456789
MS = 1_000_000
SECOND = 1_000_000_000
INT64_MAX = 2**63 - 1


# =========================================================================== age_ns
@pytest.mark.requirement("S00-ASG-23")
@pytest.mark.parametrize(
    ("now_ns", "reference_ns", "expected"),
    [
        (NOW, NOW, 0),
        (NOW, NOW - 1, 1),
        (NOW, NOW - 250 * MS, 250 * MS),
        (NOW, 0, NOW),
        (0, 0, 0),
        (INT64_MAX, 0, INT64_MAX),
        (INT64_MAX, INT64_MAX - 1, 1),
    ],
)
def test_age_is_now_minus_reference(now_ns: int, reference_ns: int, expected: int) -> None:
    age = age_ns(now_ns=now_ns, reference_ns=reference_ns)
    assert age == expected
    assert type(age) is int


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize("ahead", [1, 2, 1_000, 250 * MS, 3600 * 10**9])
def test_future_reference_raises_future_data_error(ahead: int) -> None:
    with pytest.raises(FutureDataError) as excinfo:
        age_ns(now_ns=NOW, reference_ns=NOW + ahead)
    error = excinfo.value
    assert error.code == "GIE.TIME.FUTURE"
    assert isinstance(error, TimeError)
    assert isinstance(error, GieError)
    assert not isinstance(error, StaleDataError)
    assert error.details["now_ns"] == NOW
    assert error.details["reference_ns"] == NOW + ahead
    assert error.details["future_tolerance_ns"] == 0
    assert error.to_dict()["code"] == "GIE.TIME.FUTURE"


@pytest.mark.requirement("S00-ASG-23")
@pytest.mark.parametrize(
    ("ahead", "tolerance", "accepted"),
    [
        (0, 0, True),
        (1, 0, False),
        (1, 1, True),  # exactly at the tolerance
        (2, 1, False),  # one nanosecond beyond the tolerance
        (4_999_999, 5 * MS, True),
        (5 * MS, 5 * MS, True),  # exactly at the tolerance
        (5 * MS + 1, 5 * MS, False),
        (10 * MS, 5 * MS, False),
    ],
)
def test_future_tolerance_boundary(ahead: int, tolerance: int, accepted: bool) -> None:
    arguments = {"now_ns": NOW, "reference_ns": NOW + ahead, "future_tolerance_ns": tolerance}
    if accepted:
        # within the tolerance the age is reported as zero, never negative
        assert age_ns(**arguments) == 0
        assert is_fresh(max_age_ns=1, **arguments) is True
        assert require_fresh(max_age_ns=1, what="tick", **arguments) == 0
    else:
        with pytest.raises(FutureDataError) as excinfo:
            age_ns(**arguments)
        assert excinfo.value.details["future_tolerance_ns"] == tolerance
        with pytest.raises(FutureDataError):
            is_fresh(max_age_ns=10**18, **arguments)
        with pytest.raises(FutureDataError):
            require_fresh(max_age_ns=10**18, what="tick", **arguments)


@pytest.mark.requirement("S00-ASG-23")
def test_future_tolerance_does_not_change_the_age_of_past_data() -> None:
    for tolerance in (0, 1, 5 * MS, SECOND - 1, SECOND):
        assert age_ns(now_ns=NOW, reference_ns=NOW - 7, future_tolerance_ns=tolerance) == 7
        assert is_fresh(now_ns=NOW, reference_ns=NOW - 7, max_age_ns=8, future_tolerance_ns=tolerance) is True
        # the tolerance never extends max_age: at age == max_age the data is stale
        assert (
            is_fresh(now_ns=NOW, reference_ns=NOW - 8, max_age_ns=8, future_tolerance_ns=tolerance) is False
        )


@pytest.mark.requirement("S00-ASG-23")
def test_default_future_tolerance_is_zero() -> None:
    with pytest.raises(FutureDataError):
        age_ns(now_ns=NOW, reference_ns=NOW + 1)
    with pytest.raises(FutureDataError):
        is_fresh(now_ns=NOW, reference_ns=NOW + 1, max_age_ns=10**18)
    with pytest.raises(FutureDataError):
        require_fresh(now_ns=NOW, reference_ns=NOW + 1, max_age_ns=10**18, what="tick")


# =========================================================================== is_fresh
@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize(
    ("age", "max_age", "fresh"),
    [
        (0, 1, True),
        (1, 1, False),  # exactly at the limit: stale (fail closed)
        (0, 250 * MS, True),
        (250 * MS - 1, 250 * MS, True),  # last fresh nanosecond
        (250 * MS, 250 * MS, False),  # exactly at the limit: stale
        (250 * MS + 1, 250 * MS, False),
        (10**18, 250 * MS, False),
        (10**18, 10**18 + 1, True),
    ],
)
def test_fresh_iff_age_is_strictly_below_the_limit(age: int, max_age: int, fresh: bool) -> None:
    reference = NOW - age
    result = is_fresh(now_ns=NOW, reference_ns=reference, max_age_ns=max_age)
    assert result is fresh
    if fresh:
        assert require_fresh(now_ns=NOW, reference_ns=reference, max_age_ns=max_age, what="tick") == age
    else:
        with pytest.raises(StaleDataError):
            require_fresh(now_ns=NOW, reference_ns=reference, max_age_ns=max_age, what="tick")


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_future_data_is_an_error_not_fresh_and_not_stale() -> None:
    """A future reference is "never very fresh": the answer is an error, not True and not False."""
    with pytest.raises(FutureDataError) as excinfo:
        is_fresh(now_ns=NOW, reference_ns=NOW + 1, max_age_ns=250 * MS)
    assert not isinstance(excinfo.value, StaleDataError)
    with pytest.raises(FutureDataError):
        require_fresh(now_ns=NOW, reference_ns=NOW + 1, max_age_ns=250 * MS, what="tick")


# =========================================================================== require_fresh
@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
def test_require_fresh_returns_the_age_or_raises_stale_data_error() -> None:
    limit = 250 * MS
    age = require_fresh(now_ns=NOW, reference_ns=NOW - 123, max_age_ns=limit, what="XAUUSD tick")
    assert age == 123
    assert type(age) is int

    with pytest.raises(StaleDataError) as excinfo:
        require_fresh(now_ns=NOW, reference_ns=NOW - limit, max_age_ns=limit, what="XAUUSD tick")
    error = excinfo.value
    assert error.code == "GIE.TIME.STALE"
    assert isinstance(error, TimeError)
    assert isinstance(error, GieError)
    assert not isinstance(error, FutureDataError)
    assert not isinstance(error, ContractError)
    assert error.details == {"what": "XAUUSD tick", "age_ns": limit, "max_age_ns": limit, "now_ns": NOW}
    assert "XAUUSD tick" in error.message
    assert "GIE.TIME.STALE" in str(error)
    assert error.to_dict() == {
        "code": "GIE.TIME.STALE",
        "message": error.message,
        "details": {"what": "XAUUSD tick", "age_ns": limit, "max_age_ns": limit, "now_ns": NOW},
    }
    # the journal form survives canonical JSON with nanosecond integers intact
    assert strict_loads(canonical_dumps(error.to_dict())) == error.to_dict()


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_stale_error_reports_the_exact_age() -> None:
    with pytest.raises(StaleDataError) as excinfo:
        require_fresh(now_ns=NOW, reference_ns=NOW - 10**12 - 1, max_age_ns=10**12, what="candidate")
    assert excinfo.value.details["age_ns"] == 10**12 + 1
    assert excinfo.value.details["max_age_ns"] == 10**12
    assert excinfo.value.details["now_ns"] == NOW


# =========================================================================== expiry
@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize(
    ("now_ns", "valid_until_ns", "expired"),
    [
        (NOW - 1, NOW, False),  # last usable nanosecond
        (NOW, NOW, True),  # at the expiry instant it is already expired
        (NOW + 1, NOW, True),
        (0, 1, False),
        (0, 0, True),
        (NOW, INT64_MAX, False),
        (INT64_MAX, INT64_MAX, True),
        (NOW, 0, True),
    ],
)
def test_expired_iff_now_is_at_or_after_valid_until(now_ns: int, valid_until_ns: int, expired: bool) -> None:
    result = is_expired(now_ns=now_ns, valid_until_ns=valid_until_ns)
    assert result is expired
    if expired:
        with pytest.raises(StaleDataError):
            require_not_expired(now_ns=now_ns, valid_until_ns=valid_until_ns, what="plan")
    else:
        remaining = require_not_expired(now_ns=now_ns, valid_until_ns=valid_until_ns, what="plan")
        assert remaining == valid_until_ns - now_ns
        assert remaining > 0
        assert type(remaining) is int


@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
def test_require_not_expired_error_code_and_details() -> None:
    with pytest.raises(StaleDataError) as excinfo:
        require_not_expired(now_ns=NOW, valid_until_ns=NOW, what="risk decision")
    error = excinfo.value
    assert error.code == "GIE.TIME.STALE"
    assert isinstance(error, TimeError)
    assert error.details == {"what": "risk decision", "now_ns": NOW, "valid_until_ns": NOW}
    assert "risk decision" in error.message
    assert strict_loads(canonical_dumps(error.to_dict())) == error.to_dict()

    with pytest.raises(StaleDataError) as later:
        require_not_expired(now_ns=NOW + 5, valid_until_ns=NOW, what="risk decision")
    assert later.value.details == {"what": "risk decision", "now_ns": NOW + 5, "valid_until_ns": NOW}


# =========================================================================== invalid arguments
_NOT_AN_INT: list[Any] = [True, False, 1.0, 0.0, 1.5, float("nan"), "1", None, Decimal("1"), b"1", [1], (1,)]

_VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "age_ns": {"now_ns": NOW, "reference_ns": NOW - 5, "future_tolerance_ns": 0},
    "is_fresh": {"now_ns": NOW, "reference_ns": NOW - 5, "max_age_ns": 10, "future_tolerance_ns": 0},
    "require_fresh": {
        "now_ns": NOW,
        "reference_ns": NOW - 5,
        "max_age_ns": 10,
        "future_tolerance_ns": 0,
        "what": "tick",
    },
    "is_expired": {"now_ns": NOW, "valid_until_ns": NOW + 5},
    "require_not_expired": {"now_ns": NOW, "valid_until_ns": NOW + 5, "what": "plan"},
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "age_ns": age_ns,
    "is_fresh": is_fresh,
    "require_fresh": require_fresh,
    "is_expired": is_expired,
    "require_not_expired": require_not_expired,
}
_INT_ARGUMENTS = [
    (name, argument)
    for name, arguments in _VALID_ARGUMENTS.items()
    for argument in arguments
    if argument != "what"
]


@pytest.mark.requirement("S00-ASG-23")
@pytest.mark.parametrize("name", list(_FUNCTIONS))
def test_valid_baseline_arguments_are_accepted(name: str) -> None:
    """Positive control for the rejection tests below: the unmodified arguments are valid."""
    expected = {
        "age_ns": 5,
        "is_fresh": True,
        "require_fresh": 5,
        "is_expired": False,
        "require_not_expired": 5,
    }
    result = _FUNCTIONS[name](**_VALID_ARGUMENTS[name])
    assert result == expected[name]
    assert type(result) is type(expected[name])


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize(("name", "argument"), _INT_ARGUMENTS, ids=[f"{n}.{a}" for n, a in _INT_ARGUMENTS])
@pytest.mark.parametrize("bad", _NOT_AN_INT, ids=repr)
def test_non_integer_arguments_are_rejected(name: str, argument: str, bad: Any) -> None:
    arguments = dict(_VALID_ARGUMENTS[name])
    arguments[argument] = bad
    with pytest.raises(ContractValidationError) as excinfo:
        _FUNCTIONS[name](**arguments)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize("max_age", [0, -1, -250 * MS, -(10**18)])
def test_non_positive_max_age_is_rejected(max_age: int) -> None:
    with pytest.raises(ContractValidationError):
        is_fresh(now_ns=NOW, reference_ns=NOW, max_age_ns=max_age)
    with pytest.raises(ContractValidationError):
        require_fresh(now_ns=NOW, reference_ns=NOW, max_age_ns=max_age, what="tick")
    # also when the data would be stale or from the future: the limit itself is invalid
    with pytest.raises(ContractValidationError):
        is_fresh(now_ns=NOW, reference_ns=NOW - 10**15, max_age_ns=max_age)
    with pytest.raises(ContractValidationError):
        require_fresh(now_ns=NOW, reference_ns=NOW - 10**15, max_age_ns=max_age, what="tick")


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize("tolerance", [-1, -5 * MS, -(10**18)])
def test_negative_future_tolerance_is_rejected(tolerance: int) -> None:
    with pytest.raises(ContractValidationError):
        age_ns(now_ns=NOW, reference_ns=NOW - 1, future_tolerance_ns=tolerance)
    with pytest.raises(ContractValidationError):
        is_fresh(now_ns=NOW, reference_ns=NOW - 1, max_age_ns=10, future_tolerance_ns=tolerance)
    with pytest.raises(ContractValidationError):
        require_fresh(
            now_ns=NOW, reference_ns=NOW - 1, max_age_ns=10, what="tick", future_tolerance_ns=tolerance
        )
    # a negative tolerance must not turn into "data must be at least that old"
    with pytest.raises(ContractValidationError):
        age_ns(now_ns=NOW, reference_ns=NOW, future_tolerance_ns=tolerance)


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
def test_validation_errors_are_not_freshness_verdicts() -> None:
    """An invalid argument is a contract error, distinct from stale or future data."""
    with pytest.raises(ContractValidationError) as excinfo:
        is_fresh(now_ns=NOW, reference_ns=NOW, max_age_ns=0)
    assert not isinstance(excinfo.value, TimeError)
    assert isinstance(excinfo.value, GieError)


# =========================================================================== properties
_INSTANTS = st.integers(min_value=0, max_value=INT64_MAX)
_DURATIONS = st.integers(min_value=1, max_value=INT64_MAX)
_TOLERANCES = st.integers(min_value=0, max_value=SECOND)  # the documented bound of the tolerance


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@PROPERTY
@example(now_ns=NOW, reference_ns=NOW, max_age_ns=1, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW - 250 * MS, max_age_ns=250 * MS, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW - 250 * MS + 1, max_age_ns=250 * MS, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW + 5, max_age_ns=250 * MS, tolerance=5)
@example(now_ns=NOW, reference_ns=NOW + 6, max_age_ns=250 * MS, tolerance=5)
@example(now_ns=NOW, reference_ns=NOW + SECOND, max_age_ns=1, tolerance=SECOND)
@example(now_ns=NOW, reference_ns=NOW + SECOND + 1, max_age_ns=1, tolerance=SECOND)
@example(now_ns=0, reference_ns=INT64_MAX, max_age_ns=INT64_MAX, tolerance=SECOND)
@example(now_ns=INT64_MAX, reference_ns=0, max_age_ns=INT64_MAX, tolerance=0)
@given(now_ns=_INSTANTS, reference_ns=_INSTANTS, max_age_ns=_DURATIONS, tolerance=_TOLERANCES)
def test_age_based_freshness_matches_the_documented_rule(
    now_ns: int, reference_ns: int, max_age_ns: int, tolerance: int
) -> None:
    arguments = {"now_ns": now_ns, "reference_ns": reference_ns, "future_tolerance_ns": tolerance}
    if reference_ns - now_ns > tolerance:
        with pytest.raises(FutureDataError):
            age_ns(**arguments)
        with pytest.raises(FutureDataError):
            is_fresh(max_age_ns=max_age_ns, **arguments)
        with pytest.raises(FutureDataError):
            require_fresh(max_age_ns=max_age_ns, what="synthetic", **arguments)
        return
    expected_age = max(now_ns - reference_ns, 0)
    expected_fresh = expected_age < max_age_ns
    assert age_ns(**arguments) == expected_age
    assert is_fresh(max_age_ns=max_age_ns, **arguments) is expected_fresh
    if expected_fresh:
        assert require_fresh(max_age_ns=max_age_ns, what="synthetic", **arguments) == expected_age
    else:
        with pytest.raises(StaleDataError) as excinfo:
            require_fresh(max_age_ns=max_age_ns, what="synthetic", **arguments)
        assert excinfo.value.details["age_ns"] == expected_age


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@PROPERTY
@given(reference_ns=st.integers(min_value=0, max_value=INT64_MAX - 10**12), max_age_ns=st.integers(1, 10**12))
def test_freshness_flips_exactly_at_the_limit(reference_ns: int, max_age_ns: int) -> None:
    """Scanning ``now`` across the limit: fresh up to the last nanosecond, stale from the limit on."""
    last_fresh = reference_ns + max_age_ns - 1
    first_stale = reference_ns + max_age_ns
    assert is_fresh(now_ns=reference_ns, reference_ns=reference_ns, max_age_ns=max_age_ns) is True
    assert is_fresh(now_ns=last_fresh, reference_ns=reference_ns, max_age_ns=max_age_ns) is True
    assert is_fresh(now_ns=first_stale, reference_ns=reference_ns, max_age_ns=max_age_ns) is False
    assert is_fresh(now_ns=first_stale + 1, reference_ns=reference_ns, max_age_ns=max_age_ns) is False


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@PROPERTY
@example(now_ns=NOW, valid_until_ns=NOW)
@example(now_ns=NOW - 1, valid_until_ns=NOW)
@example(now_ns=NOW + 1, valid_until_ns=NOW)
@given(now_ns=_INSTANTS, valid_until_ns=_INSTANTS)
def test_expiry_matches_the_documented_rule(now_ns: int, valid_until_ns: int) -> None:
    usable = now_ns < valid_until_ns
    assert is_expired(now_ns=now_ns, valid_until_ns=valid_until_ns) is (not usable)
    if usable:
        remaining = require_not_expired(now_ns=now_ns, valid_until_ns=valid_until_ns, what="synthetic")
        assert remaining == valid_until_ns - now_ns
    else:
        with pytest.raises(StaleDataError) as excinfo:
            require_not_expired(now_ns=now_ns, valid_until_ns=valid_until_ns, what="synthetic")
        assert excinfo.value.details["valid_until_ns"] == valid_until_ns
        assert excinfo.value.details["now_ns"] == now_ns


# =========================================================================== errors are data
@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize(
    "call",
    [
        lambda: age_ns(now_ns=0, reference_ns=2**70),
        lambda: is_fresh(now_ns=0, reference_ns=1, max_age_ns=-(2**70)),
        lambda: age_ns(now_ns=0, reference_ns=0, future_tolerance_ns=-(2**70)),
        lambda: require_not_expired(now_ns=2**70, valid_until_ns=0, what="plan"),
    ],
    ids=["future-reference", "max-age", "tolerance", "expired"],
)
def test_errors_for_integers_beyond_64_bits_can_be_journaled(call: Callable[[], Any]) -> None:
    """Errors are data: ``to_dict()`` is the journal form and journals are canonical JSON.

    Each call is refused (fail closed); the refusal itself must still be recordable.
    """
    with pytest.raises(GieError) as excinfo:
        call()
    journal_form = excinfo.value.to_dict()
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


# =========================================================================== bounded future tolerance
@pytest.mark.requirement("S00-ASG-23", "S00-ASG-20")
def test_the_largest_future_tolerance_is_one_second() -> None:
    """Section 3.3: the tolerance for cross-host skew is bounded to one second."""
    assert MAX_FUTURE_TOLERANCE_NS == 1_000_000_000
    assert MAX_FUTURE_TOLERANCE_NS == NS_PER_SECOND
    assert type(MAX_FUTURE_TOLERANCE_NS) is int


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize("tolerance", [0, 1, SECOND - 1, SECOND])
def test_tolerance_within_the_bound_is_accepted(tolerance: int) -> None:
    reference = NOW + tolerance  # exactly as far in the future as tolerated
    assert age_ns(now_ns=NOW, reference_ns=reference, future_tolerance_ns=tolerance) == 0
    assert is_fresh(now_ns=NOW, reference_ns=reference, max_age_ns=1, future_tolerance_ns=tolerance) is True
    assert (
        require_fresh(
            now_ns=NOW, reference_ns=reference, max_age_ns=1, what="tick", future_tolerance_ns=tolerance
        )
        == 0
    )
    # one nanosecond further is future data, whatever the tolerance
    with pytest.raises(FutureDataError) as excinfo:
        age_ns(now_ns=NOW, reference_ns=reference + 1, future_tolerance_ns=tolerance)
    assert excinfo.value.details["future_tolerance_ns"] == tolerance


@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
@pytest.mark.parametrize(
    "tolerance",
    [SECOND + 1, 2 * SECOND, 10**10, 10**12, 3600 * SECOND, INT64_MAX - 1, INT64_MAX],
    ids=["1s+1ns", "2s", "10s", "1000s", "1h", "int64max-1", "int64max"],
)
def test_tolerance_beyond_one_second_is_rejected(tolerance: int) -> None:
    """A wide tolerance would silently accept look-ahead; it is an invalid argument, not a verdict."""
    calls: list[Callable[[], Any]] = [
        # also for data from the past, which the tolerance could not even affect
        lambda: age_ns(now_ns=NOW, reference_ns=NOW - 7, future_tolerance_ns=tolerance),
        lambda: is_fresh(now_ns=NOW, reference_ns=NOW - 7, max_age_ns=8, future_tolerance_ns=tolerance),
        lambda: require_fresh(
            now_ns=NOW, reference_ns=NOW - 7, max_age_ns=8, what="tick", future_tolerance_ns=tolerance
        ),
        # and for the future data it would have let through
        lambda: age_ns(now_ns=NOW, reference_ns=NOW + SECOND + 1, future_tolerance_ns=tolerance),
        lambda: is_fresh(
            now_ns=NOW, reference_ns=NOW + SECOND + 1, max_age_ns=8, future_tolerance_ns=tolerance
        ),
        lambda: require_fresh(
            now_ns=NOW, reference_ns=NOW + SECOND + 1, max_age_ns=8, what="tick", future_tolerance_ns=tolerance
        ),
    ]
    for call in calls:
        with pytest.raises(ContractValidationError) as excinfo:
            call()
        error = excinfo.value
        assert error.code == "GIE.CONTRACT.INVALID"
        assert not isinstance(error, TimeError)
        assert "future_tolerance_ns" in error.message
        assert strict_loads(canonical_dumps(error.to_dict()))["code"] == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize("tolerance", [2**63, 2**64, 2**70, 10**30, -(2**70)])
def test_tolerance_beyond_64_bits_is_rejected_with_a_journalable_error(tolerance: int) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        age_ns(now_ns=NOW, reference_ns=NOW, future_tolerance_ns=tolerance)
    journal_form = excinfo.value.to_dict()
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


# =========================================================================== instants are 0 .. 2**63-1
_INSTANT_ARGUMENTS = [
    ("age_ns", "now_ns"),
    ("age_ns", "reference_ns"),
    ("is_fresh", "now_ns"),
    ("is_fresh", "reference_ns"),
    ("require_fresh", "now_ns"),
    ("require_fresh", "reference_ns"),
    ("is_expired", "now_ns"),
    ("is_expired", "valid_until_ns"),
    ("require_not_expired", "now_ns"),
    ("require_not_expired", "valid_until_ns"),
]
_INSTANT_IDS = [f"{name}.{argument}" for name, argument in _INSTANT_ARGUMENTS]


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(("name", "argument"), _INSTANT_ARGUMENTS, ids=_INSTANT_IDS)
@pytest.mark.parametrize("bad", [-1, -2, -250 * MS, -NOW, -INT64_MAX], ids=repr)
def test_negative_instants_are_rejected(name: str, argument: str, bad: int) -> None:
    """An instant is a count of nanoseconds since the epoch, ``>= 0``; a negative one is not data."""
    arguments = dict(_VALID_ARGUMENTS[name])
    arguments[argument] = bad
    with pytest.raises(ContractValidationError) as excinfo:
        _FUNCTIONS[name](**arguments)
    error = excinfo.value
    assert error.code == "GIE.CONTRACT.INVALID"
    assert not isinstance(error, TimeError)
    assert argument in error.message
    assert error.details["value"] == bad


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(("name", "argument"), _INSTANT_ARGUMENTS, ids=_INSTANT_IDS)
@pytest.mark.parametrize("bad", [2**63, 2**63 + 1, 2**64, 10**30], ids=["2**63", "2**63+1", "2**64", "1e30"])
def test_instants_beyond_the_largest_instant_are_rejected(name: str, argument: str, bad: int) -> None:
    arguments = dict(_VALID_ARGUMENTS[name])
    arguments[argument] = bad
    with pytest.raises(ContractValidationError) as excinfo:
        _FUNCTIONS[name](**arguments)
    error = excinfo.value
    assert error.code == "GIE.CONTRACT.INVALID"
    assert argument in error.message
    journal_form = error.to_dict()
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-20")
def test_the_limits_of_the_instant_range_are_accepted() -> None:
    assert MAX_TIME_NS == INT64_MAX
    assert age_ns(now_ns=0, reference_ns=0) == 0
    assert age_ns(now_ns=MAX_TIME_NS, reference_ns=MAX_TIME_NS) == 0
    assert age_ns(now_ns=MAX_TIME_NS, reference_ns=0) == MAX_TIME_NS
    assert is_fresh(now_ns=MAX_TIME_NS, reference_ns=0, max_age_ns=MAX_TIME_NS) is False
    assert is_fresh(now_ns=MAX_TIME_NS - 1, reference_ns=0, max_age_ns=MAX_TIME_NS) is True
    assert require_fresh(now_ns=MAX_TIME_NS, reference_ns=1, max_age_ns=MAX_TIME_NS, what="tick") == (
        MAX_TIME_NS - 1
    )
    assert is_expired(now_ns=0, valid_until_ns=MAX_TIME_NS) is False
    assert is_expired(now_ns=MAX_TIME_NS, valid_until_ns=MAX_TIME_NS) is True
    assert is_expired(now_ns=0, valid_until_ns=0) is True
    assert require_not_expired(now_ns=0, valid_until_ns=MAX_TIME_NS, what="plan") == MAX_TIME_NS
    with pytest.raises(FutureDataError):
        age_ns(now_ns=0, reference_ns=MAX_TIME_NS)
    with pytest.raises(FutureDataError):
        age_ns(now_ns=0, reference_ns=MAX_TIME_NS, future_tolerance_ns=MAX_FUTURE_TOLERANCE_NS)


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
def test_a_negative_valid_until_is_not_an_already_expired_record() -> None:
    """``is_expired`` must not answer True for garbage: the argument is refused instead."""
    with pytest.raises(ContractValidationError):
        is_expired(now_ns=NOW, valid_until_ns=-1)
    with pytest.raises(ContractValidationError) as excinfo:
        require_not_expired(now_ns=NOW, valid_until_ns=-1, what="plan")
    assert not isinstance(excinfo.value, StaleDataError)


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@pytest.mark.parametrize("max_age", [2**63, 2**64, 10**30], ids=["2**63", "2**64", "1e30"])
def test_max_age_beyond_64_bits_is_rejected(max_age: int) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        is_fresh(now_ns=NOW, reference_ns=NOW, max_age_ns=max_age)
    assert "max_age_ns" in excinfo.value.message
    with pytest.raises(ContractValidationError):
        require_fresh(now_ns=NOW, reference_ns=NOW, max_age_ns=max_age, what="tick")
    # the largest duration that fits is still a valid limit
    assert is_fresh(now_ns=NOW, reference_ns=0, max_age_ns=INT64_MAX) is True


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
def test_an_instant_too_large_to_print_is_rejected_with_a_contract_error() -> None:
    """Python integers have no upper bound; the refusal of any of them is a ``GieError``."""
    with pytest.raises(ContractValidationError):
        age_ns(now_ns=NOW, reference_ns=10**5000)


# =========================================================================== clock-based helpers
def _replay(now_ns: int = NOW) -> ReplayClock:
    return ReplayClock(now_ns)


def _paper(now_ns: int = NOW) -> SystemClock:
    """A SYSTEM clock whose time sources are injected constants (no wall clock is read)."""
    return SystemClock(wall_source=lambda: now_ns, monotonic_source=lambda: 0)


_CLOCK_FACTORIES: dict[str, Callable[[int], Clock]] = {"replay": _replay, "paper": _paper}
_CLOCKS = pytest.mark.parametrize("make_clock", list(_CLOCK_FACTORIES.values()), ids=list(_CLOCK_FACTORIES))


@pytest.mark.requirement("S00-ASG-23")
def test_the_test_clocks_have_the_documented_modes() -> None:
    """Positive control for everything below."""
    replay, paper = _replay(), _paper()
    assert isinstance(replay, Clock)
    assert isinstance(paper, Clock)
    assert replay.mode is ClockMode.REPLAY
    assert paper.mode is ClockMode.SYSTEM
    assert replay.now_ns() == NOW
    assert paper.now_ns() == NOW


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@_CLOCKS
@pytest.mark.parametrize(
    ("age", "max_age", "fresh"),
    [
        (0, 1, True),
        (1, 1, False),
        (250 * MS - 1, 250 * MS, True),  # last fresh nanosecond
        (250 * MS, 250 * MS, False),  # exactly at the limit: stale (fail closed)
        (250 * MS + 1, 250 * MS, False),
        (NOW, NOW + 1, True),
        (NOW, NOW, False),
    ],
)
def test_clock_based_age_freshness_is_evaluated_at_the_clock_reading(
    make_clock: Callable[[int], Clock], age: int, max_age: int, fresh: bool
) -> None:
    clock = make_clock(NOW)
    reference = NOW - age
    assert is_fresh_now(clock, reference_ns=reference, max_age_ns=max_age) is fresh
    assert is_fresh_now(clock, reference_ns=reference, max_age_ns=max_age) is is_fresh(
        now_ns=NOW, reference_ns=reference, max_age_ns=max_age
    )
    if fresh:
        result = require_fresh_now(clock, reference_ns=reference, max_age_ns=max_age, what="tick")
        assert result == age
        assert type(result) is int
    else:
        with pytest.raises(StaleDataError) as excinfo:
            require_fresh_now(clock, reference_ns=reference, max_age_ns=max_age, what="XAUUSD tick")
        error = excinfo.value
        assert error.code == "GIE.TIME.STALE"
        assert error.details == {"what": "XAUUSD tick", "age_ns": age, "max_age_ns": max_age, "now_ns": NOW}
        assert "XAUUSD tick" in error.message


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_replay_freshness_follows_virtual_time() -> None:
    """Replay evaluates freshness against virtual time: the verdict changes only when it is advanced."""
    reference = NOW
    limit = 250 * MS
    clock = ReplayClock(reference)
    assert require_fresh_now(clock, reference_ns=reference, max_age_ns=limit, what="tick") == 0
    clock.advance_to(reference + limit - 1)
    assert is_fresh_now(clock, reference_ns=reference, max_age_ns=limit) is True
    assert require_fresh_now(clock, reference_ns=reference, max_age_ns=limit, what="tick") == limit - 1
    assert is_fresh_now(clock, reference_ns=reference, max_age_ns=limit) is True  # asking changes nothing
    clock.advance_by(1)
    assert is_fresh_now(clock, reference_ns=reference, max_age_ns=limit) is False
    with pytest.raises(StaleDataError) as excinfo:
        require_fresh_now(clock, reference_ns=reference, max_age_ns=limit, what="tick")
    assert excinfo.value.details["age_ns"] == limit
    assert excinfo.value.details["now_ns"] == reference + limit
    assert clock.now_ns() == reference + limit  # the helpers never move the clock


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@_CLOCKS
def test_clock_based_future_reference_raises_future_data_error(make_clock: Callable[[int], Clock]) -> None:
    clock = make_clock(NOW)
    for ahead in (1, 250 * MS, SECOND, 3600 * SECOND):
        with pytest.raises(FutureDataError) as excinfo:
            is_fresh_now(clock, reference_ns=NOW + ahead, max_age_ns=10**18)
        assert excinfo.value.code == "GIE.TIME.FUTURE"
        assert excinfo.value.details["now_ns"] == NOW
        assert excinfo.value.details["reference_ns"] == NOW + ahead
        assert excinfo.value.details["future_tolerance_ns"] == 0
        with pytest.raises(FutureDataError):
            require_fresh_now(clock, reference_ns=NOW + ahead, max_age_ns=10**18, what="tick")


@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
@pytest.mark.parametrize("tolerance", [1, 2, 5 * MS, SECOND - 1, SECOND], ids=repr)
@pytest.mark.parametrize("offset", [-7, 0, 1], ids=["past", "now", "future"])
def test_replay_refuses_any_non_zero_future_tolerance(tolerance: int, offset: int) -> None:
    """In replay a tolerance is look-ahead: refused whether or not the data is in the future."""
    clock = ReplayClock(NOW)
    reference = NOW + offset
    with pytest.raises(ContractValidationError) as as_bool:
        is_fresh_now(clock, reference_ns=reference, max_age_ns=10, future_tolerance_ns=tolerance)
    with pytest.raises(ContractValidationError) as as_age:
        require_fresh_now(
            clock, reference_ns=reference, max_age_ns=10, what="tick", future_tolerance_ns=tolerance
        )
    for excinfo in (as_bool, as_age):
        error = excinfo.value
        assert error.code == "GIE.CONTRACT.INVALID"
        assert not isinstance(error, TimeError)
        assert "replay" in error.message.lower()
        assert error.details["value"] == tolerance
        assert strict_loads(canonical_dumps(error.to_dict())) == error.to_dict()
    assert clock.now_ns() == NOW


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_replay_accepts_a_zero_tolerance_and_still_rejects_future_data() -> None:
    clock = ReplayClock(NOW)
    assert is_fresh_now(clock, reference_ns=NOW - 7, max_age_ns=8, future_tolerance_ns=0) is True
    assert require_fresh_now(clock, reference_ns=NOW, max_age_ns=1, what="tick", future_tolerance_ns=0) == 0
    with pytest.raises(FutureDataError):
        is_fresh_now(clock, reference_ns=NOW + 1, max_age_ns=8, future_tolerance_ns=0)
    with pytest.raises(FutureDataError):
        require_fresh_now(clock, reference_ns=NOW + 1, max_age_ns=8, what="tick", future_tolerance_ns=0)


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize("tolerance", [1, 5 * MS, SECOND])
def test_paper_tolerates_bounded_cross_host_skew(tolerance: int) -> None:
    """The tolerance exists for paper mode only; within it the age is reported as zero."""
    clock = _paper(NOW)
    at_tolerance = NOW + tolerance
    assert is_fresh_now(clock, reference_ns=at_tolerance, max_age_ns=1, future_tolerance_ns=tolerance) is True
    assert (
        require_fresh_now(
            clock, reference_ns=at_tolerance, max_age_ns=1, what="tick", future_tolerance_ns=tolerance
        )
        == 0
    )
    with pytest.raises(FutureDataError) as excinfo:
        is_fresh_now(clock, reference_ns=at_tolerance + 1, max_age_ns=1, future_tolerance_ns=tolerance)
    assert excinfo.value.details["future_tolerance_ns"] == tolerance
    with pytest.raises(FutureDataError):
        require_fresh_now(
            clock, reference_ns=at_tolerance + 1, max_age_ns=1, what="tick", future_tolerance_ns=tolerance
        )
    # the tolerance never extends max_age for data from the past
    assert is_fresh_now(clock, reference_ns=NOW - 8, max_age_ns=8, future_tolerance_ns=tolerance) is False


@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
@_CLOCKS
@pytest.mark.parametrize("tolerance", [SECOND + 1, 10**12, INT64_MAX, -1, -SECOND], ids=repr)
def test_clock_based_helpers_bound_the_tolerance_in_every_mode(
    make_clock: Callable[[int], Clock], tolerance: int
) -> None:
    clock = make_clock(NOW)
    with pytest.raises(ContractValidationError) as excinfo:
        is_fresh_now(clock, reference_ns=NOW - 1, max_age_ns=10, future_tolerance_ns=tolerance)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"
    with pytest.raises(ContractValidationError):
        require_fresh_now(
            clock, reference_ns=NOW - 1, max_age_ns=10, what="tick", future_tolerance_ns=tolerance
        )


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@_CLOCKS
@pytest.mark.parametrize("bad", [True, False, 1.0, 0.0, "0", None, Decimal("0"), [0]], ids=repr)
def test_clock_based_helpers_reject_a_tolerance_that_is_not_an_int(
    make_clock: Callable[[int], Clock], bad: Any
) -> None:
    """``False`` and ``0.0`` are not the integer zero: nothing is coerced, also not in replay."""
    clock = make_clock(NOW)
    with pytest.raises(ContractValidationError):
        is_fresh_now(clock, reference_ns=NOW - 1, max_age_ns=10, future_tolerance_ns=bad)
    with pytest.raises(ContractValidationError):
        require_fresh_now(clock, reference_ns=NOW - 1, max_age_ns=10, what="tick", future_tolerance_ns=bad)


@pytest.mark.requirement("S00-ASG-23", "S00-ASG-24")
@_CLOCKS
def test_clock_based_helpers_validate_their_other_arguments(make_clock: Callable[[int], Clock]) -> None:
    clock = make_clock(NOW)
    for max_age in (0, -1, 2**63, True, 1.0, "10", None):
        with pytest.raises(ContractValidationError):
            is_fresh_now(clock, reference_ns=NOW, max_age_ns=max_age)  # type: ignore[arg-type]
        with pytest.raises(ContractValidationError):
            require_fresh_now(clock, reference_ns=NOW, max_age_ns=max_age, what="tick")  # type: ignore[arg-type]
    for reference in (-1, 2**63, True, 1.5, "0", None):
        with pytest.raises(ContractValidationError):
            is_fresh_now(clock, reference_ns=reference, max_age_ns=10)  # type: ignore[arg-type]
        with pytest.raises(ContractValidationError):
            require_fresh_now(clock, reference_ns=reference, max_age_ns=10, what="tick")  # type: ignore[arg-type]
    for valid_until in (-1, 2**63, True, 1.5, "0", None):
        with pytest.raises(ContractValidationError):
            is_expired_now(clock, valid_until_ns=valid_until)  # type: ignore[arg-type]
        with pytest.raises(ContractValidationError):
            require_not_expired_now(clock, valid_until_ns=valid_until, what="plan")  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@_CLOCKS
@pytest.mark.parametrize(
    ("offset", "expired"),
    [(1, False), (250 * MS, False), (0, True), (-1, True), (-250 * MS, True)],
    ids=["one-ns-left", "250ms-left", "at-expiry", "one-ns-late", "250ms-late"],
)
def test_clock_based_expiry_is_evaluated_at_the_clock_reading(
    make_clock: Callable[[int], Clock], offset: int, expired: bool
) -> None:
    clock = make_clock(NOW)
    valid_until = NOW + offset
    assert is_expired_now(clock, valid_until_ns=valid_until) is expired
    assert is_expired_now(clock, valid_until_ns=valid_until) is is_expired(
        now_ns=NOW, valid_until_ns=valid_until
    )
    if expired:
        with pytest.raises(StaleDataError) as excinfo:
            require_not_expired_now(clock, valid_until_ns=valid_until, what="risk decision")
        error = excinfo.value
        assert error.code == "GIE.TIME.STALE"
        assert error.details == {"what": "risk decision", "now_ns": NOW, "valid_until_ns": valid_until}
        assert "risk decision" in error.message
    else:
        remaining = require_not_expired_now(clock, valid_until_ns=valid_until, what="risk decision")
        assert remaining == offset
        assert type(remaining) is int


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_replay_expiry_follows_virtual_time() -> None:
    valid_until = NOW + 5
    clock = ReplayClock(NOW)
    assert require_not_expired_now(clock, valid_until_ns=valid_until, what="plan") == 5
    clock.advance_to(valid_until - 1)
    assert is_expired_now(clock, valid_until_ns=valid_until) is False
    assert require_not_expired_now(clock, valid_until_ns=valid_until, what="plan") == 1
    clock.advance_by(1)  # at the expiry instant it is already expired
    assert is_expired_now(clock, valid_until_ns=valid_until) is True
    with pytest.raises(StaleDataError):
        require_not_expired_now(clock, valid_until_ns=valid_until, what="plan")
    clock.advance_by(10**12)
    assert is_expired_now(clock, valid_until_ns=valid_until) is True


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_a_timer_callback_sees_freshness_at_its_due_instant() -> None:
    """While a replay timer runs ``now_ns`` is its due time, so expiry checks inside it are exact."""
    valid_until = NOW + 100
    clock = ReplayClock(NOW)
    seen: list[tuple[int, bool]] = []
    for due in (valid_until - 1, valid_until):
        clock.call_at(due, lambda due_ns: seen.append((due_ns, is_expired_now(clock, valid_until_ns=valid_until))))
    clock.advance_to(NOW + 10**9)
    assert seen == [(valid_until - 1, False), (valid_until, True)]


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_paper_clock_helpers_use_the_injected_time_source() -> None:
    """The helpers ask the clock, never the operating system: the injected reading decides."""
    readings = iter([NOW, NOW + 249 * MS, NOW + 250 * MS, NOW + 251 * MS])
    clock = SystemClock(wall_source=lambda: next(readings), monotonic_source=lambda: 0)
    verdicts = [is_fresh_now(clock, reference_ns=NOW, max_age_ns=250 * MS) for _ in range(4)]
    assert verdicts == [True, True, False, False]


class _ModeAsPlainText:
    """Synthetic clock double whose ``mode`` is the wire text ``"REPLAY"`` instead of the enum member.

    ``ClockMode`` is a ``str`` enum, so this value compares equal to ``ClockMode.REPLAY``.
    """

    regressions_observed = 0

    def __init__(self, now_ns: int) -> None:
        self._now_ns = now_ns
        self.mode = "REPLAY"

    def now_ns(self) -> int:
        return self._now_ns

    def monotonic_ns(self) -> int:
        return 0


@pytest.mark.requirement("S00-ASG-23", "G03-12", "S00-ASG-24")
def test_a_clock_that_reports_replay_as_text_gets_no_future_tolerance() -> None:
    """Fail closed: the tolerance "exists for paper mode only".

    A clock whose mode equals ``ClockMode.REPLAY`` is a replay clock; granting it a tolerance lets
    data from the future through as "fresh, age 0", which is exactly the look-ahead the rule forbids.
    """
    clock: Any = _ModeAsPlainText(NOW)
    assert clock.mode == ClockMode.REPLAY
    assert isinstance(clock, Clock)
    assert is_fresh_now(clock, reference_ns=NOW - 1, max_age_ns=10) is True  # usable without a tolerance
    with pytest.raises(ContractValidationError):
        is_fresh_now(clock, reference_ns=NOW + 5, max_age_ns=10, future_tolerance_ns=5)
    with pytest.raises(ContractValidationError):
        require_fresh_now(clock, reference_ns=NOW + 5, max_age_ns=10, what="tick", future_tolerance_ns=5)


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@PROPERTY
@example(now_ns=NOW, reference_ns=NOW, max_age_ns=1, valid_until_ns=NOW)
@example(now_ns=NOW, reference_ns=NOW + 1, max_age_ns=1, valid_until_ns=NOW + 1)
@example(now_ns=0, reference_ns=0, max_age_ns=INT64_MAX, valid_until_ns=0)
@example(now_ns=INT64_MAX, reference_ns=0, max_age_ns=INT64_MAX, valid_until_ns=INT64_MAX)
@given(now_ns=_INSTANTS, reference_ns=_INSTANTS, max_age_ns=_DURATIONS, valid_until_ns=_INSTANTS)
def test_clock_based_helpers_agree_with_the_plain_functions(
    now_ns: int, reference_ns: int, max_age_ns: int, valid_until_ns: int
) -> None:
    """The ``*_now`` forms are the plain functions evaluated at ``clock.now_ns()``, in both modes."""
    for make_clock in _CLOCK_FACTORIES.values():
        clock = make_clock(now_ns)
        if reference_ns > now_ns:
            with pytest.raises(FutureDataError):
                is_fresh_now(clock, reference_ns=reference_ns, max_age_ns=max_age_ns)
            with pytest.raises(FutureDataError):
                require_fresh_now(clock, reference_ns=reference_ns, max_age_ns=max_age_ns, what="synthetic")
        else:
            age = now_ns - reference_ns
            assert is_fresh_now(clock, reference_ns=reference_ns, max_age_ns=max_age_ns) is (age < max_age_ns)
            if age < max_age_ns:
                assert (
                    require_fresh_now(
                        clock, reference_ns=reference_ns, max_age_ns=max_age_ns, what="synthetic"
                    )
                    == age
                )
            else:
                with pytest.raises(StaleDataError) as excinfo:
                    require_fresh_now(
                        clock, reference_ns=reference_ns, max_age_ns=max_age_ns, what="synthetic"
                    )
                assert excinfo.value.details["age_ns"] == age
        usable = now_ns < valid_until_ns
        assert is_expired_now(clock, valid_until_ns=valid_until_ns) is (not usable)
        if usable:
            assert require_not_expired_now(clock, valid_until_ns=valid_until_ns, what="synthetic") == (
                valid_until_ns - now_ns
            )
        else:
            with pytest.raises(StaleDataError):
                require_not_expired_now(clock, valid_until_ns=valid_until_ns, what="synthetic")
