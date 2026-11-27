"""Position contract - snapshot of one position on the account."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import Dec, EnumOf, Id, Int, Invariant, Opt, Str, TimeNs, fld, inv
from gie.contracts.common import (
    CURRENCY_PATTERN,
    GOLD_SYMBOL,
    PRICE,
    REASON_CODE_PATTERN,
    REFERENCE_PATTERN,
    ZERO,
    Produced,
    Side,
    symbol_field,
)
from gie.contracts.registry import contract
from gie.core.canonical import EXACT
from gie.core.errors import IllegalTransitionError


class PositionOwner(str, Enum):
    """Who opened the position.

    ``EXTERNAL`` positions belong to other systems trading the same account
    (Gold Bot, AI trader, breakout bot, manual trades).  GIE observes them for
    account-wide risk (G05-12), never manages them, and never learns from them
    (G07-06: no manual-trade learning - learning uses GIE-owned positions only).
    Their snapshots report what is observed: GIE's own rules (stops only
    tighten, volume never grows) do not apply to them.
    """

    GIE = "GIE"
    EXTERNAL = "EXTERNAL"


class PositionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


_MONEY: Final[Dec] = Dec(label="money")


def _gie(position: Position) -> bool:
    return position.owner is PositionOwner.GIE


def _owner_fields(position: Position) -> bool:
    if _gie(position):
        return (
            position.symbol == GOLD_SYMBOL
            and position.plan_id is not None
            and position.opening_client_order_id is not None
            and position.initial_stop_price is not None
            and position.initial_risk_amount is not None
            and position.external_ref is None
            and position.position_id
            == identity.position_id(position.run_id, position.opening_client_order_id)
            and (position.state is PositionState.CLOSED or position.stop_price is not None)
            and (position.state is PositionState.CLOSED or position.mark_price is not None)
        )
    return (
        position.external_ref is not None
        and position.plan_id is None
        and position.opening_client_order_id is None
    )


def _stop_geometry(position: Position) -> bool:
    if not _gie(position) or position.initial_stop_price is None:
        return True
    buy = position.side is Side.BUY
    initial = position.initial_stop_price
    if not (initial < position.average_entry_price if buy else initial > position.average_entry_price):
        return False
    if position.stop_price is None:
        return True
    return position.stop_price >= initial if buy else position.stop_price <= initial


def _stop_tightened_or_equal(side: Side, before: Decimal | None, after: Decimal | None) -> bool:
    if before is None:
        return True
    if after is None:
        return False  # a stop may never be removed
    return after >= before if side is Side.BUY else after <= before


@final
@contract("Position", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Position(Produced):
    """State of one position, marked at the price at which it could be closed now.

    Stream: ``position/<producer>``; ``revision`` numbers the snapshots of one
    position from 0.  Ordering time: ``decision_time_ns``.

    *Executable net P&L* is what closing now would really yield: the open
    volume marked at the exit-side quote (bid for BUY, ask for SELL) minus
    estimated exit costs, plus everything already realised net of costs.
    ``peak_net_pnl_amount`` is its high-water mark and drives the profit ratchet;
    ``trough_net_pnl_amount`` is its low-water mark (adverse excursion).
    """

    position_id: str = fld(
        Id("pos"),
        "identity.position_id(run_id, opening_client_order_id) for GIE positions; "
        "identity.external_position_id(account alias, ticket) for EXTERNAL ones.",
    )
    revision: int = fld(Int(ge=0, label="revision"), "Snapshot number of this position: 0, 1, 2, ...")
    owner: PositionOwner = fld(EnumOf(PositionOwner), "GIE, or another system on the same account.")
    external_ref: str | None = fld(
        Opt(Str(pattern=REFERENCE_PATTERN, max_len=64, label="reference")),
        "Broker ticket of an EXTERNAL position; null for GIE positions.  Never an account number.",
    )
    symbol: str = symbol_field("XAUUSD for GIE positions; any instrument for EXTERNAL ones.")
    side: Side = fld(EnumOf(Side), "BUY = long, SELL = short.")
    state: PositionState = fld(EnumOf(PositionState), "OPEN or CLOSED.")
    volume_lots: Decimal = fld(Dec(ge="0", label="volume"), "Open volume; 0 when closed.", unit="lots")
    initial_volume_lots: Decimal = fld(
        Dec(gt="0", label="volume"),
        "Volume at opening (for EXTERNAL positions: the largest volume observed so far).",
        unit="lots",
    )
    average_entry_price: Decimal = fld(PRICE, "Volume-weighted entry price.", unit="quote currency per unit")
    initial_stop_price: Decimal | None = fld(
        Opt(PRICE), "Hard stop at opening; mandatory for GIE positions.", unit="quote currency per unit"
    )
    stop_price: Decimal | None = fld(
        Opt(PRICE),
        "Current hard protective stop (trigger of the working PROTECTIVE_STOP order).",
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
        "Highest executable net P&L (realized + unrealized) since opening.",
        unit="account currency",
    )
    peak_time_ns: int = fld(
        TimeNs(), "Decision time of the snapshot at which the peak was reached.", unit="ns since epoch, UTC"
    )
    trough_net_pnl_amount: Decimal = fld(
        _MONEY,
        "Lowest executable net P&L (realized + unrealized) since opening.",
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
    close_reason: str | None = fld(
        Opt(Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code")),
        "Why the position closed (e.g. PROTECTIVE_STOP, TAKE_PROFIT, RATCHET, TIME_EXIT); "
        "set if and only if a GIE position is CLOSED.",
    )
    plan_id: str | None = fld(Opt(Id("pln")), "Plan revision that opened a GIE position.")
    opening_client_order_id: str | None = fld(Opt(Id("ord")), "Entry order of a GIE position.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "POS-1",
            "state is OPEN iff volume_lots > 0 iff closed_time_ns is null",
            lambda r: (r.state is PositionState.OPEN) == (r.volume_lots > ZERO) == (r.closed_time_ns is None),
        ),
        inv(
            "POS-2",
            "volume_lots <= initial_volume_lots (a GIE position is never increased: no averaging "
            "down; for EXTERNAL positions initial_volume_lots is the largest observed volume)",
            lambda r: r.volume_lots <= r.initial_volume_lots,
        ),
        inv(
            "POS-3",
            "a GIE position is XAUUSD, has plan, opening order, initial stop and initial risk, has "
            "position_id = identity.position_id(run_id, opening_client_order_id) and, while open, "
            "a stop and a mark price; an EXTERNAL position has external_ref and no plan or opening order",
            _owner_fields,
        ),
        inv(
            "POS-4",
            "for a GIE position the initial stop is on the losing side of the entry and the stop is "
            "never wider than the initial stop (BUY: initial < entry and stop >= initial; SELL mirrored)",
            _stop_geometry,
        ),
        inv(
            "POS-5",
            "trough_net_pnl_amount <= realized + unrealized <= peak_net_pnl_amount",
            lambda r: (
                r.trough_net_pnl_amount
                <= EXACT.add(r.realized_net_pnl_amount, r.unrealized_net_pnl_amount)
                <= r.peak_net_pnl_amount
            ),
        ),
        inv(
            "POS-6",
            "a CLOSED position has unrealized_net_pnl_amount = 0",
            lambda r: r.state is PositionState.OPEN or r.unrealized_net_pnl_amount == ZERO,
        ),
        inv(
            "POS-7",
            "opened_time_ns <= peak_time_ns <= decision_time_ns, and opened_time_ns <= closed_time_ns "
            "<= decision_time_ns when closed",
            lambda r: (
                r.opened_time_ns <= r.peak_time_ns <= r.decision_time_ns
                and (r.closed_time_ns is None or r.opened_time_ns <= r.closed_time_ns <= r.decision_time_ns)
            ),
        ),
        inv(
            "POS-8",
            "close_reason is null while OPEN and set for a CLOSED GIE position",
            lambda r: (
                r.close_reason is None
                if r.state is PositionState.OPEN
                else (r.close_reason is not None or not _gie(r))
            ),
        ),
    )

    @property
    def net_pnl_amount(self) -> Decimal:
        """Executable net P&L: realised plus unrealised."""
        return EXACT.add(self.realized_net_pnl_amount, self.unrealized_net_pnl_amount)

    @property
    def net_r(self) -> float | None:
        """Executable net P&L in multiples of the initial risk (``None`` for external positions).

        An R multiple is a statistical quantity, so it is a float; the money amounts stay exact.
        """
        if self.initial_risk_amount is None:
            return None
        return float(self.net_pnl_amount) / float(self.initial_risk_amount)


#: Fields that never change during the life of any position.
POSITION_IMMUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "position_id",
    "run_id",
    "mode",
    "owner",
    "external_ref",
    "symbol",
    "side",
    "account_currency",
    "opened_time_ns",
    "plan_id",
    "opening_client_order_id",
)
#: Further fields that never change during the life of a GIE-owned position.
GIE_POSITION_IMMUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "initial_volume_lots",
    "average_entry_price",
    "initial_stop_price",
    "initial_risk_amount",
)


def validate_position_transition(previous: Position | None, snapshot: Position) -> None:
    """Check that *snapshot* may follow *previous* in the life of one position.

    For GIE-owned positions it enforces the monotonic rules of the master
    specification: the stop never widens and is never removed (G04-11), the
    executable net P&L peak never decreases (G04-13, G04-15) and the position is never
    increased (G04-10).  For EXTERNAL positions, which GIE only observes, it
    checks identity and time order and accepts whatever the other system did.
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
    for name in POSITION_IMMUTABLE_FIELDS:
        if getattr(previous, name) != getattr(snapshot, name):
            raise fail(f"{name} may not change during the life of a position")
    for name in ("decision_time_ns", "as_of_time_ns", "seq"):
        if getattr(snapshot, name) < getattr(previous, name):
            raise fail(f"{name} may not decrease")
    if snapshot.peak_net_pnl_amount < previous.peak_net_pnl_amount:
        raise fail("peak_net_pnl_amount may not decrease")
    if snapshot.trough_net_pnl_amount > previous.trough_net_pnl_amount:
        raise fail("trough_net_pnl_amount may not increase")
    if snapshot.peak_net_pnl_amount == previous.peak_net_pnl_amount:
        if snapshot.peak_time_ns != previous.peak_time_ns:
            raise fail("peak_time_ns may only change when a new peak is reached")
    elif snapshot.peak_time_ns != snapshot.decision_time_ns:
        raise fail("a new peak is stamped with the decision time of the snapshot that reached it")
    if snapshot.owner is not PositionOwner.GIE:
        if snapshot.initial_volume_lots < previous.initial_volume_lots:
            raise fail("the largest observed volume of an external position may not decrease")
        return
    for name in GIE_POSITION_IMMUTABLE_FIELDS:
        if getattr(previous, name) != getattr(snapshot, name):
            raise fail(f"{name} may not change during the life of a GIE position")
    if snapshot.volume_lots > previous.volume_lots:
        raise fail("volume_lots may not increase (no averaging down, no adding to a position)")
    if snapshot.state is PositionState.OPEN and not _stop_tightened_or_equal(
        snapshot.side, previous.stop_price, snapshot.stop_price
    ):
        raise fail("the protective stop may not be widened or removed")
    if snapshot.costs_amount < previous.costs_amount:
        raise fail("costs_amount may not decrease")
