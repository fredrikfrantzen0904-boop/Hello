"""TradePlan contract - one executable way to trade an opportunity."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    Bool,
    Dec,
    EnumOf,
    Float,
    Id,
    Int,
    Invariant,
    ListOf,
    Obj,
    Opt,
    Record,
    Str,
    TimeNs,
    fld,
    inv,
    record,
)
from gie.contracts.common import (
    CODE_PATTERN,
    ONE,
    PRICE,
    Contribution,
    Horizon,
    OrderType,
    PriceCondition,
    Produced,
    Side,
    contributions_field,
    reason_codes_field,
    symbol_field,
)
from gie.contracts.model_version import ModelStatus
from gie.contracts.registry import contract
from gie.core.canonical import EXACT
from gie.core.errors import IllegalTransitionError


class EntryStyle(str, Enum):
    """How the plan intends to get in (brief 05: market / limit / retest / pullback).

    ``MARKET`` enters now at the executable price.  ``LIMIT`` and ``STOP`` rest an
    order at ``entry_price``.  ``RETEST`` and ``PULLBACK`` wait for price to come
    back to ``entry_price`` (the intended level); they may be executed with a
    resting limit order or, on confirmation, with a market order.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    RETEST = "RETEST"
    PULLBACK = "PULLBACK"
    STOP = "STOP"


class PlanStatus(str, Enum):
    """Lifecycle of a plan.

    * ``PREPARED`` - constructed and kept ready (both directions may be prepared);
      not yet eligible for execution.
    * ``ARMED`` - its conditions are set: it may be selected as soon as its entry
      trigger occurs.
    * ``SELECTED`` - chosen by the ranking; this is the status the risk gate
      evaluates and the only one an entry order may be built from.
    * ``WITHDRAWN`` - invalidated by actual market behaviour.
    * ``EXPIRED`` - aged out.
    * ``REJECTED`` - failed a gate of the decision engine (reason codes say which).
    """

    PREPARED = "PREPARED"
    ARMED = "ARMED"
    SELECTED = "SELECTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


_USABLE: Final[tuple[PlanStatus, ...]] = (PlanStatus.PREPARED, PlanStatus.ARMED, PlanStatus.SELECTED)
_EXECUTABLE: Final[tuple[PlanStatus, ...]] = (PlanStatus.ARMED, PlanStatus.SELECTED)
_FINAL: Final[frozenset[PlanStatus]] = frozenset(
    {PlanStatus.WITHDRAWN, PlanStatus.EXPIRED, PlanStatus.REJECTED}
)

#: ``status before -> statuses allowed after`` for revisions of one plan.  A plan
#: never moves back towards PREPARED, and WITHDRAWN, EXPIRED and REJECTED are final.
PLAN_STATUS_TRANSITIONS: Final[dict[PlanStatus | None, frozenset[PlanStatus]]] = {
    None: frozenset(PlanStatus),
    PlanStatus.PREPARED: frozenset(PlanStatus),
    PlanStatus.ARMED: frozenset(PlanStatus) - {PlanStatus.PREPARED},
    PlanStatus.SELECTED: _FINAL | {PlanStatus.SELECTED},
    PlanStatus.WITHDRAWN: frozenset(),
    PlanStatus.EXPIRED: frozenset(),
    PlanStatus.REJECTED: frozenset(),
}

