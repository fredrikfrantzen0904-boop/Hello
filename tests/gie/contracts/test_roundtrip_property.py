"""Property-based part of acceptance test S00-ACC-01 "Contract round-trip tests".

Hypothesis generates *valid mutations* of the labelled sample records and checks, for every one
of them, the round-trip rules of ``specifications/SHARED_CONTRACTS.md`` section 7:

* ``from_json(to_json(r)) == r`` and ``to_json(from_json(text)) == text`` (canonical bytes);
* exact decimals (integers scaled by powers of ten, up to 18 integer and 12 fractional digits),
  floats within their bounds including ``1.0`` and 1e19-scale values that are written as integer
  literals, instants up to the largest instant ``2**63 - 1``;
* lists whose order carries no meaning are stored and written in their canonical order whatever
  order the producer (or the wire) supplies;
* optional fields set to ``None`` are written as explicit ``null``;
* the wire form validates against the generated JSON Schema.

The runs are deterministic (``derandomize=True``, no example database, no deadline).

All records are SYNTHETIC FIXTURES derived from ``tests/gie/samples.py``.  The generated prices,
sizes, estimates and times are arbitrary numbers: not market data, not broker data and not
evidence of any trading performance.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from decimal import Decimal
from functools import cache
from typing import Any

import jsonschema
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gie.contracts import (
    ClockMode,
    ContractRef,
    Contribution,
    DailyState,
    Destination,
    ModelStatus,
    NamedValue,
    OrderEventType,
    PlanStatus,
    PriceLevel,
    QuantilePoint,
    Record,
    RiskOutcome,
    RunManifest,
    RunMode,
    RunModel,
    Target,
    TickFlag,
    derive_run_id,
    fingerprint,
    from_json,
    from_wire,
    to_bytes,
    to_json,
    to_wire,
)
from gie.contracts._spec import Dec, FieldType, ListOf, Obj, Opt
from gie.contracts.registry import all_contracts
from gie.contracts.schema_gen import json_schema
from gie.core.canonical import canonical_dumps, decimal_to_str, fraction_of, strict_loads
from gie.core.errors import ContractValidationError
from gie.core.timeutil import MAX_TIME_NS
from tests.gie import samples

PROPERTY = settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.too_slow],
)

T0 = samples.T0

# --------------------------------------------------------------------------- strategies
#: Instants over the whole range, with the boundaries and the first integers beyond 2**53.
instants = st.one_of(
    st.integers(0, MAX_TIME_NS),
    st.sampled_from([0, 1, 2**53 - 1, 2**53, 2**53 + 1, MAX_TIME_NS - 1, MAX_TIME_NS]),
)
counts = st.one_of(st.integers(0, 2**63 - 1), st.sampled_from([0, 1, 2**53 + 1, 2**63 - 1]))


def scaled(units: int, scale: int) -> Decimal:
    """``units * 10**-scale`` as an exact decimal (never built from a float)."""
    return Decimal(f"{units}E-{scale}")


def decimals(min_units: int, max_units: int, max_scale: int = 12) -> st.SearchStrategy[Decimal]:
    return st.builds(scaled, st.integers(min_units, max_units), st.integers(0, max_scale))


#: Positive exact decimals from 0.000000000001 up to 18 integer digits.
prices = st.one_of(
    decimals(1, 10**18 - 1),
    st.sampled_from(
        [
            Decimal("0.000000000001"),
            Decimal("999999999999999999.999999999999"),
            Decimal("999999999999999999"),
            Decimal("2650.10"),
            Decimal("1E+17"),
        ]
    ),
)
volumes = st.one_of(decimals(0, 10**18 - 1), st.sampled_from([Decimal(0), Decimal("-0"), Decimal("0.00")]))
money = decimals(-(10**18) + 1, 10**18 - 1)


def _in_integer_band(value: float) -> bool:
    """Doubles strictly between 2**53 and 2**63 in magnitude (all of them are integers)."""
    return 2.0**53 < abs(value) < 2.0**63


SPECIAL_FLOATS = [
    0.0,
    -0.0,
    1.0,
    -1.0,
    0.1,
    1 / 3,
    1e-7,
    5e-324,
    2.0**53,
    1e18,
    1.5e18,
    2.0**63,
    1e19,
    -1e19,
    1.8446744073709552e19,
    1e21,
    1e22,
    1.7976931348623157e308,
    -1.7976931348623157e308,
]
any_finite_float = st.floats(allow_nan=False, allow_infinity=False)
#: Finite doubles outside the open band (2**53, 2**63), plus hand-picked values inside and around it.
floats = st.one_of(
    any_finite_float.filter(lambda x: not _in_integer_band(x)), st.sampled_from(SPECIAL_FLOATS)
)
non_negative_floats = st.one_of(
    st.floats(min_value=0.0, allow_nan=False, allow_infinity=False).filter(lambda x: not _in_integer_band(x)),
    st.sampled_from([0.0, 1.0, 1e19, 1e22, 2.0**63]),
)
positive_floats = st.one_of(
    st.floats(min_value=0.0, exclude_min=True, allow_nan=False, allow_infinity=False).filter(
        lambda x: not _in_integer_band(x)
    ),
    st.sampled_from([5e-324, 1.0, 1e19, 1e22]),
)
unit_floats = st.one_of(st.floats(0.0, 1.0), st.sampled_from([0.0, 1.0, 0.5, 5e-324, 0.9999999999999999]))

CODE_NAMES = [
    "aa",
    "bars_available",
    "lag_seconds",
    "oos_mean_net_r",
    "range_minutes",
    "session_london",
    "spread_ratio",
    "sweep_depth",
    "volatility_z",
    "zz_9",
]
REASON_CODES = ["AA", "DAILY_PROFIT_FLOOR", "FIXTURE_NOTE", "FIXTURE_REJECTION", "STALE_CANDIDATE", "ZZ_9"]
LEVEL_ROLES = ["FVG_HIGH", "FVG_LOW", "OBSERVED_VALUE", "RANGE_HIGH", "RANGE_LOW", "SWEPT_LEVEL"]
FEED_IDS = ["a", "fixture", "fixture-b", "fixture.v2", "fixture:2026", "z9_feed"]
CONTRACT_NAMES = [
    "Candidate",
    "Clock",
    "Evidence",
    "OrderEvent",
    "Position",
    "RiskDecision",
    "Tick",
    "TradePlan",
]


def ids(prefix: str) -> st.SearchStrategy[str]:
    return st.integers(0, 2**128 - 1).map(lambda number: f"{prefix}_{number:032x}")


def shuffled_subset(
    pool: list[Any], *, min_size: int = 0, max_size: int | None = None
) -> st.SearchStrategy[Any]:
    """A subset of *pool* in an arbitrary order."""
    return st.lists(st.sampled_from(pool), unique=True, min_size=min_size, max_size=max_size).flatmap(
        st.permutations
    )


named_values = st.lists(st.sampled_from(CODE_NAMES), unique=True, max_size=len(CODE_NAMES)).flatmap(
    lambda names: st.tuples(
        *[st.builds(NamedValue, name=st.just(name), value=st.none() | floats) for name in names]
    )
)


# --------------------------------------------------------------------------- shared assertions
@cache
def _validator(contract: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json_schema(all_contracts()[contract]))


def _unwrap(ftype: FieldType) -> FieldType:
    return ftype.inner if isinstance(ftype, Opt) else ftype


def assert_round_trip(original: Record) -> dict[str, Any]:
    """The round-trip rules of section 7 for one record; returns its wire form."""
    cls = type(original)
    assert cls.CONTRACT is not None
    text = to_json(original)
    back = from_json(text)
    assert type(back) is cls
    assert back == original
    assert hash(back) == hash(original)
    assert to_json(back) == text, "canonical bytes identity"
    assert to_bytes(original) == text.encode("utf-8")
    wire = to_wire(original)
    assert from_wire(wire) == original
    parsed = strict_loads(text)
    # The text does not say whether a number without a fraction was an integer or a double
    # (gie.core.canonical): an integral double beyond 2**53 is read back as an integer literal and
    # the field type decides.  Parsed text and wire form are therefore the same JSON value.
    assert canonical_dumps(parsed) == text == canonical_dumps(wire)
    assert_same_json(parsed, wire)
    assert from_wire(parsed) == original
    assert fingerprint(back) == fingerprint(original)
    assert wire["contract"] == cls.CONTRACT
    assert wire["schema_version"] == cls.VERSION
    assert set(wire) == {"contract", "schema_version"} | {spec.name for spec in cls.FIELD_SPECS}
    for spec in cls.FIELD_SPECS:
        assert (wire[spec.name] is None) is (getattr(original, spec.name) is None)
    errors = list(_validator(cls.CONTRACT).iter_errors(parsed))
    assert errors == [], errors[0].message
    return wire


def assert_same_json(parsed: Any, wire: Any, path: str = "") -> None:
    """*parsed* (text read back) is the JSON value *wire*; an int literal stands for a double only
    when it is the canonical rendering of that double (``_spec.Float``)."""
    if isinstance(wire, dict):
        assert type(parsed) is dict, path
        assert set(parsed) == set(wire), path
        for key, value in wire.items():
            assert_same_json(parsed[key], value, f"{path}.{key}")
    elif isinstance(wire, list):
        assert type(parsed) is list, path
        assert len(parsed) == len(wire), path
        for index, (left, right) in enumerate(zip(parsed, wire, strict=True)):
            assert_same_json(left, right, f"{path}[{index}]")
    elif type(wire) is float and type(parsed) is int:
        assert str(parsed) == canonical_dumps(wire), path
        assert float(parsed) == wire, path
    else:
        assert type(parsed) is type(wire), path
        assert parsed == wire, path


def replace(item: Record, **changes: Any) -> Record:
    """``dataclasses.replace`` that keeps a RunManifest valid: its ``run_id`` is derived (RUN-1)."""
    if isinstance(item, RunManifest) and "run_id" not in changes:
        fields = {spec.name: getattr(item, spec.name) for spec in RunManifest.FIELD_SPECS}
        fields.pop("run_id")
        fields.update(changes)
        return RunManifest.create(**fields)
    return dataclasses.replace(item, **changes)  # type: ignore[type-var]


def assert_same_record(left: Record, right: Record) -> None:
    assert left == right
    assert hash(left) == hash(right)
    assert to_json(left) == to_json(right)
    assert fingerprint(left) == fingerprint(right)


def _sorted_by(items: tuple[Any, ...], key: Callable[[Any], Any]) -> bool:
    keys = [key(item) for item in items]
    return keys == sorted(keys)


# =========================================================================== one property per contract
@pytest.mark.requirement("S00-ACC-01", "S00-ASG-11", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_tick_round_trip_property(data: st.DataObject) -> None:
    bid, ask = data.draw(prices, label="bid"), data.draw(prices, label="ask")
    event, receive = data.draw(instants, label="event"), data.draw(instants, label="receive")
    free = [flag for flag in TickFlag if flag not in (TickFlag.CROSSED, TickFlag.EVENT_AFTER_RECEIVE)]
    flags = list(data.draw(st.lists(st.sampled_from(free), unique=True), label="flags"))
    if ask < bid:
        flags.append(TickFlag.CROSSED)
    if event > receive:
        flags.append(TickFlag.EVENT_AFTER_RECEIVE)
    fields = {
        "seq": data.draw(counts, label="seq"),
        "event_time_ns": event,
        "receive_time_ns": receive,
        "bid": bid,
        "ask": ask,
        "bid_volume": data.draw(st.none() | volumes, label="bid_volume"),
        "ask_volume": data.draw(st.none() | volumes, label="ask_volume"),
        "symbol": data.draw(
            st.sampled_from(["XAUUSD", "XAGUSD", "US10Y_REAL", "A", "9.Z-_"]), label="symbol"
        ),
    }
    tick = samples.tick(**fields, flags=tuple(data.draw(st.permutations(flags), label="order")))
    wire = assert_round_trip(tick)
    assert tick.flags == tuple(sorted(flags, key=lambda flag: flag.value)), "sorted by value"
    assert_same_record(tick, samples.tick(**fields, flags=sorted(flags, key=lambda flag: flag.value)))
    assert tick.receive_time_ns == receive
    assert wire["receive_time_ns"] == receive
    assert Decimal(wire["bid"]) == bid
    assert type(wire["bid"]) is str
    reordered = {**wire, "flags": list(data.draw(st.permutations(wire["flags"]), label="wire order"))}
    assert_same_record(from_wire(reordered), tick)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-12", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_evidence_round_trip_property(data: st.DataObject) -> None:
    observable, as_of, decision = sorted(
        data.draw(st.tuples(*[st.integers(0, MAX_TIME_NS - 1)] * 3), label="times")
    )
    level_keys = data.draw(
        st.lists(st.tuples(st.sampled_from(LEVEL_ROLES), prices), unique=True, max_size=6), label="levels"
    )
    levels = [
        PriceLevel(role=role, price=price, identity=data.draw(st.booleans(), label="identity"))
        for role, price in level_keys
    ]
    features = data.draw(named_values, label="features")
    fields = {
        "decision_time_ns": decision,
        "as_of_time_ns": as_of,
        "observable_time_ns": observable,
        "valid_until_ns": data.draw(st.integers(decision + 1, MAX_TIME_NS), label="valid_until"),
        "anchor_time_ns": data.draw(instants, label="anchor"),
        "bullish_score": data.draw(unit_floats, label="bullish"),
        "bearish_score": data.draw(unit_floats, label="bearish"),
        "seq": data.draw(counts, label="seq"),
        "invalidation": data.draw(st.none() | st.just(samples.evidence().invalidation), label="invalidation"),
    }
    evidence = samples.evidence(
        **fields,
        levels=tuple(data.draw(st.permutations(levels), label="level order")),
        features=tuple(data.draw(st.permutations(features), label="feature order")),
    )
    wire = assert_round_trip(evidence)
    assert _sorted_by(evidence.features, lambda item: item.name)
    assert _sorted_by(evidence.levels, lambda item: (item.role, item.price))
    assert_same_record(
        evidence,
        samples.evidence(
            **fields,
            levels=sorted(levels, key=lambda item: (item.role, item.price)),
            features=sorted(features, key=lambda item: item.name),
        ),
    )
    for item, raw in zip(evidence.features, wire["features"], strict=True):
        assert (raw["value"] is None) is (item.value is None), "unknown stays null, never 0"
    reordered = copy.deepcopy(wire)
    reordered["features"] = list(data.draw(st.permutations(wire["features"]), label="wire features"))
    reordered["levels"] = list(data.draw(st.permutations(wire["levels"]), label="wire levels"))
    assert_same_record(from_wire(reordered), evidence)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-13", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_candidate_round_trip_property(data: st.DataObject) -> None:
    quantile_levels = sorted(
        data.draw(st.lists(st.floats(0.0, 1.0, exclude_min=True, exclude_max=True), unique=True, max_size=5))
    )
    quantile_values = sorted(
        data.draw(st.lists(floats, min_size=len(quantile_levels), max_size=len(quantile_levels)))
    )
    destination_keys = data.draw(
        st.lists(st.tuples(st.sampled_from(CODE_NAMES), st.sampled_from(CODE_NAMES)), unique=True, max_size=5)
    )
    destinations = []
    for scenario, label in destination_keys:
        estimated = data.draw(st.booleans())
        destinations.append(
            Destination(
                scenario=scenario,
                label=label,
                price=data.draw(prices),
                probability_frac=data.draw(unit_floats) if estimated else None,
                horizon_ns=data.draw(instants) if estimated else None,
            )
        )
    contributions = [
        Contribution(evidence_id=evidence_id, feature=None, weight_r=data.draw(floats))
        for evidence_id in data.draw(st.lists(ids("evd"), unique=True, max_size=4))
    ] + [
        Contribution(evidence_id=None, feature=feature, weight_r=data.draw(floats))
        for feature in data.draw(st.lists(st.sampled_from(CODE_NAMES), unique=True, max_size=4))
    ]
    market_events = data.draw(
        st.lists(ids("mev"), unique=True, min_size=1, max_size=5), label="market events"
    )
    evidence_ids = data.draw(st.lists(ids("evd"), unique=True, min_size=1, max_size=5), label="evidence")
    fields = {
        "expected_net_r": data.draw(floats, label="expected_net_r"),
        "net_r_stdev": data.draw(st.none() | non_negative_floats, label="stdev"),
        "expected_net_r_stderr": data.draw(non_negative_floats, label="stderr"),
        "support_count": data.draw(st.none() | counts, label="support"),
        "net_r_quantiles": tuple(
            QuantilePoint(q_frac=q, value_r=v) for q, v in zip(quantile_levels, quantile_values, strict=True)
        ),
        "win_probability_frac": data.draw(st.none() | unit_floats, label="win probability"),
        "expected_win_r": data.draw(st.none() | non_negative_floats, label="win"),
        "expected_loss_r": data.draw(st.none() | non_negative_floats, label="loss"),
        "expected_cost_r": data.draw(st.none() | non_negative_floats, label="cost"),
        "expected_holding_time_ns": data.draw(st.none() | instants, label="holding"),
        "quality_score": data.draw(st.none() | unit_floats, label="quality"),
        "baseline_r": data.draw(st.none() | floats, label="baseline"),
        "reference_price": data.draw(prices, label="reference"),
        "risk_unit_price": data.draw(prices, label="risk unit"),
    }
    candidate = samples.candidate(
        **fields,
        market_event_ids=tuple(data.draw(st.permutations(market_events))),
        evidence_ids=tuple(data.draw(st.permutations(evidence_ids))),
        destinations=tuple(data.draw(st.permutations(destinations))),
        contributions=tuple(data.draw(st.permutations(contributions))),
    )
    wire = assert_round_trip(candidate)
    assert candidate.market_event_ids == tuple(sorted(market_events))
    assert candidate.evidence_ids == tuple(sorted(evidence_ids))
    assert _sorted_by(candidate.destinations, lambda item: (item.scenario, item.label))
    assert _sorted_by(candidate.contributions, lambda item: item.key)
    assert [point.q_frac for point in candidate.net_r_quantiles] == quantile_levels, "quantile order is kept"
    assert_same_record(
        candidate,
        samples.candidate(
            **fields,
            market_event_ids=sorted(market_events),
            evidence_ids=sorted(evidence_ids),
            destinations=destinations,
            contributions=contributions,
        ),
    )
    reordered = copy.deepcopy(wire)
    for name in ("market_event_ids", "evidence_ids", "destinations", "contributions"):
        reordered[name] = list(data.draw(st.permutations(wire[name]), label=f"wire {name}"))
    assert_same_record(from_wire(reordered), candidate)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-14", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_trade_plan_round_trip_property(data: st.DataObject) -> None:
    # A BUY limit plan of the fixture: entry 2649.80, stop 2647.80; targets lie above the entry.
    offsets = data.draw(st.lists(st.integers(1, 10**9), unique=True, min_size=1, max_size=8), label="targets")
    shares = data.draw(
        st.lists(st.integers(0, 1000), min_size=len(offsets), max_size=len(offsets)), label="shares"
    )
    total = sum(shares)
    if total > 1000:
        shares = [share * 1000 // total for share in shares]
    # PLN-15: no two targets have the same (scenario, label); the labels are drawn distinct.
    labels = data.draw(
        st.lists(st.sampled_from(CODE_NAMES), unique=True, min_size=len(offsets), max_size=len(offsets)),
        label="labels",
    )
    targets = [
        Target(
            label=label,
            scenario=data.draw(st.none() | st.sampled_from(CODE_NAMES), label="scenario"),
            price=scaled(264_980 + offset, 2),
            close_frac=scaled(share, 3),
        )
        for offset, share, label in zip(offsets, shares, labels, strict=True)
    ]
    reasons = data.draw(shuffled_subset(REASON_CODES, min_size=1), label="reasons")
    selected = data.draw(st.booleans(), label="selected")
    fields: dict[str, Any] = {
        "expected_net_r": data.draw(positive_floats if selected else floats, label="expected_net_r"),
        "net_r_stdev": data.draw(st.none() | non_negative_floats, label="stdev"),
        "expected_net_r_stderr": data.draw(non_negative_floats, label="stderr"),
        "expected_cost_r": data.draw(non_negative_floats, label="cost"),
        "baseline_r": data.draw(st.none() | floats, label="baseline"),
        "support_count": data.draw(st.none() | counts, label="support"),
        "max_spread": data.draw(st.none() | prices, label="max_spread"),
        "max_entry_slippage": data.draw(st.none() | volumes, label="slippage"),
        "overnight_allowed": data.draw(st.booleans(), label="overnight"),
        "variant": data.draw(st.integers(0, 255), label="variant"),
    }
    if not selected:
        fields.update(status=PlanStatus.REJECTED, status_reasons=tuple(reasons), valid_until_ns=T0)
    plan = samples.trade_plan(**fields, targets=tuple(data.draw(st.permutations(targets), label="order")))
    wire = assert_round_trip(plan)
    assert _sorted_by(plan.targets, lambda item: item.price), "sorted by price"
    assert len(plan.targets) == len(targets)
    if not selected:
        assert plan.status_reasons == tuple(sorted(reasons))
    assert_same_record(
        plan, samples.trade_plan(**fields, targets=sorted(targets, key=lambda item: item.price))
    )
    reordered = {**wire, "targets": list(data.draw(st.permutations(wire["targets"]), label="wire order"))}
    assert_same_record(from_wire(reordered), plan)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-15", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_risk_decision_round_trip_property(data: st.DataObject) -> None:
    # D-028 "amounts decide, fractions report": the amounts are drawn, the fractions are derived from
    # them with gie.core.canonical.fraction_of (RSK-9); risk_frac <= 1, so risk_amount <= reference equity.
    equity_units = data.draw(st.integers(100, 10**14), label="reference equity in cents")
    approved = data.draw(st.booleans(), label="approved")
    amount_units = data.draw(st.integers(1, equity_units * 10**6), label="risk amount in 1e-8")
    reasons = data.draw(shuffled_subset(REASON_CODES, min_size=1), label="reasons")
    decision_time = data.draw(st.integers(0, MAX_TIME_NS - 1), label="decision")
    margin_free = data.draw(decimals(0, 10**17), label="margin free")
    fields: dict[str, Any] = {
        "reference_equity_amount": scaled(equity_units, 2),
        "daily_state": DailyState.NORMAL,
        "daily_pnl_amount": data.draw(money, label="daily pnl"),
        "profit_floor_amount": None,
        "decision_time_ns": decision_time,
        "as_of_time_ns": data.draw(st.integers(0, decision_time), label="as_of"),
        "valid_until_ns": data.draw(st.integers(decision_time + 1, MAX_TIME_NS), label="valid_until"),
        "attempt": data.draw(st.integers(0, 65535), label="attempt"),
        "seq": data.draw(counts, label="seq"),
    }
    if approved:
        # RSK-13: an approval states every aggregate it rests on, and margin_required <= margin_free.
        fields.update(
            outcome=RiskOutcome.APPROVED_REDUCED,
            risk_amount=scaled(amount_units, 8),
            volume_lots=data.draw(prices, label="volume"),
            equity_amount=data.draw(money, label="equity"),
            open_risk_amount=data.draw(volumes, label="open risk"),
            pending_risk_amount=data.draw(volumes, label="pending risk"),
            margin_free_amount=margin_free,
            margin_required_amount=data.draw(
                decimals(0, 10**17).filter(lambda value: value <= margin_free), label="margin required"
            ),
            gap_stress_loss_amount=data.draw(volumes, label="gap stress"),
        )
    else:
        # RSK-2: a rejection grants nothing; the aggregates may be unknown (null).
        fields.update(
            outcome=RiskOutcome.REJECTED,
            risk_amount=Decimal(0),
            volume_lots=Decimal(0),
            equity_amount=data.draw(st.none() | money, label="equity"),
            open_risk_amount=data.draw(st.none() | volumes, label="open risk"),
            pending_risk_amount=data.draw(st.none() | volumes, label="pending risk"),
            margin_free_amount=data.draw(st.none() | money, label="margin free"),
            margin_required_amount=None,
            gap_stress_loss_amount=data.draw(st.none() | volumes, label="gap stress"),
        )
    decision = samples.risk_decision(**fields, reason_codes=tuple(reasons))
    wire = assert_round_trip(decision)
    assert decision.reason_codes == tuple(sorted(reasons)), "sorted ascending"
    assert_same_record(decision, samples.risk_decision(**fields, reason_codes=sorted(reasons)))
    assert wire["trading_day"] == decision.trading_day
    assert Decimal(wire["risk_amount"]) == fields["risk_amount"]
    equity = fields["reference_equity_amount"]
    assert wire["risk_frac"] == decimal_to_str(fraction_of(fields["risk_amount"], equity, round_up=True))
    assert wire["daily_pnl_frac"] == decimal_to_str(
        fraction_of(fields["daily_pnl_amount"], equity, round_up=False)
    )
    assert wire["profit_floor_frac"] is None
    assert (wire["reservation_id"] is None) is (not approved)
    reordered = {**wire, "reason_codes": list(data.draw(st.permutations(wire["reason_codes"])))}
    assert_same_record(from_wire(reordered), decision)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-16", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_order_event_round_trip_property(data: st.DataObject) -> None:
    # BUY limit entry: stop below limit and reference, take profit above both.
    limit_units = data.draw(st.integers(10**6, 10**12), label="limit in 1e-4")
    reference_units = limit_units + data.draw(st.integers(0, 10**5), label="reference above limit")
    stop_units = limit_units - data.draw(st.integers(1, 10**6 - 1), label="stop distance")
    take_profit = data.draw(st.none() | st.integers(1, 10**6), label="take profit distance")
    event_time = data.draw(st.integers(0, MAX_TIME_NS - 1), label="event")
    fields = {
        "volume_lots": data.draw(prices, label="volume"),
        "limit_price": scaled(limit_units, 4),
        "reference_price": scaled(reference_units, 4),
        "attached_stop_price": scaled(stop_units, 4),
        "attached_take_profit_price": None
        if take_profit is None
        else scaled(reference_units + take_profit, 4),
        "event_time_ns": event_time,
        "receive_time_ns": data.draw(st.integers(event_time, MAX_TIME_NS), label="receive"),
        "valid_until_ns": data.draw(st.integers(event_time + 1, MAX_TIME_NS), label="valid_until"),
        "seq": data.draw(counts, label="seq"),
    }
    order = samples.order_event(**fields)
    wire = assert_round_trip(order)
    assert order.event_type is OrderEventType.SUBMIT_REQUESTED
    assert wire["venue"] == "SIMULATED"
    assert wire["symbol"] == "XAUUSD"
    assert (wire["attached_take_profit_price"] is None) is (take_profit is None)
    assert Decimal(wire["limit_price"]) == scaled(limit_units, 4)
    assert wire["receive_time_ns"] == fields["receive_time_ns"]


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-17", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_position_round_trip_property(data: st.DataObject) -> None:
    realized = data.draw(st.integers(-(10**14), 10**14), label="realized in cents")
    unrealized = data.draw(st.integers(-(10**14), 10**14), label="unrealized in cents")
    peak = realized + unrealized + data.draw(st.integers(0, 10**14), label="above")
    trough = realized + unrealized - data.draw(st.integers(0, 10**14), label="below")
    opened, peak_time, decision = sorted(data.draw(st.tuples(*[instants] * 3), label="times"))
    initial_volume = data.draw(st.integers(1, 10**9), label="initial volume in 1e-2")
    fields = {
        "realized_net_pnl_amount": scaled(realized, 2),
        "unrealized_net_pnl_amount": scaled(unrealized, 2),
        "peak_net_pnl_amount": scaled(peak, 2),
        "trough_net_pnl_amount": scaled(trough, 2),
        "costs_amount": data.draw(volumes, label="costs"),
        "swap_amount": data.draw(money, label="swap"),
        "initial_risk_amount": data.draw(prices, label="initial risk"),
        "initial_volume_lots": scaled(initial_volume, 2),
        "volume_lots": scaled(data.draw(st.integers(1, initial_volume), label="volume"), 2),
        "mark_price": data.draw(prices, label="mark"),
        "take_profit_price": data.draw(st.none() | prices, label="take profit"),
        "opened_time_ns": opened,
        "peak_time_ns": peak_time,
        "decision_time_ns": decision,
        "as_of_time_ns": data.draw(st.integers(0, decision), label="as_of"),
        "revision": data.draw(counts, label="revision"),
    }
    position = samples.position(**fields)
    wire = assert_round_trip(position)
    assert Decimal(wire["peak_net_pnl_amount"]) == scaled(peak, 2)
    assert Decimal(wire["trough_net_pnl_amount"]) == scaled(trough, 2)
    assert wire["closed_time_ns"] is None
    assert wire["close_reason"] is None
    assert (wire["take_profit_price"] is None) is (fields["take_profit_price"] is None)
    assert wire["decision_time_ns"] == decision


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-18", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_model_version_round_trip_property(data: st.DataObject) -> None:
    metrics = data.draw(named_values, label="metrics")
    references = [
        ContractRef(
            contract=name, version=f"1.{data.draw(st.integers(0, 99))}.{data.draw(st.integers(0, 99))}"
        )
        for name in data.draw(st.lists(st.sampled_from(CONTRACT_NAMES), unique=True), label="contracts")
    ]
    reasons = data.draw(shuffled_subset(REASON_CODES), label="reasons")
    status_time = data.draw(st.integers(T0, MAX_TIME_NS), label="status time")
    fields = {
        "status_time_ns": status_time,
        "seed": data.draw(st.none() | counts, label="seed"),
        "embargo_ns": data.draw(st.none() | instants, label="embargo"),
        "split_spec_sha256": data.draw(st.none() | st.just("34" * 32), label="split"),
        "feature_schema_sha256": data.draw(st.none() | st.just("cd" * 32), label="features"),
        "training_config_sha256": data.draw(st.none() | st.just("12" * 32), label="config"),
        "derived_from_model_version_id": data.draw(st.none() | ids("mdl"), label="lineage"),
        "seq": data.draw(counts, label="seq"),
    }
    model = samples.model_version(
        **fields,
        metrics=tuple(data.draw(st.permutations(metrics), label="metric order")),
        contract_versions=tuple(data.draw(st.permutations(references), label="reference order")),
        status_reasons=tuple(reasons),
    )
    wire = assert_round_trip(model)
    assert _sorted_by(model.metrics, lambda item: item.name)
    assert _sorted_by(model.contract_versions, lambda item: item.contract)
    assert model.status_reasons == tuple(sorted(reasons))
    assert_same_record(
        model,
        samples.model_version(
            **fields,
            metrics=sorted(metrics, key=lambda item: item.name),
            contract_versions=sorted(references, key=lambda item: item.contract),
            status_reasons=sorted(reasons),
        ),
    )
    reordered = copy.deepcopy(wire)
    for name in ("metrics", "contract_versions", "status_reasons"):
        reordered[name] = list(data.draw(st.permutations(wire[name]), label=f"wire {name}"))
    assert_same_record(from_wire(reordered), model)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-19", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(wall=instants, monotonic=counts, regressions=counts, seq=counts, paper=st.booleans())
@example(wall=MAX_TIME_NS, monotonic=2**63 - 1, regressions=2**63 - 1, seq=2**63 - 1, paper=True)
@example(wall=0, monotonic=0, regressions=0, seq=0, paper=False)
def test_clock_round_trip_property(
    wall: int, monotonic: int, regressions: int, seq: int, paper: bool
) -> None:
    if paper:
        reading = samples.clock_reading(
            mode=RunMode.PAPER,
            clock_mode=ClockMode.SYSTEM,
            wall_time_ns=wall,
            monotonic_ns=monotonic,
            regressions_observed=regressions,
            seq=seq,
        )
    else:
        reading = samples.clock_reading(wall_time_ns=wall, monotonic_ns=monotonic, seq=seq)
    wire = assert_round_trip(reading)
    assert wire["wall_time_ns"] == wall
    assert type(wire["wall_time_ns"]) is int
    assert wire["monotonic_ns"] == monotonic
    assert f'"wall_time_ns":{wall}' in to_json(reading), "exact digits, also beyond 2**53"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_run_manifest_round_trip_property(data: st.DataObject) -> None:
    sources = data.draw(shuffled_subset(FEED_IDS, min_size=1), label="sources")
    model_ids = data.draw(st.lists(ids("mdl"), unique=True, max_size=6), label="models")
    models = []
    for model_id in model_ids:
        status = data.draw(st.sampled_from(list(ModelStatus)), label="status")
        window = sorted(data.draw(st.lists(instants, unique=True, min_size=2, max_size=2), label="window"))
        bounded = data.draw(st.booleans(), label="bounded")
        models.append(
            RunModel(
                model_version_id=model_id,
                status=status,
                validated=status is ModelStatus.ACTIVE and data.draw(st.booleans(), label="validated"),
                valid_from_ns=window[0] if bounded else None,
                valid_until_ns=window[1] if bounded else None,
            )
        )
    paper = data.draw(st.booleans(), label="paper")
    start, end = sorted(data.draw(st.lists(instants, unique=True, min_size=2, max_size=2), label="period"))
    # RUN-2: a replay states its data period and no creation time, a paper run the reverse.
    fields: dict[str, Any] = {
        "mode": RunMode.PAPER if paper else RunMode.REPLAY,
        "data_start_ns": None if paper else start,
        "data_end_ns": None if paper else end,
        "created_time_ns": data.draw(instants, label="created") if paper else None,
        "seed": data.draw(st.none() | counts, label="seed"),
        "label": data.draw(st.sampled_from(["fixture", "fixture-b", "a", "z9_.-x"]), label="label"),
        "code_tree_dirty": data.draw(st.booleans(), label="dirty"),
    }
    manifest = samples.run_manifest(**fields, data_sources=tuple(sources), models=tuple(models))
    wire = assert_round_trip(manifest)
    assert manifest.data_sources == tuple(sorted(sources))
    order = sorted(
        models,
        key=lambda item: (-1 if item.valid_from_ns is None else item.valid_from_ns, item.model_version_id),
    )
    assert manifest.models == tuple(order), "sorted by start of validity, then id"
    assert manifest.run_id == derive_run_id(
        **{
            spec.name: getattr(manifest, spec.name)
            for spec in RunManifest.FIELD_SPECS
            if spec.name != "run_id"
        }
    ), "RUN-1: the run id is derived from every other field"
    assert_same_record(
        manifest,
        samples.run_manifest(**fields, data_sources=sorted(sources), models=list(reversed(models))),
    )
    reordered = copy.deepcopy(wire)
    for name in ("data_sources", "models"):
        reordered[name] = list(data.draw(st.permutations(wire[name]), label=f"wire {name}"))
    assert_same_record(from_wire(reordered), manifest)
    assert wire["data_end_ns"] == (None if paper else end)
    assert wire["created_time_ns"] == fields["created_time_ns"]
    assert wire["shadow_of_run_id"] is None


# =========================================================================== floats
@pytest.mark.requirement("S00-ACC-01", "S00-ASG-12", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(value=floats)
@example(value=1.0)
@example(value=1e19)
@example(value=-1e19)
@example(value=2.0**63)
@example(value=1e22)
def test_float_values_outside_the_integer_band_round_trip(value: float) -> None:
    """Finite doubles up to 2**53 and from 2**63 on (the companion test below covers the band between)."""
    evidence = samples.evidence(features=(NamedValue(name="sweep_depth", value=value),))
    wire = assert_round_trip(evidence)
    stored = evidence.features[0].value
    assert stored == value
    assert type(stored) is float
    assert wire["features"][0]["value"] == value
    back = from_json(to_json(evidence))
    assert type(back.features[0].value) is float
    assert back.features[0].value == value
    if value == int(value) and abs(value) < 1e21:
        token = to_json(evidence).split('"value":')[1].split("}")[0]
        assert token.lstrip("-").isdigit(), "an integral double is written as an integer literal"


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-12", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(value=any_finite_float)
@example(value=1.2345678901234568e18)
@example(value=4.611686018427388e18)
def test_every_finite_float_value_round_trips(value: float) -> None:
    """The unrestricted property: *every* finite double a float field may hold survives the round trip.

    This includes the doubles between 2**53 and 2**63, which the canonical writer renders as an
    integer literal with the ECMAScript shortest-digits rule.
    """
    evidence = samples.evidence(features=(NamedValue(name="sweep_depth", value=value),))
    assert evidence.features[0].value == value
    assert from_json(to_json(evidence)) == evidence
    assert to_json(from_json(to_json(evidence))) == to_json(evidence)


# =========================================================================== all sample variants
VARIANTS: list[tuple[str, str, Record]] = [
    (contract, variant, item)
    for contract, by_variant in samples.variants().items()
    for variant, item in by_variant.items()
]


def _nullable_fields(item: Record) -> list[str]:
    """Optional fields that are set and may be set to None without breaking an invariant."""
    names = []
    for spec in type(item).FIELD_SPECS:
        if spec.ftype.optional and getattr(item, spec.name) is not None:
            try:
                replace(item, **{spec.name: None})
            except ContractValidationError:
                continue
            names.append(spec.name)
    return names


NULLABLE: list[tuple[int, list[str]]] = [
    (index, names) for index, (_c, _v, item) in enumerate(VARIANTS) if (names := _nullable_fields(item))
]


def _canonical_arrays(record_type: type[Record], node: dict[str, Any]) -> list[list[Any]]:
    """Every array of *node* (recursively) whose order is canonical and that has at least two items."""
    found: list[list[Any]] = []
    for spec in record_type.FIELD_SPECS:
        raw = node.get(spec.name)
        inner = _unwrap(spec.ftype)
        if isinstance(inner, Obj) and isinstance(raw, dict):
            found.extend(_canonical_arrays(inner.record_type, raw))
        elif isinstance(inner, ListOf) and isinstance(raw, list):
            if inner.canonical_sort is not None and len(raw) >= 2:
                found.append(raw)
            if isinstance(inner.inner, Obj):
                for item in raw:
                    found.extend(_canonical_arrays(inner.inner.record_type, item))
    return found


def _decimal_slots(record_type: type[Record], node: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Every (object, key) of *node* (recursively) that holds a decimal string."""
    found: list[tuple[dict[str, Any], str]] = []
    for spec in record_type.FIELD_SPECS:
        raw = node.get(spec.name)
        inner = _unwrap(spec.ftype)
        if isinstance(inner, Dec) and isinstance(raw, str):
            found.append((node, spec.name))
        elif isinstance(inner, Obj) and isinstance(raw, dict):
            found.extend(_decimal_slots(inner.record_type, raw))
        elif isinstance(inner, ListOf) and isinstance(inner.inner, Obj) and isinstance(raw, list):
            for item in raw:
                found.extend(_decimal_slots(inner.inner.record_type, item))
    return found


