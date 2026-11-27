"""Canonical identity functions (requirement S00-ASG-22, "IDs").

Every identifier in the contracts is derived by one of these functions, so any
consumer can recompute and cross-check it.  Where all identity parts are fields
of the record itself, the record *verifies* its own identifier on construction.

Identity parts never include wall-clock readings taken at creation time, random
numbers or unpersisted counters; that is what makes a replay reproduce the
same identifiers byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from gie.core.ids import make_id


def run_id(mode: str, config_sha256: str, label: str) -> str:
    """Run identity: mode, SHA-256 of the complete run configuration, operator label.

    The configuration hash must cover data range, seeds, parameters and code
    commit, so identical inputs always give the identical run id.
    """
    return make_id("run", mode, config_sha256, label)


def market_event_id(
    kind: str,
    symbol: str,
    timeframe: str,
    anchor_time_ns: int,
    level_prices: Iterable[Decimal],
) -> str:
    """Identity of a market event, shared by every producer that observes it.

    Two engines that report the same kind of event on the same symbol and
    timeframe, anchored at the same instant with the same set of price levels,
    obtain the same identifier.  Exact evidence de-duplication (G02-02) is
    equality of this identifier.
    """
    prices = sorted(set(level_prices))
    return make_id("mev", kind, symbol, timeframe, anchor_time_ns, list(prices))


def evidence_id(engine: str, market_event: str, revision: int) -> str:
    return make_id("evd", engine, market_event, revision)


def candidate_id(setup_type: str, side: str, evidence_ids: Iterable[str], revision: int) -> str:
    return make_id("cnd", setup_type, side, sorted(set(evidence_ids)), revision)


def alternative_group_id(candidate: str) -> str:
    """Identity of the set of mutually exclusive plans built for one candidate."""
    return make_id("grp", candidate)


def plan_id(candidate: str, entry_style: str, variant: int, revision: int) -> str:
    return make_id("pln", candidate, entry_style, variant, revision)


def risk_decision_id(run: str, plan: str, attempt: int) -> str:
    return make_id("rsk", run, plan, attempt)


def reservation_id(decision: str) -> str:
    return make_id("rsv", decision)


def client_order_id(run: str, origin_id: str, intent: str, attempt: int) -> str:
    """Idempotency key of an order.

    ``origin_id`` is the plan id for entries and the position id for protective
    and exit orders.  Re-sending after a crash recomputes the same key, so the
    (simulated) broker can drop the duplicate (G08-07).
    """
    return make_id("ord", run, origin_id, intent, attempt)


def broker_order_id(client_order: str) -> str:
    """Identifier assigned by the simulated broker when it accepts an order."""
    return make_id("brk", client_order)


def order_event_id(client_order: str, order_seq: int) -> str:
    return make_id("oev", client_order, order_seq)


def position_id(run: str, opening_client_order: str) -> str:
    return make_id("pos", run, opening_client_order)


def external_position_id(account_ref: str, external_ref: str) -> str:
    """Identity of a position opened by another system on the same account."""
    return make_id("pos", "external", account_ref, external_ref)


def model_version_id(model_name: str, semver: str, artifact_sha256: str) -> str:
    return make_id("mdl", model_name, semver, artifact_sha256)