_PURE_STYLES: Final[dict[EntryStyle, OrderType]] = {
    EntryStyle.MARKET: OrderType.MARKET,
    EntryStyle.LIMIT: OrderType.LIMIT,
    EntryStyle.STOP: OrderType.STOP,
}


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Target(Record):
    """A profit destination of the plan.

    ``close_frac`` is the share of the initial volume to close there; 0 marks a
    destination without a scheduled close (the exit is then left to the profit
    ratchet).
    """

    label: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Destination name.")
    scenario: str | None = fld(
        Opt(Str(pattern=CODE_PATTERN, max_len=48, label="code")),
        "Scenario of the candidate's destination this target is: the destination (scenario, label) "
        "of the plan's candidate, with the same price.  Null when the target is not one of the "
        "candidate's predicted destinations.",
    )
    price: Decimal = fld(PRICE, "Target price.", unit="USD per troy ounce")
    close_frac: Decimal = fld(
        Dec(ge="0", le="1", label="fraction"),
        "Share of the initial volume to close at this target (0 = no scheduled close).",
        unit="fraction of initial volume",
    )


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class PlanEstimate(Record):
    """Estimates for one concrete plan geometry, returned by the plan scorer (session 04).

    All values are net of expected costs and in R of the *plan's own* risk
    distance (entry to stop).
    """

    model_version_id: str = fld(Id("mdl"), "Model (or rule set) version that produced the estimate.")
    model_status: ModelStatus = fld(EnumOf(ModelStatus), "Promotion status of that model.")
    expected_net_r: float = fld(Float(label="R multiple"), "Expected net result.", unit="R")
    net_r_stdev: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Outcome dispersion of a single result.", unit="R"
    )
    expected_net_r_stderr: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Standard error of expected_net_r.", unit="R"
    )
    support_count: int | None = fld(Opt(Int(ge=0, label="count")), "Effective number of cases behind it.")
    expected_cost_r: float = fld(
        Float(ge=0.0, label="R multiple"), "Expected costs already deducted in expected_net_r.", unit="R"
    )
    baseline_r: float | None = fld(
        Opt(Float(label="R multiple")), "Part of expected_net_r not attributed to a contribution.", unit="R"
    )
    contributions: tuple[Contribution, ...] = contributions_field("Attribution of expected_net_r.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "EST-1",
            "the estimate states its uncertainty: net_r_stdev or expected_net_r_stderr",
            lambda r: r.net_r_stdev is not None or r.expected_net_r_stderr is not None,
        ),
    )


def _entry(plan: TradePlan) -> Decimal:
    return plan.entry_price if plan.entry_price is not None else plan.reference_price


def _stop_on_losing_side(plan: TradePlan) -> bool:
    entry = _entry(plan)
    return plan.stop_price < entry if plan.side is Side.BUY else plan.stop_price > entry


def _targets_on_winning_side(plan: TradePlan) -> bool:
    entry = _entry(plan)
    if plan.side is Side.BUY:
        return all(target.price > entry for target in plan.targets)
    return all(target.price < entry for target in plan.targets)


def _resting_entry_beyond_market(plan: TradePlan) -> bool:
    """A resting entry order of a usable plan is not marketable at the price it was planned against."""
    if plan.status not in _USABLE or plan.entry_price is None:
        return True
    buy = plan.side is Side.BUY
    if plan.entry_order_type is OrderType.LIMIT:
        return plan.entry_price <= plan.reference_price if buy else plan.entry_price >= plan.reference_price
    if plan.entry_order_type is OrderType.STOP:
        return plan.entry_price >= plan.reference_price if buy else plan.entry_price <= plan.reference_price
    return True


def _targets_named_once(plan: TradePlan) -> bool:
    keys = [(target.scenario, target.label) for target in plan.targets]
    return len(keys) == len(set(keys))


def _condition_prices_positive(plan: TradePlan) -> bool:
    """A plan is always XAUUSD, so the level of each of its price conditions is a gold price."""
    trigger = () if plan.entry_trigger is None else (plan.entry_trigger,)
    return all(condition.price > 0 for condition in (*trigger, *plan.invalidations))


