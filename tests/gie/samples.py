"""SYNTHETIC FIXTURES - explicitly labelled test data (requirement COM-07).

Every record built here carries ``DataOrigin.SYNTHETIC_FIXTURE``.  The prices,
sizes, estimates, approvals and names are invented for testing the contracts
only.  They are not market data, not broker data, not owner decisions and not
evidence of any trading performance.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal as D
from typing import Any

from gie.contracts import (
    Candidate,
    CandidateStatus,
    ClockMode,
    ClockReading,
    ContractRef,
    Contribution,
    DailyState,
    DataOrigin,
    Destination,
    EntryStyle,
    Evidence,
    EvidenceStage,
    EvidenceState,
    ExecutionVenue,
    Horizon,
    MarketSession,
    ModelKind,
    ModelStatus,
    ModelVersion,
    NamedValue,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderOriginator,
    OrderState,
    OrderType,
    PlanStatus,
    Position,
    PositionOwner,
    PositionState,
    PriceBasis,
    PriceCondition,
    PriceLevel,
    PriceTrigger,
    ProducerClass,
    QuantilePoint,
    Record,
    RiskDecision,
    RiskOutcome,
    RiskRegime,
    RunManifest,
    RunMode,
    RunModel,
    RunRole,
    Side,
    Target,
    Tick,
    TickFlag,
    Timeframe,
    TradePlan,
    derive_run_id,
    fingerprint,
    identity,
)
from gie.core.canonical import fraction_of
from gie.core.params import load_research_parameters
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND, trading_day

FIXTURE_LABEL = "SYNTHETIC FIXTURE - not market data"

#: 2026-11-26 09:30:00 UTC (10:30 in Prague).  Arbitrary instant used by all samples.
T0 = 1_795_685_400 * NS_PER_SECOND
ORIGIN = DataOrigin.SYNTHETIC_FIXTURE
CONFIG_SHA = "0" * 64
RUN_LABEL = "fixture"
CODE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
ARTIFACT_SHA = "ab" * 32
MODEL_NAME = "fixture_net_r"
MODEL_ID = identity.model_version_id(MODEL_NAME, "0.1.0", ARTIFACT_SHA)
SHADOW_MODEL_ID = identity.model_version_id("fixture_shadow", "0.2.0", "cd" * 32)


def _pinned(model_version_id: str, status: ModelStatus, **overrides: Any) -> RunModel:
    fields: dict[str, Any] = {
        "model_version_id": model_version_id,
        "status": status,
        "validated": False,  # fixture models rest on fixture data and are never validated
        "valid_from_ns": None,
        "valid_until_ns": None,
    }
    fields.update(overrides)
    return RunModel(**fields)


def _manifest_fields() -> dict[str, Any]:
    return {
        "producer": "app.run",
        "seq": 0,
        "mode": RunMode.REPLAY,
        "role": RunRole.RESEARCH,
        "risk_regime": RiskRegime.CHALLENGE_RULES,
        "label": RUN_LABEL,
        "config_sha256": CONFIG_SHA,
        "code_commit": CODE_COMMIT,
        "code_tree_dirty": False,
        "contract_set_version": "1.0.0",
        "parameters_sha256": load_research_parameters().parameters_sha256,
        "data_origin": ORIGIN,
        "data_sources": ("fixture",),
        "data_start_ns": T0 - 3600 * NS_PER_SECOND,
        "data_end_ns": T0 + 3600 * NS_PER_SECOND,
        "seed": 7,
        "models": (
            _pinned(MODEL_ID, ModelStatus.RESEARCH),
            _pinned(SHADOW_MODEL_ID, ModelStatus.SHADOW),
        ),
        "shadow_of_run_id": None,
        "created_time_ns": None,
    }


#: Identity of the sample run: derived from the whole sample manifest (see ``run_manifest``).
RUN_ID = derive_run_id(**_manifest_fields())
#: Not an owner decision: a fixture reference used only to exercise the approval rules.
FIXTURE_APPROVER = "FIXTURE-OWNER"
FIXTURE_DECISION = "D-999"


def _header(producer: str, seq: int = 0, *, t: int = T0) -> dict[str, Any]:
    return {
        "producer": producer,
        "seq": seq,
        "run_id": RUN_ID,
        "mode": RunMode.REPLAY,
        "data_origin": ORIGIN,
        "decision_time_ns": t,
        "as_of_time_ns": t - NS_PER_MS,
    }


def run_manifest(**overrides: Any) -> RunManifest:
    """The manifest of the sample run; ``run_id`` is derived from the other fields unless given."""
    fields = _manifest_fields()
    fields.update(overrides)
    if "run_id" not in fields:
        return RunManifest.create(**fields)
    return RunManifest(**fields)


def tick(**overrides: Any) -> Tick:
    fields: dict[str, Any] = {
        "symbol": "XAUUSD",
        "source": "fixture",
        "origin": ORIGIN,
        "seq": 0,
        "event_time_ns": T0 - 3 * NS_PER_MS,
        "receive_time_ns": T0 - NS_PER_MS,
        "bid": D("2650.10"),
        "ask": D("2650.35"),
        "bid_volume": None,
        "ask_volume": None,
        "flags": (TickFlag.RECEIVE_TIME_MODELLED,),
    }
    fields.update(overrides)
    return Tick(**fields)


def model_version(**overrides: Any) -> ModelVersion:
    fields: dict[str, Any] = {
        "producer": "learning.registry",
        "seq": 0,
        "revision": 0,
        "model_name": MODEL_NAME,
        "semver": "0.1.0",
        "kind": ModelKind.NET_R_MODEL,
        "status": ModelStatus.RESEARCH,
        "status_reasons": (),
        "status_time_ns": T0 - 3600 * NS_PER_SECOND,
        "created_time_ns": T0 - 7200 * NS_PER_SECOND,
        "artifact_sha256": ARTIFACT_SHA,
        "artifact_uri": "models/research/fixture_net_r/0.1.0/model.bin",
        "code_commit": CODE_COMMIT,
        "training_data_origin": ORIGIN,
        "training_data_sha256": "ef" * 32,
        "training_config_sha256": "12" * 32,
        "training_data_start_ns": T0 - 90 * 86400 * NS_PER_SECOND,
        "training_information_end_ns": T0 - 30 * 86400 * NS_PER_SECOND,
        "embargo_ns": 86400 * NS_PER_SECOND,
        "split_spec_sha256": "34" * 32,
        "feature_schema_sha256": "cd" * 32,
        "contract_versions": (
            ContractRef(contract="Tick", version="1.0.0"),
            ContractRef(contract="Evidence", version="1.0.0"),
        ),
        "derived_from_model_version_id": None,
        "component_model_version_ids": (),
        "seed": 7,
        "metrics": (NamedValue(name="oos_mean_net_r", value=0.0),),
        "evaluation_report_uri": None,
        "evaluation_report_sha256": None,
        "evaluation_data_origin": None,
        "promotion_criteria_sha256": None,
        "shadow_run_ids": (),
        "replaces_model_version_id": None,
        "rollback_of_model_version_id": None,
        "approved_by": None,
        "approval_reference": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "model_version_id",
        identity.model_version_id(fields["model_name"], fields["semver"], fields["artifact_sha256"]),
    )
    return ModelVersion(**fields)


def evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        **_header("strategy_intelligence.sweep_reversal"),
        "related_market_event_id": None,
        "producer_class": ProducerClass.STRATEGY_ENGINE,
        "engine": "sweep_reversal",
        "kind": "liquidity_sweep",
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M5,
        "anchor_time_ns": T0 - 300 * NS_PER_SECOND,
        "state": EvidenceState.ACTIVE,
        "stage": EvidenceStage.TRIGGERED,
        "bullish_score": 0.7,
        "bearish_score": 0.0,
        "levels": (PriceLevel(role="SWEPT_LEVEL", price=D("2648.40"), identity=True),),
        "invalidation": PriceCondition(
            price=D("2647.90"), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.BID, timeframe=None
        ),
        "valid_until_ns": T0 + 300 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_evidence_id": None,
        "contradicts_market_event_ids": (),
        "missing_reason": None,
        "features": (NamedValue(name="sweep_depth", value=0.6),),
    }
    fields.update(overrides)
    fields.setdefault("observable_time_ns", fields["as_of_time_ns"])
    no_event = fields["state"] in (EvidenceState.INACTIVE, EvidenceState.MISSING)
    if "market_event_id" not in fields:
        if no_event:
            fields["market_event_id"] = identity.evaluation_slot_id(
                fields["engine"],
                fields["kind"],
                fields["symbol"],
                fields["timeframe"].value,
                fields["anchor_time_ns"],
            )
        else:
            fields["market_event_id"] = identity.market_event_id(
                fields["kind"],
                fields["symbol"],
                fields["timeframe"].value,
                fields["anchor_time_ns"],
                [level.price for level in fields["levels"] if level.identity],
            )
    fields.setdefault(
        "evidence_id",
        identity.evidence_id(fields["engine"], fields["market_event_id"], fields["revision"]),
    )
    if fields["revision"] > 0 and fields.get("supersedes_evidence_id") is None:
        fields["supersedes_evidence_id"] = identity.evidence_id(
            fields["engine"], fields["market_event_id"], fields["revision"] - 1
        )
    return Evidence(**fields)


def candidate(**overrides: Any) -> Candidate:
    base = evidence()
    fields: dict[str, Any] = {
        **_header("prediction.scorer"),
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "setup_type": "sweep_reversal_long",
        "horizon": Horizon.SCALP,
        "session": MarketSession.LONDON,
        "market_event_ids": (base.market_event_id,),
        "evidence_ids": (base.evidence_id,),
        "reference_price": D("2650.35"),
        "status": CandidateStatus.AVAILABLE,
        "status_reasons": (),
        "model_version_id": MODEL_ID,
        "model_status": ModelStatus.RESEARCH,
        "unscored_reason": None,
        "risk_unit_price": D("2.00"),
        "expected_net_r": 0.25,
        "net_r_stdev": 1.1,
        "expected_net_r_stderr": 0.12,
        "support_count": 84,
        "net_r_quantiles": (
            QuantilePoint(q_frac=0.1, value_r=-1.0),
            QuantilePoint(q_frac=0.5, value_r=0.2),
            QuantilePoint(q_frac=0.9, value_r=2.0),
        ),
        "win_probability_frac": 0.48,
        "expected_win_r": 1.5,
        "expected_loss_r": 1.0,
        "expected_cost_r": 0.08,
        "expected_holding_time_ns": 900 * NS_PER_SECOND,
        "quality_score": 0.6,
        "memory_cutoff_time_ns": T0 - 86400 * NS_PER_SECOND,
        "destinations": (
            Destination(
                scenario="reversal",
                label="prior_high",
                price=D("2655.00"),
                probability_frac=0.4,
                horizon_ns=1800 * NS_PER_SECOND,
            ),
        ),
        "baseline_r": 0.05,
        "contributions": (
            Contribution(evidence_id=base.evidence_id, feature=None, weight_r=0.15),
            Contribution(evidence_id=None, feature="session_london", weight_r=0.05),
        ),
        "valid_until_ns": T0 + 120 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_candidate_id": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "opportunity_id",
        identity.opportunity_id(
            fields["setup_type"], fields["side"].value, fields["horizon"].value, fields["market_event_ids"]
        ),
    )
    fields.setdefault(
        "candidate_id",
        identity.candidate_id(fields["opportunity_id"], fields["model_version_id"], fields["revision"]),
    )
    if fields["revision"] > 0 and fields.get("supersedes_candidate_id") is None:
        fields["supersedes_candidate_id"] = identity.candidate_id(
            fields["opportunity_id"], fields["model_version_id"], fields["revision"] - 1
        )
    return Candidate(**fields)


def unscored_candidate(**overrides: Any) -> Candidate:
    fields: dict[str, Any] = {
        "side": Side.SELL,
        "setup_type": "sweep_reversal_short",
        "reference_price": D("2650.10"),
        "model_version_id": None,
        "model_status": None,
        "unscored_reason": "NO_MODEL_AVAILABLE",
        "risk_unit_price": None,
        "expected_net_r": None,
        "net_r_stdev": None,
        "expected_net_r_stderr": None,
        "support_count": None,
        "net_r_quantiles": (),
        "win_probability_frac": None,
        "expected_win_r": None,
        "expected_loss_r": None,
        "expected_cost_r": None,
        "expected_holding_time_ns": None,
        "quality_score": None,
        "memory_cutoff_time_ns": None,
        "destinations": (),
        "baseline_r": None,
        "contributions": (),
    }
    fields.update(overrides)
    return candidate(**fields)


def trade_plan(**overrides: Any) -> TradePlan:
    source = candidate()
    fields: dict[str, Any] = {
        **_header("decision.planner"),
        "candidate_id": source.candidate_id,
        "opportunity_id": source.opportunity_id,
        "variant": 0,
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "horizon": Horizon.SCALP,
        "entry_style": EntryStyle.LIMIT,
        "entry_order_type": OrderType.LIMIT,
        "entry_price": D("2649.80"),
        "reference_price": D("2650.35"),
        "entry_trigger": None,
        "invalidations": (
            PriceCondition(
                price=D("2648.00"), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.BID, timeframe=None
            ),
        ),
        "stop_price": D("2647.80"),
        "targets": (
            Target(label="first", price=D("2652.80"), close_frac=D("0.5")),
            # the candidate's predicted destination "reversal / prior_high"
            Target(label="prior_high", scenario="reversal", price=D("2655.00"), close_frac=D("0.5")),
        ),
        "expected_net_r": 0.25,
        "net_r_stdev": 1.1,
        "expected_net_r_stderr": 0.12,
        "support_count": 84,
        "expected_cost_r": 0.08,
        "model_version_id": MODEL_ID,
        "model_status": ModelStatus.RESEARCH,
        "max_spread": D("0.40"),
        "max_entry_slippage": D("0.10"),
        "overnight_allowed": False,
        "status": PlanStatus.SELECTED,
        "status_reasons": (),
        "baseline_r": 0.05,
        "contributions": (Contribution(evidence_id=evidence().evidence_id, feature=None, weight_r=0.20),),
        "valid_until_ns": T0 + 120 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_plan_id": None,
    }
    fields.update(overrides)
    fields.setdefault("alternative_group_id", identity.alternative_group_id(fields["opportunity_id"]))
    fields.setdefault(
        "plan_id",
        identity.plan_id(
            fields["candidate_id"], fields["entry_style"].value, fields["variant"], fields["revision"]
        ),
    )
    if fields["revision"] > 0 and fields.get("supersedes_plan_id") is None:
        fields["supersedes_plan_id"] = identity.plan_id(
            fields["candidate_id"],
            fields["entry_style"].value,
            fields["variant"],
            fields["revision"] - 1,
        )
    return TradePlan(**fields)


def risk_decision(plan: TradePlan | None = None, **overrides: Any) -> RiskDecision:
    plan = trade_plan() if plan is None else plan
    fields: dict[str, Any] = {
        **_header("risk.gate"),
        "plan_id": plan.plan_id,
        "plan_fingerprint": fingerprint(plan),
        "alternative_group_id": plan.alternative_group_id,
        "attempt": 0,
        "outcome": RiskOutcome.APPROVED,
        "reason_codes": (),
        "volume_lots": D("0.50"),
        "risk_amount": D("104.00"),
        "reference_equity_amount": D("100000.00"),
        "quality_validated": False,
        "quality_model_version_id": None,
        "account_currency": "USD",
        "daily_state": DailyState.NORMAL,
        "daily_pnl_amount": D("400.00"),
        "profit_floor_amount": None,
        "valid_until_ns": T0 + 30 * NS_PER_SECOND,
        "equity_amount": D("100400.00"),
        "open_risk_amount": D("0"),
        "pending_risk_amount": D("0"),
        "margin_required_amount": D("1325.18"),
        "margin_free_amount": D("100400.00"),
        "gap_stress_loss_amount": D("310.00"),
    }
    fields.update(overrides)
    # The fractions are derived from the amounts (RSK-9) unless a test supplies them.
    equity = fields["reference_equity_amount"]
    fields.setdefault("risk_frac", fraction_of(fields["risk_amount"], equity, round_up=True))
    fields.setdefault("daily_pnl_frac", fraction_of(fields["daily_pnl_amount"], equity, round_up=False))
    floor = fields["profit_floor_amount"]
    fields.setdefault(
        "profit_floor_frac", None if floor is None else fraction_of(floor, equity, round_up=True)
    )
    fields.setdefault("trading_day", trading_day(fields["decision_time_ns"]))
    fields.setdefault(
        "decision_id",
        identity.risk_decision_id(fields["run_id"], fields["plan_id"], fields["attempt"]),
    )
    if "reservation_id" not in fields:
        fields["reservation_id"] = (
            None
            if fields["outcome"] is RiskOutcome.REJECTED
            else identity.reservation_id(fields["decision_id"])
        )
    return RiskDecision(**fields)


def rejected_risk_decision(plan: TradePlan | None = None, **overrides: Any) -> RiskDecision:
    fields: dict[str, Any] = {
        "outcome": RiskOutcome.REJECTED,
        "reason_codes": ("FIXTURE_REJECTION",),
        "volume_lots": D("0"),
        "risk_amount": D("0"),
        "margin_required_amount": None,
        "gap_stress_loss_amount": None,
    }
    fields.update(overrides)
    return risk_decision(plan, **fields)


def order_event(**overrides: Any) -> OrderEvent:
    plan = trade_plan()
    decision = risk_decision(plan)
    fields: dict[str, Any] = {
        "producer": "execution.sim",
        "seq": 0,
        "run_id": RUN_ID,
        "mode": RunMode.REPLAY,
        "data_origin": ORIGIN,
        "originator": OrderOriginator.EXECUTION,
        "attempt": 0,
        "order_seq": 0,
        "event_type": OrderEventType.SUBMIT_REQUESTED,
        "order_state": OrderState.PENDING_SUBMIT,
        "event_time_ns": T0 + NS_PER_MS,
        "receive_time_ns": T0 + NS_PER_MS,
        "venue": ExecutionVenue.SIMULATED,
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "intent": OrderIntent.ENTRY,
        "volume_lots": D("0.50"),
        "limit_price": D("2649.80"),
        "trigger_price": None,
        "reference_price": D("2650.35"),
        "attached_stop_price": D("2647.80"),
        "attached_take_profit_price": None,
        "valid_until_ns": T0 + 30 * NS_PER_SECOND,
        "requested_volume_lots": None,
        "requested_limit_price": None,
        "requested_trigger_price": None,
        "requested_valid_until_ns": None,
        "plan_id": plan.plan_id,
        "risk_decision_id": decision.decision_id,
        "alternative_group_id": plan.alternative_group_id,
        "position_id": None,
        "broker_order_id": None,
        "cumulative_filled_lots": D("0"),
        "last_fill_lots": None,
        "last_fill_price": None,
        "average_fill_price": None,
        "last_fill_cost_amount": None,
        "fill_tick_source": None,
        "fill_tick_seq": None,
        "reason_code": None,
    }
    fields.update(overrides)
    # Only an entry carries the plan's slippage limit.
    fields.setdefault("max_slippage", D("0.10") if fields["intent"] is OrderIntent.ENTRY else None)
    if "client_order_id" not in fields:
        origin = (
            fields["risk_decision_id"] if fields["intent"] is OrderIntent.ENTRY else fields["position_id"]
        )
        fields["client_order_id"] = identity.client_order_id(
            fields["run_id"], origin, fields["intent"].value, fields["attempt"]
        )
    fields.setdefault(
        "order_event_id", identity.order_event_id(fields["client_order_id"], fields["order_seq"])
    )
    return OrderEvent(**fields)


def position(**overrides: Any) -> Position:
    entry = order_event()
    fields: dict[str, Any] = {
        **_header("execution.sim", t=T0 + 5 * NS_PER_SECOND),
        "revision": 0,
        "owner": PositionOwner.GIE,
        "external_ref": None,
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "state": PositionState.OPEN,
        "volume_lots": D("0.50"),
        "initial_volume_lots": D("0.50"),
        "average_entry_price": D("2649.80"),
        "initial_stop_price": D("2647.80"),
        "stop_price": D("2647.80"),
        "take_profit_price": None,
        "mark_price": D("2650.60"),
        "initial_risk_amount": D("104.00"),
        "realized_net_pnl_amount": D("-2.00"),
        "unrealized_net_pnl_amount": D("38.00"),
        "peak_net_pnl_amount": D("36.00"),
        "trough_net_pnl_amount": D("-14.50"),
        "costs_amount": D("2.00"),
        "swap_amount": D("0"),
        "account_currency": "USD",
        "opened_time_ns": T0 + 2 * NS_PER_SECOND,
        "closed_time_ns": None,
        "close_reason": None,
        "plan_id": entry.plan_id,
        "opening_client_order_id": entry.client_order_id,
    }
    fields.update(overrides)
    fields.setdefault("peak_time_ns", fields["as_of_time_ns"])
    if "position_id" not in fields:
        fields["position_id"] = identity.position_id(fields["run_id"], fields["opening_client_order_id"])
    # An open GIE position names its working protective order (attempt 0 for the position).
    protected = fields["owner"] is PositionOwner.GIE and fields["state"] is PositionState.OPEN
    fields.setdefault(
        "protective_client_order_id",
        identity.client_order_id(fields["run_id"], fields["position_id"], "PROTECTIVE_STOP", 0)
        if protected
        else None,
    )
    fields.setdefault("unprotected_reason", None)
    return Position(**fields)


def external_position(**overrides: Any) -> Position:
    fields: dict[str, Any] = {
        "owner": PositionOwner.EXTERNAL,
        "external_ref": "FIXTURE-TICKET-1",
        "position_id": identity.external_position_id("fixture-account", "FIXTURE-TICKET-1"),
        "symbol": "EURUSD",
        "side": Side.SELL,
        "average_entry_price": D("1.0850"),
        "initial_stop_price": None,
        "stop_price": None,
        "mark_price": D("1.0842"),
        "initial_risk_amount": None,
        "plan_id": None,
        "opening_client_order_id": None,
        "realized_net_pnl_amount": D("0"),
        "unrealized_net_pnl_amount": D("40.00"),
        "peak_net_pnl_amount": D("55.00"),
        "trough_net_pnl_amount": D("-5.00"),
        "costs_amount": D("0"),
    }
    fields.update(overrides)
    return position(**fields)


def clock_reading(**overrides: Any) -> ClockReading:
    fields: dict[str, Any] = {
        "producer": "core.clock",
        "seq": 0,
        "run_id": RUN_ID,
        "mode": RunMode.REPLAY,
        "clock_mode": ClockMode.REPLAY,
        "wall_time_ns": T0,
        "monotonic_ns": 0,
        "regressions_observed": 0,
    }
    fields.update(overrides)
    fields.setdefault("trading_day", trading_day(fields["wall_time_ns"]))
    return ClockReading(**fields)


#: One valid sample builder per contract name.
BUILDERS = {
    "RunManifest": run_manifest,
    "Tick": tick,
    "Evidence": evidence,
    "Candidate": candidate,
    "TradePlan": trade_plan,
    "RiskDecision": risk_decision,
    "OrderEvent": order_event,
    "Position": position,
    "ModelVersion": model_version,
    "Clock": clock_reading,
}


def all_samples() -> dict[str, Record]:
    return {name: build() for name, build in BUILDERS.items()}


# --------------------------------------------------------------------------- lifecycle variants
def order_lifecycle() -> list[OrderEvent]:
    """A complete, valid life of one entry order: submit -> accept -> partial fill -> fill."""
    submit = order_event()
    common: dict[str, Any] = {"broker_order_id": identity.broker_order_id(submit.client_order_id)}
    accepted = order_event(
        **common,
        seq=1,
        order_seq=1,
        event_type=OrderEventType.ACCEPTED,
        order_state=OrderState.WORKING,
        event_time_ns=T0 + 2 * NS_PER_MS,
        receive_time_ns=T0 + 3 * NS_PER_MS,
    )
    partial = order_event(
        **common,
        seq=2,
        order_seq=2,
        event_type=OrderEventType.PARTIALLY_FILLED,
        order_state=OrderState.PARTIALLY_FILLED,
        event_time_ns=T0 + NS_PER_SECOND,
        receive_time_ns=T0 + NS_PER_SECOND + NS_PER_MS,
        cumulative_filled_lots=D("0.20"),
        last_fill_lots=D("0.20"),
        last_fill_price=D("2649.80"),
        average_fill_price=D("2649.80"),
        last_fill_cost_amount=D("0.80"),
        fill_tick_source="fixture",
        fill_tick_seq=1,
    )
    filled = order_event(
        **common,
        seq=3,
        order_seq=3,
        event_type=OrderEventType.FILLED,
        order_state=OrderState.FILLED,
        event_time_ns=T0 + 2 * NS_PER_SECOND,
        receive_time_ns=T0 + 2 * NS_PER_SECOND + NS_PER_MS,
        cumulative_filled_lots=D("0.50"),
        last_fill_lots=D("0.30"),
        last_fill_price=D("2649.78"),
        average_fill_price=D("2649.788"),
        last_fill_cost_amount=D("1.20"),
        fill_tick_source="fixture",
        fill_tick_seq=2,
    )
    return [submit, accepted, partial, filled]


def protective_stop_lifecycle() -> list[OrderEvent]:
    """The stop order of the sample position: created by the simulated broker when the entry
    filled, working, then tightened by a replacement the execution engine asked for."""
    held = position()
    base: dict[str, Any] = {
        "originator": OrderOriginator.BROKER,
        "side": Side.SELL,
        "order_type": OrderType.STOP,
        "intent": OrderIntent.PROTECTIVE_STOP,
        "limit_price": None,
        "trigger_price": D("2647.80"),
        "reference_price": None,
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": held.position_id,
        "reason_code": "ENTRY_FILLED",
    }
    t = T0 + 2 * NS_PER_SECOND + NS_PER_MS  # the instant the FILLED report of the entry was observed
    submit = order_event(**base, seq=4, event_time_ns=t, receive_time_ns=t)
    broker = identity.broker_order_id(submit.client_order_id)
    working = order_event(
        **{**base, "reason_code": "ENTRY_FILLED"},
        seq=5,
        order_seq=1,
        event_type=OrderEventType.ACCEPTED,
        order_state=OrderState.WORKING,
        broker_order_id=broker,
        event_time_ns=t,
        receive_time_ns=t + NS_PER_MS,
    )
    request = order_event(
        **{**base, "reason_code": "RATCHET"},
        seq=6,
        order_seq=2,
        event_type=OrderEventType.REPLACE_REQUESTED,
        order_state=OrderState.PENDING_REPLACE,
        broker_order_id=broker,
        requested_trigger_price=D("2649.90"),
        event_time_ns=T0 + 60 * NS_PER_SECOND,
        receive_time_ns=T0 + 60 * NS_PER_SECOND,
    )
    replaced = order_event(
        **{**base, "reason_code": "RATCHET", "trigger_price": D("2649.90")},
        seq=7,
        order_seq=3,
        event_type=OrderEventType.REPLACED,
        order_state=OrderState.WORKING,
        broker_order_id=broker,
        event_time_ns=T0 + 60 * NS_PER_SECOND,
        receive_time_ns=T0 + 60 * NS_PER_SECOND + NS_PER_MS,
    )
    return [submit, working, request, replaced]


def position_lifecycle() -> list[Position]:
    """Open -> stop tightened and partial close -> closed.  Stop only tightens, peak only rises."""
    opened = position()
    t1 = T0 + 60 * NS_PER_SECOND
    tightened = position(
        revision=1,
        seq=1,
        decision_time_ns=t1,
        as_of_time_ns=t1 - NS_PER_MS,
        volume_lots=D("0.25"),
        stop_price=D("2649.90"),
        mark_price=D("2652.80"),
        realized_net_pnl_amount=D("72.00"),
        unrealized_net_pnl_amount=D("74.00"),
        peak_net_pnl_amount=D("146.00"),
        peak_time_ns=t1 - NS_PER_MS,
        costs_amount=D("3.00"),
    )
    t2 = T0 + 300 * NS_PER_SECOND
    closed = position(
        revision=2,
        seq=2,
        decision_time_ns=t2,
        as_of_time_ns=t2 - NS_PER_MS,
        state=PositionState.CLOSED,
        volume_lots=D("0"),
        stop_price=D("2649.90"),
        mark_price=None,
        realized_net_pnl_amount=D("73.50"),
        unrealized_net_pnl_amount=D("0"),
        peak_net_pnl_amount=D("146.00"),
        peak_time_ns=t1 - NS_PER_MS,
        costs_amount=D("4.00"),
        closed_time_ns=T0 + 299 * NS_PER_SECOND,
        close_reason="PROTECTIVE_STOP",
    )
    return [opened, tightened, closed]


def model_lifecycle() -> list[ModelVersion]:
    """RESEARCH -> SHADOW -> ACTIVE.  The approval is a FIXTURE value, not an owner decision."""
    research = model_version()
    shadow = model_version(
        seq=1, revision=1, status=ModelStatus.SHADOW, status_time_ns=T0 - 1800 * NS_PER_SECOND
    )
    active = model_version(
        seq=2,
        revision=2,
        status=ModelStatus.ACTIVE,
        status_time_ns=T0 - 600 * NS_PER_SECOND,
        evaluation_report_uri="models/research/fixture_net_r/0.1.0/model_card.md",
        evaluation_report_sha256="56" * 32,
        evaluation_data_origin=ORIGIN,
        promotion_criteria_sha256="78" * 32,
        shadow_run_ids=(RUN_ID,),
        approved_by=FIXTURE_APPROVER,
        approval_reference=FIXTURE_DECISION,
    )
    return [research, shadow, active]


def run_story() -> list[Record]:
    """A small complete run in journal order - a coherent trace, not independent samples.

    SYNTHETIC FIXTURE.  One opportunity is planned, approved and entered with a
    limit order that fills in two parts; the simulated broker creates the
    attached protective stop at the first fill and lets its volume follow the
    second; the position is valued, half of it is closed at a target, the stop
    is tightened and reduced, and the stop finally closes the rest.  Every
    record can be published to a journal in this order (``MemoryJournal``), so
    the trace exercises the chain, reference and aggregate rules of the run
    ledger together.
    """
    ms = NS_PER_MS
    sec = NS_PER_SECOND

    def quote(seq: int, receive_ns: int, bid: str, ask: str) -> Tick:
        return tick(
            seq=seq, event_time_ns=receive_ns - 2 * ms, receive_time_ns=receive_ns, bid=D(bid), ask=D(ask)
        )

    plan = trade_plan()
    decision = risk_decision(plan)
    submit, accepted, partial, filled = order_lifecycle()
    entry_id = submit.client_order_id
    position_id = identity.position_id(RUN_ID, entry_id)

    def order(seq: int, event: OrderEvent) -> OrderEvent:
        return dataclasses.replace(event, seq=seq)

    stop: dict[str, Any] = {
        "originator": OrderOriginator.BROKER,
        "side": Side.SELL,
        "order_type": OrderType.STOP,
        "intent": OrderIntent.PROTECTIVE_STOP,
        "limit_price": None,
        "trigger_price": D("2647.80"),
        "reference_price": None,
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": position_id,
        "volume_lots": D("0.20"),
        "reason_code": "ENTRY_FILLED",
    }
    t_first = T0 + sec + ms  # first fill observed
    stop_submit = order_event(**stop, seq=3, event_time_ns=t_first, receive_time_ns=t_first)
    broker_stop = identity.broker_order_id(stop_submit.client_order_id)
    stop_accepted = order_event(
        **stop,
        seq=4,
        order_seq=1,
        event_type=OrderEventType.ACCEPTED,
        order_state=OrderState.WORKING,
        broker_order_id=broker_stop,
        event_time_ns=t_first,
        receive_time_ns=t_first + ms,
    )
    t_second = T0 + 2 * sec + ms  # second fill observed
    stop_grown = order_event(
        **{**stop, "volume_lots": D("0.50")},
        seq=6,
        order_seq=2,
        event_type=OrderEventType.RECONCILED,
        order_state=OrderState.WORKING,
        broker_order_id=broker_stop,
        event_time_ns=t_second,
        receive_time_ns=t_second,
    )

    held: dict[str, Any] = {
        "opened_time_ns": T0 + sec,
        "protective_client_order_id": stop_submit.client_order_id,
    }
    first = position(
        **held,
        revision=0,
        seq=0,
        decision_time_ns=t_first + ms,
        as_of_time_ns=T0 + sec,
        volume_lots=D("0.20"),
        initial_volume_lots=D("0.20"),
        average_entry_price=D("2649.80"),
        initial_risk_amount=D("41.60"),
        mark_price=D("2649.55"),
        realized_net_pnl_amount=D("-0.80"),
        unrealized_net_pnl_amount=D("-5.00"),
        peak_net_pnl_amount=D("-5.80"),
        peak_time_ns=T0 + sec,
        trough_net_pnl_amount=D("-5.80"),
        costs_amount=D("0.80"),
    )
    full = position(
        **held,
        revision=1,
        seq=1,
        decision_time_ns=t_second + ms,
        as_of_time_ns=T0 + 2 * sec,
        average_entry_price=D("2649.788"),
        mark_price=D("2649.54"),
        realized_net_pnl_amount=D("-2.00"),
        unrealized_net_pnl_amount=D("-12.50"),
        peak_net_pnl_amount=D("-5.80"),
        peak_time_ns=T0 + sec,
        trough_net_pnl_amount=D("-14.50"),
    )
    valued = position(**held, revision=2, seq=2, average_entry_price=D("2649.788"))

    t_target = T0 + 60 * sec
    exit_order: dict[str, Any] = {
        "side": Side.SELL,
        "order_type": OrderType.MARKET,
        "intent": OrderIntent.EXIT,
        "limit_price": None,
        "reference_price": D("2652.80"),
        "attached_stop_price": None,
        "valid_until_ns": None,
        "plan_id": None,
        "risk_decision_id": None,
        "alternative_group_id": None,
        "position_id": position_id,
        "volume_lots": D("0.25"),
        "reason_code": "TARGET_REACHED",
    }
    exit_submit = order_event(**exit_order, seq=7, event_time_ns=t_target, receive_time_ns=t_target)
    exit_filled = order_event(
        **exit_order,
        seq=8,
        order_seq=1,
        event_type=OrderEventType.FILLED,
        order_state=OrderState.FILLED,
        broker_order_id=identity.broker_order_id(exit_submit.client_order_id),
        event_time_ns=t_target,
        receive_time_ns=t_target + ms,
        cumulative_filled_lots=D("0.25"),
        last_fill_lots=D("0.25"),
        last_fill_price=D("2652.80"),
        average_fill_price=D("2652.80"),
        last_fill_cost_amount=D("1.00"),
        fill_tick_source="fixture",
        fill_tick_seq=4,
    )
    reduced = position(
        **held,
        revision=3,
        seq=3,
        decision_time_ns=t_target + 2 * ms,
        as_of_time_ns=t_target - ms,
        average_entry_price=D("2649.788"),
        volume_lots=D("0.25"),
        stop_price=D("2649.90"),
        mark_price=D("2652.80"),
        realized_net_pnl_amount=D("72.00"),
        unrealized_net_pnl_amount=D("74.00"),
        peak_net_pnl_amount=D("146.00"),
        peak_time_ns=t_target - ms,
        costs_amount=D("3.00"),
    )
    tightened: dict[str, Any] = {**stop, "volume_lots": D("0.50"), "broker_order_id": broker_stop}
    stop_request = order_event(
        **{**tightened, "reason_code": "RATCHET"},
        seq=9,
        order_seq=3,
        event_type=OrderEventType.REPLACE_REQUESTED,
        order_state=OrderState.PENDING_REPLACE,
        requested_trigger_price=D("2649.90"),
        requested_volume_lots=D("0.25"),
        event_time_ns=t_target + 2 * ms,
        receive_time_ns=t_target + 2 * ms,
    )
    stop_replaced = order_event(
        **{**tightened, "reason_code": "RATCHET", "trigger_price": D("2649.90"), "volume_lots": D("0.25")},
        seq=10,
        order_seq=4,
        event_type=OrderEventType.REPLACED,
        order_state=OrderState.WORKING,
        event_time_ns=t_target + 2 * ms,
        receive_time_ns=t_target + 3 * ms,
    )
    t_stop = T0 + 299 * sec
    stop_filled = order_event(
        **{
            **tightened,
            "reason_code": "STOP_TRIGGERED",
            "trigger_price": D("2649.90"),
            "volume_lots": D("0.25"),
        },
        seq=11,
        order_seq=5,
        event_type=OrderEventType.FILLED,
        order_state=OrderState.FILLED,
        event_time_ns=t_stop,
        receive_time_ns=t_stop + ms,
        cumulative_filled_lots=D("0.25"),
        last_fill_lots=D("0.25"),
        last_fill_price=D("2649.85"),
        average_fill_price=D("2649.85"),
        last_fill_cost_amount=D("1.00"),
        fill_tick_source="fixture",
        fill_tick_seq=5,
    )
    closed = position(
        opened_time_ns=T0 + sec,
        revision=4,
        seq=4,
        decision_time_ns=t_stop + 2 * ms,
        as_of_time_ns=t_stop,
        average_entry_price=D("2649.788"),
        state=PositionState.CLOSED,
        volume_lots=D("0"),
        stop_price=D("2649.90"),
        mark_price=None,
        realized_net_pnl_amount=D("73.50"),
        unrealized_net_pnl_amount=D("0"),
        peak_net_pnl_amount=D("146.00"),
        peak_time_ns=t_target - ms,
        costs_amount=D("4.00"),
        closed_time_ns=t_stop,
        close_reason="PROTECTIVE_STOP",
    )
    return [
        run_manifest(),
        quote(0, T0 - ms, "2650.10", "2650.35"),
        evidence(),
        candidate(),
        plan,
        decision,
        order(0, submit),
        order(1, accepted),
        quote(1, T0 + sec, "2649.55", "2649.80"),
        order(2, partial),
        stop_submit,
        stop_accepted,
        first,
        quote(2, T0 + 2 * sec, "2649.54", "2649.78"),
        order(5, filled),
        stop_grown,
        full,
        quote(3, T0 + 5 * sec - ms, "2650.60", "2650.85"),
        valued,
        quote(4, t_target - ms, "2652.80", "2653.05"),
        exit_submit,
        exit_filled,
        reduced,
        stop_request,
        stop_replaced,
        quote(5, t_stop, "2649.85", "2650.10"),
        stop_filled,
        closed,
    ]


def variants() -> dict[str, dict[str, Record]]:
    """Named, valid sample records per contract covering the main shapes of each contract."""
    base_evidence = evidence()
    breakout = evidence(
        engine="cavemen",
        producer="strategy_intelligence.cavemen",
        kind="breakout",
        levels=(PriceLevel(role="BREAKOUT_LEVEL", price=D("2651.20"), identity=True),),
        invalidation=None,
        features=(),
    )
    orders = order_lifecycle()
    stops = protective_stop_lifecycle()
    positions = position_lifecycle()
    models = model_lifecycle()
    plan = trade_plan()
    return {
        "RunManifest": {
            "replay_research": run_manifest(),
            "paper_primary": run_manifest(
                mode=RunMode.PAPER,
                role=RunRole.PRIMARY,
                label="fixture-paper",
                data_start_ns=None,
                data_end_ns=None,
                created_time_ns=T0 + 86_400 * NS_PER_SECOND,
                seed=None,
                # a primary run uses owner-approved models only (here: the fixture approval)
                models=(_pinned(MODEL_ID, ModelStatus.ACTIVE),),
            ),
            "replay_shadow": run_manifest(
                role=RunRole.SHADOW, label="fixture-shadow", shadow_of_run_id=RUN_ID
            ),
        },
        "Tick": {
            "typical": tick(),
            "measured_receive_time": tick(seq=1, flags=(), bid_volume=D("3.5"), ask_volume=D("1")),
            "crossed": tick(
                seq=2,
                bid=D("2650.40"),
                ask=D("2650.30"),
                flags=(TickFlag.CROSSED, TickFlag.RECEIVE_TIME_MODELLED),
            ),
            "event_after_receive": tick(
                seq=3,
                event_time_ns=T0,
                receive_time_ns=T0 - NS_PER_MS,
                flags=(TickFlag.EVENT_AFTER_RECEIVE,),
            ),
            "observed_silver": tick(symbol="XAGUSD", bid=D("31.115"), ask=D("31.14")),
        },
        "Evidence": {
            "active_bullish": base_evidence,
            "active_non_directional": evidence(
                engine="orb_vwap",
                producer="strategy_intelligence.orb_vwap",
                kind="opening_range",
                stage=EvidenceStage.FORMING,
                bullish_score=0.0,
                bearish_score=0.0,
                levels=(
                    PriceLevel(role="RANGE_HIGH", price=D("2651.90"), identity=True),
                    PriceLevel(role="RANGE_LOW", price=D("2648.10"), identity=True),
                ),
                invalidation=None,
                features=(NamedValue(name="range_minutes", value=15.0),),
            ),
            "inactive": evidence(
                engine="orb_vwap",
                producer="strategy_intelligence.orb_vwap",
                kind="breakout",
                state=EvidenceState.INACTIVE,
                stage=EvidenceStage.NONE,
                bullish_score=0.0,
                bearish_score=0.0,
                levels=(),
                invalidation=None,
                features=(),
            ),
            "missing": evidence(
                engine="vwap_trend_rider",
                producer="strategy_intelligence.vwap_trend_rider",
                kind="trend_state",
                state=EvidenceState.MISSING,
                stage=EvidenceStage.NONE,
                bullish_score=None,
                bearish_score=None,
                levels=(),
                invalidation=None,
                missing_reason="INSUFFICIENT_HISTORY",
                features=(NamedValue(name="bars_available", value=None),),
            ),
            "contra": evidence(
                engine="liquidity_hunter",
                producer="strategy_intelligence.liquidity_hunter",
                kind="rejection",
                state=EvidenceState.CONTRA,
                stage=EvidenceStage.TRIGGERED,
                bullish_score=0.0,
                bearish_score=0.6,
                levels=(PriceLevel(role="REJECTION_HIGH", price=D("2650.90"), identity=True),),
                invalidation=None,
                contradicts_market_event_ids=(base_evidence.market_event_id,),
            ),
            "invalidated_revision": evidence(
                revision=1,
                seq=1,
                state=EvidenceState.INVALIDATED,
                stage=EvidenceStage.COMPLETED,
                bullish_score=0.0,
                bearish_score=0.0,
                decision_time_ns=T0 + 30 * NS_PER_SECOND,
                as_of_time_ns=T0 + 30 * NS_PER_SECOND,
            ),
            "same_event_other_engine": evidence(
                engine="liquidity_hunter",
                producer="strategy_intelligence.liquidity_hunter",
                bullish_score=0.4,
                features=(),
            ),
            "cavemen_breakout": breakout,
            "cavemen_pending_limit": evidence(
                engine="cavemen",
                producer="strategy_intelligence.cavemen",
                seq=1,
                kind="pending_limit",
                stage=EvidenceStage.ARMED,
                bullish_score=0.55,
                related_market_event_id=breakout.market_event_id,
                levels=(PriceLevel(role="PENDING_LIMIT", price=D("2650.90"), identity=True),),
                invalidation=PriceCondition(
                    price=D("2649.50"),
                    trigger=PriceTrigger.BELOW,
                    basis=PriceBasis.CANDLE_CLOSE,
                    timeframe=Timeframe.M5,
                ),
                features=(),
            ),
            "intermarket_real_yield": evidence(
                producer_class=ProducerClass.INTERMARKET,
                engine="real_yield_lead",
                producer="intermarket.lead_lag",
                kind="lead_lag_signal",
                symbol="US10Y_REAL",
                timeframe=Timeframe.NONE,
                stage=EvidenceStage.NONE,
                bullish_score=0.3,
                bearish_score=0.0,
                levels=(PriceLevel(role="OBSERVED_VALUE", price=D("-0.35"), identity=False),),
                invalidation=None,
                features=(NamedValue(name="lag_seconds", value=45.0),),
            ),
        },
        "Candidate": {
            "scored": candidate(),
            "unscored": unscored_candidate(),
            "scored_by_shadow_model": candidate(
                model_version_id=SHADOW_MODEL_ID,
                model_status=ModelStatus.SHADOW,
                expected_net_r=1.0,
                net_r_stdev=None,
                expected_net_r_stderr=None,
                support_count=None,
                contributions=(),
                baseline_r=None,
            ),
            "rejected_revision": candidate(
                revision=1,
                seq=1,
                status=CandidateStatus.REJECTED,
                status_reasons=("NEGATIVE_EXPECTED_NET_R",),
                expected_net_r=-0.15,
                decision_time_ns=T0 + NS_PER_SECOND,
                as_of_time_ns=T0 + NS_PER_SECOND,
            ),
        },
        "TradePlan": {
            "limit_buy_selected": plan,
            "market_sell_prepared": trade_plan(
                side=Side.SELL,
                entry_style=EntryStyle.MARKET,
                entry_order_type=OrderType.MARKET,
                entry_price=None,
                reference_price=D("2650.10"),
                invalidations=(),
                stop_price=D("2652.10"),
                targets=(Target(label="first", price=D("2646.10"), close_frac=D("0")),),
                horizon=Horizon.INTRADAY,
                overnight_allowed=True,
                max_spread=None,
                max_entry_slippage=None,
                status=PlanStatus.PREPARED,
                expected_net_r=-0.1,
            ),
            "pullback_by_market_order_armed": trade_plan(
                variant=1,
                entry_style=EntryStyle.PULLBACK,
                entry_order_type=OrderType.MARKET,
                entry_price=D("2648.20"),
                stop_price=D("2646.90"),
                targets=(Target(label="back_to_quote", price=D("2650.00"), close_frac=D("1")),),
                entry_trigger=PriceCondition(
                    price=D("2648.20"),
                    trigger=PriceTrigger.AT_OR_BELOW,
                    basis=PriceBasis.ASK,
                    timeframe=None,
                ),
                status=PlanStatus.ARMED,
                net_r_stdev=None,
            ),
            # Codec edge values, not a plausible estimate: a double beyond 2^63 and one between 2^53
            # and 2^63 whose shortest digits are not its exact integer value.
            "float_edge_values": trade_plan(
                variant=3,
                net_r_stdev=1e19,
                expected_net_r_stderr=float(1234567890123456768),
            ),
            "rejected_stale": trade_plan(
                variant=2,
                status=PlanStatus.REJECTED,
                status_reasons=("STALE_CANDIDATE",),
                valid_until_ns=T0,
            ),
        },
        "RiskDecision": {
            "approved": risk_decision(plan),
            "approved_reduced_defensive": risk_decision(
                plan,
                attempt=1,
                seq=1,
                outcome=RiskOutcome.APPROVED_REDUCED,
                reason_codes=("DEFENSIVE_STATE_REDUCTION",),
                volume_lots=D("0.25"),
                risk_amount=D("52.00"),
                daily_state=DailyState.DEFENSIVE,
                daily_pnl_amount=D("-2100.00"),
            ),
            "approved_within_surplus": risk_decision(
                plan,
                attempt=2,
                seq=2,
                daily_state=DailyState.PROFIT_PROTECTION,
                daily_pnl_amount=D("6200.00"),
                profit_floor_amount=D("5000.00"),
            ),
            "rejected_at_floor": rejected_risk_decision(
                plan,
                attempt=3,
                seq=3,
                reason_codes=("DAILY_PROFIT_FLOOR",),
                daily_state=DailyState.PROFIT_PROTECTION,
                daily_pnl_amount=D("5000.00"),
                profit_floor_amount=D("5000.00"),
            ),
            "rejected_halt": rejected_risk_decision(
                plan,
                attempt=4,
                seq=4,
                reason_codes=("EMERGENCY_HALT",),
                daily_state=DailyState.EMERGENCY_HALT,
                daily_pnl_amount=D("-3000.00"),
            ),
        },
        "OrderEvent": {
            "entry_submit_requested": orders[0],
            "entry_accepted": orders[1],
            "entry_partially_filled": orders[2],
            "entry_filled": orders[3],
            "entry_rejected": order_event(
                seq=1,
                order_seq=1,
                event_type=OrderEventType.REJECTED,
                order_state=OrderState.REJECTED,
                event_time_ns=T0 + 2 * NS_PER_MS,
                receive_time_ns=T0 + 3 * NS_PER_MS,
                reason_code="MARKET_CLOSED",
            ),
            "protective_stop_submit": stops[0],
            "protective_stop_replace_requested": stops[2],
            "protective_stop_replaced": stops[3],
        },
        "Position": {
            "open": positions[0],
            "tightened_partial": positions[1],
            "closed": positions[2],
            "external_other_bot": external_position(),
        },
        "ModelVersion": {
            "research": models[0],
            "shadow": models[1],
            "active_fixture_approval": models[2],
            "ruleset_baseline": model_version(
                model_name="fixture_ruleset",
                kind=ModelKind.RULESET,
                training_data_origin=None,
                training_data_sha256=None,
                training_config_sha256=None,
                training_data_start_ns=None,
                training_information_end_ns=None,
                embargo_ns=None,
                split_spec_sha256=None,
                feature_schema_sha256=None,
                seed=None,
                metrics=(),
            ),
        },
        "Clock": {
            "replay": clock_reading(),
            "paper_system": clock_reading(
                mode=RunMode.PAPER,
                clock_mode=ClockMode.SYSTEM,
                monotonic_ns=123_456_789,
                seq=7,
                regressions_observed=1,
            ),
        },
    }
