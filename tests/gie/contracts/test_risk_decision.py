"""Independent tests for the ``RiskDecision`` contract and ``validate_daily_state_sequence``.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (sections 3.4, 9.1, 9.2),
``specifications/contracts/FIELDS.md`` (RiskDecision fields and invariants RSK-1..12) and the
owner's master specification G05 ("emergency halt", "nondecreasing daily profit floor", "at
exactly the floor no new risk", "track Prague day") - not from the implementation.

SYNTHETIC FIXTURE: every record in this module is built with the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``).  The sizes, fractions, equity values
and instants are invented.  They are not market data, not account data and not evidence of any
trading performance.  The percentages used are fixture numbers, not approved limits.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gie.contracts import (
    DailyState,
    LifecycleLedger,
    RiskDecision,
    RiskOutcome,
    from_wire,
    identity,
    to_wire,
    validate_daily_state_sequence,
)
from gie.core.errors import ContractValidationError, IllegalTransitionError
from gie.core.timeutil import NS_PER_HOUR, NS_PER_SECOND
from tests.gie import samples

pytestmark = pytest.mark.requirement("S00-ASG-15")

T0 = samples.T0
#: One unit in the last place of a wire decimal (12 fractional digits are the canonical maximum).
ULP = D("0.000000000001")
PROTECT = DailyState.PROFIT_PROTECTION
APPROVALS = (RiskOutcome.APPROVED, RiskOutcome.APPROVED_REDUCED)


def _utc_ns(*wall_time: int) -> int:
    """Instant of a UTC wall time ``(year, month, day[, hour[, minute]])``.

    Plain UTC arithmetic of the standard library, independent of the code under test.
    """
    moment = dt.datetime(*wall_time, tzinfo=dt.UTC)  # type: ignore[arg-type,misc]
    return int(moment.timestamp()) * NS_PER_SECOND


def _violates(invariant: str, build: Callable[[], object]) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as caught:
        build()
    assert caught.value.details.get("invariant") == invariant, caught.value
    assert caught.value.details.get("contract") == "RiskDecision"
    assert caught.value.code == "GIE.CONTRACT.INVALID"
    return caught.value


def _field_error(path: str, build: Callable[[], object]) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as caught:
        build()
    assert caught.value.details.get("path") == path, caught.value
    return caught.value


def _approval(outcome: RiskOutcome = RiskOutcome.APPROVED, **overrides: Any) -> RiskDecision:
    if outcome is RiskOutcome.APPROVED_REDUCED:
        overrides.setdefault("reason_codes", ("FIXTURE_REDUCTION",))
    return samples.risk_decision(outcome=outcome, **overrides)


def _at(time_ns: int, **overrides: Any) -> RiskDecision:
    """An approval decided at *time_ns* (watermark and expiry moved along)."""
    fields: dict[str, Any] = {
        "decision_time_ns": time_ns,
        "as_of_time_ns": time_ns,
        "valid_until_ns": time_ns + 30 * NS_PER_SECOND,
    }
    fields.update(overrides)
    return samples.risk_decision(**fields)


def _rejected_at(time_ns: int, **overrides: Any) -> RiskDecision:
    fields: dict[str, Any] = {
        "decision_time_ns": time_ns,
        "as_of_time_ns": time_ns,
        "valid_until_ns": time_ns + 30 * NS_PER_SECOND,
    }
    fields.update(overrides)
    return samples.rejected_risk_decision(**fields)


# --------------------------------------------------------------------------- samples
def test_every_named_sample_decision_is_a_valid_record_and_survives_the_wire() -> None:
    variants = samples.variants()["RiskDecision"]
    assert set(variants) == {
        "approved",
        "approved_reduced_defensive",
        "approved_within_surplus",
        "rejected_at_floor",
        "rejected_halt",
    }
    for record in variants.values():
        assert type(record) is RiskDecision
        assert from_wire(to_wire(record), RiskDecision) == record


def test_approved_property_follows_the_outcome() -> None:
    assert samples.risk_decision().approved is True
    assert _approval(RiskOutcome.APPROVED_REDUCED).approved is True
    assert samples.rejected_risk_decision().approved is False


# --------------------------------------------------------------------------- RSK-1
@pytest.mark.requirement("S08-ACC-06")
def test_rsk1_decision_id_is_derived_from_run_plan_and_attempt() -> None:
    decision = samples.risk_decision()
    assert decision.decision_id == identity.risk_decision_id(decision.run_id, decision.plan_id, 0)
    again = samples.risk_decision(attempt=1, seq=1)
    assert again.decision_id == identity.risk_decision_id(again.run_id, again.plan_id, 1)
    assert again.decision_id != decision.decision_id


@pytest.mark.requirement("S08-ACC-06")
def test_rsk1_refuses_a_decision_id_of_another_attempt_plan_or_run() -> None:
    good = samples.risk_decision()
    other_plan = samples.trade_plan(variant=1)
    other_run = identity.run_id("REPLAY", "1" * 64, "other-fixture")
    foreign_ids = [
        identity.risk_decision_id(good.run_id, good.plan_id, 1),
        identity.risk_decision_id(good.run_id, other_plan.plan_id, 0),
        identity.risk_decision_id(other_run, good.plan_id, 0),
    ]
    for foreign in foreign_ids:
        _violates("RSK-1", lambda foreign=foreign: samples.risk_decision(decision_id=foreign))


# --------------------------------------------------------------------------- RSK-2
def test_rsk2_a_rejection_that_grants_nothing_is_accepted() -> None:
    rejected = samples.rejected_risk_decision()
    assert rejected.volume_lots == 0
    assert rejected.risk_amount == 0
    assert rejected.risk_frac == 0
    assert rejected.reservation_id is None


@pytest.mark.parametrize(
    "grant",
    [
        {"volume_lots": D("0.01")},
        {"risk_amount": D("0.01"), "risk_frac": D("0.001")},
        {"risk_frac": D("0.000000000001")},
        {"volume_lots": D("0.50"), "risk_amount": D("104.00"), "risk_frac": D("0.00104")},
    ],
    ids=["volume", "risk_amount", "risk_frac", "everything"],
)
def test_rsk2_a_rejection_may_not_grant_volume_or_risk(grant: dict[str, D]) -> None:
    _violates("RSK-2", lambda: samples.rejected_risk_decision(**grant))


@pytest.mark.requirement("S08-ACC-06")
def test_rsk2_a_rejection_may_not_reserve_risk() -> None:
    decision_id = samples.rejected_risk_decision().decision_id
    _violates(
        "RSK-2",
        lambda: samples.rejected_risk_decision(reservation_id=identity.reservation_id(decision_id)),
    )


# --------------------------------------------------------------------------- RSK-3
@pytest.mark.requirement("S08-ACC-06")
@pytest.mark.parametrize("outcome", APPROVALS)
def test_rsk3_an_approval_carries_volume_risk_and_its_derived_reservation(outcome: RiskOutcome) -> None:
    decision = _approval(outcome)
    assert decision.volume_lots > 0
    assert decision.risk_amount > 0
    assert decision.risk_frac > 0
    assert decision.reservation_id == identity.reservation_id(decision.decision_id)


@pytest.mark.parametrize("outcome", APPROVALS)
@pytest.mark.parametrize(
    "missing",
    [
        {"volume_lots": D("0")},
        {"risk_amount": D("0")},
        {"risk_amount": D("0"), "risk_frac": D("0")},
        {"volume_lots": D("0"), "risk_amount": D("0"), "risk_frac": D("0")},
    ],
    ids=["no_volume", "no_risk_amount", "no_risk", "nothing"],
)
def test_rsk3_an_approval_without_volume_or_risk_is_refused(
    outcome: RiskOutcome, missing: dict[str, D]
) -> None:
    _violates("RSK-3", lambda: _approval(outcome, **missing))


@pytest.mark.requirement("S08-ACC-06")
@pytest.mark.parametrize("outcome", APPROVALS)
def test_rsk3_an_approval_needs_exactly_its_own_reservation(outcome: RiskOutcome) -> None:
    other = identity.reservation_id(samples.risk_decision(attempt=7).decision_id)
    _violates("RSK-3", lambda: _approval(outcome, reservation_id=None))
    _violates("RSK-3", lambda: _approval(outcome, reservation_id=other))


def test_rsk3_a_zero_risk_fraction_with_a_positive_amount_is_never_an_approval() -> None:
    with pytest.raises(ContractValidationError) as caught:
        samples.risk_decision(risk_frac=D("0"))
    assert caught.value.details.get("invariant") in {"RSK-3", "RSK-9"}


# --------------------------------------------------------------------------- RSK-4
def test_rsk4_reasons_are_optional_for_a_plain_approval_only() -> None:
    assert samples.risk_decision(reason_codes=()).reason_codes == ()
    assert _approval(RiskOutcome.APPROVED_REDUCED, reason_codes=("B_CODE", "A_CODE")).reason_codes == (
        "A_CODE",
        "B_CODE",
    )
    _violates("RSK-4", lambda: _approval(RiskOutcome.APPROVED_REDUCED, reason_codes=()))
    _violates("RSK-4", lambda: samples.rejected_risk_decision(reason_codes=()))


# --------------------------------------------------------------------------- RSK-5
@pytest.mark.requirement("G05-04")
def test_rsk5_emergency_halt_can_only_reject() -> None:
    halted = samples.rejected_risk_decision(
        daily_state=DailyState.EMERGENCY_HALT, daily_pnl_frac=D("-0.03"), reason_codes=("EMERGENCY_HALT",)
    )
    assert halted.outcome is RiskOutcome.REJECTED
    assert halted.approved is False


@pytest.mark.requirement("G05-04")
@pytest.mark.parametrize("outcome", APPROVALS)
@pytest.mark.parametrize("pnl", [D("-0.03"), D("-0.5"), D("0"), D("0.2")])
def test_rsk5_emergency_halt_never_approves_whatever_the_pnl(outcome: RiskOutcome, pnl: D) -> None:
    _violates(
        "RSK-5",
        lambda: _approval(outcome, daily_state=DailyState.EMERGENCY_HALT, daily_pnl_frac=pnl),
    )


# --------------------------------------------------------------------------- RSK-6
@pytest.mark.requirement("G05-06")
def test_rsk6_the_floor_exists_exactly_in_profit_protection() -> None:
    protected = _approval(daily_state=PROTECT, daily_pnl_frac=D("0.062"), profit_floor_frac=D("0.05"))
    assert protected.profit_floor_frac == D("0.05")
    for state in (DailyState.NORMAL, DailyState.DEFENSIVE):
        assert _approval(daily_state=state).profit_floor_frac is None
    _violates("RSK-6", lambda: _approval(daily_state=PROTECT, daily_pnl_frac=D("0.062")))
    for state in (DailyState.NORMAL, DailyState.DEFENSIVE):
        _violates(
            "RSK-6",
            lambda state=state: _approval(
                daily_state=state, daily_pnl_frac=D("0.062"), profit_floor_frac=D("0.05")
            ),
        )
    _violates(
        "RSK-6",
        lambda: samples.rejected_risk_decision(
            daily_state=DailyState.EMERGENCY_HALT, daily_pnl_frac=D("-0.03"), profit_floor_frac=D("0.05")
        ),
    )


# --------------------------------------------------------------------------- RSK-7
@pytest.mark.requirement("G05-08")
@pytest.mark.parametrize("outcome", APPROVALS)
@pytest.mark.parametrize(
    ("pnl", "floor"),
    [
        (D("0.05"), D("0.05")),
        (D("0.050"), D("0.05")),
        (D("0.05"), D("0.050000000000")),
        (D("0.049999999999"), D("0.05")),
        (D("0.01"), D("0.05")),
        (D("-0.01"), D("0.05")),
    ],
    ids=["exactly", "exactly_other_scale", "exactly_padded", "one_ulp_below", "below", "negative"],
)
def test_rsk7_at_or_below_the_floor_only_a_rejection_can_be_constructed(
    outcome: RiskOutcome, pnl: D, floor: D
) -> None:
    # The smallest representable risk is still new risk: nothing may be approved at the floor.
    _violates(
        "RSK-7",
        lambda: _approval(
            outcome,
            daily_state=PROTECT,
            daily_pnl_frac=pnl,
            profit_floor_frac=floor,
            risk_frac=ULP,
            risk_amount=D("0.0000001"),
        ),
    )
    rejected = samples.rejected_risk_decision(
        daily_state=PROTECT, daily_pnl_frac=pnl, profit_floor_frac=floor
    )
    assert rejected.outcome is RiskOutcome.REJECTED


@pytest.mark.requirement("G05-08")
def test_rsk7_is_also_enforced_when_a_decision_is_decoded() -> None:
    wire = to_wire(samples.variants()["RiskDecision"]["approved_within_surplus"])
    assert wire["profit_floor_frac"] == "0.05"
    wire["daily_pnl_frac"] = "0.05"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire, RiskDecision)
    assert caught.value.details.get("invariant") == "RSK-7"


# --------------------------------------------------------------------------- RSK-8
@pytest.mark.requirement("G05-08", "G05-06")
@pytest.mark.parametrize("outcome", APPROVALS)
def test_rsk8_a_hair_above_the_floor_only_the_surplus_may_be_risked(outcome: RiskOutcome) -> None:
    floor = D("0.05")
    hair_above = floor + ULP
    exact = _approval(
        outcome,
        daily_state=PROTECT,
        daily_pnl_frac=hair_above,
        profit_floor_frac=floor,
        risk_frac=ULP,
        risk_amount=D("0.0000001"),
    )
    assert exact.risk_frac == exact.daily_pnl_frac - exact.profit_floor_frac
    _violates(
        "RSK-8",
        lambda: _approval(
            outcome,
            daily_state=PROTECT,
            daily_pnl_frac=hair_above,
            profit_floor_frac=floor,
            risk_frac=2 * ULP,
            risk_amount=D("0.0000001"),
        ),
    )


@pytest.mark.requirement("G05-08", "G05-06")
def test_rsk8_surplus_boundary_exactly_equal_and_one_unit_in_the_last_place_above() -> None:
    floor, pnl = D("0.05"), D("0.05104")
    surplus = pnl - floor
    assert surplus == D("0.00104")
    equal = _approval(daily_state=PROTECT, daily_pnl_frac=pnl, profit_floor_frac=floor, risk_frac=surplus)
    assert equal.risk_frac == surplus
    below = _approval(
        daily_state=PROTECT,
        daily_pnl_frac=pnl,
        profit_floor_frac=floor,
        risk_frac=surplus - ULP,
        risk_amount=D("103.99"),
    )
    assert below.risk_frac < surplus
    _violates(
        "RSK-8",
        lambda: _approval(
            daily_state=PROTECT, daily_pnl_frac=pnl, profit_floor_frac=floor, risk_frac=surplus + ULP
        ),
    )


@pytest.mark.requirement("G05-08")
def test_rsk8_does_not_restrict_a_rejection_above_the_floor() -> None:
    rejected = samples.rejected_risk_decision(
        daily_state=PROTECT, daily_pnl_frac=D("0.09"), profit_floor_frac=D("0.05")
    )
    assert rejected.risk_frac == 0


@pytest.mark.requirement("G05-08", "G05-06")
@settings(derandomize=True, max_examples=150, deadline=None)
@given(
    pnl_units=st.integers(min_value=-2_000, max_value=120_000),
    floor_units=st.integers(min_value=1, max_value=100_000),
    risk_units=st.integers(min_value=1, max_value=30_000),
)
def test_rsk7_rsk8_an_approval_exists_iff_pnl_is_above_the_floor_and_risk_within_surplus(
    pnl_units: int, floor_units: int, risk_units: int
) -> None:
    scale = D("0.000001")
    pnl, floor, risk = pnl_units * scale, floor_units * scale, risk_units * scale
    equity = D("100000.00")

    def build() -> RiskDecision:
        return _approval(
            daily_state=PROTECT,
            daily_pnl_frac=pnl,
            profit_floor_frac=floor,
            risk_frac=risk,
            risk_amount=risk * equity,
            reference_equity_amount=equity,
        )

    if pnl > floor and risk <= pnl - floor:
        assert build().approved is True
    else:
        with pytest.raises(ContractValidationError) as caught:
            build()
        assert caught.value.details.get("invariant") == ("RSK-7" if pnl <= floor else "RSK-8")


# --------------------------------------------------------------------------- RSK-9
@pytest.mark.parametrize(
    ("risk_amount", "risk_frac", "equity"),
    [
        (D("104.00"), D("0.00104"), D("100000.00")),
        (D("103.999999999999"), D("0.00104"), D("100000.00")),
        (D("33.333333333333"), D("0.000333333334"), D("100000.00")),
        (D("0.01"), D("1"), D("0.01")),
    ],
    ids=["exact_boundary", "one_ulp_below", "rounded_up_fraction", "whole_equity"],
)
def test_rsk9_the_amount_may_reach_but_not_exceed_fraction_times_equity(
    risk_amount: D, risk_frac: D, equity: D
) -> None:
    decision = _approval(risk_amount=risk_amount, risk_frac=risk_frac, reference_equity_amount=equity)
    assert decision.risk_amount <= decision.risk_frac * decision.reference_equity_amount


@pytest.mark.parametrize(
    ("risk_amount", "risk_frac", "equity"),
    [
        (D("104.000000000001"), D("0.00104"), D("100000.00")),
        (D("104.01"), D("0.00104"), D("100000.00")),
        (D("33.333333333333"), D("0.000333333333"), D("100000.00")),
        (D("104.00"), D("0.00104"), D("99999.99")),
    ],
    ids=["one_ulp_above", "a_cent_above", "fraction_rounded_down", "smaller_equity"],
)
def test_rsk9_an_understated_risk_fraction_is_refused(risk_amount: D, risk_frac: D, equity: D) -> None:
    _violates(
        "RSK-9",
        lambda: _approval(risk_amount=risk_amount, risk_frac=risk_frac, reference_equity_amount=equity),
    )


# --------------------------------------------------------------------------- RSK-10
@pytest.mark.parametrize("outcome", APPROVALS)
def test_rsk10_an_approval_must_expire_after_its_decision_time(outcome: RiskOutcome) -> None:
    assert _approval(outcome, valid_until_ns=T0 + 1).valid_until_ns == T0 + 1
    _violates("RSK-10", lambda: _approval(outcome, valid_until_ns=T0))
    _violates("RSK-10", lambda: _approval(outcome, valid_until_ns=T0 - 1))


def test_rsk10_does_not_apply_to_a_rejection() -> None:
    assert samples.rejected_risk_decision(valid_until_ns=T0).valid_until_ns == T0


# --------------------------------------------------------------------------- RSK-11
#: (instant, Prague trading day, a plausible but wrong day).  Winter: Prague is UTC+1.  Summer: UTC+2.
_PRAGUE_CASES = [
    pytest.param(_utc_ns(2026, 11, 26, 23) - 1, "2026-11-26", "2026-11-27", id="winter_1ns_before_midnight"),
    pytest.param(_utc_ns(2026, 11, 26, 23), "2026-11-27", "2026-11-26", id="winter_midnight"),
    pytest.param(_utc_ns(2026, 11, 26, 23) + 1, "2026-11-27", "2026-11-26", id="winter_1ns_after_midnight"),
    pytest.param(_utc_ns(2026, 11, 26, 22, 30), "2026-11-26", "2026-11-27", id="winter_not_utc_plus_2"),
    pytest.param(_utc_ns(2026, 7, 14, 22) - 1, "2026-07-14", "2026-07-15", id="summer_1ns_before_midnight"),
    pytest.param(_utc_ns(2026, 7, 14, 22), "2026-07-15", "2026-07-14", id="summer_midnight"),
    pytest.param(_utc_ns(2026, 7, 14, 22) + 1, "2026-07-15", "2026-07-14", id="summer_1ns_after_midnight"),
    pytest.param(_utc_ns(2026, 7, 14, 23, 30), "2026-07-15", "2026-07-14", id="summer_not_the_utc_date"),
    pytest.param(_utc_ns(2026, 3, 28, 23) - 1, "2026-03-28", "2026-03-29", id="spring_dst_day_begins_cet"),
    pytest.param(_utc_ns(2026, 3, 29, 22) - 1, "2026-03-29", "2026-03-30", id="spring_dst_day_end_before"),
    pytest.param(_utc_ns(2026, 3, 29, 22), "2026-03-30", "2026-03-29", id="spring_dst_day_ends_cest"),
    pytest.param(_utc_ns(2026, 10, 24, 22), "2026-10-25", "2026-10-24", id="autumn_dst_day_begins_cest"),
    pytest.param(_utc_ns(2026, 10, 25, 22), "2026-10-25", "2026-10-26", id="autumn_dst_25th_hour"),
    pytest.param(_utc_ns(2026, 10, 25, 23) - 1, "2026-10-25", "2026-10-26", id="autumn_dst_day_end_before"),
    pytest.param(_utc_ns(2026, 10, 25, 23), "2026-10-26", "2026-10-25", id="autumn_dst_day_ends_cet"),
]


@pytest.mark.requirement("G05-10")
@pytest.mark.parametrize(("time_ns", "prague_day", "wrong_day"), _PRAGUE_CASES)
def test_rsk11_trading_day_is_the_prague_date_of_the_decision_time(
    time_ns: int, prague_day: str, wrong_day: str
) -> None:
    assert _at(time_ns, trading_day=prague_day).trading_day == prague_day
    assert _rejected_at(time_ns, trading_day=prague_day).trading_day == prague_day
    _violates("RSK-11", lambda: _at(time_ns, trading_day=wrong_day))
    _violates("RSK-11", lambda: _rejected_at(time_ns, trading_day=wrong_day))


@pytest.mark.requirement("G05-10")
def test_rsk11_the_sample_instant_is_a_prague_morning() -> None:
    assert _utc_ns(2026, 11, 26, 9, 30) == T0
    assert samples.risk_decision(trading_day="2026-11-26").trading_day == "2026-11-26"
    _violates("RSK-11", lambda: samples.risk_decision(trading_day="2026-11-25"))


@pytest.mark.requirement("G05-10")
@pytest.mark.parametrize("label", ["2026-02-30", "2026-13-01", "26-11-2026", "2026-11-26T00:00", "20261126"])
def test_trading_day_must_be_a_real_calendar_date_label(label: str) -> None:
    _field_error("trading_day", lambda: samples.risk_decision(trading_day=label))


# --------------------------------------------------------------------------- RSK-12
def test_rsk12_quality_model_is_named_iff_the_size_used_validated_quality() -> None:
    validated = samples.risk_decision(quality_validated=True, quality_model_version_id=samples.MODEL_ID)
    assert validated.quality_model_version_id == samples.MODEL_ID
    assert samples.risk_decision().quality_model_version_id is None
    _violates("RSK-12", lambda: samples.risk_decision(quality_validated=True))
    _violates("RSK-12", lambda: samples.risk_decision(quality_model_version_id=samples.MODEL_ID))
    _violates(
        "RSK-12",
        lambda: samples.rejected_risk_decision(quality_validated=True, quality_model_version_id=None),
    )


# --------------------------------------------------------------------------- fields
def test_plan_fingerprint_accepts_a_lower_case_sha256() -> None:
    assert samples.risk_decision(plan_fingerprint="0123456789abcdef" * 4).plan_fingerprint.startswith("0123")


@pytest.mark.parametrize(
    "fingerprint",
    ["A" * 64, "ab" * 31 + "a", "ab" * 32 + "a", "g" * 64, " " * 64, "ab" * 31 + "a\n", ""],
    ids=["upper_case", "63_chars", "65_chars", "not_hex", "blank", "trailing_newline", "empty"],
)
def test_plan_fingerprint_must_match_the_sha256_pattern(fingerprint: str) -> None:
    _field_error("plan_fingerprint", lambda: samples.risk_decision(plan_fingerprint=fingerprint))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("risk_frac", D("1.000000000001")),
        ("risk_frac", D("-0.001")),
        ("risk_frac", 0.00104),
        ("volume_lots", D("-0.01")),
        ("volume_lots", 0.5),
        ("risk_amount", D("-1")),
        ("risk_amount", 104),
        ("reference_equity_amount", D("0")),
        ("reference_equity_amount", D("NaN")),
        ("daily_pnl_frac", 0.004),
        ("account_currency", "usd"),
        ("account_currency", "USDT"),
        ("outcome", "APPROVED"),
        ("daily_state", "NORMAL"),
        ("attempt", 65536),
        ("quality_validated", 1),
        ("reservation_id", samples.risk_decision().decision_id),
        ("open_risk_amount", D("-0.01")),
        ("valid_until_ns", -1),
    ],
)
def test_invalid_field_values_are_refused_and_never_coerced(path: str, value: object) -> None:
    _field_error(path, lambda: samples.risk_decision(**{path: value}))


def test_reason_codes_are_unique_upper_case_codes_in_canonical_order() -> None:
    reduced = _approval(RiskOutcome.APPROVED_REDUCED, reason_codes=("Z_LAST", "A_FIRST"))
    assert reduced.reason_codes == ("A_FIRST", "Z_LAST")
    with pytest.raises(ContractValidationError):
        _approval(RiskOutcome.APPROVED_REDUCED, reason_codes=("DUPLICATE", "DUPLICATE"))
    with pytest.raises(ContractValidationError):
        _approval(RiskOutcome.APPROVED_REDUCED, reason_codes=("lower_case",))


def test_a_decision_cannot_depend_on_data_received_after_it_was_made() -> None:
    with pytest.raises(ContractValidationError) as caught:
        samples.risk_decision(as_of_time_ns=T0 + 1)
    assert caught.value.details.get("invariant") == "CAUSAL-1"


# --------------------------------------------------------------------------- daily-state sequence
def _refused_sequence(previous: RiskDecision, decision: RiskDecision) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as caught:
        validate_daily_state_sequence(previous, decision)
    assert caught.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert caught.value.details.get("decision_id") == decision.decision_id
    return caught.value


def _halt(time_ns: int, **overrides: Any) -> RiskDecision:
    fields: dict[str, Any] = {
        "daily_state": DailyState.EMERGENCY_HALT,
        "daily_pnl_frac": D("-0.03"),
        "reason_codes": ("EMERGENCY_HALT",),
    }
    fields.update(overrides)
    return _rejected_at(time_ns, **fields)


def _protected(time_ns: int, floor: str, pnl: str = "0.09", **overrides: Any) -> RiskDecision:
    return _at(time_ns, daily_state=PROTECT, daily_pnl_frac=D(pnl), profit_floor_frac=D(floor), **overrides)


LATER = T0 + 60 * NS_PER_SECOND
NEXT_DAY = T0 + 24 * NS_PER_HOUR


def test_the_first_decision_of_a_gate_has_no_predecessor_to_violate() -> None:
    validate_daily_state_sequence(None, _halt(T0))
    validate_daily_state_sequence(None, _protected(T0, "0.05"))


@pytest.mark.requirement("G05-04")
def test_same_day_states_other_than_halt_may_follow_each_other_freely() -> None:
    normal = _at(T0)
    defensive = _at(LATER, seq=1, attempt=1, daily_state=DailyState.DEFENSIVE, daily_pnl_frac=D("-0.02"))
    back = _at(LATER + 1, seq=2, attempt=2)
    halt = _halt(LATER + 2, seq=3, attempt=3)
    assert {d.trading_day for d in (normal, defensive, back, halt)} == {"2026-11-26"}
    validate_daily_state_sequence(normal, defensive)
    validate_daily_state_sequence(defensive, back)
    validate_daily_state_sequence(back, halt)


@pytest.mark.requirement("G05-04")
@pytest.mark.parametrize("state", [DailyState.NORMAL, DailyState.DEFENSIVE, PROTECT])
def test_an_emergency_halt_stays_in_force_for_the_rest_of_the_trading_day(state: DailyState) -> None:
    halt = _halt(T0)
    extra: dict[str, Any] = {"daily_state": state, "seq": 1, "attempt": 1}
    if state is PROTECT:
        extra.update(daily_pnl_frac=D("0.09"), profit_floor_frac=D("0.05"))
    rejected_after = _rejected_at(LATER, **extra)
    approved_after = _at(LATER, **extra)
    assert halt.trading_day == rejected_after.trading_day == approved_after.trading_day == "2026-11-26"
    for follower in (rejected_after, approved_after):
        error = _refused_sequence(halt, follower)
        assert "halt" in str(error.details.get("rule"))
    # Just before the next Prague midnight the halt is still in force.
    last_instant = _utc_ns(2026, 11, 26, 23) - 1
    still_today = _rejected_at(last_instant, trading_day="2026-11-26", **extra)
    _refused_sequence(halt, still_today)


@pytest.mark.requirement("G05-04")
def test_a_halt_may_be_repeated_within_the_day() -> None:
    validate_daily_state_sequence(_halt(T0), _halt(LATER, seq=1, attempt=1))


@pytest.mark.requirement("G05-04", "G05-10")
def test_an_emergency_halt_resets_on_the_next_prague_trading_day() -> None:
    halt = _halt(T0)
    midnight = _utc_ns(2026, 11, 26, 23)
    first_of_new_day = _at(midnight, seq=1, attempt=1, trading_day="2026-11-27")
    tomorrow = _at(NEXT_DAY, seq=1, attempt=1, trading_day="2026-11-27")
    validate_daily_state_sequence(halt, first_of_new_day)
    validate_daily_state_sequence(halt, tomorrow)


@pytest.mark.requirement("G05-06")
def test_the_daily_profit_floor_may_stay_or_rise_within_the_day() -> None:
    first = _protected(T0, "0.05")
    validate_daily_state_sequence(first, _protected(LATER, "0.05", seq=1, attempt=1))
    validate_daily_state_sequence(first, _protected(LATER, "0.050000000001", seq=1, attempt=1))
    validate_daily_state_sequence(first, _protected(LATER, "0.08", seq=1, attempt=1))


@pytest.mark.requirement("G05-06")
@pytest.mark.parametrize("lower", ["0.049999999999", "0.04", "0.000001"])
def test_the_daily_profit_floor_never_decreases_within_the_day(lower: str) -> None:
    first = _protected(T0, "0.05")
    for follower in (
        _protected(LATER, lower, seq=1, attempt=1),
        _rejected_at(
            LATER, seq=1, attempt=1, daily_state=PROTECT, daily_pnl_frac=D("0.09"), profit_floor_frac=D(lower)
        ),
    ):
        assert follower.trading_day == first.trading_day
        error = _refused_sequence(first, follower)
        assert "floor" in str(error.details.get("rule"))


@pytest.mark.requirement("G05-06", "G05-10")
def test_a_new_trading_day_starts_with_a_new_floor() -> None:
    first = _protected(T0, "0.05")
    validate_daily_state_sequence(
        first, _protected(NEXT_DAY, "0.01", "0.02", seq=1, attempt=1, trading_day="2026-11-27")
    )
    validate_daily_state_sequence(first, _at(NEXT_DAY, seq=1, attempt=1, trading_day="2026-11-27"))


@pytest.mark.requirement("G05-06")
def test_the_floor_cannot_be_lowered_by_leaving_and_re_entering_profit_protection_in_one_day() -> None:
    # SHARED_CONTRACTS 9.2: "Within a trading day ... the profit floor never decreases", enforced by
    # the lifecycle ledger.  0.05 -> (no floor) -> 0.03 on one trading day is a decrease.
    ledger = LifecycleLedger()
    protected = _protected(T0, "0.05", "0.062")
    left = _at(T0 + NS_PER_SECOND, seq=1, attempt=1, daily_pnl_frac=D("0.04"))
    lowered = _protected(T0 + 2 * NS_PER_SECOND, "0.03", "0.045", seq=2, attempt=2)
    assert protected.trading_day == left.trading_day == lowered.trading_day == "2026-11-26"
    assert ledger.admit(protected) is True

    def leave_and_re_enter_lower() -> None:
        ledger.admit(left)
        ledger.admit(lowered)

    with pytest.raises(IllegalTransitionError):
        leave_and_re_enter_lower()


def test_decisions_at_the_same_instant_may_follow_each_other() -> None:
    validate_daily_state_sequence(_at(T0), _at(T0, seq=1, attempt=1))


def test_decision_time_never_goes_back() -> None:
    later = _at(LATER)
    error = _refused_sequence(later, _at(LATER - 1, seq=1, attempt=1))
    assert "decision_time_ns" in str(error.details.get("rule"))
    _refused_sequence(later, _at(T0, seq=1, attempt=1))


@pytest.mark.requirement("G05-10")
def test_the_trading_day_never_goes_back() -> None:
    tomorrow = _at(NEXT_DAY, trading_day="2026-11-27")
    yesterday = _at(T0, seq=1, attempt=1, trading_day="2026-11-26")
    _refused_sequence(tomorrow, yesterday)
    # A halt of an earlier day cannot be "escaped" backwards either.
    _refused_sequence(_halt(NEXT_DAY, trading_day="2026-11-27"), yesterday)