@final
@contract("TradePlan", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlan(Produced):
    """Executable plan: entry, hard stop, destinations, expected net R and uncertainty.

    Stream: ``trade_plan/<producer>``.  Ordering time: ``decision_time_ns``.

    * Plans that share ``alternative_group_id`` are mutually exclusive ways to
      trade the same opportunity: at most one of them may ever be executed.
    * A plan carries no size; size is granted by a RiskDecision, which is bound
      to this exact revision (``plan_id`` and the plan's fingerprint).  A new
      revision voids the approval and the reservation of the revision it
      supersedes, and a working entry order of the superseded revision must be
      cancelled.
    * The risk geometry (stop and destinations) is checked against the
      *effective entry*: ``entry_price``, or ``reference_price`` for a market
      entry.  A market-style plan that is triggered later is re-issued as a new
      revision with a fresh ``reference_price`` before risk evaluates it.
    """

    plan_id: str = fld(Id("pln"), "identity.plan_id(candidate_id, entry_style, variant, revision).")
    candidate_id: str = fld(Id("cnd"), "Candidate (one scorer's view) this plan was built from.")
    opportunity_id: str = fld(Id("opp"), "Stable identity of the opportunity (same as on the candidate).")
    alternative_group_id: str = fld(
        Id("grp"), "identity.alternative_group_id(opportunity_id); alternatives exclude each other."
    )
    variant: int = fld(
        Int(ge=0, le=255, label="variant"), "Distinguishes several plans of the same entry style."
    )
    symbol: str = symbol_field("Always XAUUSD: GIE trades only gold.", gold_only=True)
    side: Side = fld(EnumOf(Side), "Direction of the trade.")
    horizon: Horizon = fld(EnumOf(Horizon), "Scalp or intraday.")
    entry_style: EntryStyle = fld(EnumOf(EntryStyle), "Intended way of entering.")
    entry_order_type: OrderType = fld(EnumOf(OrderType), "Order type used for the entry.")
    entry_price: Decimal | None = fld(
        Opt(PRICE),
        "Intended entry level: the limit or stop-trigger price, or the retest / pullback level. "
        "Null only for entry style MARKET.",
        unit="USD per troy ounce",
    )
    reference_price: Decimal = fld(
        PRICE,
        "Executable price at decision time: ask for BUY, bid for SELL.",
        unit="USD per troy ounce",
    )
    entry_trigger: PriceCondition | None = fld(
        Opt(Obj(PriceCondition)),
        "Observable condition that must occur before the entry is attempted (e.g. the confirmation "
        "of a retest); null when none.",
    )
    invalidations: tuple[PriceCondition, ...] = fld(
        ListOf(Obj(PriceCondition), max_items=8),
        "Observable conditions that invalidate the plan before entry (actual behaviour overrides "
        "the expectation); evaluated by the decision engine, which then emits a WITHDRAWN revision.",
    )
    stop_price: Decimal = fld(
        PRICE,
        "Hard protective stop.  In later revisions it may move towards the entry, never away from it.",
        unit="USD per troy ounce",
    )
    targets: tuple[Target, ...] = fld(
        ListOf(
            Obj(Target),
            min_items=1,
            max_items=8,
            unique_key=lambda target: target.price,
            canonical_sort=lambda target: target.price,
            sorted_label="sorted by price",
        ),
        "Profit destinations, at least one.",
    )
    expected_net_r: float = fld(
        Float(label="R multiple"), "Expected net result in R of this plan's own risk distance.", unit="R"
    )
    net_r_stdev: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Outcome dispersion of a single result.", unit="R"
    )
    expected_net_r_stderr: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Standard error of expected_net_r.", unit="R"
    )
    support_count: int | None = fld(Opt(Int(ge=0, label="count")), "Effective number of cases behind it.")
    expected_cost_r: float = fld(
        Float(ge=0.0, label="R multiple"),
        "Expected spread, commission and slippage already deducted in expected_net_r.",
        unit="R",
    )
    model_version_id: str = fld(Id("mdl"), "Model (or rule set) version behind the estimates.")
    model_status: ModelStatus = fld(
        EnumOf(ModelStatus), "Promotion status of that model at decision time (only ACTIVE is validated)."
    )
    max_spread: Decimal | None = fld(
        Opt(PRICE), "Do not enter while the spread is wider than this.", unit="USD per troy ounce"
    )
    max_entry_slippage: Decimal | None = fld(
        Opt(Dec(ge="0", label="price distance")),
        "Largest acceptable adverse difference between intended and achievable entry price.",
        unit="USD per troy ounce",
    )
    overnight_allowed: bool = fld(
        Bool(), "Whether the position may be held past the daily rollover (never over a weekend)."
    )
    status: PlanStatus = fld(EnumOf(PlanStatus), "Lifecycle status of this revision.")
    status_reasons: tuple[str, ...] = reason_codes_field("Why the plan was rejected or withdrawn.")
    baseline_r: float | None = fld(
        Opt(Float(label="R multiple")), "Part of expected_net_r not attributed to a contribution.", unit="R"
    )
    contributions: tuple[Contribution, ...] = contributions_field(
        "Attribution of expected_net_r to evidence and features."
    )
    valid_until_ns: int = fld(
        TimeNs(),
        "Expiry instant: the plan must not be acted on at or after it.  It is not later than the "
        "expiry of its candidate.",
        unit="ns since epoch, UTC",
    )
    revision: int = fld(Int(ge=0, label="revision"), "0 for the first record, +1 per update.")
    supersedes_plan_id: str | None = fld(
        Opt(Id("pln")), "Previous revision replaced by this record; null for revision 0."
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "PLN-1",
            "plan_id equals identity.plan_id(candidate_id, entry_style, variant, revision)",
            lambda r: (
                r.plan_id == identity.plan_id(r.candidate_id, r.entry_style.value, r.variant, r.revision)
            ),
        ),
        inv(
            "PLN-2",
            "alternative_group_id equals identity.alternative_group_id(opportunity_id)",
            lambda r: r.alternative_group_id == identity.alternative_group_id(r.opportunity_id),
        ),
        inv(
            "PLN-3",
            "entry_price is null if and only if entry_style is MARKET",
            lambda r: (r.entry_price is None) == (r.entry_style is EntryStyle.MARKET),
        ),
        inv(
            "PLN-4",
            "entry styles MARKET, LIMIT and STOP use the order type of the same name; RETEST and "
            "PULLBACK use LIMIT or MARKET",
            lambda r: (
                _PURE_STYLES[r.entry_style] is r.entry_order_type
                if r.entry_style in _PURE_STYLES
                else r.entry_order_type in (OrderType.LIMIT, OrderType.MARKET)
            ),
        ),
        inv(
            "PLN-5",
            "the stop is on the losing side of the effective entry (BUY: stop < entry, SELL: stop > entry)",
            _stop_on_losing_side,
        ),
        inv(
            "PLN-6",
            "every target is on the winning side of the effective entry",
            _targets_on_winning_side,
        ),
        inv(
            "PLN-7",
            "the target close fractions sum to at most 1",
            lambda r: (
                functools.reduce(EXACT.add, (target.close_frac for target in r.targets), Decimal(0)) <= ONE
            ),
        ),
        inv(
            "PLN-8",
            "when status is PREPARED, ARMED or SELECTED, valid_until_ns is later than decision_time_ns",
            lambda r: r.status not in _USABLE or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "PLN-9",
            "status_reasons is non-empty when status is REJECTED or WITHDRAWN",
            lambda r: r.status not in (PlanStatus.REJECTED, PlanStatus.WITHDRAWN) or bool(r.status_reasons),
        ),
        inv(
            "PLN-10",
            "supersedes_plan_id is null for revision 0 and otherwise equals the id of the previous "
            "revision, identity.plan_id(candidate_id, entry_style, variant, revision - 1)",
            lambda r: (
                r.supersedes_plan_id is None
                if r.revision == 0
                else r.supersedes_plan_id
                == identity.plan_id(r.candidate_id, r.entry_style.value, r.variant, r.revision - 1)
            ),
        ),
        inv(
            "PLN-11",
            "an ARMED or SELECTED plan has expected_net_r > 0 (positive expectancy gate)",
            lambda r: r.status not in _EXECUTABLE or r.expected_net_r > 0,
        ),
        inv(
            "PLN-12",
            "the plan states its uncertainty: net_r_stdev or expected_net_r_stderr",
            lambda r: r.net_r_stdev is not None or r.expected_net_r_stderr is not None,
        ),
        inv(
            "PLN-13",
            "entry_trigger and every invalidation of a plan are prices greater than zero",
            _condition_prices_positive,
        ),
        inv(
            "PLN-14",
            "while the plan is PREPARED, ARMED or SELECTED its resting entry lies beyond the market it "
            "was planned against: with order type LIMIT a BUY entry_price <= reference_price and a SELL "
            "entry_price >= reference_price; with order type STOP a BUY entry_price >= reference_price "
            "and a SELL entry_price <= reference_price",
            _resting_entry_beyond_market,
        ),
        inv(
            "PLN-15",
            "no two targets have the same (scenario, label)",
            _targets_named_once,
        ),
    )

    @property
    def effective_entry_price(self) -> Decimal:
        """Entry price used for risk geometry: ``entry_price``, or ``reference_price`` for MARKET style."""
        return _entry(self)

    @property
    def risk_per_unit(self) -> Decimal:
        """Price distance between effective entry and hard stop (always positive); this is 1 R."""
        return EXACT.abs(EXACT.subtract(_entry(self), self.stop_price))


