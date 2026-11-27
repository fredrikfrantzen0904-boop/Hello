"""Canonical identity functions (requirement S00-ASG-22, "IDs").

Every identifier in the contracts is derived by one of these functions, so any
consumer can recompute and cross-check it.  Where all identity parts are fields
of the record itself, the record *verifies* its own identifier on construction.

Identity parts never include wall-clock readings taken at creation time, random
numbers or unpersisted counters; that is what makes a replay reproduce the
same identifiers byte for byte.

==================  ====================================================  ==============
identifier          identity parts                                        unique within
==================  ====================================================  ==============
run_id              mode, configuration SHA-256, label                    everything
market_event_id     kind, symbol, timeframe, anchor time, identity levels everything
evaluation_slot_id  engine, kind, symbol, timeframe, anchor time          everything
evidence_id         engine, market event (or slot), revision              everything
opportunity_id      setup type, side, horizon, market events              everything
candidate_id        opportunity, scoring model (or none), revision        everything
alternative_group   opportunity                                           everything
plan_id             candidate, entry style, variant, revision             everything
risk_decision_id    run, plan, attempt                                    everything
reservation_id      risk decision                                         everything
client_order_id     run, origin (risk decision or position), intent,      everything
                    attempt
broker_order_id     client order                                          everything
order_event_id      client order, order sequence                          everything
position_id         run, opening client order (or account alias, ticket)  everything
model_version_id    model name, version, artifact SHA-256                 everything
==================  ====================================================  ==============
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from gie.core.canonical import decimal_to_str
from gie.core.errors import CanonicalEncodingError, ContractValidationError
from gie.core.ids import make_id, require_id
from gie.core.routing_lock import parse_run_mode

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_REFERENCE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ContractValidationError(f"{what} must be non-empty printable text", value=repr(value)[:64])
    return value


def _count(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{what} must be an int >= 0", value=repr(value)[:64])
    return value


def run_id(mode: object, manifest_sha256: str) -> str:
    """Run identity: the mode and the hash of everything else the run manifest states.

    ``manifest_sha256`` is the content hash of every field of the ``RunManifest``
    except ``run_id`` itself (see ``gie.contracts.run.derive_run_id``), so the
    role, the configuration hash, code commit, data sources and period, seed,
    parameter hash and model schedule are all bound: a run id cannot be claimed
    for a run that differs in any of them, and two replays with identical
    manifests are, by definition, the same run.
    """
    run_mode = parse_run_mode(mode)
    if not isinstance(manifest_sha256, str) or not _SHA256_RE.fullmatch(manifest_sha256):
        raise ContractValidationError("manifest_sha256 must be 64 lower-case hex characters")
    return make_id("run", run_mode.value, manifest_sha256)


def market_event_id(
    kind: str,
    symbol: str,
    timeframe: str,
    anchor_time_ns: int,
    identity_prices: Iterable[Decimal],
) -> str:
    """Identity of a market event, shared by every producer that observes it.

    Two engines that report the same kind of event on the same symbol and
    timeframe, anchored at the same instant with the same set of identity level
    prices, obtain the same identifier.  Exact evidence de-duplication (G02-02)
    is equality of this identifier.
    """
    return make_id(
        "mev",
        _text(kind, "kind"),
        _text(symbol, "symbol"),
        _text(timeframe, "timeframe"),
        _count(anchor_time_ns, "anchor_time_ns"),
        _price_set(identity_prices),
    )


def _price_set(identity_prices: Iterable[Decimal]) -> list[Decimal]:
    """Identity level prices as a sorted list without duplicates (numerically equal = same level).

    Every price is validated *before* the set is built: NaN cannot be ordered, a signalling NaN
    cannot even be hashed, and neither they nor infinities or values outside the wire range are
    prices.  They are refused with a contract error instead of a ``decimal`` or ``TypeError``.
    """
    try:
        prices = list(identity_prices)
    except TypeError as exc:
        raise ContractValidationError("identity prices must be an iterable of Decimal values") from exc
    for price in prices:
        if not isinstance(price, Decimal):
            raise ContractValidationError(
                "identity prices must be Decimal values", actual_type=type(price).__name__
            )
        try:
            decimal_to_str(price)
        except CanonicalEncodingError as exc:
            raise ContractValidationError(
                "identity prices must be finite decimals within the supported range", value=str(price)[:64]
            ) from exc
    return sorted(set(prices))


def evaluation_slot_id(engine: str, kind: str, symbol: str, timeframe: str, anchor_time_ns: int) -> str:
    """Identity used by INACTIVE and MISSING evidence, which describes no market event.

    It contains the engine, so "engine A found nothing" and "engine B could not
    evaluate" can never be merged by de-duplication.
    """
    return make_id(
        "mev",
        "slot",
        _text(engine, "engine"),
        _text(kind, "kind"),
        _text(symbol, "symbol"),
        _text(timeframe, "timeframe"),
        _count(anchor_time_ns, "anchor_time_ns"),
    )


def evidence_id(engine: str, market_event: str, revision: int) -> str:
    return make_id(
        "evd", _text(engine, "engine"), require_id(market_event, "mev"), _count(revision, "revision")
    )


def opportunity_id(setup_type: str, side: str, horizon: str, market_event_ids: Iterable[str]) -> str:
    """Stable identity of one opportunity: setup, direction, horizon and the market events behind it.

    It does not change when evidence or the candidate is revised, when another
    engine confirms the same market event, or when another model scores it.
    """
    events = sorted({require_id(item, "mev") for item in market_event_ids})
    if not events:
        raise ContractValidationError("an opportunity needs at least one market event")
    return make_id(
        "opp", _text(setup_type, "setup_type"), _text(side, "side"), _text(horizon, "horizon"), events
    )


def candidate_id(opportunity: str, model_version: str | None, revision: int) -> str:
    """One scorer's view of an opportunity (``model_version`` is None for an unscored candidate)."""
    scorer = None if model_version is None else require_id(model_version, "mdl")
    return make_id("cnd", require_id(opportunity, "opp"), scorer, _count(revision, "revision"))


