"""RiskDecision contract - the risk gate's answer to one trade plan."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    Bool,
    Dec,
    EnumOf,
    Id,
    Int,
    Invariant,
    Opt,
    Str,
    TimeNs,
    TradingDay,
    fld,
    inv,
)
from gie.contracts.common import CURRENCY_PATTERN, SHA256_PATTERN, ZERO, Produced, reason_codes_field
from gie.contracts.registry import contract
from gie.core.canonical import EXACT
from gie.core.errors import IllegalTransitionError
from gie.core.timeutil import trading_day


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


_MONEY: Final[Dec] = Dec(label="money")
_MONEY_NON_NEGATIVE: Final[Dec] = Dec(ge="0", label="money")
_FRACTION: Final[Dec] = Dec(label="fraction")


def _approved(decision: RiskDecision) -> bool:
    return decision.outcome is not RiskOutcome.REJECTED


def _within_surplus(decision: RiskDecision) -> bool:
    if not _approved(decision) or decision.profit_floor_frac is None:
        return True
    surplus = EXACT.subtract(decision.daily_pnl_frac, decision.profit_floor_frac)
    return decision.risk_frac <= surplus


@final
@contract("RiskDecision", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class RiskDecision(Produced):
    """Approval, reduction or rejection of a plan, with the size and risk granted.

    Stream: ``risk_decision/<producer>``.  Ordering time: ``decision_time_ns``.

    * An approval is bound to exactly one plan revision - by ``plan_id`` and by
      ``plan_fingerprint``, the hash of the plan that was evaluated - reserves
      risk atomically (``reservation_id``) and expires at ``valid_until_ns``.
      It can be spent by exactly one entry order (the order's identity is derived
      from ``decision_id``).
    * All fractions are relative to ``reference_equity_amount``, the equity the
      account's daily limits are measured against (defined by the account rules,
      session 08).  ``risk_frac`` is rounded **up**, so that
      ``risk_amount <= risk_frac * reference_equity_amount`` always holds and a
      limit checked on the fraction is never understated.
    * ``daily_pnl_frac`` is account-wide: it includes open positions and the
      positions of every other system on the account.
    """

    decision_id: str = fld(Id("rsk"), "identity.risk_decision_id(run_id, plan_id, attempt).")
    plan_id: str = fld(Id("pln"), "Plan revision that was evaluated.")
    plan_fingerprint: str = fld(
        Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256"),
        "Fingerprint (gie.contracts.fingerprint) of the evaluated plan: binds the decision to its content.",
    )
    alternative_group_id: str = fld(
        Id("grp"), "Alternative group of the plan: at most one plan of a group is ever executed."
    )
    attempt: int = fld(
        Int(ge=0, le=65535, label="attempt"),
        "0 for the first evaluation of the plan, +1 per re-evaluation.",
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
        Dec(ge="0", le="1", label="fraction"),
        "risk_amount as a fraction of reference_equity_amount, rounded up; 0 when rejected.",
        unit="fraction",
    )
    reference_equity_amount: Decimal = fld(
        Dec(gt="0", label="money"),
        "Equity that the day's fractions refer to (per the account rules).",
        unit="account currency",
    )
    quality_validated: bool = fld(
        Bool(),
        "True only when the size was adapted using quality from an owner-approved (ACTIVE) model version.",
    )
    quality_model_version_id: str | None = fld(
        Opt(Id("mdl")), "That model version; set if and only if quality_validated is true."
    )
    account_currency: str = fld(
        Str(pattern=CURRENCY_PATTERN, min_len=3, max_len=3, label="currency"), "ISO 4217 account currency."
    )
    trading_day: str = fld(TradingDay(), "Prague trading day of decision_time_ns.")
    daily_state: DailyState = fld(EnumOf(DailyState), "Daily risk state at decision time.")
    daily_pnl_frac: Decimal = fld(
        _FRACTION,
        "Account-wide net result of the trading day including open positions of all systems.",
        unit="fraction of reference equity",
    )
    profit_floor_frac: Decimal | None = fld(
        Opt(_FRACTION),
        "Protected, non-decreasing daily profit floor; set if and only if daily_state is PROFIT_PROTECTION.",
        unit="fraction of reference equity",
    )
    reservation_id: str | None = fld(
        Opt(Id("rsv")), "identity.reservation_id(decision_id) of the reserved risk; null when rejected."
    )
    valid_until_ns: int = fld(
        TimeNs(),
        "Expiry instant of the approval and its reservation; not later than the plan's expiry.",
        unit="ns since epoch, UTC",
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

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
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
            "an approval grants volume_lots > 0, risk_amount > 0 and risk_frac > 0 and carries "
            "reservation_id = identity.reservation_id(decision_id)",
            lambda r: (
                not _approved(r)
                or (
                    r.volume_lots > ZERO
                    and r.risk_amount > ZERO
                    and r.risk_frac > ZERO
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
            "above the profit floor an approval risks at most the surplus: "
            "risk_frac <= daily_pnl_frac - profit_floor_frac",
            _within_surplus,
        ),
        inv(
            "RSK-9",
            "risk_amount <= risk_frac * reference_equity_amount (the fraction is rounded up, never down)",
            lambda r: r.risk_amount <= EXACT.multiply(r.risk_frac, r.reference_equity_amount),
        ),
        inv(
            "RSK-10",
            "an approval expires later than decision_time_ns",
            lambda r: not _approved(r) or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "RSK-11",
            "trading_day is the Europe/Prague calendar date of decision_time_ns",
            lambda r: r.trading_day == trading_day(r.decision_time_ns),
        ),
        inv(
            "RSK-12",
            "quality_model_version_id is set if and only if quality_validated is true",
            lambda r: (r.quality_model_version_id is not None) == r.quality_validated,
        ),
    )

    @property
    def approved(self) -> bool:
        return _approved(self)


def validate_daily_state_sequence(previous: RiskDecision | None, decision: RiskDecision) -> None:
    """Check two consecutive decisions of one risk gate against the daily-state rules of G05.

    Within one trading day: an emergency halt stays in force, and a profit floor
    never decreases.  The full state machine (thresholds, recovery level) is the
    risk engine's; these are the parts the master specification states without
    exception.
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal daily-state sequence: {rule}", decision_id=decision.decision_id, rule=rule
        )

    if previous is None:
        return
    if decision.decision_time_ns < previous.decision_time_ns:
        raise fail("decision_time_ns may not decrease")
    if decision.trading_day < previous.trading_day:
        raise fail("trading_day may not go backwards")
    if decision.trading_day != previous.trading_day:
        return
    if decision.account_currency != previous.account_currency:
        raise fail("account_currency may not change within a trading day")
    if (
        previous.daily_state is DailyState.EMERGENCY_HALT
        and decision.daily_state is not DailyState.EMERGENCY_HALT
    ):
        raise fail("an emergency halt stays in force until the next trading day")
    if (
        previous.profit_floor_frac is not None
        and decision.profit_floor_frac is not None
        and decision.profit_floor_frac < previous.profit_floor_frac
    ):
        raise fail("the daily profit floor may not decrease")