_PLAN_IMMUTABLE: Final[tuple[str, ...]] = (
    "run_id",
    "mode",
    "producer",
    "candidate_id",
    "opportunity_id",
    "alternative_group_id",
    "variant",
    "symbol",
    "side",
    "horizon",
    "entry_style",
)


def validate_plan_transition(previous: TradePlan | None, revision: TradePlan) -> None:
    """Check that *revision* may follow *previous* in the chain of one plan.

    The risk of a plan is never widened along its chain (G04-11):

    * the stop may only move towards the entry, never away from it;
    * for every entry style except MARKET the risk distance ``|entry_price - stop_price|``
      (1 R) may not increase either, so an intended entry level that moves away from an
      unchanged stop is refused as well;
    * a MARKET plan has no intended entry level: it is re-issued with a fresh
      ``reference_price`` before risk evaluates it, and only its stop is held.

    A final status ends the chain.
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal plan transition: {rule}",
            plan_id=revision.plan_id,
            revision=revision.revision,
            rule=rule,
        )

    before = None if previous is None else previous.status
    if revision.status not in PLAN_STATUS_TRANSITIONS[before]:
        assert before is not None
        raise fail(f"{before.value} -> {revision.status.value} is not allowed")
    if previous is None:
        if revision.revision != 0:
            raise fail("the first record of a plan has revision 0")
        return
    if revision.revision != previous.revision + 1:
        raise fail("revision must increase by exactly 1")
    if revision.supersedes_plan_id != previous.plan_id:
        raise fail("supersedes_plan_id must name the previous revision")
    for name in _PLAN_IMMUTABLE:
        if getattr(previous, name) != getattr(revision, name):
            raise fail(f"{name} may not change between revisions")
    widened = (
        revision.stop_price < previous.stop_price
        if revision.side is Side.BUY
        else revision.stop_price > previous.stop_price
    )
    if widened:
        raise fail("the stop may not be moved away from the entry (no widening)")
    # entry_style is immutable along the chain, so both revisions state an entry level or neither.
    if revision.entry_style is not EntryStyle.MARKET and revision.risk_per_unit > previous.risk_per_unit:
        raise fail(
            "the risk distance between entry_price and stop_price may not increase between revisions "
            "(an entry that moves away from the stop widens 1 R)"
        )
    for name in ("decision_time_ns", "as_of_time_ns", "seq"):
        if getattr(revision, name) < getattr(previous, name):
            raise fail(f"{name} may not decrease")