def alternative_group_id(opportunity: str) -> str:
    """Identity of the set of mutually exclusive plans built for one opportunity."""
    return make_id("grp", require_id(opportunity, "opp"))


def plan_id(candidate: str, entry_style: str, variant: int, revision: int) -> str:
    return make_id(
        "pln",
        require_id(candidate, "cnd"),
        _text(entry_style, "entry_style"),
        _count(variant, "variant"),
        _count(revision, "revision"),
    )


def risk_decision_id(run: str, plan: str, attempt: int) -> str:
    return make_id("rsk", require_id(run, "run"), require_id(plan, "pln"), _count(attempt, "attempt"))


def reservation_id(decision: str) -> str:
    return make_id("rsv", require_id(decision, "rsk"))


def client_order_id(run: str, origin_id: str, intent: str, attempt: int) -> str:
    """Idempotency key of an order.

    ``origin_id`` is the **risk decision id** for an entry (with ``attempt`` 0, so
    one approval can only ever produce one entry order) and the **position id**
    for protective and exit orders (``attempt`` numbers the orders of that intent
    for the position).  Re-sending after a crash recomputes the same key, so the
    simulated broker drops the duplicate (G08-07).
    """
    if not isinstance(origin_id, str) or origin_id[:4] not in ("rsk_", "pos_"):
        raise ContractValidationError("order origin must be a risk decision id or a position id")
    require_id(origin_id, origin_id[:3])
    return make_id(
        "ord", require_id(run, "run"), origin_id, _text(intent, "intent"), _count(attempt, "attempt")
    )


def broker_order_id(client_order: str) -> str:
    """Identifier assigned by the simulated broker when it accepts an order."""
    return make_id("brk", require_id(client_order, "ord"))


def order_event_id(client_order: str, order_seq: int) -> str:
    return make_id("oev", require_id(client_order, "ord"), _count(order_seq, "order_seq"))


def position_id(run: str, opening_client_order: str) -> str:
    return make_id("pos", require_id(run, "run"), require_id(opening_client_order, "ord"))


def external_position_id(account_alias: str, external_ref: str) -> str:
    """Identity of a position opened by another system on the same account.

    ``account_alias`` MUST be a locally configured alias, never the broker login
    or account number: the identifier is written to journals and evidence files.
    """
    for value, what in ((account_alias, "account_alias"), (external_ref, "external_ref")):
        if not isinstance(value, str) or not _REFERENCE_RE.fullmatch(value):
            raise ContractValidationError(f"{what} must match [A-Za-z0-9._:-]{{1,64}}")
    return make_id("pos", "external", account_alias, external_ref)


def model_version_id(model_name: str, semver: str, artifact_sha256: str) -> str:
    if not isinstance(artifact_sha256, str) or not _SHA256_RE.fullmatch(artifact_sha256):
        raise ContractValidationError("artifact_sha256 must be 64 lower-case hex characters")
    return make_id("mdl", _text(model_name, "model_name"), _text(semver, "semver"), artifact_sha256)
