"""RiskDecision contract - the risk gate's answer to one trade plan."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from gie.contracts import identity
from gie.contracts._spec import Bool, Dec, EnumOf, Id, Int, Opt, Str, TimeNs, TradingDay, fld, inv
from gie.contracts.common import ZERO, Produced, reason_codes_field
from gie.contracts.registry import contract
from gie.core.timeutil import trading_day

CURRENCY_PATTERN = r"^[A-Z]{3}$"


class RiskOutcome(str, Enum):
    APPROVED = "APPROVED"
    APPROVED_REDUCED = "APPROVED_REDUCED"
    REJECTED = "REJECTED"


class DailyState(str, Enum):
    """Account-wide daily risk state (G05).  Thresholds are research parameters."""

    NORMAL = "NORMAL"
    DEFENSIVE = "DEFENSIVE"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    EMERGENCY_HALT = "EMERGENCY_HALT"


_MONEY = Dec(label="money")
_MONEY_NON_NEGATIVE = Dec(ge="0", label="money")


def _approved(decision: RiskDecision) -> bool:
    return decision.outcome is not RiskOutcome.REJECTED


@contract("RiskDecision", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class RiskDecision(Produced):
    """Approval, reduction or rejection of a plan, with the size and risk granted.

    Stream: ``risk_decision/<producer>``.  Ordering time: ``decision_time_ns``.
    An approval is bound to exactly one plan, reserves risk atomically
    (``reservation_id``) and expires at ``valid_until_ns``; execution must not
    act on an expired approval.  All fractions are relative to the day's
    reference equity defined by the account rules (session 08).
    """

    decision_id: str = fld(Id("rsk"), "identity.risk_decision_id(run_id, plan_id, attempt).")
    plan_id: str = fld(Id("pln"), "Plan that was evaluated.")
    attempt: int = fld(
        Int(ge=0, le=65535, label="attempt"), "0 for the first evaluation of the plan, +1 per re-evaluation."
    )
    outcome: RiskOutcome = fld(EnumOf(RiskOutcome), "Result of the evaluation.")
    reason_codes: tuple[str, ...] = reason_codes_field(
        "Reasons for a reduction or rejection (rule identifiers of the risk engine)."
    )
    volume_lots: Decimal = fld(Dec(ge="0", label="volume"), "Granted volume; 0 when rejected.", unit="lots")
    risk_amount: Decimal = fld(
        _MONEY_NON_NEGATIVE,
        "Worst-case loss at the hard stop for the granted volume including expected costs; 0 when rejected.",
        unit="account currency",
    )
    risk_frac: Decimal = fld(
        Dec(ge="0", label="fraction"),
        "risk_amount as a fraction of the day's reference equity.",
        unit="fraction",
    )
    quality_validated: bool = fld(
        Bool(),
        "True only when the size was adapted using quality from an owner-approved (ACTIVE) model version.",
    )
    account_currency: str = fld(
        Str(pattern=CURRENCY_PATTERN, min_len=3, max_len=3, label="currency"), "ISO 4217 account currency."
    )
    trading_day: str = fld(TradingDay(), "Prague trading day of decision_time_ns.")
    daily_state: DailyState = fld(EnumOf(DailyState), "Daily risk state at decision time.")
    daily_pnl_frac: Decimal = fld(
        Dec(label="fraction"),
        "Account-wide net result of the trading day including open positions of all systems.",
        unit="fraction of reference equity",
    )
    profit_floor_frac: Decimal | None = fld(
        Opt(Dec(label="fraction")),
        "Protected, non-decreasing daily profit floor; set if and only if daily_state is PROFIT_PROTECTION.",
        unit="fraction of reference equity",
    )
    reservation_id: str | None = fld(
        Opt(Id("rsv")), "identity.reservation_id(decision_id) of the reserved risk; null when rejected."
    )
    valid_until_ns: int = fld(
        TimeNs(), "Expiry instant of the approval and its reservation.", unit="ns since epoch, UTC"
    )
    equity_amount: Decimal | None = fld(
        Opt(_MONEY), "Account equity at decision time.", unit="account currency"
    )
    open_risk_amount: Decimal | None = fld(
        Opt(_MONEY_NON_NEGATIVE),
        "Aggregate risk of open positions before this decision.",
        unit="account currency",
    )
    pending_risk_amount: Decimal | None = fld(
        Opt(_MONEY_NON_NEGATIVE),
        "Aggregate risk of pending orders and unexpired reservations before this decision.",
        unit="account currency",
    )
    margin_required_amount: Decimal | None = fld(
        Opt(_MONEY_NON_NEGATIVE), "Margin required for the granted volume.", unit="account currency"
    )
    margin_free_amount: Decimal | None = fld(
        Opt(_MONEY), "Free margin before this decision.", unit="account currency"
    )
    gap_stress_loss_amount: Decimal | None = fld(
        Opt(_MONEY_NON_NEGATIVE),
        "Aggregate loss in the gap-stress scenario including this trade.",
        unit="account currency",
    )

    INVARIANTS = (
        inv(
            "RSK-1",
            "decision_id equals identity.risk_decision_id(run_id, plan_id, attempt)",
            lambda r: r.decision_id == identity.risk_decision_id(r.run_id, r.plan_id, r.attempt),
        ),
        inv(
            "RSK-2",
            "REJECTED grants nothing: volume_lots = 0, risk_amount = 0, risk_frac = 0, no reservation",
            lambda r: (
                _approved(r)
                or (
                    r.volume_lots == ZERO
                    and r.risk_amount == ZERO
                    and r.risk_frac == ZERO
                    and r.reservation_id is None
                )
            ),
        ),
        inv(
            "RSK-3",
            "an approval grants volume_lots > 0 and risk_amount > 0 and carries "
            "reservation_id = identity.reservation_id(decision_id)",
            lambda r: (
                not _approved(r)
                or (
                    r.volume_lots > ZERO
                    and r.risk_amount > ZERO
                    and r.reservation_id == identity.reservation_id(r.decision_id)
                )
            ),
        ),
        inv(
            "RSK-4",
            "reason_codes is non-empty unless outcome is APPROVED",
            lambda r: r.outcome is RiskOutcome.APPROVED or bool(r.reason_codes),
        ),
        inv(
            "RSK-5",
            "EMERGENCY_HALT never approves: outcome is REJECTED",
            lambda r: r.daily_state is not DailyState.EMERGENCY_HALT or r.outcome is RiskOutcome.REJECTED,
        ),
        inv(
            "RSK-6",
            "profit_floor_frac is set if and only if daily_state is PROFIT_PROTECTION",
            lambda r: (r.profit_floor_frac is not None) == (r.daily_state is DailyState.PROFIT_PROTECTION),
        ),
        inv(
            "RSK-7",
            "at or below the profit floor no new risk is approved (G05: at exactly the floor no new risk)",
            lambda r: (
                r.profit_floor_frac is None
                or r.daily_pnl_frac > r.profit_floor_frac
                or r.outcome is RiskOutcome.REJECTED
            ),
        ),
        inv(
            "RSK-8",
            "an approval expires later than decision_time_ns",
            lambda r: not _approved(r) or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "RSK-9",
            "trading_day is the Europe/Prague calendar date of decision_time_ns",
            lambda r: r.trading_day == trading_day(r.decision_time_ns),
        ),
    )

    @property
    def approved(self) -> bool:
        return _approved(self)
