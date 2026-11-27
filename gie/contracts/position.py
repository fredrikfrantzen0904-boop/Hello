"""Position contract - snapshot of one position on the account."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from gie.contracts._spec import Dec, EnumOf, Id, Int, Opt, Str, TimeNs, fld, inv
from gie.contracts.common import GOLD_SYMBOL, PRICE, ZERO, Produced, Side, symbol_field
from gie.contracts.registry import contract
from gie.contracts.risk_decision import CURRENCY_PATTERN
from gie.core.errors import IllegalTransitionError


class PositionOwner(str, Enum):
    """Who opened the position.

    ``EXTERNAL`` positions belong to other systems trading the same account
    (Gold Bot, AI trader, breakout bot, manual).  GIE observes them for
    account-wide risk (G05-12) and never manages them.
    """

    GIE = "GIE"
    EXTERNAL = "EXTERNAL"


class PositionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


_MONEY = Dec(label="money")


def _gie_fields(position: Position) -> bool:
    if position.owner is PositionOwner.GIE:
        return (
            position.symbol == GOLD_SYMBOL
            and position.plan_id is not None
            and position.opening_client_order_id is not None
            and position.initial_stop_price is not None
            and position.initial_risk_amount is not None
            and position.external_ref is None
            and (position.state is PositionState.CLOSED or position.stop_price is not None)
            and (position.state is PositionState.CLOSED or position.mark_price is not None)
        )
    return (
        position.external_ref is not None
        and position.plan_id is None
        and position.opening_client_order_id is None
    )


def _stop_not_widened(position: Position) -> bool:
    if position.stop_price is None or position.initial_stop_price is None:
        return True
    if position.side is Side.BUY:
        return position.stop_price >= position.initial_stop_price
    return position.stop_price <= position.initial_stop_price


def _stop_tightened_or_equal(side: Side, before: Decimal | None, after: Decimal | None) -> bool:
    if before is None:
        return True  # an unprotected (external) position may gain a stop
    if after is None:
        return False  # a stop may never be removed
    return after >= before if side is Side.BUY else after <= before


@contract("Position", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Position(Produced):
    """State of one position, marked at the price at which it could be closed now.

    Stream: ``position/<producer>``; ``revision`` numbers the snapshots of one
    position from 0.  Ordering time: ``decision_time_ns``.

    *Executable net P&L* is what closing now would really yield: the open
    volume marked at the exit-side quote (bid for BUY, ask for SELL) minus
    estimated exit costs, plus everything already realised net of costs.
    ``peak_net_pnl_amount`` is its high-water mark and drives the profit ratchet.
    """

    position_id: str = fld(
        Id("pos"), "identity.position_id(run, opening order) or identity.external_position_id(...)."
    )
    revision: int = fld(Int(ge=0, label="revision"), "Snapshot number of this position: 0, 1, 2, ...")
    owner: PositionOwner = fld(EnumOf(PositionOwner), "GIE, or another system on the same account.")
    external_ref: str | None = fld(
        Opt(Str(max_len=64, label="reference")),
        "Broker ticket of an EXTERNAL position; null for GIE positions.",
    )
    symbol: str = symbol_field("XAUUSD for GIE positions; any instrument for EXTERNAL ones.")
    side: Side = fld(EnumOf(Side), "BUY = long, SELL = short.")
    state: PositionState = fld(EnumOf(PositionState), "OPEN or CLOSED.")
    volume_lots: Decimal = fld(Dec(ge="0", label="volume"), "Open volume; 0 when closed.", unit="lots")
    initial_volume_lots: Decimal = fld(Dec(gt="0", label="volume"), "Volume at opening.", unit="lots")
    average_entry_price: Decimal = fld(PRICE, "Volume-weighted entry price.", unit="quote currency per unit")
    initial_stop_price: Decimal | None = fld(
        Opt(PRICE), "Hard stop at opening; mandatory for GIE positions.", unit="quote currency per unit"
    )
    stop_price: Decimal | None = fld(
        Opt(PRICE),
        "Current hard protective stop; never further from the entry than initial_stop_price.",
        unit="quote currency per unit",
    )
    take_profit_price: Decimal | None = fld(
        Opt(PRICE), "Current broker-side take profit, if any.", unit="quote currency per unit"
    )
    mark_price: Decimal | None = fld(
        Opt(PRICE),
        "Exit-side quote used for the valuation: bid for BUY, ask for SELL.",
        unit="quote currency per unit",
    )
    initial_risk_amount: Decimal | None = fld(
        Opt(Dec(gt="0", label="money")),
        "Amount risked at opening (1 R); mandatory for GIE positions.",
        unit="account currency",
    )
    realized_net_pnl_amount: Decimal = fld(
        _MONEY,
        "Result already realised by partial or full closes, net of costs and swap.",
        unit="account currency",
    )
    unrealized_net_pnl_amount: Decimal = fld(
        _MONEY,
        "Result of closing the open volume at mark_price, net of estimated exit costs; 0 when closed.",
        unit="account currency",
    )
    peak_net_pnl_amount: Decimal = fld(
        _MONEY,
        "Highest value of realized + unrealized executable net P&L since opening (never decreases).",
        unit="account currency",
    )
    costs_amount: Decimal = fld(
        Dec(ge="0", label="money"),
        "Commissions and fees charged so far (already inside realized).",
        unit="account currency",
    )
    swap_amount: Decimal = fld(
        _MONEY,
        "Overnight financing charged (-) or credited (+) so far (already inside realized).",
        unit="account currency",
    )
    account_currency: str = fld(
        Str(pattern=CURRENCY_PATTERN, min_len=3, max_len=3, label="currency"), "ISO 4217 account currency."
    )
    opened_time_ns: int = fld(TimeNs(), "When the position was opened.", unit="ns since epoch, UTC")
    closed_time_ns: int | None = fld(
        Opt(TimeNs()), "When the position was fully closed; null while open.", unit="ns since epoch, UTC"
    )
    plan_id: str | None = fld(Opt(Id("pln")), "Plan that opened a GIE position.")
    opening_client_order_id: str | None = fld(Opt(Id("ord")), "Entry order of a GIE position.")

    INVARIANTS = (
        inv(
            "POS-1",
            "state is OPEN iff volume_lots > 0 iff closed_time_ns is null",
            lambda r: (r.state is PositionState.OPEN) == (r.volume_lots > ZERO) == (r.closed_time_ns is None),
        ),
        inv(
            "POS-2",
            "volume_lots <= initial_volume_lots (a position is never increased: no averaging down)",
            lambda r: r.volume_lots <= r.initial_volume_lots,
        ),
        inv(
            "POS-3",
            "a GIE position is XAUUSD with plan, opening order, initial stop and initial risk, and while "
            "open "
            "has a stop and a mark price; an EXTERNAL position has external_ref and no plan or opening order",
            _gie_fields,
        ),
        inv(
            "POS-4",
            "the stop is never wider than the initial stop (BUY: stop >= initial, SELL: stop <= initial)",
            _stop_not_widened,
        ),
        inv(
            "POS-5",
            "peak_net_pnl_amount >= realized_net_pnl_amount + unrealized_net_pnl_amount",
            lambda r: r.peak_net_pnl_amount >= r.realized_net_pnl_amount + r.unrealized_net_pnl_amount,
        ),
        inv(
            "POS-6",
            "a CLOSED position has unrealized_net_pnl_amount = 0",
            lambda r: r.state is PositionState.OPEN or r.unrealized_net_pnl_amount == ZERO,
        ),
        inv(
            "POS-7",
            "opened_time_ns <= closed_time_ns (when closed) <= decision_time_ns",
            lambda r: (
                r.opened_time_ns <= r.decision_time_ns
                and (r.closed_time_ns is None or r.opened_time_ns <= r.closed_time_ns <= r.decision_time_ns)
            ),
        ),
    )

    @property
    def net_pnl_amount(self) -> Decimal:
        """Executable net P&L: realised plus unrealised."""
        return self.realized_net_pnl_amount + self.unrealized_net_pnl_amount

    @property
    def net_r(self) -> Decimal | None:
        """Executable net P&L in multiples of the initial risk (null for external positions)."""
        if self.initial_risk_amount is None:
            return None
        return self.net_pnl_amount / self.initial_risk_amount


_POSITION_IMMUTABLE = (
    "position_id",
    "run_id",
    "mode",
    "owner",
    "external_ref",
    "symbol",
    "side",
    "initial_volume_lots",
    "initial_stop_price",
    "initial_risk_amount",
    "account_currency",
    "opened_time_ns",
    "plan_id",
    "opening_client_order_id",
)


def validate_position_transition(previous: Position | None, snapshot: Position) -> None:
    """Check that *snapshot* may follow *previous* in the life of one position.

    Enforces the monotonic rules of the master specification: the stop never
    widens and is never removed (G04-11), the executable net P&L peak never
    decreases (G04-13), and the position is never increased (G04-10).
    """

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal position transition: {rule}",
            position_id=snapshot.position_id,
            revision=snapshot.revision,
            rule=rule,
        )

    if previous is None:
        if snapshot.revision != 0:
            raise fail("the first snapshot of a position has revision 0")
        return
    if previous.state is PositionState.CLOSED:
        raise fail("no snapshot may follow a CLOSED position")
    if snapshot.revision != previous.revision + 1:
        raise fail("revision must increase by exactly 1")
    for name in _POSITION_IMMUTABLE:
        if getattr(previous, name) != getattr(snapshot, name):
            raise fail(f"{name} may not change during the life of a position")
    if snapshot.volume_lots > previous.volume_lots:
        raise fail("volume_lots may not increase (no averaging down, no adding to a position)")
    if not _stop_tightened_or_equal(snapshot.side, previous.stop_price, snapshot.stop_price) and (
        snapshot.state is PositionState.OPEN
    ):
        raise fail("the protective stop may not be widened or removed")
    if snapshot.peak_net_pnl_amount < previous.peak_net_pnl_amount:
        raise fail("peak_net_pnl_amount may not decrease")
    if (
        snapshot.decision_time_ns < previous.decision_time_ns
        or snapshot.as_of_time_ns < previous.as_of_time_ns
    ):
        raise fail("snapshot times may not decrease")
    if snapshot.costs_amount < previous.costs_amount:
        raise fail("costs_amount may not decrease")
