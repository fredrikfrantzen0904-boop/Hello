"""SYNTHETIC FIXTURES - explicitly labelled test data (requirement COM-07).

Every record built here carries ``DataOrigin.SYNTHETIC_FIXTURE``.  The prices,
sizes and estimates are invented for testing the contracts only.  They are not
market data, not broker data and not evidence of any trading performance.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from gie.contracts import (
    Candidate,
    CandidateStatus,
    ClockMode,
    ClockReading,
    ContractRef,
    DailyState,
    DataOrigin,
    Destination,
    EntryStyle,
    Evidence,
    EvidenceContribution,
    EvidenceStage,
    EvidenceState,
    ExecutionVenue,
    Horizon,
    Invalidation,
    MarketSession,
    ModelKind,
    ModelStatus,
    ModelVersion,
    NamedValue,
    OrderEvent,
    OrderEventType,
    OrderIntent,
    OrderState,
    OrderType,
    PlanStatus,
    Position,
    PositionOwner,
    PositionState,
    PriceBasis,
    PriceLevel,
    PriceTrigger,
    ProducerClass,
    QuantilePoint,
    Record,
    RiskDecision,
    RiskOutcome,
    RunMode,
    Side,
    Target,
    Tick,
    TickFlag,
    Timeframe,
    TradePlan,
    identity,
)
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND, trading_day

FIXTURE_LABEL = "SYNTHETIC FIXTURE - not market data"

#: 2026-11-26 09:30:00 UTC (10:30 in Prague).  Arbitrary instant used by all samples.
T0 = 1_795_685_400 * NS_PER_SECOND
ORIGIN = DataOrigin.SYNTHETIC_FIXTURE
RUN_ID = identity.run_id("REPLAY", "0" * 64, "fixture")
CODE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
ARTIFACT_SHA = "ab" * 32


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
        "flags": (),
    }
    fields.update(overrides)
    return Tick(**fields)


def model_version(**overrides: Any) -> ModelVersion:
    fields: dict[str, Any] = {
        "producer": "learning.registry",
        "seq": 0,
        "revision": 0,
        "model_name": "fixture_net_r",
        "semver": "0.1.0",
        "kind": ModelKind.NET_R_MODEL,
        "status": ModelStatus.RESEARCH,
        "status_time_ns": T0 - 3600 * NS_PER_SECOND,
        "created_time_ns": T0 - 7200 * NS_PER_SECOND,
        "artifact_sha256": ARTIFACT_SHA,
        "artifact_uri": "models/research/fixture_net_r/0.1.0/model.bin",
        "code_commit": CODE_COMMIT,
        "training_data_start_ns": T0 - 90 * 86400 * NS_PER_SECOND,
        "training_data_end_ns": T0 - 30 * 86400 * NS_PER_SECOND,
        "feature_schema_sha256": "cd" * 32,
        "contract_versions": (
            ContractRef(contract="Tick", version="1.0.0"),
            ContractRef(contract="Evidence", version="1.0.0"),
        ),
        "parent_model_version_id": None,
        "seed": 7,
        "metrics": (NamedValue(name="oos_mean_net_r", value=0.0),),
        "approved_by": None,
        "approval_reference": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "model_version_id",
        identity.model_version_id(fields["model_name"], fields["semver"], fields["artifact_sha256"]),
    )
    return ModelVersion(**fields)


MODEL_ID = identity.model_version_id("fixture_net_r", "0.1.0", ARTIFACT_SHA)


def evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        **_header("strategy_intelligence.sweep_reversal"),
        "producer_class": ProducerClass.STRATEGY_ENGINE,
        "engine": "sweep_reversal",
        "kind": "liquidity_sweep",
        "symbol": "XAUUSD",
        "timeframe": Timeframe.M5,
        "anchor_time_ns": T0 - 60 * NS_PER_SECOND,
        "state": EvidenceState.ACTIVE,
        "stage": EvidenceStage.TRIGGERED,
        "bullish_score": 0.7,
        "bearish_score": 0.0,
        "levels": (PriceLevel(role="SWEPT_LEVEL", price=D("2648.40")),),
        "invalidation": Invalidation(
            price=D("2647.90"), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.BID, timeframe=None
        ),
        "valid_until_ns": T0 + 300 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_evidence_id": None,
        "contradicts_evidence_id": None,
        "missing_reason": None,
        "features": (NamedValue(name="sweep_depth", value=0.6),),
    }
    fields.update(overrides)
    fields.setdefault(
        "market_event_id",
        identity.market_event_id(
            fields["kind"],
            fields["symbol"],
            fields["timeframe"].value,
            fields["anchor_time_ns"],
            [level.price for level in fields["levels"]],
        ),
    )
    fields.setdefault(
        "evidence_id",
        identity.evidence_id(fields["engine"], fields["market_event_id"], fields["revision"]),
    )
    return Evidence(**fields)


def candidate(**overrides: Any) -> Candidate:
    fields: dict[str, Any] = {
        **_header("prediction.scorer"),
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "setup_type": "sweep_reversal_long",
        "horizon": Horizon.SCALP,
        "session": MarketSession.LONDON,
        "evidence_ids": (evidence().evidence_id,),
        "reference_price": D("2650.35"),
        "status": CandidateStatus.AVAILABLE,
        "status_reasons": (),
        "model_version_id": MODEL_ID,
        "expected_net_r": 0.25,
        "net_r_stdev": 1.1,
        "net_r_quantiles": (
            QuantilePoint(q_frac=0.1, value_r=-1.0),
            QuantilePoint(q_frac=0.5, value_r=0.2),
            QuantilePoint(q_frac=0.9, value_r=2.0),
        ),
        "win_probability_frac": 0.48,
        "expected_cost_r": 0.08,
        "expected_holding_time_ns": 900 * NS_PER_SECOND,
        "quality_score": 0.6,
        "destinations": (
            Destination(
                label="prior_high", price=D("2655.00"), probability_frac=0.4, expected_time_to_reach_ns=None
            ),
        ),
        "valid_until_ns": T0 + 120 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_candidate_id": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "candidate_id",
        identity.candidate_id(
            fields["setup_type"], fields["side"].value, fields["evidence_ids"], fields["revision"]
        ),
    )
    return Candidate(**fields)


def trade_plan(**overrides: Any) -> TradePlan:
    fields: dict[str, Any] = {
        **_header("decision.planner"),
        "candidate_id": candidate().candidate_id,
        "variant": 0,
        "symbol": "XAUUSD",
        "side": Side.BUY,
        "horizon": Horizon.SCALP,
        "entry_style": EntryStyle.LIMIT,
        "entry_order_type": OrderType.LIMIT,
        "entry_price": D("2649.80"),
        "reference_price": D("2650.35"),
        "stop_price": D("2647.80"),
        "targets": (
            Target(label="first", price=D("2652.80"), close_frac=D("0.5")),
            Target(label="prior_high", price=D("2655.00"), close_frac=D("0.5")),
        ),
        "expected_net_r": 0.25,
        "net_r_stdev": 1.1,
        "expected_cost_r": 0.08,
        "model_version_id": MODEL_ID,
        "max_spread": D("0.40"),
        "max_entry_slippage": D("0.10"),
        "overnight_allowed": False,
        "status": PlanStatus.PREPARED,
        "status_reasons": (),
        "contributions": (EvidenceContribution(evidence_id=evidence().evidence_id, weight=0.25),),
        "valid_until_ns": T0 + 120 * NS_PER_SECOND,
        "revision": 0,
        "supersedes_plan_id": None,
    }
    fields.update(overrides)
    fields.setdefault("alternative_group_id", identity.alternative_group_id(fields["candidate_id"]))
    fields.setdefault(
        "plan_id",
        identity.plan_id(
            fields["candidate_id"], fields["entry_style"].value, fields["variant"], fields["revision"]
        ),
    )
    return TradePlan(**fields)


def risk_decision(**overrides: Any) -> RiskDecision:
    fields: dict[str, Any] = {
        **_header("risk.gate"),
        "plan_id": trade_plan().plan_id,
        "attempt": 0,
        "outcome": RiskOutcome.APPROVED,
        "reason_codes": (),
        "volume_lots": D("0.50"),
        "risk_amount": D("104.00"),
        "risk_frac": D("0.00104"),
        "quality_validated": False,
        "account_currency": "USD",
        "daily_state": DailyState.NORMAL,
        "daily_pnl_frac": D("0.004"),
        "profit_floor_frac": None,
        "valid_until_ns": T0 + 30 * NS_PER_SECOND,
        "equity_amount": D("100400.00"),
        "open_risk_amount": D("0"),
        "pending_risk_amount": D("0"),
        "margin_required_amount": D("1325.18"),
        "margin_free_amount": D("100400.00"),
        "gap_stress_loss_amount": D("310.00"),
    }
    fields.update(overrides)
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


def order_event(**overrides: Any) -> OrderEvent:
    plan = trade_plan()
    fields: dict[str, Any] = {
        "producer": "execution.sim",
        "seq": 0,
        "run_id": RUN_ID,
        "mode": RunMode.REPLAY,
        "data_origin": ORIGIN,
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
        "attached_stop_price": D("2647.80"),
        "attached_take_profit_price": None,
        "valid_until_ns": T0 + 120 * NS_PER_SECOND,
        "plan_id": plan.plan_id,
        "risk_decision_id": risk_decision().decision_id,
        "position_id": None,
        "broker_order_id": None,
        "cumulative_filled_lots": D("0"),
        "last_fill_lots": None,
        "last_fill_price": None,
        "average_fill_price": None,
        "last_fill_cost_amount": None,
        "reason_code": None,
    }
    fields.update(overrides)
    fields.setdefault(
        "client_order_id",
        identity.client_order_id(fields["run_id"], plan.plan_id, fields["intent"].value, 0),
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
        "costs_amount": D("2.00"),
        "swap_amount": D("0"),
        "account_currency": "USD",
        "opened_time_ns": T0 + 2 * NS_PER_SECOND,
        "closed_time_ns": None,
        "plan_id": entry.plan_id,
        "opening_client_order_id": entry.client_order_id,
    }
    fields.update(overrides)
    fields.setdefault("position_id", identity.position_id(fields["run_id"], entry.client_order_id))
    return Position(**fields)


def clock_reading(**overrides: Any) -> ClockReading:
    fields: dict[str, Any] = {
        "producer": "core.clock",
        "seq": 0,
        "run_id": RUN_ID,
        "mode": RunMode.REPLAY,
        "clock_mode": ClockMode.REPLAY,
        "wall_time_ns": T0,
        "monotonic_ns": 0,
    }
    fields.update(overrides)
    fields.setdefault("trading_day", trading_day(fields["wall_time_ns"]))
    return ClockReading(**fields)


#: One valid sample builder per contract name.
BUILDERS = {
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
    common: dict[str, Any] = {
        "client_order_id": submit.client_order_id,
        "broker_order_id": identity.broker_order_id(submit.client_order_id),
    }
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
    )
    return [submit, accepted, partial, filled]


def position_lifecycle() -> list[Position]:
    """Open -> stop tightened and partial close -> closed.  Stop only tightens, peak only rises."""
    opened = position()
    tightened = position(
        revision=1,
        seq=1,
        decision_time_ns=T0 + 60 * NS_PER_SECOND,
        as_of_time_ns=T0 + 60 * NS_PER_SECOND - NS_PER_MS,
        volume_lots=D("0.25"),
        stop_price=D("2649.90"),
        mark_price=D("2652.80"),
        realized_net_pnl_amount=D("72.00"),
        unrealized_net_pnl_amount=D("74.00"),
        peak_net_pnl_amount=D("146.00"),
        costs_amount=D("3.00"),
    )
    closed = position(
        revision=2,
        seq=2,
        decision_time_ns=T0 + 300 * NS_PER_SECOND,
        as_of_time_ns=T0 + 300 * NS_PER_SECOND - NS_PER_MS,
        state=PositionState.CLOSED,
        volume_lots=D("0"),
        stop_price=D("2649.90"),
        mark_price=None,
        realized_net_pnl_amount=D("73.50"),
        unrealized_net_pnl_amount=D("0"),
        peak_net_pnl_amount=D("146.00"),
        costs_amount=D("4.00"),
        closed_time_ns=T0 + 299 * NS_PER_SECOND,
    )
    return [opened, tightened, closed]


def model_lifecycle() -> list[ModelVersion]:
    """RESEARCH -> SHADOW -> ACTIVE (with a recorded owner approval - fixture values)."""
    research = model_version()
    shadow = model_version(
        seq=1, revision=1, status=ModelStatus.SHADOW, status_time_ns=T0 - 1800 * NS_PER_SECOND
    )
    active = model_version(
        seq=2,
        revision=2,
        status=ModelStatus.ACTIVE,
        status_time_ns=T0 - 600 * NS_PER_SECOND,
        approved_by="FIXTURE-OWNER",
        approval_reference="D-999",
    )
    return [research, shadow, active]


def variants() -> dict[str, dict[str, Record]]:
    """Named, valid sample records per contract covering the main shapes of each contract."""
    base_evidence = evidence()
    protective_position = position()
    orders = order_lifecycle()
    positions = position_lifecycle()
    models = model_lifecycle()
    halted_time = T0
    return {
        "Tick": {
            "typical": tick(),
            "with_volumes": tick(seq=1, bid_volume=D("3.5"), ask_volume=D("1")),
            "crossed": tick(seq=2, bid=D("2650.40"), ask=D("2650.30"), flags=(TickFlag.CROSSED,)),
            "event_after_receive": tick(
                seq=3,
                event_time_ns=T0,
                receive_time_ns=T0 - NS_PER_MS,
                flags=(TickFlag.STALE, TickFlag.EVENT_AFTER_RECEIVE),
            ),
            "observed_silver": tick(symbol="XAGUSD", bid=D("31.115"), ask=D("31.14")),
        },
        "Evidence": {
            "active_bullish": base_evidence,
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
                features=(),
            ),
            "contra": evidence(
                engine="liquidity_hunter",
                producer="strategy_intelligence.liquidity_hunter",
                state=EvidenceState.CONTRA,
                stage=EvidenceStage.TRIGGERED,
                bullish_score=0.0,
                bearish_score=0.6,
                contradicts_evidence_id=base_evidence.evidence_id,
            ),
            "invalidated_revision": evidence(
                revision=1,
                seq=1,
                supersedes_evidence_id=base_evidence.evidence_id,
                state=EvidenceState.INVALIDATED,
                stage=EvidenceStage.COMPLETED,
                bullish_score=0.0,
                bearish_score=0.0,
                decision_time_ns=T0 + 30 * NS_PER_SECOND,
                as_of_time_ns=T0 + 30 * NS_PER_SECOND,
            ),
            "cavemen_pending_limit": evidence(
                engine="cavemen",
                producer="strategy_intelligence.cavemen",
                kind="pending_limit",
                stage=EvidenceStage.ARMED,
                bullish_score=0.55,
                levels=(
                    PriceLevel(role="BREAKOUT_LEVEL", price=D("2651.20")),
                    PriceLevel(role="PENDING_LIMIT", price=D("2650.90")),
                ),
                invalidation=Invalidation(
                    price=D("2649.50"),
                    trigger=PriceTrigger.AT_OR_BELOW,
                    basis=PriceBasis.CANDLE_CLOSE,
                    timeframe=Timeframe.M5,
                ),
            ),
            "intermarket": evidence(
                producer_class=ProducerClass.INTERMARKET,
                engine="dxy_lead",
                producer="intermarket.lead_lag",
                kind="lead_lag_signal",
                symbol="DXY",
                timeframe=Timeframe.M1,
                stage=EvidenceStage.NONE,
                bullish_score=0.0,
                bearish_score=0.3,
                levels=(),
                invalidation=None,
                features=(NamedValue(name="lag_seconds", value=45.0),),
            ),
        },
        "Candidate": {
            "scored": candidate(),
            "unscored": candidate(
                side=Side.SELL,
                setup_type="sweep_reversal_short",
                reference_price=D("2650.10"),
                model_version_id=None,
                expected_net_r=None,
                net_r_stdev=None,
                net_r_quantiles=(),
                win_probability_frac=None,
                expected_cost_r=None,
                expected_holding_time_ns=None,
                quality_score=None,
                destinations=(),
            ),
            "rejected_negative": candidate(
                revision=1,
                seq=1,
                supersedes_candidate_id=candidate().candidate_id,
                status=CandidateStatus.REJECTED,
                status_reasons=("NEGATIVE_EXPECTED_NET_R",),
                expected_net_r=-0.15,
            ),
        },
        "TradePlan": {
            "limit_buy": trade_plan(),
            "market_sell": trade_plan(
                side=Side.SELL,
                entry_style=EntryStyle.MARKET,
                entry_order_type=OrderType.MARKET,
                entry_price=None,
                reference_price=D("2650.10"),
                stop_price=D("2652.10"),
                targets=(Target(label="first", price=D("2646.10"), close_frac=D("1")),),
                horizon=Horizon.INTRADAY,
                overnight_allowed=True,
                max_spread=None,
                max_entry_slippage=None,
            ),
            "rejected_stale": trade_plan(
                variant=1,
                status=PlanStatus.REJECTED,
                status_reasons=("STALE_CANDIDATE",),
                valid_until_ns=T0,
            ),
        },
        "RiskDecision": {
            "approved": risk_decision(),
            "approved_reduced": risk_decision(
                attempt=1,
                seq=1,
                outcome=RiskOutcome.APPROVED_REDUCED,
                reason_codes=("DEFENSIVE_STATE_REDUCTION",),
                volume_lots=D("0.25"),
                risk_amount=D("52.00"),
                risk_frac=D("0.00052"),
                daily_state=DailyState.DEFENSIVE,
                daily_pnl_frac=D("-0.021"),
            ),
            "rejected_at_floor": risk_decision(
                attempt=2,
                seq=2,
                outcome=RiskOutcome.REJECTED,
                reason_codes=("DAILY_PROFIT_FLOOR",),
                volume_lots=D("0"),
                risk_amount=D("0"),
                risk_frac=D("0"),
                daily_state=DailyState.PROFIT_PROTECTION,
                daily_pnl_frac=D("0.05"),
                profit_floor_frac=D("0.05"),
                margin_required_amount=None,
                gap_stress_loss_amount=None,
            ),
            "rejected_halt": risk_decision(
                attempt=3,
                seq=3,
                decision_time_ns=halted_time,
                outcome=RiskOutcome.REJECTED,
                reason_codes=("EMERGENCY_HALT",),
                volume_lots=D("0"),
                risk_amount=D("0"),
                risk_frac=D("0"),
                daily_state=DailyState.EMERGENCY_HALT,
                daily_pnl_frac=D("-0.03"),
            ),
        },
        "OrderEvent": {
            "submit_requested": orders[0],
            "accepted": orders[1],
            "partially_filled": orders[2],
            "filled": orders[3],
            "rejected": order_event(
                seq=1,
                order_seq=1,
                event_type=OrderEventType.REJECTED,
                order_state=OrderState.REJECTED,
                event_time_ns=T0 + 2 * NS_PER_MS,
                receive_time_ns=T0 + 3 * NS_PER_MS,
                reason_code="MARKET_CLOSED",
            ),
            "protective_stop": order_event(
                seq=4,
                side=Side.SELL,
                order_type=OrderType.STOP,
                intent=OrderIntent.PROTECTIVE_STOP,
                limit_price=None,
                trigger_price=D("2647.80"),
                attached_stop_price=None,
                valid_until_ns=None,
                plan_id=None,
                risk_decision_id=None,
                position_id=protective_position.position_id,
                client_order_id=identity.client_order_id(
                    RUN_ID, protective_position.position_id, OrderIntent.PROTECTIVE_STOP.value, 0
                ),
                event_time_ns=T0 + 2 * NS_PER_SECOND,
                receive_time_ns=T0 + 2 * NS_PER_SECOND,
            ),
        },
        "Position": {
            "open": positions[0],
            "tightened_partial": positions[1],
            "closed": positions[2],
            "external_other_bot": position(
                owner=PositionOwner.EXTERNAL,
                external_ref="FIXTURE-TICKET-1",
                position_id=identity.external_position_id("fixture-account", "FIXTURE-TICKET-1"),
                symbol="EURUSD",
                side=Side.SELL,
                average_entry_price=D("1.0850"),
                initial_stop_price=None,
                stop_price=None,
                mark_price=D("1.0842"),
                initial_risk_amount=None,
                plan_id=None,
                opening_client_order_id=None,
                realized_net_pnl_amount=D("0"),
                unrealized_net_pnl_amount=D("40.00"),
                peak_net_pnl_amount=D("55.00"),
                costs_amount=D("0"),
            ),
        },
        "ModelVersion": {
            "research": models[0],
            "shadow": models[1],
            "active": models[2],
            "ruleset_baseline": model_version(
                model_name="fixture_ruleset",
                kind=ModelKind.RULESET,
                training_data_start_ns=None,
                training_data_end_ns=None,
                feature_schema_sha256=None,
                seed=None,
                metrics=(),
            ),
        },
        "Clock": {
            "replay": clock_reading(),
            "paper_system": clock_reading(
                mode=RunMode.PAPER, clock_mode=ClockMode.SYSTEM, monotonic_ns=123_456_789, seq=7
            ),
        },
    }
