"""Behavioural surface of the contracts, published and locked with the schemas.

A JSON Schema describes the shape of a message.  The rules that give a contract
its meaning - which stream it belongs to, how its identifier is derived, which
fields never change, which lifecycle transitions exist - live in code.  This
module renders those rules as plain data so that they are published inside the
schema files (keyword ``x-gie-behaviour``) and covered by the same lock and
evolution check: changing a transition table or an identity rule changes the
schema and therefore needs a MAJOR version bump.
"""

from __future__ import annotations

from typing import Any

from gie.contracts._spec import Record
from gie.contracts.candidate import CANDIDATE_STATUS_TRANSITIONS
from gie.contracts.model_version import MODEL_IMMUTABLE_FIELDS, MODEL_STATUS_TRANSITIONS
from gie.contracts.order_event import (
    ORDER_IMMUTABLE_FIELDS,
    ORDER_REPLACEABLE_FIELDS,
    ORDER_REQUEST_EVENTS,
    ORDER_TRANSITIONS,
    TERMINAL_ORDER_STATES,
)
from gie.contracts.ordering import STREAMS
from gie.contracts.position import GIE_POSITION_IMMUTABLE_FIELDS, POSITION_IMMUTABLE_FIELDS
from gie.contracts.trade_plan import PLAN_STATUS_TRANSITIONS
from gie.core.ordering import StreamRank

#: contract name -> how its identifiers are derived (documentation of gie.contracts.identity).
IDENTITY_RULES: dict[str, dict[str, str]] = {
    "Tick": {"identity": "(source, symbol, seq)"},
    "Evidence": {
        "market_event_id": "market_event_id(kind, symbol, timeframe, anchor_time_ns, identity level prices); "
        "evaluation_slot_id(engine, kind, symbol, timeframe, anchor_time_ns) for INACTIVE and MISSING",
        "evidence_id": "evidence_id(engine, market_event_id, revision)",
    },
    "Candidate": {
        "opportunity_id": "opportunity_id(setup_type, side, horizon, market_event_ids)",
        "candidate_id": "candidate_id(opportunity_id, model_version_id, revision)",
    },
    "TradePlan": {
        "plan_id": "plan_id(candidate_id, entry_style, variant, revision)",
        "alternative_group_id": "alternative_group_id(opportunity_id)",
    },
    "RiskDecision": {
        "decision_id": "risk_decision_id(run_id, plan_id, attempt)",
        "reservation_id": "reservation_id(decision_id)",
        "plan_fingerprint": "fingerprint(plan): sha256 of contract name, MAJOR and fields",
    },
    "OrderEvent": {
        "client_order_id": "client_order_id(run_id, risk_decision_id, intent, 0) for ENTRY; "
        "client_order_id(run_id, position_id, intent, attempt) otherwise",
        "order_event_id": "order_event_id(client_order_id, order_seq)",
        "broker_order_id": "broker_order_id(client_order_id)",
    },
    "Position": {
        "position_id": "position_id(run_id, opening_client_order_id) for GIE; "
        "external_position_id(account alias, external_ref) for EXTERNAL"
    },
    "ModelVersion": {"model_version_id": "model_version_id(model_name, semver, artifact_sha256)"},
    "Clock": {"identity": "(producer, seq)"},
    "RunManifest": {"run_id": "run_id(mode, config_sha256, label)"},
}


def _transitions(table: dict[Any, frozenset[Any]]) -> list[dict[str, Any]]:
    rows = []
    for before, after in table.items():
        rows.append(
            {
                "from": None if before is None else before.value,
                "to": sorted(state.value for state in after),
            }
        )
    return sorted(rows, key=lambda row: "" if row["from"] is None else row["from"])


def behaviour(record_type: type[Record]) -> dict[str, Any]:
    """Behavioural description of one top-level contract (plain JSON data)."""
    name = record_type.CONTRACT
    assert name is not None
    prefix, time_field, rank = STREAMS[record_type]
    out: dict[str, Any] = {
        "stream": f"{prefix}/<source>/<symbol>" if name == "Tick" else f"{prefix}/<producer>",
        "ordering_time": time_field,
        "rank": {"name": rank.name, "value": int(rank)},
        "identity": IDENTITY_RULES[name],
    }
    if name == "OrderEvent":
        out["rank_of_requests"] = {
            "name": StreamRank.ORDER_REQUEST.name,
            "value": int(StreamRank.ORDER_REQUEST),
        }
        out["request_events"] = sorted(event.value for event in ORDER_REQUEST_EVENTS)
        out["terminal_states"] = sorted(state.value for state in TERMINAL_ORDER_STATES)
        out["immutable_fields"] = list(ORDER_IMMUTABLE_FIELDS)
        out["replaceable_fields"] = list(ORDER_REPLACEABLE_FIELDS)
        out["transitions"] = sorted(
            (
                {
                    "from": None if before is None else before.value,
                    "event": event.value,
                    "to": sorted(state.value for state in after),
                }
                for (before, event), after in ORDER_TRANSITIONS.items()
            ),
            key=lambda row: ("" if row["from"] is None else str(row["from"]), str(row["event"])),
        )
    elif name == "Position":
        out["immutable_fields"] = list(POSITION_IMMUTABLE_FIELDS)
        out["immutable_fields_gie_owner"] = list(GIE_POSITION_IMMUTABLE_FIELDS)
        out["monotonic_gie_owner"] = [
            "volume_lots never increases",
            "stop_price never moves away from the entry and is never removed while open",
            "peak_net_pnl_amount never decreases",
            "trough_net_pnl_amount never increases",
        ]
    elif name == "ModelVersion":
        out["immutable_fields"] = list(MODEL_IMMUTABLE_FIELDS)
        out["transitions"] = _transitions(MODEL_STATUS_TRANSITIONS)
    elif name == "TradePlan":
        out["transitions"] = _transitions(PLAN_STATUS_TRANSITIONS)
        out["monotonic"] = ["stop_price never moves away from the entry between revisions"]
    elif name == "Candidate":
        out["transitions"] = _transitions(CANDIDATE_STATUS_TRANSITIONS)
    elif name == "Evidence":
        out["final_states"] = ["EXPIRED", "INVALIDATED"]
        out["monotonic"] = ["identity levels and market_event_id never change between revisions"]
    return out
