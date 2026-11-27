"""Independent tests for ``gie.core.freshness`` (requirements S00-ASG-23, G03-12).

The expected behaviour is the normative text of ``specifications/SHARED_CONTRACTS.md``
section 3.3 and decision D-010:

* *Expiry*: a record is usable iff ``now_ns < valid_until_ns``; at the expiry instant it is
  already expired.
* *Age*: data is fresh iff ``0 <= now_ns - reference_ns < max_age_ns``.
* *Future data*: a reference later than ``now_ns`` is never fresh; it raises
  ``FutureDataError``.  ``future_tolerance_ns`` is the largest amount by which the reference
  may lie in the future; within the tolerance the age is reported as ``0``.

All instants are synthetic fixtures passed in explicitly; nothing here reads a clock.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import (
    ContractError,
    ContractValidationError,
    FutureDataError,
    GieError,
    StaleDataError,
    TimeError,
)
from gie.core.freshness import age_ns, is_expired, is_fresh, require_fresh, require_not_expired

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
    for tolerance in (0, 1, 5 * MS, 10**12):
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
_TOLERANCES = st.integers(min_value=0, max_value=10**10)


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@PROPERTY
@example(now_ns=NOW, reference_ns=NOW, max_age_ns=1, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW - 250 * MS, max_age_ns=250 * MS, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW - 250 * MS + 1, max_age_ns=250 * MS, tolerance=0)
@example(now_ns=NOW, reference_ns=NOW + 5, max_age_ns=250 * MS, tolerance=5)
@example(now_ns=NOW, reference_ns=NOW + 6, max_age_ns=250 * MS, tolerance=5)
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
