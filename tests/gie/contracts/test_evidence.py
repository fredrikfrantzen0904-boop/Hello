"""Independent tests for the ``Evidence`` contract (``gie/contracts/evidence.py``) and the shared value
objects it uses (``PriceLevel``, ``PriceCondition``, ``NamedValue``, ``derive_origin``).

ALL DATA IN THIS MODULE IS SYNTHETIC FIXTURE DATA.  Every record is built with the labelled
builders of ``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); engines, levels, scores and
instants are invented.  Nothing here is market data, an engine output or evidence of performance,
and nothing here claims that the six existing engines have been adapted (session 02 has not started).

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - "Evidence 1.0.0" with the invariants CAUSAL-1 and
  EVD-1 .. EVD-13, the nested value objects (COND-1) and the enumerations;
* ``specifications/SHARED_CONTRACTS.md`` - section 2 (unknown is ``null``, never 0; levels of a
  non-price series may have any sign but must be ``> 0`` for XAUUSD), section 3.1 (observable time
  ``<=`` watermark ``<=`` decision time), section 4 (market events, evaluation slots and the one
  de-duplication function), section 5 (error semantics) and section 9.2 (the identity of a market
  event never changes between revisions);
* ``specifications/extracted/02_Strategy_Intelligence.txt`` - bullish/bearish scores, event ids,
  freshness, levels, stages, invalidation, missing/contra states, the Cavemen pending limit, exact
  de-duplication of shared sweep/FVG/breakout evidence, inactive vs missing evidence.

The de-duplication tests carry the requirement ids G02-02 and S02-ACC-02 and the inactive-vs-missing
tests S02-ACC-03 as *requirement* markers: they verify the contract-level rules those acceptance
tests rest on, not the adapters of session 02.
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
    PENDING_LIMIT_KIND,
    PENDING_LIMIT_ROLE,
    SHARED_EVIDENCE_KINDS,
    STRATEGY_ENGINES,
    DataOrigin,
    Evidence,
    EvidenceGroup,
    EvidenceStage,
    EvidenceState,
    NamedValue,
    PriceBasis,
    PriceCondition,
    PriceLevel,
    PriceTrigger,
    ProducerClass,
    RunMode,
    Timeframe,
    derive_origin,
    from_json,
    from_wire,
    group_by_market_event,
    identity,
    ordering_time_ns,
    stream_name,
    to_json,
    to_wire,
    validate_evidence_transition,
)
from gie.core.errors import ContractValidationError, GieError, IllegalTransitionError
from gie.core.freshness import is_expired
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
ANCHOR = T0 - 300 * NS_PER_SECOND
SWEPT = D("2648.40")
#: A well-formed market event id that none of the records below carries.
OTHER_EVENT = identity.market_event_id("breakout", "XAUUSD", "M15", T0 - 900 * NS_PER_SECOND, [D("2651.20")])
ANOTHER_EVENT = identity.market_event_id("fair_value_gap", "XAUUSD", "M1", T0 - 60 * NS_PER_SECOND, [])

Build = Callable[[], object]


# --------------------------------------------------------------------------- labelled fixture builders
def _engine(engine: str) -> dict[str, Any]:
    return {"engine": engine, "producer": f"strategy_intelligence.{engine}"}


def _active(engine: str = "sweep_reversal", **overrides: Any) -> Evidence:
    return samples.evidence(**{**_engine(engine), **overrides})


def _inactive(engine: str = "orb_vwap", **overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        **_engine(engine),
        "kind": "breakout",
        "state": EvidenceState.INACTIVE,
        "stage": EvidenceStage.NONE,
        "bullish_score": 0.0,
        "bearish_score": 0.0,
        "levels": (),
        "invalidation": None,
        "features": (),
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _missing(engine: str = "vwap_trend_rider", **overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        **_engine(engine),
        "kind": "breakout",
        "state": EvidenceState.MISSING,
        "stage": EvidenceStage.NONE,
        "bullish_score": None,
        "bearish_score": None,
        "levels": (),
        "invalidation": None,
        "missing_reason": "INSUFFICIENT_HISTORY",
        "features": (),
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _contra(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        **_engine("liquidity_hunter"),
        "kind": "rejection",
        "state": EvidenceState.CONTRA,
        "bullish_score": 0.0,
        "bearish_score": 0.6,
        "levels": (PriceLevel(role="REJECTION_HIGH", price=D("2650.90"), identity=True),),
        "invalidation": None,
        "contradicts_market_event_ids": (samples.evidence().market_event_id,),
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _closed(state: EvidenceState, **overrides: Any) -> Evidence:
    """A revision-1 record in a final state (INVALIDATED or EXPIRED) of the sample observation."""
    fields: dict[str, Any] = {
        "revision": 1,
        "seq": 1,
        "state": state,
        "stage": EvidenceStage.COMPLETED,
        "bullish_score": 0.0,
        "bearish_score": 0.0,
        "decision_time_ns": T0 + 30 * NS_PER_SECOND,
        "as_of_time_ns": T0 + 30 * NS_PER_SECOND,
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _pending_limit(**overrides: Any) -> Evidence:
    """The Cavemen resting limit at breakout close (brief 02), as a labelled fixture."""
    fields: dict[str, Any] = {
        **_engine("cavemen"),
        "kind": PENDING_LIMIT_KIND,
        "stage": EvidenceStage.ARMED,
        "bullish_score": 0.55,
        "bearish_score": 0.0,
        "related_market_event_id": OTHER_EVENT,
        "levels": (PriceLevel(role=PENDING_LIMIT_ROLE, price=D("2650.90"), identity=True),),
        "invalidation": None,
        "features": (),
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _intermarket(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "producer_class": ProducerClass.INTERMARKET,
        "engine": "real_yield_lead",
        "producer": "intermarket.lead_lag",
        "kind": "lead_lag_signal",
        "symbol": "US10Y_REAL",
        "timeframe": Timeframe.NONE,
        "stage": EvidenceStage.NONE,
        "bullish_score": 0.3,
        "bearish_score": 0.0,
        "levels": (PriceLevel(role="OBSERVED_VALUE", price=D("-0.35"), identity=False),),
        "invalidation": None,
        "features": (NamedValue(name="lag_seconds", value=45.0),),
    }
    fields.update(overrides)
    return samples.evidence(**fields)


def _level(role: str, price: str, *, is_identity: bool) -> PriceLevel:
    return PriceLevel(role=role, price=D(price), identity=is_identity)


def _condition(
    price: str, basis: PriceBasis = PriceBasis.BID, timeframe: Timeframe | None = None
) -> PriceCondition:
    return PriceCondition(price=D(price), trigger=PriceTrigger.AT_OR_BELOW, basis=basis, timeframe=timeframe)


def _changed(record: Evidence, **changes: Any) -> Evidence:
    """Copy *record* with single fields replaced; nothing is recomputed (the copy is re-validated)."""
    return dataclasses.replace(record, **changes)


def _slot_id(engine: str, kind: str = "breakout") -> str:
    return identity.evaluation_slot_id(engine, kind, "XAUUSD", "M5", ANCHOR)


def _event_id(kind: str = "liquidity_sweep", prices: tuple[str, ...] = ("2648.40",)) -> str:
    return identity.market_event_id(kind, "XAUUSD", "M5", ANCHOR, [D(price) for price in prices])


def _invariant_error(build: Build, invariant_id: str) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as info:
        build()
    error = info.value
    assert error.details.get("invariant") == invariant_id, str(error)
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
EVIDENCE_INVARIANTS = ("CAUSAL-1", *(f"EVD-{number}" for number in range(1, 14)))


@pytest.mark.requirement("S00-ASG-12")
def test_evidence_declares_exactly_the_invariants_of_the_field_reference() -> None:
    assert tuple(rule.id for rule in Evidence.ALL_INVARIANTS) == EVIDENCE_INVARIANTS
    assert (Evidence.CONTRACT, Evidence.VERSION) == ("Evidence", "1.0.0")
    assert [rule.id for rule in PriceCondition.ALL_INVARIANTS] == ["COND-1"]


@pytest.mark.requirement("S00-ASG-12")
def test_published_vocabularies() -> None:
    assert [state.value for state in EvidenceState] == [
        "ACTIVE",
        "INACTIVE",
        "MISSING",
        "CONTRA",
        "INVALIDATED",
        "EXPIRED",
    ]
    assert [stage.value for stage in EvidenceStage] == [
        "NONE",
        "ANTICIPATED",
        "FORMING",
        "ARMED",
        "TRIGGERED",
        "CONFIRMED",
        "COMPLETED",
    ]
    assert [item.value for item in ProducerClass] == [
        "STRATEGY_ENGINE",
        "INTERMARKET",
        "VOLATILITY",
        "PATTERN",
    ]
    assert STRATEGY_ENGINES == (
        "cavemen",
        "silver_bullet",
        "liquidity_hunter",
        "orb_vwap",
        "vwap_trend_rider",
        "sweep_reversal",
    )
    assert SHARED_EVIDENCE_KINDS == ("liquidity_sweep", "fair_value_gap", "breakout")
    assert (PENDING_LIMIT_KIND, PENDING_LIMIT_ROLE) == ("pending_limit", "PENDING_LIMIT")


@pytest.mark.requirement("S00-ASG-12")
def test_sample_evidence_is_labelled_synthetic_and_immutable() -> None:
    variants = samples.variants()["Evidence"]
    assert len(variants) >= 10
    for name, record in variants.items():
        assert type(record) is Evidence, name
        assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE, name
    with pytest.raises(dataclasses.FrozenInstanceError):
        variants["active_bullish"].bullish_score = 1.0  # type: ignore[misc]


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-21")
def test_stream_and_ordering_time() -> None:
    record = _active()
    assert stream_name(record) == "evidence/strategy_intelligence.sweep_reversal"
    assert ordering_time_ns(record) == record.decision_time_ns == T0


# =========================================================================== invariants: accepted
ACCEPTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_equals_decision_time", lambda: _active(as_of_time_ns=T0, decision_time_ns=T0)),
    ("CAUSAL-1", "watermark_long_before_decision", lambda: _active(as_of_time_ns=T0 - 60 * NS_PER_SECOND)),
    ("EVD-1", "revision_0", _active),
    ("EVD-1", "revision_3", lambda: _active(revision=3, seq=3)),
    (
        "EVD-2",
        "two_identity_levels",
        lambda: _active(
            kind="fair_value_gap",
            levels=(
                _level("FVG_HIGH", "2651.90", is_identity=True),
                _level("FVG_LOW", "2650.10", is_identity=True),
            ),
        ),
    ),
    ("EVD-2", "no_identity_level", lambda: _active(levels=(_level("NOTE", "2650", is_identity=False),))),
    ("EVD-2", "inactive_carries_slot_id", _inactive),
    ("EVD-2", "missing_carries_slot_id", _missing),
    ("EVD-3", "missing_null_null", _missing),
    ("EVD-3", "active_zero_zero_non_directional", lambda: _active(bullish_score=0.0, bearish_score=0.0)),
    ("EVD-3", "active_both_directions", lambda: _active(bullish_score=1.0, bearish_score=0.25)),
    ("EVD-4", "missing_with_reason", lambda: _missing(missing_reason="FEED_GAP")),
    ("EVD-4", "inactive_without_reason", _inactive),
    ("EVD-5", "inactive_zero_zero", _inactive),
    ("EVD-5", "invalidated_zero_zero", lambda: _closed(EvidenceState.INVALIDATED)),
    ("EVD-5", "expired_zero_zero", lambda: _closed(EvidenceState.EXPIRED)),
    ("EVD-6", "contra_names_one_event", _contra),
    (
        "EVD-6",
        "contra_names_several",
        lambda: _contra(contradicts_market_event_ids=(OTHER_EVENT, ANOTHER_EVENT)),
    ),
    ("EVD-6", "active_names_none", _active),
    ("EVD-7", "revision_0_supersedes_nothing", _active),
    ("EVD-7", "revision_1_names_revision_0", lambda: _active(revision=1, seq=1)),
    ("EVD-7", "revision_7_names_revision_6", lambda: _active(revision=7, seq=7)),
    ("EVD-8", "active_expires_one_ns_after_decision", lambda: _active(valid_until_ns=T0 + 1)),
    ("EVD-8", "contra_expires_one_ns_after_decision", lambda: _contra(valid_until_ns=T0 + 1)),
    ("EVD-8", "inactive_may_be_past_expiry", lambda: _inactive(valid_until_ns=T0)),
    ("EVD-8", "expired_is_past_expiry", lambda: _closed(EvidenceState.EXPIRED, valid_until_ns=T0)),
    ("EVD-9", "intermarket_other_symbol", _intermarket),
    (
        "EVD-9",
        "pattern_producer_free_engine_id",
        lambda: _active(producer_class=ProducerClass.PATTERN, engine="pattern_scan", producer="pattern.scan"),
    ),
    (
        "EVD-9",
        "volatility_producer_on_gold",
        lambda: _active(
            producer_class=ProducerClass.VOLATILITY, engine="vol_regime", producer="volatility.regime"
        ),
    ),
    ("EVD-10", "inactive_no_event", _inactive),
    (
        "EVD-10",
        "inactive_with_non_identity_level",
        lambda: _inactive(levels=(_level("NOTE", "2650", is_identity=False),)),
    ),
    ("EVD-10", "missing_no_event", _missing),
    ("EVD-11", "observable_equals_watermark", lambda: _active(observable_time_ns=T0 - NS_PER_MS)),
    ("EVD-11", "observable_before_watermark", lambda: _active(observable_time_ns=T0 - NS_PER_SECOND)),
    ("EVD-12", "buy_limit_bullish_only", _pending_limit),
    ("EVD-12", "sell_limit_bearish_only", lambda: _pending_limit(bullish_score=0.0, bearish_score=0.4)),
    (
        "EVD-12",
        "limit_level_next_to_other_levels",
        lambda: _pending_limit(
            levels=(
                _level(PENDING_LIMIT_ROLE, "2650.90", is_identity=True),
                _level("BREAKOUT_CLOSE", "2650.90", is_identity=False),
            )
        ),
    ),
    ("EVD-13", "gold_levels_and_invalidation_positive", _active),
    (
        "EVD-13",
        "gold_smallest_positive_level",
        lambda: _active(levels=(_level("NOTE", "0.000000000001", is_identity=False),)),
    ),
    (
        "EVD-13",
        "other_symbol_any_sign",
        lambda: _intermarket(
            levels=(
                _level("OBSERVED_VALUE", "-0.35", is_identity=False),
                _level("PRIOR_VALUE", "0", is_identity=False),
                _level("REFERENCE", "1.2", is_identity=True),
            ),
            invalidation=_condition("-0.50", PriceBasis.MID),
        ),
    ),
]


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in ACCEPTED_CASES],
)
def test_invariant_accepts(invariant_id: str, build: Build) -> None:
    record = build()
    assert type(record) is Evidence
    assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE
    assert invariant_id in EVIDENCE_INVARIANTS
    # Re-validation of an existing record and the wire round trip must both succeed.
    assert dataclasses.replace(record) == record
    assert from_wire(to_wire(record), Evidence) == record


# =========================================================================== invariants: rejected
REJECTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_one_ns_after_decision", lambda: _changed(_active(), as_of_time_ns=T0 + 1)),
    (
        "CAUSAL-1",
        "watermark_long_after_decision",
        lambda: _changed(_active(), as_of_time_ns=T0 + NS_PER_SECOND),
    ),
    (
        "EVD-1",
        "id_of_another_engine",
        lambda: _changed(_active(), evidence_id=identity.evidence_id("cavemen", _event_id(), 0)),
    ),
    (
        "EVD-1",
        "id_of_another_revision",
        lambda: _changed(_active(), evidence_id=identity.evidence_id("sweep_reversal", _event_id(), 1)),
    ),
    ("EVD-1", "id_of_another_event", lambda: _changed(_active(), evidence_id=_contra().evidence_id)),
    ("EVD-2", "id_of_an_unrelated_event", lambda: _active(market_event_id=OTHER_EVENT)),
    (
        "EVD-2",
        "id_from_another_identity_price",
        lambda: _active(market_event_id=_event_id(prices=("2648.41",))),
    ),
    (
        "EVD-2",
        "id_that_includes_a_non_identity_level",
        lambda: _active(
            levels=(
                _level("SWEPT_LEVEL", "2648.40", is_identity=True),
                _level("NOTE", "2650", is_identity=False),
            ),
            market_event_id=_event_id(prices=("2648.40", "2650")),
        ),
    ),
    (
        "EVD-2",
        "id_that_ignores_an_identity_level",
        lambda: _active(
            levels=(
                _level("SWEPT_LEVEL", "2648.40", is_identity=True),
                _level("WICK_LOW", "2647.95", is_identity=True),
            ),
            market_event_id=_event_id(),
        ),
    ),
    ("EVD-2", "inactive_with_market_event_id", lambda: _inactive(market_event_id=_event_id("breakout", ()))),
    ("EVD-2", "missing_with_market_event_id", lambda: _missing(market_event_id=_event_id("breakout", ()))),
    ("EVD-2", "inactive_with_slot_of_another_engine", lambda: _inactive(market_event_id=_slot_id("cavemen"))),
    (
        "EVD-2",
        "active_with_slot_id",
        lambda: _active(market_event_id=_slot_id("sweep_reversal", "liquidity_sweep")),
    ),
    ("EVD-3", "missing_with_zero_scores", lambda: _changed(_missing(), bullish_score=0.0, bearish_score=0.0)),
    ("EVD-3", "missing_with_one_zero_score", lambda: _changed(_missing(), bearish_score=0.0)),
    (
        "EVD-3",
        "missing_with_a_real_score",
        lambda: _changed(_missing(), bullish_score=0.7, bearish_score=0.1),
    ),
    ("EVD-3", "active_with_null_scores", lambda: _changed(_active(), bullish_score=None, bearish_score=None)),
    ("EVD-3", "active_with_one_null_score", lambda: _changed(_active(), bearish_score=None)),
    (
        "EVD-3",
        "inactive_with_null_scores",
        lambda: _changed(_inactive(), bullish_score=None, bearish_score=None),
    ),
    ("EVD-3", "contra_with_one_null_score", lambda: _changed(_contra(), bullish_score=None)),
    ("EVD-4", "missing_without_reason", lambda: _changed(_missing(), missing_reason=None)),
    ("EVD-4", "inactive_with_reason", lambda: _changed(_inactive(), missing_reason="NOT_EVALUATED")),
    ("EVD-4", "active_with_reason", lambda: _changed(_active(), missing_reason="INSUFFICIENT_HISTORY")),
    ("EVD-5", "inactive_with_bullish_score", lambda: _changed(_inactive(), bullish_score=0.5)),
    ("EVD-5", "inactive_with_tiny_bearish_score", lambda: _changed(_inactive(), bearish_score=5e-324)),
    (
        "EVD-5",
        "invalidated_with_score",
        lambda: _changed(_closed(EvidenceState.INVALIDATED), bullish_score=0.7),
    ),
    ("EVD-5", "expired_with_score", lambda: _changed(_closed(EvidenceState.EXPIRED), bearish_score=0.2)),
    ("EVD-6", "contra_names_nothing", lambda: _changed(_contra(), contradicts_market_event_ids=())),
    (
        "EVD-6",
        "active_names_an_event",
        lambda: _changed(_active(), contradicts_market_event_ids=(OTHER_EVENT,)),
    ),
    (
        "EVD-6",
        "inactive_names_an_event",
        lambda: _changed(_inactive(), contradicts_market_event_ids=(OTHER_EVENT,)),
    ),
    (
        "EVD-6",
        "invalidated_names_an_event",
        lambda: _changed(_closed(EvidenceState.INVALIDATED), contradicts_market_event_ids=(OTHER_EVENT,)),
    ),
    (
        "EVD-6",
        "contra_names_itself",
        lambda: _changed(_contra(), contradicts_market_event_ids=(_contra().market_event_id,)),
    ),
    (
        "EVD-6",
        "contra_names_itself_among_others",
        lambda: _changed(_contra(), contradicts_market_event_ids=(OTHER_EVENT, _contra().market_event_id)),
    ),
    (
        "EVD-7",
        "revision_0_names_a_predecessor",
        lambda: _changed(
            _active(), supersedes_evidence_id=identity.evidence_id("sweep_reversal", _event_id(), 0)
        ),
    ),
    (
        "EVD-7",
        "revision_1_names_nothing",
        lambda: _changed(_active(revision=1, seq=1), supersedes_evidence_id=None),
    ),
    (
        "EVD-7",
        "revision_1_names_itself",
        lambda: _changed(
            _active(revision=1, seq=1), supersedes_evidence_id=_active(revision=1, seq=1).evidence_id
        ),
    ),
    (
        "EVD-7",
        "revision_2_skips_revision_1",
        lambda: _changed(_active(revision=2, seq=2), supersedes_evidence_id=_active().evidence_id),
    ),
    (
        "EVD-7",
        "revision_1_names_another_engines_record",
        lambda: _changed(
            _active(revision=1, seq=1), supersedes_evidence_id=_active("liquidity_hunter").evidence_id
        ),
    ),
    ("EVD-8", "active_expires_at_decision_time", lambda: _changed(_active(), valid_until_ns=T0)),
    ("EVD-8", "active_expired_before_decision", lambda: _changed(_active(), valid_until_ns=T0 - 1)),
    ("EVD-8", "contra_expires_at_decision_time", lambda: _changed(_contra(), valid_until_ns=T0)),
    ("EVD-9", "strategy_engine_unknown_id", lambda: _active("mystery_engine")),
    ("EVD-9", "strategy_engine_existing_bot_name", lambda: _active("gold_bot")),
    ("EVD-9", "strategy_engine_on_silver", lambda: _active("cavemen", symbol="XAGUSD")),
    (
        "EVD-9",
        "intermarket_record_relabelled_as_strategy_engine",
        lambda: _changed(_intermarket(), producer_class=ProducerClass.STRATEGY_ENGINE),
    ),
    ("EVD-10", "inactive_with_stage", lambda: _changed(_inactive(), stage=EvidenceStage.FORMING)),
    (
        "EVD-10",
        "inactive_with_identity_level",
        lambda: _changed(_inactive(), levels=(_level("BREAKOUT_LEVEL", "2651.20", is_identity=True),)),
    ),
    (
        "EVD-10",
        "inactive_with_related_event",
        lambda: _changed(_inactive(), related_market_event_id=OTHER_EVENT),
    ),
    ("EVD-10", "missing_with_stage", lambda: _changed(_missing(), stage=EvidenceStage.ANTICIPATED)),
    (
        "EVD-10",
        "missing_with_identity_level",
        lambda: _changed(_missing(), levels=(_level("BREAKOUT_LEVEL", "2651.20", is_identity=True),)),
    ),
    (
        "EVD-10",
        "missing_with_related_event",
        lambda: _changed(_missing(), related_market_event_id=OTHER_EVENT),
    ),
    (
        "EVD-11",
        "observable_one_ns_after_watermark",
        lambda: _changed(_active(), observable_time_ns=T0 - NS_PER_MS + 1),
    ),
    ("EVD-11", "observable_at_decision_after_watermark", lambda: _changed(_active(), observable_time_ns=T0)),
    ("EVD-12", "no_related_breakout", lambda: _changed(_pending_limit(), related_market_event_id=None)),
    ("EVD-12", "both_directions", lambda: _changed(_pending_limit(), bearish_score=0.2)),
    ("EVD-12", "no_direction", lambda: _changed(_pending_limit(), bullish_score=0.0)),
    ("EVD-12", "no_level_at_all", lambda: _pending_limit(levels=())),
    (
        "EVD-12",
        "only_a_level_with_another_role",
        lambda: _pending_limit(levels=(_level("BREAKOUT_CLOSE", "2650.90", is_identity=True),)),
    ),
    (
        "EVD-12",
        "two_pending_limit_levels",
        lambda: _pending_limit(
            levels=(
                _level(PENDING_LIMIT_ROLE, "2650.90", is_identity=True),
                _level(PENDING_LIMIT_ROLE, "2651.40", is_identity=True),
            )
        ),
    ),
    ("EVD-13", "gold_level_zero", lambda: _active(levels=(_level("NOTE", "0", is_identity=False),))),
    (
        "EVD-13",
        "gold_level_negative",
        lambda: _active(levels=(_level("NOTE", "-2648.40", is_identity=False),)),
    ),
    (
        "EVD-13",
        "gold_identity_level_negative",
        lambda: _active(levels=(_level("SWEPT_LEVEL", "-1", is_identity=True),)),
    ),
    ("EVD-13", "gold_invalidation_zero", lambda: _active(invalidation=_condition("0"))),
    ("EVD-13", "gold_invalidation_negative", lambda: _active(invalidation=_condition("-0.01"))),
    (
        "EVD-13",
        "intermarket_producer_observing_gold",
        lambda: _intermarket(symbol="XAUUSD"),
    ),
]


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in REJECTED_CASES],
)
def test_invariant_rejects(invariant_id: str, build: Build) -> None:
    error = _invariant_error(build, invariant_id)
    assert error.details["contract"] == "Evidence"
    rule = next(rule for rule in Evidence.ALL_INVARIANTS if rule.id == invariant_id)
    assert error.details["rule"] == rule.text
    assert invariant_id in error.message


@pytest.mark.requirement("S00-ASG-12")
def test_every_invariant_has_an_accepted_and_a_rejected_case() -> None:
    assert {invariant for invariant, _, _ in ACCEPTED_CASES} == set(EVIDENCE_INVARIANTS)
    assert {invariant for invariant, _, _ in REJECTED_CASES} == set(EVIDENCE_INVARIANTS)


@pytest.mark.requirement("S00-ASG-12")
def test_decoding_enforces_the_invariants() -> None:
    inactive = to_wire(_inactive())
    with pytest.raises(ContractValidationError) as info:
        from_wire({**inactive, "bullish_score": 0.5}, Evidence)
    assert info.value.details["invariant"] == "EVD-5"
    contra = to_wire(_contra())
    with pytest.raises(ContractValidationError) as info:
        from_wire({**contra, "contradicts_market_event_ids": []}, Evidence)
    assert info.value.details["invariant"] == "EVD-6"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(_active()), "market_event_id": OTHER_EVENT}, Evidence)
    assert info.value.details["invariant"] == "EVD-1"


# =========================================================================== scores, stages, engines
@pytest.mark.requirement("S00-ASG-12", "S00-ASG-20")
@pytest.mark.parametrize("field", ["bullish_score", "bearish_score"])
@pytest.mark.parametrize(
    "value",
    [-0.000001, 1.000001, 2, -1, float("nan"), float("inf"), True, "0.5", D("0.5")],
    ids=["below_0", "above_1", "int_2", "int_minus_1", "nan", "inf", "bool", "str", "decimal"],
)
def test_scores_are_finite_numbers_between_0_and_1(field: str, value: object) -> None:
    _field_error(lambda: _changed(_active(), **{field: value}), field)


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-20")
def test_score_boundaries_are_accepted() -> None:
    record = _active(bullish_score=1.0, bearish_score=0.0)
    assert (record.bullish_score, record.bearish_score) == (1.0, 0.0)
    record = _active(bullish_score=0.0, bearish_score=1.0)
    assert (record.bullish_score, record.bearish_score) == (0.0, 1.0)


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("stage", list(EvidenceStage))
def test_every_stage_is_available_to_an_active_observation(stage: EvidenceStage) -> None:
    assert _active(stage=stage).stage is stage


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(
    ("stage", "state"),
    [
        ("TRIGGERED", "ACTIVE"),
        ("DONE", "PENDING"),
        (None, None),
        (EvidenceState.ACTIVE, EvidenceStage.TRIGGERED),
        (0, 0),
    ],
    ids=["look_alike_strings", "unknown_strings", "none", "member_of_the_other_enum", "int"],
)
def test_stage_and_state_must_be_genuine_members(stage: object, state: object) -> None:
    _field_error(lambda: _changed(_active(), stage=stage), "stage")
    _field_error(lambda: _changed(_active(), state=state), "state")


@pytest.mark.requirement("S00-ASG-12", "G01-01")
@pytest.mark.parametrize("engine", STRATEGY_ENGINES)
def test_each_of_the_six_canonical_engines_may_produce_gold_evidence(engine: str) -> None:
    record = _active(engine)
    assert record.engine == engine
    assert record.producer_class is ProducerClass.STRATEGY_ENGINE
    assert record.symbol == "XAUUSD"
    assert record.evidence_id == identity.evidence_id(engine, record.market_event_id, 0)


@pytest.mark.requirement("S00-ASG-12", "G01-01")
@pytest.mark.parametrize("engine", STRATEGY_ENGINES)
@pytest.mark.parametrize("symbol", ["XAGUSD", "EURUSD", "US10Y_REAL"])
def test_no_strategy_engine_may_observe_another_instrument(engine: str, symbol: str) -> None:
    _invariant_error(lambda: _active(engine, symbol=symbol), "EVD-9")


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("engine", ["Cavemen", "c", "cave men", "cavemen-2", "", "x" * 33, None])
def test_malformed_engine_ids_are_rejected(engine: object) -> None:
    _field_error(lambda: _changed(_active(), engine=engine), "engine")


# =========================================================================== levels, invalidation, features
@pytest.mark.requirement("S00-ASG-12", "S00-ASG-20")
def test_intermarket_evidence_may_carry_levels_of_any_sign_while_gold_levels_must_be_positive() -> None:
    record = _intermarket(
        levels=(
            _level("OBSERVED_VALUE", "-0.35", is_identity=False),
            _level("ZERO_LINE", "0", is_identity=False),
        ),
        invalidation=_condition("-1.25", PriceBasis.MID),
    )
    assert [level.price for level in record.levels] == [D("-0.35"), D("0")]
    assert record.invalidation is not None
    assert record.invalidation.price == D("-1.25")
    assert record.symbol == "US10Y_REAL"
    # The very same level values are refused as soon as the observation is about XAUUSD.
    _invariant_error(
        lambda: _active(levels=(_level("OBSERVED_VALUE", "-0.35", is_identity=False),), invalidation=None),
        "EVD-13",
    )
    _invariant_error(lambda: _active(invalidation=_condition("-1.25", PriceBasis.MID)), "EVD-13")


@pytest.mark.requirement("S00-ASG-12")
def test_levels_are_unique_by_role_and_price_and_canonically_sorted() -> None:
    supplied = (
        _level("WICK_LOW", "2647.95", is_identity=False),
        _level("SWEPT_LEVEL", "2648.40", is_identity=True),
        _level("RANGE", "2652", is_identity=False),
        _level("RANGE", "2649", is_identity=False),
    )
    expected = [
        ("RANGE", D("2649")),
        ("RANGE", D("2652")),
        ("SWEPT_LEVEL", D("2648.40")),
        ("WICK_LOW", D("2647.95")),
    ]
    reference = _active(levels=supplied)
    for order in itertools.permutations(supplied):
        record = _active(levels=order)
        assert [(level.role, level.price) for level in record.levels] == expected
        assert record == reference
        assert to_json(record) == to_json(reference)
    duplicate = (_level("RANGE", "2649.0", is_identity=False), _level("RANGE", "2649.00", is_identity=True))
    error = _field_error(lambda: _changed(_active(), levels=duplicate), "levels[1]")
    assert error.details["rule"] == "duplicate item"


@pytest.mark.requirement("S00-ASG-12")
def test_level_items_must_be_price_levels_and_at_most_32() -> None:
    _field_error(
        lambda: _changed(_active(), levels=({"role": "NOTE", "price": D("1"), "identity": False},)),
        "levels[0]",
    )
    _field_error(lambda: _changed(_active(), levels=(D("2648.40"),)), "levels[0]")
    _field_error(lambda: _changed(_active(), levels=None), "levels")
    many = tuple(_level("NOTE", str(2600 + index), is_identity=False) for index in range(33))
    assert len(_active(levels=many[:32]).levels) == 32
    _field_error(lambda: _active(levels=many), "levels")


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-20")
def test_price_level_fields_are_strict() -> None:
    assert _level("FVG_HIGH", "-0.5", is_identity=True).price == D("-0.5")
    _field_error(lambda: PriceLevel(role="swept_level", price=D("1"), identity=True), "role")
    _field_error(lambda: PriceLevel(role="S", price=D("1"), identity=True), "role")
    _field_error(lambda: PriceLevel(role="SWEPT_LEVEL", price=1.5, identity=True), "price")  # type: ignore[arg-type]
    _field_error(lambda: PriceLevel(role="SWEPT_LEVEL", price=D("NaN"), identity=True), "price")
    _field_error(lambda: PriceLevel(role="SWEPT_LEVEL", price=D("1"), identity=1), "identity")  # type: ignore[arg-type]
    _field_error(lambda: PriceLevel(role="SWEPT_LEVEL", price=D("1"), identity=None), "identity")  # type: ignore[arg-type]


CANDLE_TIMEFRAMES = [frame for frame in Timeframe if frame not in (Timeframe.NONE, Timeframe.TICK)]


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("timeframe", CANDLE_TIMEFRAMES)
def test_cond1_accepts_a_candle_close_condition_with_a_candle_timeframe(timeframe: Timeframe) -> None:
    condition = _condition("2649.50", PriceBasis.CANDLE_CLOSE, timeframe)
    assert condition.timeframe is timeframe
    assert _active(invalidation=condition).invalidation == condition


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("basis", [PriceBasis.BID, PriceBasis.ASK, PriceBasis.MID])
def test_cond1_accepts_a_quote_condition_without_timeframe(basis: PriceBasis) -> None:
    assert _condition("2649.50", basis).timeframe is None


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(
    ("basis", "timeframe"),
    [
        (PriceBasis.CANDLE_CLOSE, None),
        (PriceBasis.CANDLE_CLOSE, Timeframe.NONE),
        (PriceBasis.CANDLE_CLOSE, Timeframe.TICK),
        (PriceBasis.BID, Timeframe.M5),
        (PriceBasis.ASK, Timeframe.M1),
        (PriceBasis.MID, Timeframe.NONE),
        (PriceBasis.BID, Timeframe.TICK),
    ],
)
def test_cond1_rejects_a_timeframe_that_does_not_fit_the_basis(
    basis: PriceBasis, timeframe: Timeframe | None
) -> None:
    error = _invariant_error(lambda: _condition("2649.50", basis, timeframe), "COND-1")
    assert error.details["contract"] == "PriceCondition"
    assert error.details["rule"] == "timeframe is a candle timeframe if and only if basis is CANDLE_CLOSE"


@pytest.mark.requirement("S00-ASG-12")
def test_price_condition_fields_are_strict_and_the_vocabularies_are_published() -> None:
    assert [item.value for item in PriceTrigger] == ["BELOW", "AT_OR_BELOW", "AT_OR_ABOVE", "ABOVE"]
    assert [item.value for item in PriceBasis] == ["BID", "ASK", "MID", "CANDLE_CLOSE"]
    for trigger in PriceTrigger:
        assert (
            PriceCondition(price=D("1"), trigger=trigger, basis=PriceBasis.BID, timeframe=None).trigger
            is trigger
        )
    _field_error(
        lambda: PriceCondition(
            price=2649.5, trigger=PriceTrigger.BELOW, basis=PriceBasis.BID, timeframe=None
        ),  # type: ignore[arg-type]
        "price",
    )
    _field_error(
        lambda: PriceCondition(price=D("1"), trigger="BELOW", basis=PriceBasis.BID, timeframe=None),  # type: ignore[arg-type]
        "trigger",
    )
    _field_error(
        lambda: PriceCondition(price=D("1"), trigger=PriceTrigger.BELOW, basis="BID", timeframe=None),  # type: ignore[arg-type]
        "basis",
    )


@pytest.mark.requirement("S00-ASG-12")
def test_a_nested_invariant_violation_is_reported_when_decoding_evidence() -> None:
    wire = to_wire(_active())
    wire["invalidation"] = {**wire["invalidation"], "timeframe": "M5"}
    with pytest.raises(ContractValidationError) as info:
        from_wire(wire, Evidence)
    assert info.value.details["invariant"] == "COND-1"
    assert info.value.details["path"] == "invalidation"


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-20", "S02-ACC-03")
def test_named_value_null_means_unknown_and_is_never_turned_into_zero() -> None:
    unknown = NamedValue(name="bars_available", value=None)
    zero = NamedValue(name="bars_available", value=0.0)
    assert unknown.value is None
    assert unknown != zero
    record = _missing(features=(unknown,))
    wire = to_wire(record)
    assert wire["features"] == [{"name": "bars_available", "value": None}]
    assert from_json(to_json(record), Evidence).features[0].value is None
    for bad in (float("nan"), float("inf"), "1.0", True, D("1")):
        _field_error(lambda bad=bad: NamedValue(name="bars_available", value=bad), "value")
    for name in ("Bars", "b", "bars available", ""):
        _field_error(lambda name=name: NamedValue(name=name, value=1.0), "name")


@pytest.mark.requirement("S00-ASG-12")
def test_features_are_unique_by_name_and_sorted_by_name() -> None:
    supplied = (NamedValue(name="sweep_depth", value=0.6), NamedValue(name="bars_since", value=None))
    assert [item.name for item in _active(features=supplied).features] == ["bars_since", "sweep_depth"]
    duplicate = (NamedValue(name="sweep_depth", value=0.6), NamedValue(name="sweep_depth", value=None))
    error = _field_error(lambda: _changed(_active(), features=duplicate), "features[1]")
    assert error.details["rule"] == "duplicate item"


# =========================================================================== freshness and time
@pytest.mark.requirement("S00-ASG-12", "S00-ASG-23")
def test_evidence_is_already_expired_at_its_expiry_instant() -> None:
    record = _active(valid_until_ns=T0 + 300 * NS_PER_SECOND)
    assert is_expired(now_ns=record.valid_until_ns - 1, valid_until_ns=record.valid_until_ns) is False
    assert is_expired(now_ns=record.valid_until_ns, valid_until_ns=record.valid_until_ns) is True
    # A live record that is born expired cannot exist (EVD-8) ...
    _invariant_error(lambda: _changed(record, valid_until_ns=record.decision_time_ns), "EVD-8")
    # ... while the closing revisions of an observation may be created at or after the expiry.
    for state in (EvidenceState.EXPIRED, EvidenceState.INVALIDATED):
        closed = _closed(state, valid_until_ns=T0)
        assert closed.valid_until_ns < closed.decision_time_ns


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-21")
def test_observable_time_watermark_and_decision_time_are_ordered() -> None:
    record = _active(observable_time_ns=T0 - 2 * NS_PER_MS, as_of_time_ns=T0 - NS_PER_MS, decision_time_ns=T0)
    assert record.observable_time_ns <= record.as_of_time_ns <= record.decision_time_ns
    assert record.decision_time_ns - record.observable_time_ns == 2 * NS_PER_MS  # notification delay
    same_instant = _active(observable_time_ns=T0, as_of_time_ns=T0, decision_time_ns=T0)
    assert same_instant.decision_time_ns - same_instant.observable_time_ns == 0
    _invariant_error(lambda: _changed(same_instant, observable_time_ns=T0 + 1), "EVD-11")
    _invariant_error(lambda: _changed(same_instant, as_of_time_ns=T0 + 1, observable_time_ns=T0), "CAUSAL-1")
    for field in (
        "observable_time_ns",
        "as_of_time_ns",
        "decision_time_ns",
        "anchor_time_ns",
        "valid_until_ns",
    ):
        _field_error(lambda field=field: _changed(record, **{field: -1}), field)
        _field_error(lambda field=field: _changed(record, **{field: float(T0)}), field)


# =========================================================================== inactive vs missing (S02-ACC-03)
@pytest.mark.requirement("S00-ASG-12", "S02-ACC-03")
def test_missing_evidence_has_null_scores_and_a_reason_and_is_never_zero() -> None:
    record = _missing()
    assert record.state is EvidenceState.MISSING
    assert record.bullish_score is None
    assert record.bearish_score is None
    assert record.missing_reason == "INSUFFICIENT_HISTORY"
    wire = to_wire(record)
    assert wire["bullish_score"] is None
    assert wire["bearish_score"] is None
    for scores in ((0.0, 0.0), (0, 0), (0.0, None), (None, 0.0), (-0.0, -0.0)):
        _invariant_error(
            lambda scores=scores: _changed(record, bullish_score=scores[0], bearish_score=scores[1]), "EVD-3"
        )
    _invariant_error(lambda: _changed(record, missing_reason=None), "EVD-4")
    _field_error(lambda: _changed(record, missing_reason="insufficient history"), "missing_reason")


@pytest.mark.requirement("S00-ASG-12", "S02-ACC-03")
def test_inactive_evidence_has_zero_scores_and_no_reason() -> None:
    record = _inactive()
    assert record.state is EvidenceState.INACTIVE
    assert (record.bullish_score, record.bearish_score) == (0.0, 0.0)
    assert record.bullish_score is not None
    assert record.missing_reason is None
    wire = to_wire(record)
    assert (wire["bullish_score"], wire["bearish_score"]) == (0, 0)
    assert wire["missing_reason"] is None
    _invariant_error(lambda: _changed(record, bullish_score=None, bearish_score=None), "EVD-3")
    _invariant_error(lambda: _changed(record, missing_reason="INSUFFICIENT_HISTORY"), "EVD-4")
    for scores in ((0.01, 0.0), (0.0, 0.01), (1.0, 1.0)):
        _invariant_error(
            lambda scores=scores: _changed(record, bullish_score=scores[0], bearish_score=scores[1]), "EVD-5"
        )


@pytest.mark.requirement("S00-ASG-12", "S02-ACC-03")
def test_a_known_no_and_an_unknown_cannot_be_converted_into_each_other_by_relabelling() -> None:
    inactive, missing = _inactive("orb_vwap"), _missing("orb_vwap")
    # Same engine and slot: same slot id, but the two records differ in scores and reason ...
    assert inactive.market_event_id == missing.market_event_id == _slot_id("orb_vwap")
    assert to_wire(inactive)["bullish_score"] != to_wire(missing)["bullish_score"]
    # ... and changing only the state label is refused in both directions, in memory and on the wire.
    _invariant_error(lambda: _changed(inactive, state=EvidenceState.MISSING), "EVD-3")
    _invariant_error(lambda: _changed(missing, state=EvidenceState.INACTIVE), "EVD-3")
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(missing), "bullish_score": 0, "bearish_score": 0}, Evidence)
    assert info.value.details["invariant"] == "EVD-3"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(inactive), "bullish_score": None, "bearish_score": None}, Evidence)
    assert info.value.details["invariant"] == "EVD-3"


@pytest.mark.requirement("S00-ASG-12", "S02-ACC-03")
def test_active_evidence_with_zero_scores_is_present_without_direction_and_differs_from_inactive() -> None:
    present = _active(
        "orb_vwap", kind="breakout", bullish_score=0.0, bearish_score=0.0, levels=(), invalidation=None
    )
    absent = _inactive("orb_vwap")
    assert present.state is EvidenceState.ACTIVE
    assert (present.bullish_score, present.bearish_score) == (0.0, 0.0)
    # "Present, no direction" is a market event; "absent" is an engine-scoped evaluation slot.
    assert present.market_event_id == _event_id("breakout", ())
    assert absent.market_event_id == _slot_id("orb_vwap")
    assert present.market_event_id != absent.market_event_id


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "S02-ACC-03")
def test_inactive_and_missing_carry_engine_scoped_slot_ids_that_never_coincide() -> None:
    slot_ids: set[str] = set()
    for engine in STRATEGY_ENGINES:
        inactive, missing = _inactive(engine), _missing(engine)
        assert inactive.market_event_id == missing.market_event_id == _slot_id(engine)
        assert inactive.evidence_id == identity.evidence_id(engine, _slot_id(engine), 0)
        slot_ids.add(inactive.market_event_id)
    # One slot per engine; none equals the market event id of the same kind, timeframe and anchor.
    assert len(slot_ids) == len(STRATEGY_ENGINES) == 6
    assert _event_id("breakout", ()) not in slot_ids
    assert _event_id("breakout", ("2651.20",)) not in slot_ids
    # The slot depends on kind, timeframe and anchor as well.
    assert _inactive(kind="fair_value_gap").market_event_id != _inactive().market_event_id
    assert _inactive(timeframe=Timeframe.M15).market_event_id != _inactive().market_event_id
    assert _inactive(anchor_time_ns=ANCHOR + 1).market_event_id != _inactive().market_event_id


# =========================================================================== market event identity (G02-02)
@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
def test_two_engines_reporting_the_same_event_obtain_the_same_market_event_id() -> None:
    first = _active("sweep_reversal")
    second = _active(
        "liquidity_hunter",
        bullish_score=0.4,
        bearish_score=0.1,
        stage=EvidenceStage.CONFIRMED,
        features=(NamedValue(name="pool_size", value=3.0),),
        invalidation=None,
        # Different non-identity levels, different order, different decimal spelling of the identity price.
        levels=(
            _level("WICK_LOW", "2647.95", is_identity=False),
            _level("SWEPT_LEVEL", "2648.4", is_identity=True),
            _level("ENTRY_HINT", "2649.10", is_identity=False),
        ),
        decision_time_ns=T0 + NS_PER_SECOND,
        as_of_time_ns=T0 + NS_PER_SECOND,
        valid_until_ns=T0 + 900 * NS_PER_SECOND,
    )
    assert first.market_event_id == second.market_event_id == _event_id()
    assert first.market_event_id == identity.market_event_id(
        "liquidity_sweep", "XAUUSD", "M5", ANCHOR, [SWEPT]
    )
    assert first.evidence_id != second.evidence_id


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
@pytest.mark.parametrize("kind", SHARED_EVIDENCE_KINDS)
def test_all_six_engines_share_one_id_for_a_shared_sweep_fvg_or_breakout_event(kind: str) -> None:
    levels = (
        _level("EVENT_HIGH", "2651.90", is_identity=True),
        _level("EVENT_LOW", "2650.10", is_identity=True),
    )
    records = [_active(engine, kind=kind, levels=levels, invalidation=None) for engine in STRATEGY_ENGINES]
    assert {record.market_event_id for record in records} == {_event_id(kind, ("2650.10", "2651.90"))}
    assert len({record.evidence_id for record in records}) == 6
    groups = group_by_market_event(records)
    assert len(groups) == 1
    assert [member.engine for member in groups[0].members] == sorted(STRATEGY_ENGINES)


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
def test_identity_level_order_and_roles_do_not_change_the_market_event_id() -> None:
    high, low = (
        _level("FVG_HIGH", "2651.90", is_identity=True),
        _level("FVG_LOW", "2650.10", is_identity=True),
    )
    forward = _active(kind="fair_value_gap", levels=(high, low))
    backward = _active("silver_bullet", kind="fair_value_gap", levels=(low, high))
    renamed = _active(
        "liquidity_hunter",
        kind="fair_value_gap",
        levels=(
            _level("GAP_TOP", "2651.90", is_identity=True),
            _level("GAP_BOTTOM", "2650.10", is_identity=True),
        ),
    )
    assert forward.market_event_id == backward.market_event_id == renamed.market_event_id
    assert forward.levels == backward.levels


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "fair_value_gap"}, id="kind"),
        pytest.param({"timeframe": Timeframe.M15}, id="timeframe"),
        pytest.param({"anchor_time_ns": ANCHOR + 1}, id="anchor_one_ns_later"),
        pytest.param(
            {"levels": (_level("SWEPT_LEVEL", "2648.400000000001", is_identity=True),)}, id="identity_price"
        ),
        pytest.param(
            {"levels": (_level("SWEPT_LEVEL", "2648.40", is_identity=False),)}, id="level_not_identity"
        ),
        pytest.param(
            {
                "levels": (
                    _level("SWEPT_LEVEL", "2648.40", is_identity=True),
                    _level("WICK_LOW", "2647.95", is_identity=True),
                )
            },
            id="additional_identity_level",
        ),
        pytest.param({"levels": ()}, id="no_levels"),
    ],
)
def test_a_different_event_obtains_a_different_market_event_id(overrides: dict[str, Any]) -> None:
    reference = _active()
    other = _active("liquidity_hunter", **overrides)
    assert other.market_event_id != reference.market_event_id
    assert len(group_by_market_event([reference, other])) == 2


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
def test_the_symbol_is_part_of_the_market_event_identity() -> None:
    shared = {
        "kind": "level_break",
        "timeframe": Timeframe.H1,
        "levels": (_level("LEVEL", "31.5", is_identity=True),),
    }
    silver = _intermarket(symbol="XAGUSD", **shared)
    gold = _intermarket(symbol="XAUUSD", **shared)
    assert silver.market_event_id != gold.market_event_id
    assert silver.market_event_id == identity.market_event_id(
        "level_break", "XAGUSD", "H1", ANCHOR, [D("31.5")]
    )


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22", "G02-02", "S02-ACC-02")
def test_market_event_id_does_not_depend_on_the_ambient_decimal_context() -> None:
    precise = (_level("SWEPT_LEVEL", "2648.400000000001", is_identity=True),)
    outside = _active(levels=precise)
    with decimal.localcontext(decimal.Context(prec=3, traps=[decimal.Inexact, decimal.Rounded])):
        inside = _active(levels=precise)
        coarse = _active(levels=(_level("SWEPT_LEVEL", "2648.40", is_identity=True),))
    assert inside == outside
    assert inside.market_event_id == outside.market_event_id
    # 2648.400000000001 and 2648.40 round to the same three digits; they are still different events.
    assert coarse.market_event_id != inside.market_event_id


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22")
def test_evidence_id_is_per_engine_event_and_revision_and_supersedes_links_are_verified() -> None:
    chain = [_active(revision=number, seq=number) for number in range(4)]
    assert len({record.evidence_id for record in chain}) == 4
    assert len({record.market_event_id for record in chain}) == 1
    assert chain[0].supersedes_evidence_id is None
    for previous, current in itertools.pairwise(chain):
        assert current.supersedes_evidence_id == previous.evidence_id
        assert current.evidence_id == identity.evidence_id("sweep_reversal", _event_id(), current.revision)
    for bad in ("evd_123", "mev_" + "0" * 32, "EVD_" + "0" * 32, "evd_" + "g" * 32, 5):
        _field_error(lambda bad=bad: _changed(chain[0], evidence_id=bad), "evidence_id")
    _field_error(lambda: _changed(chain[0], market_event_id=chain[0].evidence_id), "market_event_id")


# =========================================================================== group_by_market_event (G02-02)
def _dedup_fixture() -> dict[str, Evidence]:
    return {
        "sweep_by_sweep_reversal": _active(
            "sweep_reversal", decision_time_ns=T0 + NS_PER_SECOND, as_of_time_ns=T0
        ),
        "sweep_by_liquidity_hunter": _active("liquidity_hunter", bullish_score=0.4, features=()),
        "sweep_by_silver_bullet": _active("silver_bullet", bullish_score=0.9, seq=3),
        "breakout_by_cavemen": _active(
            "cavemen",
            kind="breakout",
            levels=(_level("BREAKOUT_LEVEL", "2651.20", is_identity=True),),
            invalidation=None,
        ),
        "no_breakout_by_orb_vwap": _inactive("orb_vwap"),
        "unknown_breakout_by_vwap_trend_rider": _missing("vwap_trend_rider"),
    }


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_group_by_market_event_forms_one_group_per_market_event() -> None:
    records = _dedup_fixture()
    groups = group_by_market_event(records.values())
    assert all(type(group) is EvidenceGroup for group in groups)
    assert [group.market_event_id for group in groups] == sorted(
        {r.market_event_id for r in records.values()}
    )
    assert len(groups) == 4
    by_id = {group.market_event_id: group for group in groups}
    sweep = by_id[_event_id()]
    assert [member.engine for member in sweep.members] == [
        "liquidity_hunter",
        "silver_bullet",
        "sweep_reversal",
    ]
    assert {member.market_event_id for member in sweep.members} == {sweep.market_event_id}
    assert [member.engine for member in by_id[_event_id("breakout", ("2651.20",))].members] == ["cavemen"]
    # Every input record is in exactly one group.
    grouped = [member for group in groups for member in group.members]
    assert sorted(member.evidence_id for member in grouped) == sorted(r.evidence_id for r in records.values())


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_the_representative_is_the_earliest_member_then_the_first_engine_name() -> None:
    records = _dedup_fixture()
    sweep = next(g for g in group_by_market_event(records.values()) if g.market_event_id == _event_id())
    # liquidity_hunter and silver_bullet decided at T0, sweep_reversal one second later.
    assert sweep.representative is records["sweep_by_liquidity_hunter"]
    assert sweep.representative is sweep.members[0]
    assert sweep.members[-1] is records["sweep_by_sweep_reversal"]
    earlier = _active("sweep_reversal", decision_time_ns=T0 - 1, as_of_time_ns=T0 - NS_PER_MS)
    regrouped = group_by_market_event([records["sweep_by_liquidity_hunter"], earlier])
    assert regrouped[0].representative is earlier


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_group_by_market_event_does_not_depend_on_the_input_order() -> None:
    records = list(_dedup_fixture().values())
    reference = group_by_market_event(records)
    permutations = list(itertools.permutations(records))
    assert len(permutations) == 720
    for order in permutations:
        assert group_by_market_event(order) == reference
    assert group_by_market_event(iter(reversed(records))) == reference


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_an_event_counts_once_and_member_scores_are_never_combined() -> None:
    records = _dedup_fixture()
    before = {name: to_json(record) for name, record in records.items()}
    sweep = next(g for g in group_by_market_event(list(records.values())) if g.market_event_id == _event_id())
    assert [field.name for field in dataclasses.fields(EvidenceGroup)] == [
        "market_event_id",
        "representative",
        "members",
    ]
    assert [member.bullish_score for member in sweep.members] == [0.4, 0.9, 0.7]
    assert sweep.representative.bullish_score == 0.4
    # The inputs are returned as they are: same objects, unchanged content.
    assert all(any(member is record for record in records.values()) for member in sweep.members)
    assert {name: to_json(record) for name, record in records.items()} == before


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02", "S02-ACC-03")
def test_inactive_and_missing_records_never_merge() -> None:
    records = [_inactive(engine) for engine in STRATEGY_ENGINES[:3]] + [
        _missing(engine) for engine in STRATEGY_ENGINES[3:]
    ]
    present = _active("cavemen", kind="breakout", bullish_score=0.0, levels=(), invalidation=None, seq=9)
    groups = group_by_market_event([*records, present])
    assert len(groups) == 7
    assert all(len(group.members) == 1 for group in groups)
    states = sorted(group.representative.state.value for group in groups)
    assert states == ["ACTIVE", "INACTIVE", "INACTIVE", "INACTIVE", "MISSING", "MISSING", "MISSING"]


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_two_records_of_one_engine_for_one_event_are_refused() -> None:
    first = _active()
    second_revision = _active(revision=1, seq=1, bullish_score=0.8)
    other_engine = _active("liquidity_hunter")
    for pair in ([first, second_revision], [second_revision, first], [first, first]):
        with pytest.raises(IllegalTransitionError) as info:
            group_by_market_event([other_engine, *pair])
        assert info.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
        assert info.value.details["market_event_id"] == first.market_event_id
        assert isinstance(info.value, GieError)
    # Two slot records of one engine (a known no and an unknown for the same slot) are refused as well.
    with pytest.raises(IllegalTransitionError):
        group_by_market_event([_inactive("orb_vwap"), _missing("orb_vwap", seq=1)])


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
@pytest.mark.parametrize(
    "intruder",
    [
        pytest.param(samples.candidate, id="candidate_record"),
        pytest.param(samples.tick, id="tick_record"),
        pytest.param(lambda: to_wire(samples.evidence()), id="wire_dict"),
        pytest.param(lambda: samples.evidence().market_event_id, id="bare_market_event_id"),
        pytest.param(lambda: None, id="none"),
    ],
)
def test_group_by_market_event_refuses_input_that_is_not_an_evidence_record(intruder: Build) -> None:
    with pytest.raises(TypeError, match="Evidence"):
        group_by_market_event([_active(), intruder()])  # type: ignore[list-item]


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_group_by_market_event_refuses_a_subclass_posing_as_evidence() -> None:
    class LookAlike(Evidence):  # type: ignore[misc]  # only definable below "tests."; never registered
        __slots__ = ()

    genuine = _active()
    fields = {field.name: getattr(genuine, field.name) for field in dataclasses.fields(genuine)}
    with pytest.raises(TypeError, match="Evidence"):
        group_by_market_event([LookAlike(**fields)])


@pytest.mark.requirement("S00-ASG-12", "G02-02", "S02-ACC-02")
def test_group_by_market_event_of_nothing_is_empty() -> None:
    assert group_by_market_event([]) == ()
    assert group_by_market_event(iter(())) == ()


# =========================================================================== validate_evidence_transition
def _revision(number: int = 1, **overrides: Any) -> Evidence:
    return _active(**{"revision": number, "seq": number, **overrides})


def _illegal(previous: Evidence | None, revision: Evidence) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as info:
        validate_evidence_transition(previous, revision)
    error = info.value
    assert error.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert error.details["evidence_id"] == revision.evidence_id
    assert error.details["revision"] == revision.revision
    assert isinstance(error.details["rule"], str)
    return error


@pytest.mark.requirement("S00-ASG-12", "G02-02")
def test_a_valid_chain_of_revisions_is_accepted() -> None:
    first = _active()
    second = _revision(
        1,
        stage=EvidenceStage.CONFIRMED,
        bullish_score=0.85,
        levels=(
            _level("SWEPT_LEVEL", "2648.40", is_identity=True),
            _level("WICK_LOW", "2647.95", is_identity=False),
        ),
        invalidation=_condition("2647.50"),
        features=(),
        valid_until_ns=T0 + 600 * NS_PER_SECOND,
        decision_time_ns=T0 + NS_PER_SECOND,
        as_of_time_ns=T0 + NS_PER_SECOND,
    )
    third = _revision(
        2,
        state=EvidenceState.CONTRA,
        stage=EvidenceStage.CONFIRMED,  # the stage never goes back (rework 2, section 11)
        contradicts_market_event_ids=(OTHER_EVENT,),
        decision_time_ns=T0 + 2 * NS_PER_SECOND,
        as_of_time_ns=T0 + 2 * NS_PER_SECOND,
    )
    fourth = _revision(
        3,
        state=EvidenceState.INVALIDATED,
        stage=EvidenceStage.COMPLETED,
        bullish_score=0.0,
        decision_time_ns=T0 + 2 * NS_PER_SECOND,
        as_of_time_ns=T0 + 2 * NS_PER_SECOND,
    )
    validate_evidence_transition(None, first)
    validate_evidence_transition(first, second)
    validate_evidence_transition(second, third)
    validate_evidence_transition(third, fourth)
    assert len({record.market_event_id for record in (first, second, third, fourth)}) == 1


@pytest.mark.requirement("S00-ASG-12")
def test_the_sample_chain_and_a_slot_chain_are_accepted() -> None:
    variants = samples.variants()["Evidence"]
    validate_evidence_transition(variants["active_bullish"], variants["invalidated_revision"])
    slot = [
        _inactive(),
        _missing("orb_vwap", revision=1, seq=1, missing_reason="FEED_GAP"),
        _inactive(revision=2, seq=2),
    ]
    validate_evidence_transition(None, slot[0])
    validate_evidence_transition(slot[0], slot[1])
    validate_evidence_transition(slot[1], slot[2])


@pytest.mark.requirement("S00-ASG-12")
def test_the_first_record_of_an_observation_has_revision_0() -> None:
    assert _illegal(None, _revision(1)).details["rule"] == "the first record of an observation has revision 0"
    _illegal(None, _revision(5))


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("final", [EvidenceState.INVALIDATED, EvidenceState.EXPIRED])
@pytest.mark.parametrize(
    "following", [EvidenceState.ACTIVE, EvidenceState.INVALIDATED, EvidenceState.EXPIRED]
)
def test_invalidated_and_expired_are_final(final: EvidenceState, following: EvidenceState) -> None:
    closed = _closed(final)
    scores = {} if following is EvidenceState.ACTIVE else {"bullish_score": 0.0}
    later = {"decision_time_ns": T0 + 60 * NS_PER_SECOND, "as_of_time_ns": T0 + 60 * NS_PER_SECOND}
    error = _illegal(closed, _revision(2, state=following, **scores, **later))
    assert error.details["rule"] == f"no revision may follow state {final.value}"


@pytest.mark.requirement("S00-ASG-12")
def test_the_revision_number_increases_by_exactly_one() -> None:
    first = _active()
    for wrong in (first, _revision(2), _revision(3)):
        assert _illegal(first, wrong).details["rule"] == "revision must increase by exactly 1"
    assert _illegal(_revision(2), _revision(1)).details["rule"] == "revision must increase by exactly 1"


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-22")
def test_a_revision_must_supersede_exactly_the_previous_record() -> None:
    first = _active()
    rule = "supersedes_evidence_id must name the previous revision"
    assert _illegal(first, _revision(1, anchor_time_ns=ANCHOR - 300 * NS_PER_SECOND)).details["rule"] == rule
    assert _illegal(first, _active("liquidity_hunter", revision=1, seq=1)).details["rule"] == rule


@pytest.mark.requirement("S00-ASG-12", "G02-02")
@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("run_id", {"run_id": identity.run_id("REPLAY", "1" * 64)}),
        ("mode", {"mode": RunMode.PAPER}),
        ("producer", {"producer": "strategy_intelligence.sweep_reversal_b"}),
        ("producer_class", {"producer_class": ProducerClass.PATTERN}),
        ("related_market_event_id", {"related_market_event_id": OTHER_EVENT}),
    ],
)
def test_identity_fields_may_not_change_between_revisions(field: str, overrides: dict[str, Any]) -> None:
    error = _illegal(_active(), _revision(1, **overrides))
    assert error.details["rule"] == f"{field} may not change between revisions"


@pytest.mark.requirement("S00-ASG-12", "G02-02")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "fair_value_gap"}, id="kind"),
        pytest.param({"timeframe": Timeframe.M15}, id="timeframe"),
        pytest.param({"anchor_time_ns": ANCHOR + 1}, id="anchor_time_ns"),
        pytest.param({**_engine("liquidity_hunter")}, id="engine"),
        pytest.param(
            {"levels": (_level("SWEPT_LEVEL", "2648.41", is_identity=True),)}, id="identity_level_price"
        ),
        pytest.param({"levels": ()}, id="identity_level_removed"),
        pytest.param(
            {
                "levels": (
                    _level("SWEPT_LEVEL", "2648.40", is_identity=True),
                    _level("WICK_LOW", "2647.95", is_identity=True),
                )
            },
            id="identity_level_added",
        ),
    ],
)
def test_the_market_event_of_an_observation_never_changes(overrides: dict[str, Any]) -> None:
    first = _active()
    changed = _revision(1, **overrides)
    assert changed.market_event_id != first.market_event_id or changed.engine != first.engine
    _illegal(first, changed)


@pytest.mark.requirement("S00-ASG-12", "G02-02")
@pytest.mark.parametrize(
    "levels",
    [
        pytest.param((_level("OTHER_ROLE", "2648.40", is_identity=True),), id="role_renamed"),
        pytest.param(
            (
                _level("SWEPT_LEVEL", "2648.40", is_identity=True),
                _level("SECOND_ROLE", "2648.40", is_identity=True),
            ),
            id="second_identity_level_at_the_same_price",
        ),
    ],
)
def test_identity_levels_are_immutable_even_when_the_event_id_stays_the_same(
    levels: tuple[PriceLevel, ...],
) -> None:
    first = _active()
    changed = _revision(1, levels=levels)
    assert changed.market_event_id == first.market_event_id  # the id only covers the prices
    assert _illegal(first, changed).details["rule"] == "identity levels may not change between revisions"


@pytest.mark.requirement("S00-ASG-12", "G02-02")
def test_non_identity_levels_may_change_between_revisions() -> None:
    first = _active(
        levels=(
            _level("SWEPT_LEVEL", "2648.40", is_identity=True),
            _level("WICK_LOW", "2647.95", is_identity=False),
        )
    )
    moved = _revision(
        1,
        levels=(
            _level("SWEPT_LEVEL", "2648.4", is_identity=True),
            _level("WICK_LOW", "2647.80", is_identity=False),
        ),
    )
    dropped = _revision(2, levels=(_level("SWEPT_LEVEL", "2648.40", is_identity=True),))
    validate_evidence_transition(first, moved)
    validate_evidence_transition(moved, dropped)


@pytest.mark.requirement("S00-ASG-12", "S02-ACC-03")
def test_an_evaluation_slot_chain_and_a_market_event_chain_never_continue_each_other() -> None:
    quiet = _inactive("cavemen")
    unknown = _missing("cavemen")
    event = _active("cavemen", kind="breakout", levels=(), invalidation=None)
    _illegal(quiet, _active("cavemen", kind="breakout", levels=(), invalidation=None, revision=1, seq=1))
    _illegal(unknown, _active("cavemen", kind="breakout", levels=(), invalidation=None, revision=1, seq=1))
    _illegal(event, _inactive("cavemen", revision=1, seq=1))
    _illegal(event, _missing("cavemen", revision=1, seq=1))


@pytest.mark.requirement("S00-ASG-12", "S00-ASG-21")
def test_times_and_sequence_numbers_do_not_decrease_along_a_chain() -> None:
    first = _active(seq=4)
    same_instant = _revision(1, seq=5)
    validate_evidence_transition(first, same_instant)
    earlier_decision = _revision(1, seq=5, decision_time_ns=T0 - 1, as_of_time_ns=T0 - NS_PER_MS)
    assert _illegal(first, earlier_decision).details["rule"] == "decision_time_ns may not decrease"
    earlier_watermark = _revision(1, seq=5, as_of_time_ns=T0 - NS_PER_MS - 1)
    assert _illegal(first, earlier_watermark).details["rule"] == "as_of_time_ns may not decrease"
    assert _illegal(first, _revision(1, seq=3)).details["rule"] == "seq may not decrease"


#: Documented order of validate_evidence_transition (change notice section 11):
#: NONE < ANTICIPATED < FORMING < ARMED < TRIGGERED < CONFIRMED < COMPLETED.
STAGES_IN_ORDER = (
    EvidenceStage.NONE,
    EvidenceStage.ANTICIPATED,
    EvidenceStage.FORMING,
    EvidenceStage.ARMED,
    EvidenceStage.TRIGGERED,
    EvidenceStage.CONFIRMED,
    EvidenceStage.COMPLETED,
)


@pytest.mark.requirement("S00-ASG-12")
def test_the_stage_vocabulary_is_the_documented_progression() -> None:
    assert tuple(EvidenceStage) == STAGES_IN_ORDER


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(
    ("before", "after"),
    list(itertools.product(STAGES_IN_ORDER, repeat=2)),
    ids=lambda stage: stage.value,
)
def test_the_stage_never_goes_back(before: EvidenceStage, after: EvidenceStage) -> None:
    previous = _active(stage=before)
    revision = _revision(1, stage=after)
    if STAGES_IN_ORDER.index(after) >= STAGES_IN_ORDER.index(before):
        validate_evidence_transition(previous, revision)
    else:
        error = _illegal(previous, revision)
        assert error.details["rule"] == (
            f"stage may not go back ({before.value} -> {after.value}); a setup that falls back is "
            "INVALIDATED and a new observation starts"
        )


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("state", [EvidenceState.CONTRA, EvidenceState.INVALIDATED, EvidenceState.EXPIRED])
def test_the_stage_does_not_go_back_in_a_contra_or_closing_revision_either(state: EvidenceState) -> None:
    previous = _active(stage=EvidenceStage.CONFIRMED)
    fields: dict[str, Any] = {"state": state}
    if state is EvidenceState.CONTRA:
        fields["contradicts_market_event_ids"] = (OTHER_EVENT,)
    else:
        fields.update(bullish_score=0.0, bearish_score=0.0)
    validate_evidence_transition(previous, _revision(1, stage=EvidenceStage.CONFIRMED, **fields))
    validate_evidence_transition(previous, _revision(1, stage=EvidenceStage.COMPLETED, **fields))
    error = _illegal(previous, _revision(1, stage=EvidenceStage.TRIGGERED, **fields))
    assert "stage may not go back (CONFIRMED -> TRIGGERED)" in error.details["rule"]


@pytest.mark.requirement("S00-ASG-12")
def test_an_illegal_transition_error_is_journalable_data() -> None:
    error = _illegal(_active(), _revision(2))
    payload = error.to_dict()
    assert payload["code"] == "GIE.STATE.ILLEGAL_TRANSITION"
    assert payload["details"] == dict(error.details)
    assert "revision must increase by exactly 1" in str(payload["message"])


# =========================================================================== derive_origin
ORIGINS_BEST_FIRST = [DataOrigin.BROKER_FEED, DataOrigin.HISTORICAL, DataOrigin.SYNTHETIC_FIXTURE]


@pytest.mark.requirement("S00-ASG-12")
def test_data_origin_vocabulary_is_ordered_from_most_to_least_trustworthy() -> None:
    assert list(DataOrigin) == ORIGINS_BEST_FIRST
    for origin in DataOrigin:
        assert derive_origin(origin) is origin
        assert derive_origin(origin, origin, origin) is origin


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize(("first", "second"), list(itertools.product(ORIGINS_BEST_FIRST, repeat=2)))
def test_derive_origin_returns_the_least_trustworthy_input(first: DataOrigin, second: DataOrigin) -> None:
    expected = ORIGINS_BEST_FIRST[max(ORIGINS_BEST_FIRST.index(first), ORIGINS_BEST_FIRST.index(second))]
    assert derive_origin(first, second) is expected
    assert derive_origin(second, first) is expected
    assert derive_origin(first, second, DataOrigin.BROKER_FEED) is expected
    assert derive_origin(first, DataOrigin.SYNTHETIC_FIXTURE, second) is DataOrigin.SYNTHETIC_FIXTURE


@pytest.mark.requirement("S00-ASG-12")
def test_derive_origin_needs_at_least_one_origin() -> None:
    with pytest.raises(ValueError, match="at least one origin"):
        derive_origin()


@pytest.mark.requirement("S00-ASG-12")
@pytest.mark.parametrize("unknown", ["LIVE", "synthetic_fixture", None, 0, ProducerClass.PATTERN])
def test_derive_origin_refuses_unknown_vocabulary(unknown: object) -> None:
    with pytest.raises((KeyError, TypeError, GieError)):
        derive_origin(DataOrigin.BROKER_FEED, unknown)  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-12")
def test_a_look_alike_origin_string_can_never_enter_a_record() -> None:
    # Whatever a caller derives from plain strings, a record only accepts genuine members.
    for look_alike in ("SYNTHETIC_FIXTURE", "BROKER_FEED", "HISTORICAL"):
        error = _field_error(
            lambda look_alike=look_alike: _changed(_active(), data_origin=look_alike), "data_origin"
        )
        assert error.details["rule"] == "must be a DataOrigin member"
    derived = derive_origin(DataOrigin.HISTORICAL, DataOrigin.SYNTHETIC_FIXTURE)
    assert _changed(_active(), data_origin=derived).data_origin is DataOrigin.SYNTHETIC_FIXTURE
