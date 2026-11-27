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
from gie.core.canonical import EXACT, fraction_of
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
    if not _approved(decision) or decision.profit_floor_amount is None:
        return True
    if decision.open_risk_amount is None or decision.pending_risk_amount is None:
        return False  # unknown exposure: the surplus cannot be shown, so nothing is approved
    surplus = EXACT.subtract(decision.daily_pnl_amount, decision.profit_floor_amount)
    committed = EXACT.add(
        EXACT.add(decision.risk_amount, decision.open_risk_amount), decision.pending_risk_amount
    )
    return committed <= surplus


def _aggregates_stated(decision: RiskDecision) -> bool:
    if not _approved(decision):
        return True
    stated = (
        decision.equity_amount,
        decision.open_risk_amount,
        decision.pending_risk_amount,
        decision.margin_required_amount,
        decision.margin_free_amount,
        decision.gap_stress_loss_amount,
    )
    if any(value is None for value in stated):
        return False
    assert decision.margin_required_amount is not None
    assert decision.margin_free_amount is not None
    return decision.margin_required_amount <= decision.margin_free_amount


def _fractions_derived(decision: RiskDecision) -> bool:
    base = decision.reference_equity_amount
    if decision.daily_pnl_frac != fraction_of(decision.daily_pnl_amount, base, round_up=False):
        return False
    if decision.risk_frac != fraction_of(decision.risk_amount, base, round_up=True):
        return False
    if decision.profit_floor_amount is None:
        return decision.profit_floor_frac is None
    return decision.profit_floor_frac == fraction_of(decision.profit_floor_amount, base, round_up=True)


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
    * **Amounts decide, fractions report.**  The daily result, the profit floor
      and the granted risk are exact money amounts, and every boundary - "at
      exactly the floor no new risk", the surplus above the floor - is decided
      on them.  The ``*_frac`` fields are derived from the amounts and
      ``reference_equity_amount`` (the equity the account's daily limits are
      measured against, fixed for the trading day) with a stated rounding
      (``gie.core.canonical.fraction_of``): the daily result is rounded down,
      the floor and the risk up, so a fraction never looks better than the
      amounts.  The thresholds of the daily states are research parameters; the
      risk engine converts them to amounts with the same reference equity.
    * ``daily_pnl_amount`` is account-wide: it includes open positions and the
      positions of every other system on the account.
    * **Profit floor.**  Once a floor has been established on a trading day it is
      carried by every later decision of that day, never decreases and never
      disappears; the state is then PROFIT_PROTECTION (or EMERGENCY_HALT).  New
      risk is approved only above the floor and only up to the surplus that is
      left after the risk of open positions and pending orders.
    * **An approval states what it rests on**: equity, open and pending risk,
      margin and the gap-stress loss.  Unknown is not approved.
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
        "risk_amount as a fraction of reference_equity_amount, rounded up (derived); 0 when rejected.",
        unit="fraction",
    )
    reference_equity_amount: Decimal = fld(
        Dec(gt="0", label="money"),
        "Equity that the day's fractions and thresholds refer to (per the account rules); the same "
        "for every decision of a trading day.",
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
    daily_pnl_amount: Decimal = fld(
        _MONEY,
        "Account-wide net result of the trading day including open positions of all systems.",
        unit="account currency",
    )
    daily_pnl_frac: Decimal = fld(
        _FRACTION,
        "daily_pnl_amount as a fraction of reference_equity_amount, rounded down (derived).",
        unit="fraction of reference equity",
    )
    profit_floor_amount: Decimal | None = fld(
        Opt(Dec(gt="0", label="money")),
        "Protected daily profit floor once established: never decreases and never disappears within "
        "the trading day; mandatory in PROFIT_PROTECTION.",
        unit="account currency",
    )
    profit_floor_frac: Decimal | None = fld(
        Opt(_FRACTION),
        "profit_floor_amount as a fraction of reference_equity_amount, rounded up (derived).",
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
            "PROFIT_PROTECTION carries a profit floor; a floor is carried only in PROFIT_PROTECTION "
            "or EMERGENCY_HALT (once established it stays for the trading day)",
            lambda r: (
                r.profit_floor_amount is not None
                if r.daily_state is DailyState.PROFIT_PROTECTION
                else (r.profit_floor_amount is None or r.daily_state is DailyState.EMERGENCY_HALT)
            ),
        ),
        inv(
            "RSK-7",
            "at or below the profit floor no new risk is approved, decided on exact amounts "
            "(G05: at exactly the floor no new risk)",
            lambda r: (
                r.profit_floor_amount is None
                or r.daily_pnl_amount > r.profit_floor_amount
                or r.outcome is RiskOutcome.REJECTED
            ),
        ),
        inv(
            "RSK-8",
            "above the profit floor an approval stays within the surplus after open exposure: "
            "risk_amount + open_risk_amount + pending_risk_amount <= daily_pnl_amount - "
            "profit_floor_amount (both aggregates must be stated)",
            _within_surplus,
        ),
        inv(
            "RSK-9",
            "the fractions are derived from the amounts and reference_equity_amount: daily_pnl_frac "
            "rounded down, profit_floor_frac and risk_frac rounded up, 12 fractional digits",
            _fractions_derived,
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
        inv(
            "RSK-13",
            "an approval states the aggregates it rests on - equity, open risk, pending risk, margin "
            "required and free, gap-stress loss - and margin_required_amount <= margin_free_amount "
            "(unknown is never approved)",
            _aggregates_stated,
        ),
    )

    @property
    def approved(self) -> bool:
        return _approved(self)


@dataclass(frozen=True, slots=True)
class DailyRiskSummary:
    """What the risk decisions of one run have established for one trading day so far."""

    trading_day: str
    account_currency: str
    reference_equity_amount: Decimal
    halted: bool
    profit_floor_amount: Decimal | None
    last_decision_time_ns: int


def advance_daily_risk(summary: DailyRiskSummary | None, decision: RiskDecision) -> DailyRiskSummary:
    """Check *decision* against what the day has established and return the updated summary.

    These are the parts of the daily-state rules that the master specification
    states without exception (G05); the thresholds and the full state machine
    are the risk engine's.  Within one trading day, for every risk producer of
    the run together:

    * an emergency halt stays in force until the next trading day;
    * a profit floor, once established, never decreases and never disappears -
      so no later decision of the day can approve risk at or below it (RSK-7)
      or beyond the surplus above it (RSK-8);
    * the reference equity and the account currency do not change.

    Raises :class:`IllegalTransitionError`; a refused decision changes nothing.
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal daily-state sequence: {rule}", decision_id=decision.decision_id, rule=rule
        )

    floor = decision.profit_floor_amount
    halted = decision.daily_state is DailyState.EMERGENCY_HALT
    if summary is not None:
        if decision.decision_time_ns < summary.last_decision_time_ns:
            raise fail("decision_time_ns may not decrease")
        if decision.trading_day < summary.trading_day:
            raise fail("trading_day may not go backwards")
    if summary is not None and decision.trading_day == summary.trading_day:
        if decision.account_currency != summary.account_currency:
            raise fail("account_currency may not change within a trading day")
        if decision.reference_equity_amount != summary.reference_equity_amount:
            raise fail("reference_equity_amount may not change within a trading day")
        if summary.halted and not halted:
            raise fail("an emergency halt stays in force until the next trading day")
        if summary.profit_floor_amount is not None:
            if floor is None:
                raise fail("a daily profit floor, once established, may not disappear")
            if floor < summary.profit_floor_amount:
                raise fail("the daily profit floor may not decrease")
        halted = halted or summary.halted
    return DailyRiskSummary(
        trading_day=decision.trading_day,
        account_currency=decision.account_currency,
        reference_equity_amount=decision.reference_equity_amount,
        halted=halted,
        profit_floor_amount=floor,
        last_decision_time_ns=decision.decision_time_ns,
    )


def validate_daily_state_sequence(previous: RiskDecision | None, decision: RiskDecision) -> None:
    """Check two consecutive decisions against the daily-state rules (see :func:`advance_daily_risk`).

    Judging pairs is enough only when every decision of the day is presented in
    order; the lifecycle ledger keeps the day's summary across all risk
    producers of a run and should be preferred.
    """
    summary = None if previous is None else advance_daily_risk(None, previous)
    advance_daily_risk(summary, decision)
