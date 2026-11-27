"""TradePlan contract - one executable way to trade a candidate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from gie.contracts import identity
from gie.contracts._spec import (
    Bool,
    Dec,
    EnumOf,
    Float,
    Id,
    Int,
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
    Horizon,
    OrderType,
    Produced,
    Side,
    reason_codes_field,
    symbol_field,
)
from gie.contracts.registry import contract


class EntryStyle(str, Enum):
    """How the plan intends to get in (brief 05: market / limit / retest / pullback)."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    RETEST = "RETEST"
    PULLBACK = "PULLBACK"
    STOP = "STOP"


class PlanStatus(str, Enum):
    PREPARED = "PREPARED"
    ARMED = "ARMED"
    SELECTED = "SELECTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


_USABLE = (PlanStatus.PREPARED, PlanStatus.ARMED, PlanStatus.SELECTED)
_PURE_STYLES = {
    EntryStyle.MARKET: OrderType.MARKET,
    EntryStyle.LIMIT: OrderType.LIMIT,
    EntryStyle.STOP: OrderType.STOP,
}


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Target(Record):
    """A profit destination at which part of the position is closed."""

    label: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Destination name.")
    price: Decimal = fld(PRICE, "Target price.", unit="USD per troy ounce")
    close_frac: Decimal = fld(
        Dec(gt="0", le="1", label="fraction"),
        "Share of the initial volume to close at this target.",
        unit="fraction of initial volume",
    )


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceContribution(Record):
    """How much one evidence record contributed to the plan's score (explainability)."""

    evidence_id: str = fld(Id("evd"), "Contributing evidence.")
    weight: float = fld(Float(label="weight"), "Signed contribution to the expected net R.", unit="R")


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


@contract("TradePlan", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlan(Produced):
    """Executable plan: entry, hard stop, targets, expected net R and uncertainty.

    Stream: ``trade_plan/<producer>``.  Ordering time: ``decision_time_ns``.
    Plans that share ``alternative_group_id`` are mutually exclusive ways to
    trade the same candidate: at most one of them may ever be executed.
    A plan carries no size; size is granted by a RiskDecision.
    """

    plan_id: str = fld(Id("pln"), "identity.plan_id(candidate_id, entry_style, variant, revision).")
    candidate_id: str = fld(Id("cnd"), "Candidate this plan trades.")
    alternative_group_id: str = fld(
        Id("grp"), "identity.alternative_group_id(candidate_id); alternatives exclude each other."
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
        "Limit or stop-trigger price of the entry; null for a market entry.",
        unit="USD per troy ounce",
    )
    reference_price: Decimal = fld(
        PRICE,
        "Executable price at decision time: ask for BUY, bid for SELL.",
        unit="USD per troy ounce",
    )
    stop_price: Decimal = fld(
        PRICE,
        "Hard protective stop (the plan's invalidation).  It may later be tightened, never widened.",
        unit="USD per troy ounce",
    )
    targets: tuple[Target, ...] = fld(
        ListOf(Obj(Target), min_items=1, max_items=8), "Profit destinations, at least one."
    )
    expected_net_r: float = fld(Float(label="R multiple"), "Expected net result of this plan.", unit="R")
    net_r_stdev: float = fld(
        Float(ge=0.0, label="R multiple"), "Predictive standard deviation of the net result.", unit="R"
    )
    expected_cost_r: float = fld(
        Float(ge=0.0, label="R multiple"),
        "Expected spread, commission and slippage already deducted in expected_net_r.",
        unit="R",
    )
    model_version_id: str = fld(Id("mdl"), "Model (or rule set) version behind the estimates.")
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
    contributions: tuple[EvidenceContribution, ...] = fld(
        ListOf(
            Obj(EvidenceContribution),
            max_items=64,
            unique_key=lambda item: item.evidence_id,
            canonical_sort=lambda item: item.evidence_id,
            sorted_label="sorted by evidence_id",
        ),
        "Evidence contributions to the expected net R.",
    )
    valid_until_ns: int = fld(
        TimeNs(), "Expiry instant: the plan must not be acted on at or after it.", unit="ns since epoch, UTC"
    )
    revision: int = fld(Int(ge=0, label="revision"), "0 for the first record, +1 per update.")
    supersedes_plan_id: str | None = fld(
        Opt(Id("pln")), "Previous revision replaced by this record; null for revision 0."
    )

    INVARIANTS = (
        inv(
            "PLN-1",
            "plan_id equals identity.plan_id(candidate_id, entry_style, variant, revision)",
            lambda r: (
                r.plan_id == identity.plan_id(r.candidate_id, r.entry_style.value, r.variant, r.revision)
            ),
        ),
        inv(
            "PLN-2",
            "alternative_group_id equals identity.alternative_group_id(candidate_id)",
            lambda r: r.alternative_group_id == identity.alternative_group_id(r.candidate_id),
        ),
        inv(
            "PLN-3",
            "entry_price is null if and only if entry_order_type is MARKET",
            lambda r: (r.entry_price is None) == (r.entry_order_type is OrderType.MARKET),
        ),
        inv(
            "PLN-4",
            "entry styles MARKET, LIMIT and STOP use the order type of the same name",
            lambda r: r.entry_style not in _PURE_STYLES or _PURE_STYLES[r.entry_style] is r.entry_order_type,
        ),
        inv(
            "PLN-5",
            "the stop is on the losing side of the entry (BUY: stop < entry, SELL: stop > entry)",
            _stop_on_losing_side,
        ),
        inv(
            "PLN-6",
            "every target is on the winning side of the entry",
            _targets_on_winning_side,
        ),
        inv(
            "PLN-7",
            "the target close fractions sum to at most 1",
            lambda r: sum((target.close_frac for target in r.targets), start=Decimal(0)) <= ONE,
        ),
        inv(
            "PLN-8",
            "when status is PREPARED, ARMED or SELECTED, valid_until_ns is later than decision_time_ns",
            lambda r: r.status not in _USABLE or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "PLN-9",
            "status_reasons is non-empty when status is REJECTED or WITHDRAWN and empty when usable",
            lambda r: (
                bool(r.status_reasons)
                if r.status in (PlanStatus.REJECTED, PlanStatus.WITHDRAWN)
                else (not r.status_reasons if r.status in _USABLE else True)
            ),
        ),
        inv(
            "PLN-10",
            "supersedes_plan_id is set if and only if revision > 0",
            lambda r: (r.supersedes_plan_id is not None) == (r.revision > 0),
        ),
    )

    @property
    def effective_entry_price(self) -> Decimal:
        """Entry price used for risk geometry: the order price, or the reference price for market entries."""
        return _entry(self)

    @property
    def risk_per_unit(self) -> Decimal:
        """Price distance between entry and hard stop (always positive)."""
        return abs(_entry(self) - self.stop_price)
