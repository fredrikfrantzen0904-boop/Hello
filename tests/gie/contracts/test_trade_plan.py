"""Independent tests for the ``TradePlan`` contract (``gie/contracts/trade_plan.py``) and its nested
value objects ``Target`` and ``PlanEstimate``.

ALL DATA IN THIS MODULE IS SYNTHETIC FIXTURE DATA.  Every record is built with the labelled
builders of ``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); entries, stops, targets,
estimates and instants are invented.  Nothing here is a trading plan, a recommendation or evidence
of performance, and no order is created anywhere in this module.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - "TradePlan 1.0.0" with the invariants CAUSAL-1 and
  PLN-1 .. PLN-12, ``Target``, ``EntryStyle`` and ``PlanStatus``;
* ``specifications/SHARED_CONTRACTS.md`` - section 2 (exact decimals, what 1 R is on a plan, only
  XAUUSD can be planned), section 4 (``alternative_group_id = f(opportunity_id)``), section 9.1
  (hard stop on the losing side and destinations on the winning side of the *effective* entry; an
  ARMED or SELECTED plan has positive expected net R) and section 9.2 (a plan's stop never moves
  away from the entry between revisions; final statuses are final);
* ``specifications/extracted/05_Decision_Engine.txt`` - alternative market / limit / retest /
  pullback plans with executable entry, hard invalidation, realistic destination, expected net R
  and uncertainty; stale and negative opportunities are rejected.

The positive-expectancy tests carry S05-ACC-05 as a *requirement* marker: they verify the
contract-level gate that acceptance test rests on, not the decision engine of session 05.
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools
from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    PLAN_STATUS_TRANSITIONS,
    Contribution,
    DataOrigin,
    EntryStyle,
    Horizon,
    ModelStatus,
    OrderType,
    PlanEstimate,
    PlanStatus,
    PriceBasis,
    PriceCondition,
    PriceTrigger,
    RunMode,
    Side,
    Target,
    Timeframe,
    TradePlan,
    from_json,
    from_wire,
    identity,
    ordering_time_ns,
    stream_name,
    to_json,
    to_wire,
    validate_plan_transition,
)
from gie.core.errors import ContractValidationError, GieError, IllegalTransitionError
from gie.core.freshness import is_expired
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
STEP = D("0.000000000001")
OTHER_RUN = identity.run_id("REPLAY", "1" * 64, "other-fixture")
SHADOW_MODEL = identity.model_version_id("fixture_shadow", "0.2.0", "cd" * 32)

Build = Callable[[], object]

HOSTILE_CONTEXTS: dict[str, decimal.Context] = {
    "prec3": decimal.Context(prec=3),
    "prec1_round_up": decimal.Context(prec=1, rounding=decimal.ROUND_UP),
    "prec3_trap_inexact": decimal.Context(prec=3, traps=[decimal.Inexact, decimal.Rounded]),
}

USABLE = (PlanStatus.PREPARED, PlanStatus.ARMED, PlanStatus.SELECTED)
EXECUTABLE = (PlanStatus.ARMED, PlanStatus.SELECTED)
FINAL = (PlanStatus.WITHDRAWN, PlanStatus.EXPIRED, PlanStatus.REJECTED)
NEEDS_REASON = (PlanStatus.WITHDRAWN, PlanStatus.REJECTED)

#: Brief 05 and the EntryStyle documentation: MARKET, LIMIT and STOP use the order type of the same
#: name; RETEST and PULLBACK are executed with a resting LIMIT order or, on confirmation, at MARKET.
VALID_PAIRS = {
    (EntryStyle.MARKET, OrderType.MARKET),
    (EntryStyle.LIMIT, OrderType.LIMIT),
    (EntryStyle.STOP, OrderType.STOP),
    (EntryStyle.RETEST, OrderType.LIMIT),
    (EntryStyle.RETEST, OrderType.MARKET),
    (EntryStyle.PULLBACK, OrderType.LIMIT),
    (EntryStyle.PULLBACK, OrderType.MARKET),
}

#: Section 9.2 and the PlanStatus documentation: a plan never moves back towards PREPARED;
#: WITHDRAWN, EXPIRED and REJECTED are final.
EXPECTED_TRANSITIONS: dict[PlanStatus | None, frozenset[PlanStatus]] = {
    None: frozenset(PlanStatus),
    PlanStatus.PREPARED: frozenset(PlanStatus),
    PlanStatus.ARMED: frozenset({PlanStatus.ARMED, PlanStatus.SELECTED, *FINAL}),
    PlanStatus.SELECTED: frozenset({PlanStatus.SELECTED, *FINAL}),
    PlanStatus.WITHDRAWN: frozenset(),
    PlanStatus.EXPIRED: frozenset(),
    PlanStatus.REJECTED: frozenset(),
}


# --------------------------------------------------------------------------- labelled fixture builders
def _target(price: str, close_frac: str = "0", label: str = "fixture_target") -> Target:
    return Target(label=label, price=D(price), close_frac=D(close_frac))


def _plan(**overrides: Any) -> TradePlan:
    """BUY by LIMIT: entry 2649.80 below the reference 2650.35, stop 2647.80, targets 2652.80 / 2655.00."""
    return samples.trade_plan(**overrides)


def _sell(**overrides: Any) -> TradePlan:
    """SELL by LIMIT: entry 2650.60 above the reference 2650.10, stop 2652.60, targets 2647.60 / 2645.00."""
    fields: dict[str, Any] = {
        "side": Side.SELL,
        "entry_price": D("2650.60"),
        "reference_price": D("2650.10"),
        "stop_price": D("2652.60"),
        "targets": (_target("2647.60", "0.5", "first"), _target("2645.00", "0.5", "prior_low")),
        "invalidations": (),
    }
    fields.update(overrides)
    return samples.trade_plan(**fields)


def _market(side: Side = Side.BUY, **overrides: Any) -> TradePlan:
    """MARKET style: no intended entry level, the geometry refers to the reference price."""
    fields: dict[str, Any] = {
        "side": side,
        "entry_style": EntryStyle.MARKET,
        "entry_order_type": OrderType.MARKET,
        "entry_price": None,
        "invalidations": (),
    }
    if side is Side.SELL:
        fields.update(
            reference_price=D("2650.10"), stop_price=D("2652.10"), targets=(_target("2646.10", "1", "first"),)
        )
    fields.update(overrides)
    return samples.trade_plan(**fields)


def _pullback(side: Side = Side.BUY, order_type: OrderType = OrderType.MARKET, **overrides: Any) -> TradePlan:
    """PULLBACK: wait for price to come back to the entry; the target lies between entry and reference."""
    fields: dict[str, Any] = {
        "side": side,
        "entry_style": EntryStyle.PULLBACK,
        "entry_order_type": order_type,
        "invalidations": (),
    }
    if side is Side.BUY:
        fields.update(
            reference_price=D("2650.35"),
            entry_price=D("2648.20"),
            stop_price=D("2646.90"),
            targets=(_target("2650.00", "1", "back_to_quote"),),
        )
    else:
        fields.update(
            reference_price=D("2650.10"),
            entry_price=D("2652.00"),
            stop_price=D("2653.30"),
            targets=(_target("2650.50", "1", "back_to_quote"),),
        )
    fields.update(overrides)
    return samples.trade_plan(**fields)


def _styled(style: EntryStyle, order_type: OrderType, **overrides: Any) -> TradePlan:
    fields: dict[str, Any] = {"entry_style": style, "entry_order_type": order_type}
    if style is EntryStyle.MARKET:
        fields["entry_price"] = None
    fields.update(overrides)
    return samples.trade_plan(**fields)


def _with_status(status: PlanStatus, revision: int = 0, **overrides: Any) -> TradePlan:
    fields: dict[str, Any] = {"status": status, "revision": revision, "seq": revision}
    if status in NEEDS_REASON:
        fields["status_reasons"] = ("FIXTURE_REASON",)
    fields.update(overrides)
    return samples.trade_plan(**fields)


def _changed(record: TradePlan, **changes: Any) -> TradePlan:
    """Copy *record* with single fields replaced; nothing is recomputed (the copy is re-validated)."""
    return dataclasses.replace(record, **changes)


def _estimate(**overrides: Any) -> PlanEstimate:
    fields: dict[str, Any] = {
        "model_version_id": samples.MODEL_ID,
        "model_status": ModelStatus.RESEARCH,
        "expected_net_r": 0.25,
        "net_r_stdev": 1.1,
        "expected_net_r_stderr": 0.12,
        "support_count": 84,
        "expected_cost_r": 0.08,
        "baseline_r": 0.05,
        "contributions": (Contribution(evidence_id=None, feature="session_london", weight_r=0.05),),
    }
    fields.update(overrides)
    return PlanEstimate(**fields)


def _candidate_id(model: str | None = samples.MODEL_ID, revision: int = 0) -> str:
    return identity.candidate_id(samples.candidate().opportunity_id, model, revision)


def _plan_id(style: str = "LIMIT", variant: int = 0, revision: int = 0, candidate: str | None = None) -> str:
    return identity.plan_id(candidate or _candidate_id(), style, variant, revision)


def _other_opportunity() -> str:
    return identity.opportunity_id(
        "sweep_reversal_short", "SELL", "SCALP", samples.candidate().market_event_ids
    )


def _invariant_error(build: Build, invariant_id: str, contract: str = "TradePlan") -> ContractValidationError:
    with pytest.raises(ContractValidationError) as info:
        build()
    error = info.value
    assert error.details.get("invariant") == invariant_id, str(error)
    assert error.details.get("contract") == contract
    assert error.code == "GIE.CONTRACT.INVALID"
    return error


def _field_error(build: Build, path: str) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as info:
        build()
    error = info.value
    assert error.details.get("path") == path, str(error)
    assert "invariant" not in error.details
    return error


# =========================================================================== inventory
PLAN_INVARIANTS = ("CAUSAL-1", *(f"PLN-{number}" for number in range(1, 13)))


@pytest.mark.requirement("S00-ASG-14")
def test_trade_plan_declares_exactly_the_invariants_of_the_field_reference() -> None:
    assert tuple(rule.id for rule in TradePlan.ALL_INVARIANTS) == PLAN_INVARIANTS
    assert (TradePlan.CONTRACT, TradePlan.VERSION) == ("TradePlan", "1.0.0")
    assert [rule.id for rule in PlanEstimate.ALL_INVARIANTS] == ["EST-1"]
    assert Target.ALL_INVARIANTS == ()


@pytest.mark.requirement("S00-ASG-14")
def test_published_vocabularies_and_transition_table() -> None:
    assert [style.value for style in EntryStyle] == ["MARKET", "LIMIT", "RETEST", "PULLBACK", "STOP"]
    assert [status.value for status in PlanStatus] == [
        "PREPARED",
        "ARMED",
        "SELECTED",
        "WITHDRAWN",
        "EXPIRED",
        "REJECTED",
    ]
    assert [order_type.value for order_type in OrderType] == ["MARKET", "LIMIT", "STOP"]
    assert EXPECTED_TRANSITIONS == PLAN_STATUS_TRANSITIONS


@pytest.mark.requirement("S00-ASG-14")
def test_sample_plans_are_labelled_synthetic_and_immutable() -> None:
    variants = samples.variants()["TradePlan"]
    assert len(variants) >= 4
    for name, record in variants.items():
        assert type(record) is TradePlan, name
        assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE, name
    with pytest.raises(dataclasses.FrozenInstanceError):
        variants["limit_buy_selected"].stop_price = D("2600")  # type: ignore[misc]


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-21")
def test_stream_and_ordering_time() -> None:
    record = _plan()
    assert stream_name(record) == "trade_plan/decision.planner"
    assert ordering_time_ns(record) == record.decision_time_ns == T0


@pytest.mark.requirement("S00-ASG-14")
def test_a_plan_carries_no_size() -> None:
    names = {field.name for field in dataclasses.fields(TradePlan)}
    assert not {name for name in names if "lots" in name or "volume" in name or "risk_amount" in name}


# =========================================================================== invariants: accepted
ACCEPTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_equals_decision_time", lambda: _plan(as_of_time_ns=T0)),
    ("CAUSAL-1", "watermark_before_decision_time", lambda: _plan(as_of_time_ns=T0 - 60 * NS_PER_SECOND)),
    ("PLN-1", "variant_0_revision_0", _plan),
    ("PLN-1", "variant_255", lambda: _plan(variant=255)),
    ("PLN-1", "revision_3", lambda: _plan(revision=3, seq=3)),
    ("PLN-1", "another_scorers_candidate", lambda: _plan(candidate_id=_candidate_id(SHADOW_MODEL))),
    ("PLN-2", "group_of_the_opportunity", _plan),
    ("PLN-2", "group_of_another_opportunity", lambda: _plan(opportunity_id=_other_opportunity())),
    ("PLN-3", "market_has_no_entry_price", _market),
    ("PLN-3", "limit_has_entry_price", _plan),
    (
        "PLN-3",
        "stop_entry_above_the_quote",
        lambda: _styled(
            EntryStyle.STOP,
            OrderType.STOP,
            entry_price=D("2651.00"),
            stop_price=D("2649.00"),
            targets=(_target("2654.00", "1"),),
        ),
    ),
    ("PLN-3", "retest_has_entry_price", lambda: _styled(EntryStyle.RETEST, OrderType.LIMIT)),
    ("PLN-3", "pullback_has_entry_price", _pullback),
    ("PLN-4", "market_by_market", _market),
    ("PLN-4", "limit_by_limit", _plan),
    ("PLN-4", "stop_by_stop", lambda: _styled(EntryStyle.STOP, OrderType.STOP)),
    ("PLN-4", "retest_by_limit", lambda: _styled(EntryStyle.RETEST, OrderType.LIMIT)),
    ("PLN-4", "retest_by_market", lambda: _styled(EntryStyle.RETEST, OrderType.MARKET)),
    ("PLN-4", "pullback_by_limit", lambda: _pullback(order_type=OrderType.LIMIT)),
    ("PLN-4", "pullback_by_market", lambda: _pullback(order_type=OrderType.MARKET)),
    ("PLN-5", "buy_stop_one_step_below_entry", lambda: _plan(stop_price=D("2649.80") - STEP)),
    ("PLN-5", "sell_stop_one_step_above_entry", lambda: _sell(stop_price=D("2650.60") + STEP)),
    ("PLN-5", "market_buy_stop_one_step_below_reference", lambda: _market(stop_price=D("2650.35") - STEP)),
    (
        "PLN-5",
        "market_sell_stop_one_step_above_reference",
        lambda: _market(Side.SELL, stop_price=D("2650.10") + STEP),
    ),
    (
        "PLN-6",
        "buy_target_one_step_above_entry",
        lambda: _plan(targets=(Target(label="a_b", price=D("2649.80") + STEP, close_frac=D("1")),)),
    ),
    (
        "PLN-6",
        "sell_target_one_step_below_entry",
        lambda: _sell(targets=(Target(label="a_b", price=D("2650.60") - STEP, close_frac=D("1")),)),
    ),
    ("PLN-6", "buy_pullback_target_below_reference_above_entry", _pullback),
    ("PLN-6", "sell_pullback_target_above_reference_below_entry", lambda: _pullback(Side.SELL)),
    (
        "PLN-6",
        "buy_limit_target_between_entry_and_reference",
        lambda: _plan(targets=(_target("2650.00", "1"),)),
    ),
    ("PLN-7", "two_halves_sum_to_exactly_1", _plan),
    ("PLN-7", "single_target_closes_everything", lambda: _plan(targets=(_target("2652.80", "1"),))),
    (
        "PLN-7",
        "thirds_sum_to_exactly_1",
        lambda: _plan(
            targets=(
                _target("2652", "0.333333333333"),
                _target("2653", "0.333333333333"),
                _target("2654", "0.333333333334"),
            )
        ),
    ),
    (
        "PLN-7",
        "no_scheduled_close_at_all",
        lambda: _plan(targets=(_target("2652", "0"), _target("2655", "0"))),
    ),
    (
        "PLN-7",
        "partial_close_only",
        lambda: _plan(targets=(_target("2652", "0.25"), _target("2655", "0.000000000001"))),
    ),
    (
        "PLN-7",
        "eight_eighths",
        lambda: _plan(targets=tuple(_target(str(2651 + n), "0.125") for n in range(8))),
    ),
    ("PLN-8", "selected_expires_one_ns_later", lambda: _plan(valid_until_ns=T0 + 1)),
    (
        "PLN-8",
        "prepared_expires_one_ns_later",
        lambda: _with_status(PlanStatus.PREPARED, valid_until_ns=T0 + 1),
    ),
    ("PLN-8", "rejected_at_expiry", lambda: _with_status(PlanStatus.REJECTED, valid_until_ns=T0)),
    ("PLN-8", "expired_after_expiry", lambda: _with_status(PlanStatus.EXPIRED, valid_until_ns=T0 - 1)),
    ("PLN-8", "withdrawn_after_expiry", lambda: _with_status(PlanStatus.WITHDRAWN, valid_until_ns=0)),
    (
        "PLN-9",
        "rejected_with_reason",
        lambda: _with_status(PlanStatus.REJECTED, status_reasons=("STALE_CANDIDATE",)),
    ),
    (
        "PLN-9",
        "withdrawn_with_reasons",
        lambda: _with_status(PlanStatus.WITHDRAWN, status_reasons=("B_C", "A_B")),
    ),
    ("PLN-9", "expired_without_reason", lambda: _with_status(PlanStatus.EXPIRED)),
    ("PLN-9", "selected_without_reason", _plan),
    ("PLN-10", "revision_0_supersedes_nothing", _plan),
    ("PLN-10", "revision_1_names_revision_0", lambda: _plan(revision=1, seq=1)),
    ("PLN-10", "revision_9_names_revision_8", lambda: _plan(revision=9, seq=9)),
    ("PLN-11", "selected_positive", _plan),
    ("PLN-11", "armed_smallest_positive", lambda: _with_status(PlanStatus.ARMED, expected_net_r=5e-324)),
    ("PLN-11", "prepared_negative", lambda: _with_status(PlanStatus.PREPARED, expected_net_r=-0.1)),
    ("PLN-11", "prepared_zero", lambda: _with_status(PlanStatus.PREPARED, expected_net_r=0.0)),
    ("PLN-11", "rejected_negative", lambda: _with_status(PlanStatus.REJECTED, expected_net_r=-3.0)),
    ("PLN-12", "stdev_only", lambda: _plan(expected_net_r_stderr=None, support_count=None)),
    ("PLN-12", "stderr_only", lambda: _plan(net_r_stdev=None)),
    ("PLN-12", "zero_stdev_is_a_statement", lambda: _plan(net_r_stdev=0.0, expected_net_r_stderr=None)),
]


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in ACCEPTED_CASES],
)
def test_invariant_accepts(invariant_id: str, build: Build) -> None:
    record = build()
    assert type(record) is TradePlan
    assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE
    assert record.symbol == "XAUUSD"
    assert invariant_id in PLAN_INVARIANTS
    assert dataclasses.replace(record) == record
    assert from_wire(to_wire(record), TradePlan) == record


# =========================================================================== invariants: rejected
INVALID_PAIRS = sorted(
    (pair for pair in itertools.product(EntryStyle, OrderType) if pair not in VALID_PAIRS),
    key=lambda pair: (pair[0].value, pair[1].value),
)

REJECTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_one_ns_after_decision", lambda: _changed(_plan(), as_of_time_ns=T0 + 1)),
    ("PLN-1", "id_of_another_variant", lambda: _changed(_plan(), plan_id=_plan_id(variant=1))),
    ("PLN-1", "id_of_another_revision", lambda: _changed(_plan(), plan_id=_plan_id(revision=1))),
    ("PLN-1", "id_of_another_entry_style", lambda: _changed(_plan(), plan_id=_plan_id(style="RETEST"))),
    (
        "PLN-1",
        "id_of_another_candidate",
        lambda: _changed(_plan(), plan_id=_plan_id(candidate=_candidate_id(None))),
    ),
    ("PLN-1", "variant_changed_under_the_id", lambda: _changed(_plan(), variant=1)),
    (
        "PLN-1",
        "candidate_changed_under_the_id",
        lambda: _changed(_plan(), candidate_id=_candidate_id(SHADOW_MODEL)),
    ),
    (
        "PLN-2",
        "group_of_another_opportunity",
        lambda: _changed(_plan(), alternative_group_id=identity.alternative_group_id(_other_opportunity())),
    ),
    (
        "PLN-2",
        "opportunity_changed_under_the_group",
        lambda: _changed(_plan(), opportunity_id=_other_opportunity()),
    ),
    ("PLN-3", "limit_without_entry_price", lambda: _changed(_plan(), entry_price=None)),
    ("PLN-3", "stop_without_entry_price", lambda: _styled(EntryStyle.STOP, OrderType.STOP, entry_price=None)),
    (
        "PLN-3",
        "retest_without_entry_price",
        lambda: _styled(EntryStyle.RETEST, OrderType.LIMIT, entry_price=None),
    ),
    (
        "PLN-3",
        "pullback_by_market_without_entry_price",
        lambda: _styled(EntryStyle.PULLBACK, OrderType.MARKET, entry_price=None),
    ),
    ("PLN-3", "market_with_entry_price", lambda: _changed(_market(), entry_price=D("2650.35"))),
    ("PLN-3", "market_sell_with_entry_price", lambda: _changed(_market(Side.SELL), entry_price=D("2650.10"))),
    *[
        (
            "PLN-4",
            f"{style.value.lower()}_by_{order_type.value.lower()}",
            lambda style=style, order_type=order_type: _styled(style, order_type),
        )
        for style, order_type in INVALID_PAIRS
    ],
    ("PLN-5", "buy_stop_at_entry", lambda: _changed(_plan(), stop_price=D("2649.80"))),
    ("PLN-5", "buy_stop_one_step_above_entry", lambda: _changed(_plan(), stop_price=D("2649.80") + STEP)),
    ("PLN-5", "buy_stop_above_entry_but_below_reference", lambda: _changed(_plan(), stop_price=D("2650.00"))),
    ("PLN-5", "sell_stop_at_entry", lambda: _changed(_sell(), stop_price=D("2650.60"))),
    ("PLN-5", "sell_stop_one_step_below_entry", lambda: _changed(_sell(), stop_price=D("2650.60") - STEP)),
    (
        "PLN-5",
        "sell_stop_below_entry_but_above_reference",
        lambda: _changed(_sell(), stop_price=D("2650.30")),
    ),
    ("PLN-5", "market_buy_stop_at_reference", lambda: _changed(_market(), stop_price=D("2650.35"))),
    ("PLN-5", "market_sell_stop_at_reference", lambda: _changed(_market(Side.SELL), stop_price=D("2650.10"))),
    (
        "PLN-5",
        "market_sell_stop_below_reference",
        lambda: _changed(_market(Side.SELL), stop_price=D("2649.00")),
    ),
    ("PLN-6", "buy_target_at_entry", lambda: _changed(_plan(), targets=(_target("2649.80", "1"),))),
    ("PLN-6", "buy_target_below_entry", lambda: _changed(_plan(), targets=(_target("2649.00", "1"),))),
    (
        "PLN-6",
        "buy_one_of_two_targets_below_entry",
        lambda: _changed(_plan(), targets=(_target("2649.79", "0.5"), _target("2655.00", "0.5"))),
    ),
    ("PLN-6", "sell_target_at_entry", lambda: _changed(_sell(), targets=(_target("2650.60", "1"),))),
    ("PLN-6", "sell_target_above_entry", lambda: _changed(_sell(), targets=(_target("2651.60", "1"),))),
    (
        "PLN-6",
        "sell_one_of_two_targets_above_entry",
        lambda: _changed(_sell(), targets=(_target("2647.60", "0.5"), _target("2650.61", "0.5"))),
    ),
    (
        "PLN-6",
        "market_buy_target_at_reference",
        lambda: _changed(_market(), targets=(_target("2650.35", "1"),)),
    ),
    (
        "PLN-6",
        "market_buy_target_below_reference",
        lambda: _changed(_market(), targets=(_target("2650.00", "1"),)),
    ),
    (
        "PLN-6",
        "market_sell_target_above_reference",
        lambda: _changed(_market(Side.SELL), targets=(_target("2650.50", "1"),)),
    ),
    (
        "PLN-6",
        "buy_pullback_target_below_entry",
        lambda: _changed(_pullback(), targets=(_target("2648.00", "1"),)),
    ),
    (
        "PLN-6",
        "sell_pullback_target_above_entry",
        lambda: _changed(_pullback(Side.SELL), targets=(_target("2652.50", "1"),)),
    ),
    (
        "PLN-7",
        "one_step_above_1",
        lambda: _changed(_plan(), targets=(_target("2652", "0.5"), _target("2655", "0.500000000001"))),
    ),
    (
        "PLN-7",
        "one_and_a_trace",
        lambda: _changed(_plan(), targets=(_target("2652", "1"), _target("2655", "0.000000000001"))),
    ),
    (
        "PLN-7",
        "thirds_rounded_up",
        lambda: _changed(
            _plan(),
            targets=(
                _target("2652", "0.333333333334"),
                _target("2653", "0.333333333334"),
                _target("2654", "0.333333333334"),
            ),
        ),
    ),
    (
        "PLN-7",
        "two_full_closes",
        lambda: _changed(_plan(), targets=(_target("2652", "1"), _target("2655", "1"))),
    ),
    ("PLN-8", "selected_expires_at_decision_time", lambda: _changed(_plan(), valid_until_ns=T0)),
    ("PLN-8", "selected_already_expired", lambda: _changed(_plan(), valid_until_ns=T0 - 1)),
    ("PLN-8", "armed_expires_at_decision_time", lambda: _with_status(PlanStatus.ARMED, valid_until_ns=T0)),
    (
        "PLN-8",
        "prepared_expires_at_decision_time",
        lambda: _with_status(PlanStatus.PREPARED, valid_until_ns=T0),
    ),
    ("PLN-9", "rejected_without_reason", lambda: _changed(_plan(), status=PlanStatus.REJECTED)),
    ("PLN-9", "withdrawn_without_reason", lambda: _changed(_plan(), status=PlanStatus.WITHDRAWN)),
    ("PLN-10", "revision_0_names_a_predecessor", lambda: _changed(_plan(), supersedes_plan_id=_plan_id())),
    (
        "PLN-10",
        "revision_1_names_nothing",
        lambda: _changed(_plan(revision=1, seq=1), supersedes_plan_id=None),
    ),
    (
        "PLN-10",
        "revision_1_names_itself",
        lambda: _changed(_plan(revision=1, seq=1), supersedes_plan_id=_plan_id(revision=1)),
    ),
    (
        "PLN-10",
        "revision_2_skips_revision_1",
        lambda: _changed(_plan(revision=2, seq=2), supersedes_plan_id=_plan_id()),
    ),
    (
        "PLN-10",
        "revision_1_names_another_variant",
        lambda: _changed(_plan(revision=1, seq=1), supersedes_plan_id=_plan_id(variant=1)),
    ),
    (
        "PLN-10",
        "revision_1_names_another_style",
        lambda: _changed(_plan(revision=1, seq=1), supersedes_plan_id=_plan_id(style="MARKET")),
    ),
    ("PLN-11", "selected_zero", lambda: _changed(_plan(), expected_net_r=0.0)),
    ("PLN-11", "selected_negative_zero", lambda: _changed(_plan(), expected_net_r=-0.0)),
    ("PLN-11", "selected_integer_zero", lambda: _changed(_plan(), expected_net_r=0)),
    ("PLN-11", "selected_negative", lambda: _changed(_plan(), expected_net_r=-0.15)),
    ("PLN-11", "selected_smallest_negative", lambda: _changed(_plan(), expected_net_r=-5e-324)),
    ("PLN-11", "armed_zero", lambda: _with_status(PlanStatus.ARMED, expected_net_r=0.0)),
    ("PLN-11", "armed_negative", lambda: _with_status(PlanStatus.ARMED, expected_net_r=-1.0)),
    ("PLN-12", "no_uncertainty", lambda: _changed(_plan(), net_r_stdev=None, expected_net_r_stderr=None)),
    (
        "PLN-12",
        "support_count_alone_is_not_uncertainty",
        lambda: _changed(_plan(), net_r_stdev=None, expected_net_r_stderr=None, support_count=500),
    ),
]


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in REJECTED_CASES],
)
def test_invariant_rejects(invariant_id: str, build: Build) -> None:
    error = _invariant_error(build, invariant_id)
    rule = next(rule for rule in TradePlan.ALL_INVARIANTS if rule.id == invariant_id)
    assert error.details["rule"] == rule.text
    assert invariant_id in error.message


@pytest.mark.requirement("S00-ASG-14")
def test_every_invariant_has_an_accepted_and_a_rejected_case() -> None:
    assert {invariant for invariant, _, _ in ACCEPTED_CASES} == set(PLAN_INVARIANTS)
    assert {invariant for invariant, _, _ in REJECTED_CASES} == set(PLAN_INVARIANTS)
    assert len(INVALID_PAIRS) == 8


@pytest.mark.requirement("S00-ASG-14")
def test_decoding_enforces_the_invariants() -> None:
    wire = to_wire(_plan())
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "stop_price": "2649.8"}, TradePlan)
    assert info.value.details["invariant"] == "PLN-5"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "expected_net_r": 0}, TradePlan)
    assert info.value.details["invariant"] == "PLN-11"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "entry_price": None}, TradePlan)
    assert info.value.details["invariant"] == "PLN-3"


# =========================================================================== entry style and entry price
@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(("style", "order_type"), list(itertools.product(EntryStyle, OrderType)))
def test_entry_style_and_order_type_pairing(style: EntryStyle, order_type: OrderType) -> None:
    if (style, order_type) in VALID_PAIRS:
        record = _styled(style, order_type)
        assert (record.entry_style, record.entry_order_type) == (style, order_type)
    else:
        _invariant_error(lambda: _styled(style, order_type), "PLN-4")


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(
    ("style", "order_type"), sorted(VALID_PAIRS, key=lambda pair: (pair[0].value, pair[1].value))
)
@pytest.mark.parametrize("has_entry_price", [True, False])
def test_entry_price_is_null_if_and_only_if_the_style_is_market(
    style: EntryStyle, order_type: OrderType, has_entry_price: bool
) -> None:
    entry_price = D("2649.80") if has_entry_price else None

    def build() -> TradePlan:
        return _styled(style, order_type, entry_price=entry_price)

    if has_entry_price == (style is not EntryStyle.MARKET):
        assert build().entry_price == entry_price
    else:
        _invariant_error(build, "PLN-3")


@pytest.mark.requirement("S00-ASG-14")
def test_effective_entry_is_the_entry_price_or_the_reference_price_of_a_market_plan() -> None:
    assert _plan().effective_entry_price == D("2649.80")
    assert _market().effective_entry_price == D("2650.35")
    assert _market(Side.SELL).effective_entry_price == D("2650.10")
    # A pullback executed by a MARKET order still refers to its intended level, not to the quote.
    by_market = _pullback(order_type=OrderType.MARKET)
    by_limit = _pullback(order_type=OrderType.LIMIT)
    assert by_market.effective_entry_price == by_limit.effective_entry_price == D("2648.20")
    assert by_market.reference_price == D("2650.35")


@pytest.mark.requirement("S00-ASG-14")
def test_alternative_market_limit_retest_and_pullback_plans_can_be_built_for_one_opportunity() -> None:
    plans = [
        _market(),
        _plan(),
        _styled(EntryStyle.RETEST, OrderType.LIMIT),
        _styled(EntryStyle.RETEST, OrderType.MARKET, variant=1),
        _pullback(order_type=OrderType.LIMIT),
        _pullback(order_type=OrderType.MARKET, variant=1),
        _styled(EntryStyle.STOP, OrderType.STOP),
    ]
    assert {plan.entry_style for plan in plans} == set(EntryStyle)
    assert len({plan.plan_id for plan in plans}) == len(plans)
    assert len({plan.alternative_group_id for plan in plans}) == 1
    for plan in plans:
        assert plan.stop_price > 0  # hard invalidation
        assert len(plan.targets) >= 1  # destination
        assert plan.net_r_stdev is not None or plan.expected_net_r_stderr is not None  # uncertainty
        assert isinstance(plan.expected_net_r, float)
        assert plan.plan_id == identity.plan_id(plan.candidate_id, plan.entry_style.value, plan.variant, 0)


# =========================================================================== risk geometry (PLN-5, PLN-6)
@pytest.mark.requirement("S00-ASG-14", "G04-12")
@pytest.mark.parametrize("stop", [None, D("0"), D("-2647.80"), 2647.80, "2647.80", D("NaN")])
def test_a_plan_always_has_a_hard_stop_price(stop: object) -> None:
    _field_error(lambda: _changed(_plan(), stop_price=stop), "stop_price")


@pytest.mark.requirement("S00-ASG-14", "G04-12")
def test_buy_stop_below_and_targets_above_the_effective_entry() -> None:
    plan = _plan()
    assert plan.side is Side.BUY
    assert plan.stop_price < plan.effective_entry_price < plan.targets[0].price
    for stop in (
        plan.effective_entry_price,
        plan.effective_entry_price + STEP,
        plan.reference_price,
        D("2700"),
    ):
        _invariant_error(lambda stop=stop: _changed(plan, stop_price=stop), "PLN-5")
    for target in (plan.effective_entry_price, plan.effective_entry_price - STEP, plan.stop_price, D("1")):
        _invariant_error(lambda target=target: _changed(plan, targets=(_target(str(target), "1"),)), "PLN-6")


@pytest.mark.requirement("S00-ASG-14", "G04-12")
def test_sell_stop_above_and_targets_below_the_effective_entry() -> None:
    plan = _sell()
    assert plan.side is Side.SELL
    assert plan.stop_price > plan.effective_entry_price > plan.targets[-1].price
    for stop in (plan.effective_entry_price, plan.effective_entry_price - STEP, plan.reference_price, D("1")):
        _invariant_error(lambda stop=stop: _changed(plan, stop_price=stop), "PLN-5")
    for target in (plan.effective_entry_price, plan.effective_entry_price + STEP, plan.stop_price, D("2700")):
        _invariant_error(lambda target=target: _changed(plan, targets=(_target(str(target), "1"),)), "PLN-6")


@pytest.mark.requirement("S00-ASG-14", "G04-12")
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_a_market_plan_is_checked_against_the_reference_price(side: Side) -> None:
    plan = _market(side)
    assert plan.entry_price is None
    reference = plan.reference_price
    towards_loss = -STEP if side is Side.BUY else STEP
    assert _changed(plan, stop_price=reference + towards_loss).risk_per_unit == STEP
    _invariant_error(lambda: _changed(plan, stop_price=reference), "PLN-5")
    _invariant_error(lambda: _changed(plan, stop_price=reference - towards_loss), "PLN-5")
    assert (
        _changed(plan, targets=(_target(str(reference - towards_loss), "1"),)).targets[0].price != reference
    )
    _invariant_error(lambda: _changed(plan, targets=(_target(str(reference), "1"),)), "PLN-6")
    _invariant_error(lambda: _changed(plan, targets=(_target(str(reference + towards_loss), "1"),)), "PLN-6")


@pytest.mark.requirement("S00-ASG-14", "G04-12")
@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
def test_a_buy_pullback_may_target_a_price_below_the_current_quote_but_above_its_entry(
    order_type: OrderType,
) -> None:
    plan = _pullback(Side.BUY, order_type)
    target = plan.targets[0].price
    assert plan.effective_entry_price < target < plan.reference_price
    assert plan.stop_price < plan.effective_entry_price
    # The same geometry is impossible for a MARKET style plan, whose entry is the quote itself.
    _invariant_error(lambda: _market(targets=(_target(str(target), "1"),)), "PLN-6")


@pytest.mark.requirement("S00-ASG-14", "G04-12")
@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
def test_a_sell_pullback_may_target_a_price_above_the_current_quote_but_below_its_entry(
    order_type: OrderType,
) -> None:
    plan = _pullback(Side.SELL, order_type)
    target = plan.targets[0].price
    assert plan.reference_price < target < plan.effective_entry_price
    assert plan.stop_price > plan.effective_entry_price
    _invariant_error(lambda: _market(Side.SELL, targets=(_target(str(target), "1"),)), "PLN-6")


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20", "G04-12")
@pytest.mark.parametrize("context_name", sorted(HOSTILE_CONTEXTS))
def test_the_geometry_is_decided_exactly_under_a_hostile_decimal_context(context_name: str) -> None:
    # All three prices round to the same number with three significant digits.
    below, entry, above = D("2649.800000000000"), D("2649.800000000001"), D("2649.800000000002")
    at_entry = (Target(label="a_b", price=entry, close_frac=D("1")),)
    over = (_target("2652", "0.5"), _target("2655", "0.500000000001"))
    with decimal.localcontext(HOSTILE_CONTEXTS[context_name]):
        tight = _plan(
            entry_price=entry,
            stop_price=below,
            targets=(Target(label="a_b", price=above, close_frac=D("1")),),
        )
        _invariant_error(lambda: _plan(entry_price=entry, stop_price=entry), "PLN-5")
        _invariant_error(lambda: _plan(entry_price=entry, stop_price=above), "PLN-5")
        _invariant_error(lambda: _plan(entry_price=entry, targets=at_entry), "PLN-6")
        _invariant_error(lambda: _plan(targets=over), "PLN-7")
        risk = tight.risk_per_unit
    assert (tight.stop_price, tight.entry_price, tight.targets[0].price) == (below, entry, above)
    assert str(risk) == "1E-12"


# =========================================================================== risk_per_unit
@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
@pytest.mark.parametrize(
    ("build", "expected"),
    [
        pytest.param(_plan, "2.00", id="buy_limit"),
        pytest.param(_sell, "2.00", id="sell_limit"),
        pytest.param(_market, "2.55", id="buy_market_uses_reference"),
        pytest.param(lambda: _market(Side.SELL), "2.00", id="sell_market_uses_reference"),
        pytest.param(_pullback, "1.30", id="buy_pullback_uses_entry"),
        pytest.param(lambda: _pullback(Side.SELL), "1.30", id="sell_pullback_uses_entry"),
        pytest.param(
            lambda: _plan(entry_price=D("2649.123456789012"), stop_price=D("2647.000000000001")),
            "2.123456789011",
            id="twelve_fraction_digits",
        ),
        pytest.param(
            lambda: _plan(
                entry_price=D("999999999999999999.999999999998"),
                reference_price=D("999999999999999999.999999999998"),
                stop_price=D("0.000000000001"),
                targets=(_target("999999999999999999.999999999999", "1"),),
            ),
            "999999999999999999.999999999997",
            id="thirty_significant_digits",
        ),
    ],
)
def test_risk_per_unit_is_the_exact_distance_between_effective_entry_and_stop(
    build: Build, expected: str
) -> None:
    outside = build()
    assert isinstance(outside, TradePlan)
    results = [outside.risk_per_unit]
    for context in HOSTILE_CONTEXTS.values():
        with decimal.localcontext(context):
            inside = build()
            assert isinstance(inside, TradePlan)
            results.extend([inside.risk_per_unit, outside.risk_per_unit])
            assert inside == outside
    assert {str(value) for value in results} == {expected}
    assert outside.risk_per_unit > 0
    exact = decimal.Context(prec=64, traps=[decimal.Inexact])
    assert outside.risk_per_unit == exact.abs(
        exact.subtract(outside.effective_entry_price, outside.stop_price)
    )


# =========================================================================== targets (PLN-7)
@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
@pytest.mark.parametrize(
    ("fractions", "valid"),
    [
        (("0",), True),
        (("0", "0", "0"), True),
        (("1",), True),
        (("0.5", "0.5"), True),
        (("0.999999999999", "0.000000000001"), True),
        (("0.1", "0.2", "0.3", "0.4"), True),
        (("0.1", "0.2", "0.3", "0.400000000001"), False),
        (("0.999999999999", "0.000000000002"), False),
        (("1", "0.000000000001"), False),
        (("0.7", "0.7"), False),
    ],
)
def test_close_fractions_sum_to_at_most_one_exactly_at_the_boundary(
    fractions: tuple[str, ...], valid: bool
) -> None:
    targets = tuple(_target(str(2651 + index), fraction) for index, fraction in enumerate(fractions))
    exact = decimal.Context(prec=64, traps=[decimal.Inexact])
    total = D(0)
    for target in targets:
        total = exact.add(total, target.close_frac)
    assert (total <= 1) is valid  # the table above is consistent with exact arithmetic
    for context in (decimal.Context(prec=28), *HOSTILE_CONTEXTS.values()):
        with decimal.localcontext(context):
            if valid:
                assert _plan(targets=targets).targets == targets
            else:
                _invariant_error(lambda: _plan(targets=targets), "PLN-7")


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
def test_target_fields_are_strict() -> None:
    assert _target("2652.80", "0").close_frac == D("0")
    assert _target("2652.80", "1").close_frac == D("1")
    for fraction in (D("-0.000000000001"), D("1.000000000001"), 0.5, 1, "0.5", D("NaN"), None):
        _field_error(
            lambda fraction=fraction: Target(label="first", price=D("2652.80"), close_frac=fraction),
            "close_frac",
        )
    for price in (D("0"), D("-2652.80"), 2652.8, None, D("Infinity")):
        _field_error(lambda price=price: Target(label="first", price=price, close_frac=D("0")), "price")
    for label in ("First", "first target", "f", "", None):
        _field_error(lambda label=label: Target(label=label, price=D("2652.80"), close_frac=D("0")), "label")


@pytest.mark.requirement("S00-ASG-14")
def test_a_plan_has_at_least_one_and_at_most_eight_targets_unique_by_price_and_sorted() -> None:
    _field_error(lambda: _changed(_plan(), targets=()), "targets")
    nine = tuple(_target(str(2651 + index), "0.1") for index in range(9))
    assert len(_plan(targets=nine[:8]).targets) == 8
    _field_error(lambda: _plan(targets=nine), "targets")
    supplied = (
        _target("2655.00", "0.25", "prior_high"),
        _target("2652.80", "0.5", "first"),
        _target("2660", "0", "runner"),
    )
    reference = _plan(targets=supplied)
    for order in itertools.permutations(supplied):
        record = _plan(targets=order)
        assert [target.price for target in record.targets] == [D("2652.80"), D("2655.00"), D("2660")]
        assert record == reference
        assert to_json(record) == to_json(reference)
    duplicate = (_target("2655.00", "0.25", "prior_high"), _target("2655.0", "0.5", "round_number"))
    error = _field_error(lambda: _changed(_plan(), targets=duplicate), "targets[1]")
    assert error.details["rule"] == "duplicate item"
    _field_error(lambda: _changed(_plan(), targets=(D("2655.00"),)), "targets[0]")


# =========================================================================== expectancy gate (PLN-11)
@pytest.mark.requirement("S00-ASG-14", "S05-ACC-05")
@pytest.mark.parametrize("status", list(PlanStatus))
@pytest.mark.parametrize("expected_net_r", [-2.0, -5e-324, -0.0, 0.0, 5e-324, 0.25])
def test_only_a_plan_with_positive_expected_net_r_can_be_armed_or_selected(
    status: PlanStatus, expected_net_r: float
) -> None:
    def build() -> TradePlan:
        return _with_status(status, expected_net_r=expected_net_r)

    if status in EXECUTABLE and not expected_net_r > 0:
        error = _invariant_error(build, "PLN-11")
        assert (
            error.details["rule"]
            == "an ARMED or SELECTED plan has expected_net_r > 0 (positive expectancy gate)"
        )
    else:
        assert build().expected_net_r == expected_net_r


@pytest.mark.requirement("S00-ASG-14", "S05-ACC-05")
def test_a_negative_or_stale_opportunity_is_recorded_as_a_rejected_plan_not_as_an_error() -> None:
    negative = _with_status(
        PlanStatus.REJECTED, expected_net_r=-0.15, status_reasons=("NEGATIVE_EXPECTED_NET_R",)
    )
    assert negative.status is PlanStatus.REJECTED
    assert negative.status_reasons == ("NEGATIVE_EXPECTED_NET_R",)
    stale = _with_status(PlanStatus.REJECTED, status_reasons=("STALE_CANDIDATE",), valid_until_ns=T0)
    assert is_expired(now_ns=stale.decision_time_ns, valid_until_ns=stale.valid_until_ns) is True
    # Neither can exist in a status from which an entry order could be built.
    for status in EXECUTABLE:
        _invariant_error(lambda status=status: _changed(negative, status=status, status_reasons=()), "PLN-11")
        _invariant_error(lambda status=status: _changed(stale, status=status, status_reasons=()), "PLN-8")


@pytest.mark.requirement("S00-ASG-14", "S05-ACC-05")
def test_expected_net_r_must_be_a_finite_number() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), None, "0.25", True, D("0.25")):
        _field_error(lambda bad=bad: _changed(_plan(), expected_net_r=bad), "expected_net_r")
    for field in ("net_r_stdev", "expected_net_r_stderr", "expected_cost_r"):
        _field_error(lambda field=field: _changed(_plan(), **{field: -0.01}), field)
        _field_error(lambda field=field: _changed(_plan(), **{field: float("nan")}), field)
    _field_error(lambda: _changed(_plan(), expected_cost_r=None), "expected_cost_r")
    _field_error(lambda: _changed(_plan(), support_count=-1), "support_count")


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
@pytest.mark.parametrize(
    ("stdev", "stderr"), [(1.1, 0.12), (1.1, None), (None, 0.12), (0.0, None), (None, 0.0), (None, None)]
)
def test_uncertainty_rule(stdev: float | None, stderr: float | None) -> None:
    def build() -> TradePlan:
        return _changed(_plan(), net_r_stdev=stdev, expected_net_r_stderr=stderr)

    if stdev is None and stderr is None:
        _invariant_error(build, "PLN-12")
    else:
        assert (build().net_r_stdev, build().expected_net_r_stderr) == (stdev, stderr)


# =========================================================================== status, reasons, expiry
@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize("status", list(PlanStatus))
@pytest.mark.parametrize("reasons", [(), ("FIXTURE_REASON",)])
def test_status_and_reasons_matrix(status: PlanStatus, reasons: tuple[str, ...]) -> None:
    def build() -> TradePlan:
        return _changed(_plan(), status=status, status_reasons=reasons)

    if status in NEEDS_REASON and not reasons:
        _invariant_error(build, "PLN-9")
    else:
        assert build().status_reasons == reasons


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-23")
@pytest.mark.parametrize("status", list(PlanStatus))
@pytest.mark.parametrize("offset_ns", [-1, 0, 1])
def test_a_usable_plan_expires_after_its_decision_time(status: PlanStatus, offset_ns: int) -> None:
    def build() -> TradePlan:
        return _with_status(status, valid_until_ns=T0 + offset_ns)

    if status in USABLE and offset_ns <= 0:
        _invariant_error(build, "PLN-8")
    else:
        record = build()
        assert is_expired(now_ns=record.valid_until_ns, valid_until_ns=record.valid_until_ns) is True
        assert is_expired(now_ns=T0, valid_until_ns=record.valid_until_ns) is (offset_ns <= 0)


@pytest.mark.requirement("S00-ASG-14")
def test_status_reasons_are_unique_sorted_reason_codes() -> None:
    withdrawn = _with_status(PlanStatus.WITHDRAWN, status_reasons=("PRICE_RAN_AWAY", "INVALIDATION_HIT"))
    assert withdrawn.status_reasons == ("INVALIDATION_HIT", "PRICE_RAN_AWAY")
    duplicate = _field_error(lambda: _changed(withdrawn, status_reasons=("A_B", "A_B")), "status_reasons[1]")
    assert duplicate.details["rule"] == "duplicate item"
    _field_error(lambda: _changed(withdrawn, status_reasons=("price ran away",)), "status_reasons[0]")
    _field_error(lambda: _changed(withdrawn, status="WITHDRAWN"), "status")
    _field_error(lambda: _changed(withdrawn, entry_style="LIMIT"), "entry_style")
    _field_error(lambda: _changed(withdrawn, side="BUY"), "side")


# =========================================================================== identity and alternatives
@pytest.mark.requirement("S00-ASG-14", "S00-ASG-22")
def test_alternative_group_derives_from_the_opportunity_only() -> None:
    opportunity = samples.candidate().opportunity_id
    group = identity.alternative_group_id(opportunity)
    same_opportunity = [
        _plan(),
        _market(),
        _pullback(variant=3),
        _plan(candidate_id=_candidate_id(SHADOW_MODEL)),
        _plan(candidate_id=_candidate_id(None)),
        _plan(candidate_id=_candidate_id(revision=4)),
        _plan(revision=2, seq=2),
        _with_status(PlanStatus.REJECTED, variant=7),
    ]
    assert {plan.alternative_group_id for plan in same_opportunity} == {group}
    assert {plan.opportunity_id for plan in same_opportunity} == {opportunity}
    assert len({plan.plan_id for plan in same_opportunity}) == len(same_opportunity)
    # The opposite direction is another opportunity and therefore another group.
    opposite = _sell(opportunity_id=_other_opportunity())
    assert opposite.alternative_group_id == identity.alternative_group_id(_other_opportunity())
    assert opposite.alternative_group_id != group


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-22")
def test_plan_id_is_per_candidate_style_variant_and_revision() -> None:
    ids = {
        _plan().plan_id,
        _plan(variant=1).plan_id,
        _plan(revision=1, seq=1).plan_id,
        _styled(EntryStyle.RETEST, OrderType.LIMIT).plan_id,
        _plan(candidate_id=_candidate_id(SHADOW_MODEL)).plan_id,
    }
    assert len(ids) == 5
    # The order type is not part of the identity: variants distinguish plans of one style.
    assert (
        _styled(EntryStyle.RETEST, OrderType.LIMIT).plan_id
        == _styled(EntryStyle.RETEST, OrderType.MARKET).plan_id
    )
    assert _plan().plan_id == identity.plan_id(_candidate_id(), "LIMIT", 0, 0)
    chain = [_plan(revision=number, seq=number) for number in range(4)]
    for previous, current in itertools.pairwise(chain):
        assert current.supersedes_plan_id == previous.plan_id
    for field, value in (
        ("plan_id", _candidate_id()),
        ("candidate_id", _plan_id()),
        ("opportunity_id", _plan_id()),
        ("alternative_group_id", samples.candidate().opportunity_id),
        ("model_version_id", _plan_id()),
        ("supersedes_plan_id", _candidate_id()),
    ):
        _field_error(lambda field=field, value=value: _changed(chain[1], **{field: value}), field)
    _field_error(lambda: _plan(variant=256), "variant")
    _field_error(lambda: _changed(_plan(), variant=-1), "variant")
    _field_error(lambda: _changed(_plan(), variant=True), "variant")


# =========================================================================== other fields
@pytest.mark.requirement("S00-ASG-14", "G01-01")
@pytest.mark.parametrize("symbol", ["XAGUSD", "EURUSD", "XAUEUR", "xauusd", "XAUUSD ", "", None])
def test_a_plan_can_only_trade_xauusd(symbol: object) -> None:
    for plan in (_plan(), _market(Side.SELL), _with_status(PlanStatus.REJECTED)):
        _field_error(lambda plan=plan: _changed(plan, symbol=symbol), "symbol")
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(_plan()), "symbol": "XAGUSD"}, TradePlan)
    assert info.value.details["path"] == "symbol"
    assert info.value.details["rule"] == "must be one of ['XAUUSD']"


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
def test_prices_and_distances_are_exact_decimals_with_their_bounds() -> None:
    for field in ("entry_price", "reference_price", "max_spread"):
        for bad in (D("0"), D("-1"), 2650.35, 2650, "2650.35", D("NaN"), D("0.0000000000001")):
            _field_error(lambda field=field, bad=bad: _changed(_plan(), **{field: bad}), field)
    assert _changed(_plan(), max_entry_slippage=D("0")).max_entry_slippage == D("0")
    assert _changed(_plan(), max_spread=None, max_entry_slippage=None).max_spread is None
    _field_error(lambda: _changed(_plan(), max_entry_slippage=D("-0.01")), "max_entry_slippage")
    _field_error(lambda: _changed(_plan(), reference_price=None), "reference_price")
    _field_error(lambda: _changed(_plan(), overnight_allowed=1), "overnight_allowed")
    _field_error(lambda: _changed(_plan(), overnight_allowed=None), "overnight_allowed")


@pytest.mark.requirement("S00-ASG-14")
def test_entry_trigger_and_invalidations_are_price_conditions() -> None:
    trigger = PriceCondition(
        price=D("2648.20"), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.ASK, timeframe=None
    )
    close_below = PriceCondition(
        price=D("2647.00"), trigger=PriceTrigger.BELOW, basis=PriceBasis.CANDLE_CLOSE, timeframe=Timeframe.M5
    )
    plan = _pullback(entry_trigger=trigger, invalidations=(close_below, trigger))
    assert plan.entry_trigger == trigger
    assert plan.invalidations == (close_below, trigger)  # order is the producer's, not canonical
    assert _plan(entry_trigger=None, invalidations=()).invalidations == ()
    _field_error(lambda: _changed(plan, entry_trigger=D("2648.20")), "entry_trigger")
    _field_error(lambda: _changed(plan, invalidations=(None,)), "invalidations[0]")
    _field_error(lambda: _changed(plan, invalidations=None), "invalidations")
    assert len(_changed(plan, invalidations=(trigger,) * 8).invalidations) == 8
    _field_error(lambda: _changed(plan, invalidations=(trigger,) * 9), "invalidations")
    wire = to_wire(plan)
    wire["entry_trigger"] = {**wire["entry_trigger"], "timeframe": "M5"}
    with pytest.raises(ContractValidationError) as info:
        from_wire(wire, TradePlan)
    assert info.value.details["invariant"] == "COND-1"
    assert info.value.details["path"] == "entry_trigger"


def _gold_condition(price: str) -> PriceCondition:
    return PriceCondition(
        price=D(price), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.BID, timeframe=None
    )


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
def test_the_entry_trigger_of_a_gold_plan_must_be_a_price_greater_than_zero() -> None:
    # SHARED_CONTRACTS section 2, units table: PriceCondition.price may have any sign for a non-price
    # series but "must be > 0 for XAUUSD".  A plan is always XAUUSD (Evidence enforces this as EVD-13).
    assert _plan(entry_trigger=_gold_condition("0.000000000001")).entry_trigger == _gold_condition(
        "0.000000000001"
    )
    for price in ("0", "-0.01", "-2648.20"):
        with pytest.raises(ContractValidationError):
            _changed(_plan(), entry_trigger=_gold_condition(price))


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
def test_the_invalidations_of_a_gold_plan_must_be_prices_greater_than_zero() -> None:
    # Same rule as above.  A condition such as "bid at or below 0" can never occur, so a plan carrying
    # it has no observable invalidation at all.
    valid = _gold_condition("2648.00")
    assert _plan(invalidations=(valid,)).invalidations == (valid,)
    for price in ("0", "-0.01", "-2648.00"):
        with pytest.raises(ContractValidationError):
            _changed(_plan(), invalidations=(valid, _gold_condition(price)))


@pytest.mark.requirement("S00-ASG-14")
def test_contributions_are_unique_by_driver_and_sorted() -> None:
    evidence_id = samples.evidence().evidence_id
    supplied = (
        Contribution(evidence_id=None, feature="session_london", weight_r=0.05),
        Contribution(evidence_id=evidence_id, feature=None, weight_r=0.20),
        Contribution(evidence_id=None, feature="cost", weight_r=-0.08),
    )
    plan = _plan(contributions=supplied)
    assert [item.key for item in plan.contributions] == [
        evidence_id,
        "feature:cost",
        "feature:session_london",
    ]
    duplicate = (supplied[1], Contribution(evidence_id=evidence_id, feature=None, weight_r=0.0))
    error = _field_error(lambda: _changed(plan, contributions=duplicate), "contributions[1]")
    assert error.details["rule"] == "duplicate item"


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize("name", sorted(samples.variants()["TradePlan"]))
def test_every_sample_plan_survives_the_wire_round_trip(name: str) -> None:
    record = samples.variants()["TradePlan"][name]
    assert from_wire(to_wire(record), TradePlan) == record
    assert from_json(to_json(record), TradePlan) == record
    assert to_json(from_json(to_json(record), TradePlan)) == to_json(record)


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
def test_wire_form_writes_a_market_plan_entry_price_as_explicit_null() -> None:
    wire = to_wire(_market())
    assert "entry_price" in wire
    assert wire["entry_price"] is None
    assert wire["reference_price"] == "2650.35"
    assert wire["stop_price"] == "2647.8"
    assert [target["close_frac"] for target in wire["targets"]] == ["0.5", "0.5"]
    missing_key = {key: value for key, value in wire.items() if key != "entry_price"}
    with pytest.raises(ContractValidationError) as info:
        from_wire(missing_key, TradePlan)
    assert info.value.details["path"] == "entry_price"


# =========================================================================== PlanEstimate (EST-1)
@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
@pytest.mark.parametrize(
    ("stdev", "stderr"), [(1.1, 0.12), (1.1, None), (None, 0.12), (0.0, None), (None, 0.0)]
)
def test_est1_accepts_an_estimate_that_states_its_uncertainty(
    stdev: float | None, stderr: float | None
) -> None:
    estimate = _estimate(net_r_stdev=stdev, expected_net_r_stderr=stderr)
    assert (estimate.net_r_stdev, estimate.expected_net_r_stderr) == (stdev, stderr)
    assert type(estimate) is PlanEstimate


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-20")
@pytest.mark.parametrize("support_count", [None, 0, 500])
def test_est1_rejects_an_estimate_without_uncertainty(support_count: int | None) -> None:
    error = _invariant_error(
        lambda: _estimate(net_r_stdev=None, expected_net_r_stderr=None, support_count=support_count),
        "EST-1",
        contract="PlanEstimate",
    )
    assert (
        error.details["rule"] == "the estimate states its uncertainty: net_r_stdev or expected_net_r_stderr"
    )


@pytest.mark.requirement("S00-ASG-14")
def test_plan_estimate_fields_are_strict() -> None:
    assert _estimate(expected_net_r=-0.4, baseline_r=None, contributions=()).expected_net_r == -0.4
    _field_error(lambda: _estimate(model_version_id=None), "model_version_id")
    _field_error(lambda: _estimate(model_version_id=_plan_id()), "model_version_id")
    _field_error(lambda: _estimate(model_status="RESEARCH"), "model_status")
    _field_error(lambda: _estimate(expected_net_r=None), "expected_net_r")
    _field_error(lambda: _estimate(expected_net_r=float("nan")), "expected_net_r")
    _field_error(lambda: _estimate(expected_cost_r=-0.01), "expected_cost_r")
    _field_error(lambda: _estimate(net_r_stdev=-1.0), "net_r_stdev")
    _field_error(lambda: _estimate(expected_net_r_stderr=-1.0), "expected_net_r_stderr")
    _field_error(lambda: _estimate(support_count=-1), "support_count")
    duplicate = (
        Contribution(evidence_id=None, feature="cost", weight_r=0.1),
        Contribution(evidence_id=None, feature="cost", weight_r=0.2),
    )
    _field_error(lambda: _estimate(contributions=duplicate), "contributions[1]")


# =========================================================================== validate_plan_transition
def _illegal(previous: TradePlan | None, revision: TradePlan) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as info:
        validate_plan_transition(previous, revision)
    error = info.value
    assert error.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert error.details["plan_id"] == revision.plan_id
    assert error.details["revision"] == revision.revision
    assert isinstance(error.details["rule"], str)
    return error


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(
    "before", [None, *PlanStatus], ids=lambda status: "start" if status is None else status.value
)
@pytest.mark.parametrize("after", list(PlanStatus), ids=lambda status: status.value)
def test_every_row_of_the_plan_status_transition_table(before: PlanStatus | None, after: PlanStatus) -> None:
    previous = None if before is None else _with_status(before)
    revision = _with_status(after, revision=0 if before is None else 1)
    if after in EXPECTED_TRANSITIONS[before]:
        validate_plan_transition(previous, revision)
    else:
        assert before is not None
        error = _illegal(previous, revision)
        assert error.details["rule"] == f"{before.value} -> {after.value} is not allowed"


@pytest.mark.requirement("S00-ASG-14")
def test_the_transition_table_never_leads_back_to_prepared_and_final_statuses_are_final() -> None:
    assert set(EXPECTED_TRANSITIONS) == {None, *PlanStatus}
    for status in FINAL:
        assert EXPECTED_TRANSITIONS[status] == frozenset()
    for status in EXECUTABLE:
        assert PlanStatus.PREPARED not in EXPECTED_TRANSITIONS[status]
    assert PlanStatus.ARMED not in EXPECTED_TRANSITIONS[PlanStatus.SELECTED]
    allowed = sum(len(targets) for targets in EXPECTED_TRANSITIONS.values())
    assert allowed == 21


@pytest.mark.requirement("S00-ASG-14")
def test_the_first_record_of_a_plan_has_revision_0() -> None:
    assert (
        _illegal(None, _plan(revision=1, seq=1)).details["rule"]
        == "the first record of a plan has revision 0"
    )
    _illegal(None, _plan(revision=4, seq=4))


@pytest.mark.requirement("S00-ASG-14")
def test_the_revision_number_increases_by_exactly_one() -> None:
    first = _plan()
    for wrong in (first, _plan(revision=2, seq=2), _plan(revision=3, seq=3)):
        assert _illegal(first, wrong).details["rule"] == "revision must increase by exactly 1"
    assert _illegal(_plan(revision=2, seq=2), _plan(revision=1, seq=1)).details["rule"] == (
        "revision must increase by exactly 1"
    )


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-22")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"variant": 1}, id="variant"),
        pytest.param({"entry_style": EntryStyle.RETEST}, id="entry_style"),
        pytest.param({"candidate_id": _candidate_id(SHADOW_MODEL)}, id="candidate_id"),
    ],
)
def test_a_revision_of_another_plan_cannot_continue_the_chain(overrides: dict[str, Any]) -> None:
    error = _illegal(_plan(), _plan(revision=1, seq=1, **overrides))
    assert error.details["rule"] == "supersedes_plan_id must name the previous revision"


@pytest.mark.requirement("S00-ASG-14")
@pytest.mark.parametrize(
    ("field", "build"),
    [
        pytest.param("run_id", lambda: _plan(revision=1, seq=1, run_id=OTHER_RUN), id="run_id"),
        pytest.param("mode", lambda: _plan(revision=1, seq=1, mode=RunMode.PAPER), id="mode"),
        pytest.param(
            "producer", lambda: _plan(revision=1, seq=1, producer="decision.planner_b"), id="producer"
        ),
        pytest.param(
            "opportunity_id",
            lambda: _plan(revision=1, seq=1, opportunity_id=_other_opportunity()),
            id="opportunity",
        ),
        pytest.param("side", lambda: _sell(revision=1, seq=1), id="side"),
        pytest.param("horizon", lambda: _plan(revision=1, seq=1, horizon=Horizon.INTRADAY), id="horizon"),
    ],
)
def test_identity_fields_may_not_change_between_revisions(field: str, build: Build) -> None:
    revision = build()
    assert isinstance(revision, TradePlan)
    assert revision.supersedes_plan_id == _plan().plan_id
    assert _illegal(_plan(), revision).details["rule"] == f"{field} may not change between revisions"


@pytest.mark.requirement("S00-ASG-14", "G04-11")
@pytest.mark.parametrize(
    ("first", "tighter", "wider"),
    [
        pytest.param(_plan, D("2648.50"), D("2647.80") - STEP, id="buy_limit"),
        pytest.param(_sell, D("2651.90"), D("2652.60") + STEP, id="sell_limit"),
        pytest.param(_market, D("2649.00"), D("2647.00"), id="buy_market"),
        pytest.param(lambda: _market(Side.SELL), D("2651.00"), D("2652.10") + STEP, id="sell_market"),
        pytest.param(_pullback, D("2647.50"), D("2646.90") - STEP, id="buy_pullback"),
        pytest.param(lambda: _pullback(Side.SELL), D("2652.80"), D("2653.30") + STEP, id="sell_pullback"),
    ],
)
def test_the_stop_may_only_move_towards_the_entry(first: Build, tighter: D, wider: D) -> None:
    previous = first()
    assert isinstance(previous, TradePlan)

    def revise(stop: D) -> TradePlan:
        return _changed(
            previous,
            revision=1,
            seq=1,
            stop_price=stop,
            plan_id=identity.plan_id(previous.candidate_id, previous.entry_style.value, previous.variant, 1),
            supersedes_plan_id=previous.plan_id,
        )

    validate_plan_transition(previous, revise(previous.stop_price))
    tightened = revise(tighter)
    assert tightened.risk_per_unit < previous.risk_per_unit
    validate_plan_transition(previous, tightened)
    widened = revise(wider)
    assert widened.risk_per_unit > previous.risk_per_unit
    error = _illegal(previous, widened)
    assert error.details["rule"] == "the stop may not be moved away from the entry (no widening)"
    # Tightening is not reversible: going back to the original stop afterwards is a widening.
    back = _changed(
        previous,
        revision=2,
        seq=2,
        plan_id=identity.plan_id(previous.candidate_id, previous.entry_style.value, previous.variant, 2),
        supersedes_plan_id=tightened.plan_id,
    )
    assert (
        _illegal(tightened, back).details["rule"]
        == "the stop may not be moved away from the entry (no widening)"
    )


@pytest.mark.requirement("S00-ASG-14", "G04-11")
def test_a_closing_revision_may_not_widen_the_stop_either() -> None:
    previous = _plan()
    for status in FINAL:
        widened = _with_status(status, revision=1, stop_price=D("2640.00"))
        assert _illegal(previous, widened).details["rule"] == (
            "the stop may not be moved away from the entry (no widening)"
        )
        validate_plan_transition(previous, _with_status(status, revision=1))


@pytest.mark.requirement("S00-ASG-14")
def test_a_whole_chain_of_revisions_is_accepted() -> None:
    later = {"decision_time_ns": T0 + NS_PER_SECOND, "as_of_time_ns": T0 + NS_PER_SECOND}
    chain = [
        _with_status(PlanStatus.PREPARED, expected_net_r=-0.05),
        _with_status(PlanStatus.PREPARED, revision=1, expected_net_r=0.10),
        _with_status(PlanStatus.ARMED, revision=2, stop_price=D("2648.00")),
        _with_status(PlanStatus.SELECTED, revision=3, seq=7, stop_price=D("2648.00"), **later),
        _with_status(PlanStatus.SELECTED, revision=4, seq=8, stop_price=D("2648.40"), **later),
        _with_status(PlanStatus.WITHDRAWN, revision=5, seq=9, stop_price=D("2648.40"), **later),
    ]
    validate_plan_transition(None, chain[0])
    for previous, current in itertools.pairwise(chain):
        validate_plan_transition(previous, current)
    assert len({plan.alternative_group_id for plan in chain}) == 1
    _illegal(
        chain[-1], _with_status(PlanStatus.PREPARED, revision=6, seq=10, stop_price=D("2648.40"), **later)
    )


@pytest.mark.requirement("S00-ASG-14")
def test_a_market_plan_may_be_reissued_with_a_fresh_reference_price() -> None:
    first = _market(status=PlanStatus.ARMED)
    fresh = _market(status=PlanStatus.SELECTED, revision=1, seq=1, reference_price=D("2650.80"))
    assert fresh.reference_price != first.reference_price
    assert fresh.stop_price == first.stop_price
    validate_plan_transition(first, fresh)


@pytest.mark.requirement("S00-ASG-14", "S00-ASG-21")
def test_times_and_sequence_numbers_do_not_decrease_along_a_chain() -> None:
    first = _plan(seq=4)
    validate_plan_transition(first, _plan(revision=1, seq=5))
    earlier_decision = _plan(revision=1, seq=5, decision_time_ns=T0 - 1, as_of_time_ns=T0 - NS_PER_MS)
    assert _illegal(first, earlier_decision).details["rule"] == "decision_time_ns may not decrease"
    earlier_watermark = _plan(revision=1, seq=5, as_of_time_ns=T0 - NS_PER_MS - 1)
    assert _illegal(first, earlier_watermark).details["rule"] == "as_of_time_ns may not decrease"
    assert _illegal(first, _plan(revision=1, seq=3)).details["rule"] == "seq may not decrease"


@pytest.mark.requirement("S00-ASG-14")
def test_an_illegal_transition_error_is_journalable_data() -> None:
    error = _illegal(_with_status(PlanStatus.EXPIRED), _plan(revision=1, seq=1))
    payload = error.to_dict()
    assert payload["code"] == "GIE.STATE.ILLEGAL_TRANSITION"
    assert payload["details"] == dict(error.details)
    assert isinstance(error, GieError)