WITH_CANONICAL_ARRAYS = [
    index for index, (_c, _v, item) in enumerate(VARIANTS) if _canonical_arrays(type(item), to_wire(item))
]
WITH_DECIMALS = [
    index for index, (_c, _v, item) in enumerate(VARIANTS) if _decimal_slots(type(item), to_wire(item))
]


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_the_generic_properties_cover_the_sample_variants() -> None:
    assert len(VARIANTS) >= 45
    assert {VARIANTS[index][0] for index, _names in NULLABLE} >= {
        "Tick",
        "Evidence",
        "Candidate",
        "TradePlan",
        "RiskDecision",
        "OrderEvent",
        "Position",
        "ModelVersion",
        "RunManifest",
    }, "every contract with optional fields has a variant in which one can be set to null"
    assert sum(len(names) for _index, names in NULLABLE) >= 100
    assert len({VARIANTS[index][0] for index in WITH_CANONICAL_ARRAYS}) >= 5
    assert len({VARIANTS[index][0] for index in WITH_DECIMALS}) >= 6


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("index", range(len(NULLABLE)), ids=lambda i: "-".join(VARIANTS[NULLABLE[i][0]][:2]))
def test_each_optional_field_set_to_none_is_written_as_explicit_null(index: int) -> None:
    variant_index, names = NULLABLE[index]
    original = VARIANTS[variant_index][2]
    for name in names:
        changed = replace(original, **{name: None})
        assert changed != original
        wire = assert_round_trip(changed)
        assert name in wire
        assert wire[name] is None
        assert f'"{name}":null' in to_json(changed)
        assert fingerprint(changed) != fingerprint(original), "null is a value, not an absence"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_any_allowed_combination_of_null_optional_fields_round_trips(data: st.DataObject) -> None:
    variant_index, names = data.draw(st.sampled_from(NULLABLE), label="variant")
    original = VARIANTS[variant_index][2]
    chosen = data.draw(st.lists(st.sampled_from(names), unique=True, min_size=1), label="fields")
    changed = original
    applied = []
    for name in chosen:
        try:
            changed = replace(changed, **{name: None})
        except ContractValidationError:
            continue  # this combination is not allowed by an invariant
        applied.append(name)
    assert applied, "the first field is individually nullable"
    assert changed != original
    wire = assert_round_trip(changed)
    text = to_json(changed)
    for name in applied:
        assert wire[name] is None
        assert f'"{name}":null' in text


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_any_order_of_a_canonically_ordered_list_decodes_to_the_same_record(data: st.DataObject) -> None:
    _contract, _variant, original = VARIANTS[
        data.draw(st.sampled_from(WITH_CANONICAL_ARRAYS), label="variant")
    ]
    wire = to_wire(original)
    arrays = _canonical_arrays(type(original), wire)
    assert arrays
    for array in arrays:
        array[:] = data.draw(st.permutations(array), label="order")
    decoded = from_wire(wire)
    assert_same_record(decoded, original)
    assert to_wire(decoded) == to_wire(original), "written in canonical order again"


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@PROPERTY
@given(data=st.data())
def test_decimals_with_trailing_zeros_decode_to_the_same_record(data: st.DataObject) -> None:
    _contract, _variant, original = VARIANTS[data.draw(st.sampled_from(WITH_DECIMALS), label="variant")]
    wire = to_wire(original)
    padded = 0
    for node, key in _decimal_slots(type(original), wire):
        text = node[key]
        fraction_digits = len(text.partition(".")[2])
        zeros = data.draw(st.integers(0, 12 - fraction_digits), label="zeros")
        if zeros:
            node[key] = text + ("" if "." in text else ".") + "0" * zeros
            padded += 1
    decoded = from_wire(wire)
    assert_same_record(decoded, original)
    assert to_json(decoded) == to_json(original), "the writer emits the form without trailing zeros"
    if padded:
        assert to_wire(decoded) != wire
