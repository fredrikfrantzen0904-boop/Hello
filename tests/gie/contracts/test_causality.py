"""Independent tests for the derivation rules: ``gie.contracts.causality.check_derivation``.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/SHARED_CONTRACTS.md`` section 3.1 ("``check_derivation(child, *inputs)`` checks
  the rules that relate a derived record to its inputs: same run, not created before the inputs
  were available, watermark covers the inputs, does not outlive them, origin not better than
  theirs") and section 3.3 ("Expiry propagates.  A derived record MUST NOT outlive its inputs:
  entry order <= risk approval <= plan <= candidate <= every evidence it uses");
* the ``DataOrigin`` documentation (ordered from most to least trustworthy: BROKER_FEED,
  HISTORICAL, SYNTHETIC_FIXTURE; a derived record carries the least trustworthy origin of its inputs);
* the docstring of ``check_derivation``;
* owner specification G10 and brief 10 (no look-ahead).

SYNTHETIC FIXTURE: every record in this file is invented test data built with
``tests/gie/samples.py``.  Records that carry the origin ``BROKER_FEED`` or ``HISTORICAL`` here are
still fixtures: the origin label is set only to exercise the origin rule, and no such record is
market data or broker data.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gie.contracts import identity
from gie.contracts._spec import Record
from gie.contracts.causality import check_derivation
from gie.contracts.common import DataOrigin, Produced, RunMode
from gie.contracts.evidence import Evidence
from gie.contracts.ordering import ordering_time_ns
from gie.contracts.tick import Tick
from gie.core.errors import ContractValidationError
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
MS = NS_PER_MS
SEC = NS_PER_SECOND
OTHER_RUN_ID = identity.run_id("REPLAY", "1" * 64)

RULE_RUN = "same run"
RULE_CREATED = "not created before"
RULE_WATERMARK = "watermark"
RULE_OUTLIVE = "outlive"
RULE_ORIGIN = "origin"

#: Deterministic hypothesis runs: no random seed, no example database, no wall-clock deadline.
_PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=200)


def _refused(child: Produced, *inputs: Record) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as excinfo:
        check_derivation(child, *inputs)
    error = excinfo.value
    assert error.code == "GIE.CONTRACT.INVALID"
    assert error.details["contract"] == type(child).__name__
    assert isinstance(error.details["rule"], str)
    return error


def _evidence_at(decision_time_ns: int, as_of_time_ns: int, **overrides: Any) -> Evidence:
    return samples.evidence(decision_time_ns=decision_time_ns, as_of_time_ns=as_of_time_ns, **overrides)


def _tick(origin: DataOrigin = DataOrigin.SYNTHETIC_FIXTURE, **overrides: Any) -> Tick:
    return samples.tick(origin=origin, **overrides)


# =========================================================================== accepted derivations
@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
def test_evidence_derived_from_the_sample_tick_is_accepted() -> None:
    tick, evidence = samples.tick(), samples.evidence()
    assert evidence.decision_time_ns >= tick.receive_time_ns
    assert evidence.as_of_time_ns == tick.receive_time_ns
    assert check_derivation(evidence, tick) is None


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
def test_the_whole_sample_chain_is_a_valid_derivation() -> None:
    tick, evidence, candidate = samples.tick(), samples.evidence(), samples.candidate()
    plan, decision, position = samples.trade_plan(), samples.risk_decision(), samples.position()
    check_derivation(evidence, tick)
    check_derivation(candidate, evidence)
    check_derivation(candidate, evidence, tick, samples.model_version())
    check_derivation(plan, candidate)
    check_derivation(plan, candidate, evidence, tick)
    check_derivation(decision, plan)
    check_derivation(decision, plan, candidate, evidence, tick)
    check_derivation(position, *samples.order_lifecycle())


@pytest.mark.requirement("S10-ACC-02")
@pytest.mark.parametrize("name", sorted(samples.variants()["Evidence"]))
def test_every_sample_evidence_satisfies_the_rules_against_the_sample_tick(name: str) -> None:
    evidence = samples.variants()["Evidence"][name]
    assert isinstance(evidence, Evidence)
    check_derivation(evidence, samples.tick())


@pytest.mark.requirement("S10-ACC-02")
def test_a_derived_record_needs_at_least_one_input() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        check_derivation(samples.evidence())
    assert "input" in excinfo.value.message


# =========================================================================== rule: same run and mode
@pytest.mark.requirement("S10-ACC-02", "M-AUTH-07")
@pytest.mark.parametrize(
    ("child", "foreign_input"),
    [
        pytest.param(
            samples.candidate, lambda: samples.evidence(run_id=OTHER_RUN_ID), id="candidate-evidence"
        ),
        pytest.param(samples.trade_plan, lambda: samples.candidate(run_id=OTHER_RUN_ID), id="plan-candidate"),
        pytest.param(
            samples.risk_decision, lambda: samples.trade_plan(run_id=OTHER_RUN_ID), id="approval-plan"
        ),
        pytest.param(
            samples.position, lambda: samples.order_event(run_id=OTHER_RUN_ID), id="position-order-event"
        ),
    ],
)
def test_an_input_of_another_run_is_refused(
    child: Callable[[], Produced], foreign_input: Callable[[], Record]
) -> None:
    error = _refused(child(), foreign_input())
    assert RULE_RUN in str(error.details["rule"])
    assert error.details["input"] == type(foreign_input()).__name__


@pytest.mark.requirement("S10-ACC-02", "M-AUTH-07")
def test_an_input_of_another_run_mode_is_refused() -> None:
    error = _refused(samples.candidate(), samples.evidence(mode=RunMode.PAPER))
    assert RULE_RUN in str(error.details["rule"])
    error = _refused(samples.candidate(mode=RunMode.PAPER), samples.evidence())
    assert RULE_RUN in str(error.details["rule"])
    check_derivation(samples.candidate(mode=RunMode.PAPER), samples.evidence(mode=RunMode.PAPER))


@pytest.mark.requirement("S10-ACC-02")
def test_inputs_that_belong_to_no_run_are_not_subject_to_the_run_rule() -> None:
    # Ticks and registry entries are numbered outside the run and carry no run id.
    foreign_run_evidence = samples.evidence(run_id=OTHER_RUN_ID)
    check_derivation(foreign_run_evidence, samples.tick())
    check_derivation(samples.candidate(run_id=OTHER_RUN_ID), foreign_run_evidence, samples.model_version())


# ============================================================ rule: not created before the inputs
@pytest.mark.requirement("S10-ACC-02")
def test_a_record_created_before_its_input_was_available_is_refused() -> None:
    evidence = samples.evidence()  # available at its decision time T0, watermark T0 - 1 ms
    early = samples.candidate(decision_time_ns=T0 - MS, as_of_time_ns=T0 - MS)
    # Only this rule is violated: watermark, expiry, run and origin are all in order.
    assert early.as_of_time_ns >= evidence.as_of_time_ns
    assert early.decision_time_ns < ordering_time_ns(evidence)
    error = _refused(early, evidence)
    assert RULE_CREATED in str(error.details["rule"])
    assert error.details["input"] == "Evidence"
    check_derivation(samples.candidate(decision_time_ns=T0, as_of_time_ns=T0 - MS), evidence)


@pytest.mark.requirement("S10-ACC-02")
@pytest.mark.parametrize("lead", [1, MS, SEC])
def test_evidence_created_before_the_tick_was_received_is_refused(lead: int) -> None:
    tick = samples.tick()
    early = _evidence_at(tick.receive_time_ns - lead, tick.receive_time_ns - lead)
    error = _refused(early, tick)
    assert error.details["input"] == "Tick"


@pytest.mark.requirement("S10-ACC-02")
def test_a_position_cannot_be_derived_from_an_order_report_received_later() -> None:
    position = samples.position()  # created at T0 + 5 s
    later_report = samples.protective_stop_lifecycle()[3]  # received at T0 + 60 s + 1 ms
    assert later_report.receive_time_ns > position.decision_time_ns
    error = _refused(position, *samples.order_lifecycle(), later_report)
    assert error.details["input"] == "OrderEvent"


@pytest.mark.requirement("S10-ACC-02")
def test_creation_at_the_availability_instant_is_accepted() -> None:
    tick = samples.tick(event_time_ns=T0 - 2 * MS, receive_time_ns=T0)
    check_derivation(_evidence_at(T0, T0), tick)
    _refused(_evidence_at(T0 - 1, T0 - 1), tick)


# ============================================================ rule: watermark covers the inputs
@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
@pytest.mark.parametrize("lag", [1, MS, SEC])
def test_a_watermark_behind_the_tick_is_refused(lag: int) -> None:
    tick = samples.tick()
    stale_watermark = _evidence_at(T0, tick.receive_time_ns - lag)
    # Only this rule is violated: the evidence was created after the tick was received.
    assert stale_watermark.decision_time_ns >= tick.receive_time_ns
    error = _refused(stale_watermark, tick)
    assert RULE_WATERMARK in str(error.details["rule"])
    assert error.details["input"] == "Tick"
    check_derivation(_evidence_at(T0, tick.receive_time_ns), tick)


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23")
def test_a_watermark_behind_the_watermark_of_a_derived_input_is_refused() -> None:
    evidence = samples.evidence()
    behind = samples.candidate(as_of_time_ns=evidence.as_of_time_ns - 1)
    error = _refused(behind, evidence)
    assert RULE_WATERMARK in str(error.details["rule"])
    assert error.details["input"] == "Evidence"
    # The watermark is inherited through the chain: tick -> evidence -> candidate -> plan -> approval.
    newer = samples.evidence(decision_time_ns=T0, as_of_time_ns=T0)
    _refused(samples.candidate(), newer)
    _refused(samples.trade_plan(), samples.candidate(as_of_time_ns=T0))
    _refused(samples.risk_decision(), samples.trade_plan(as_of_time_ns=T0))


@pytest.mark.requirement("S10-ACC-02")
@_PROPERTY
@given(
    received=st.integers(min_value=-5, max_value=5),
    created=st.integers(min_value=-5, max_value=5),
    lag=st.integers(min_value=0, max_value=5),
)
def test_time_rules_for_evidence_derived_from_one_tick(received: int, created: int, lag: int) -> None:
    tick = samples.tick(event_time_ns=T0 - 10 * MS, receive_time_ns=T0 + received * MS)
    evidence = _evidence_at(T0 + created * MS, T0 + (created - lag) * MS)
    allowed = created >= received and created - lag >= received
    if allowed:
        check_derivation(evidence, tick)
    else:
        _refused(evidence, tick)


# ============================================================ rule: does not outlive its inputs
@pytest.mark.requirement("S00-ASG-23")
def test_expiry_of_the_sample_chain_is_ordered() -> None:
    evidence, candidate, plan = samples.evidence(), samples.candidate(), samples.trade_plan()
    decision, order = samples.risk_decision(), samples.order_event()
    assert order.valid_until_ns is not None
    assert (
        order.valid_until_ns
        <= decision.valid_until_ns
        <= plan.valid_until_ns
        <= candidate.valid_until_ns
        <= evidence.valid_until_ns
    )
    check_derivation(candidate, evidence)
    check_derivation(plan, candidate)
    check_derivation(decision, plan)


@pytest.mark.requirement("S00-ASG-23")
@pytest.mark.parametrize(
    ("child", "parent"),
    [
        pytest.param(samples.candidate, samples.evidence, id="candidate-outlives-evidence"),
        pytest.param(samples.trade_plan, samples.candidate, id="plan-outlives-candidate"),
        pytest.param(samples.risk_decision, samples.trade_plan, id="approval-outlives-plan"),
        pytest.param(samples.trade_plan, samples.evidence, id="plan-outlives-evidence"),
        pytest.param(samples.risk_decision, samples.evidence, id="approval-outlives-evidence"),
    ],
)
def test_a_derived_record_may_not_outlive_its_input(
    child: Callable[..., Produced], parent: Callable[[], Record]
) -> None:
    limit: int = parent().valid_until_ns  # type: ignore[attr-defined]
    check_derivation(child(valid_until_ns=limit), parent())  # expiring together is allowed
    error = _refused(child(valid_until_ns=limit + 1), parent())
    assert RULE_OUTLIVE in str(error.details["rule"])
    assert error.details["input"] == type(parent()).__name__


@pytest.mark.requirement("S00-ASG-23")
def test_the_shortest_lived_input_limits_the_derived_record() -> None:
    long_lived = samples.evidence()
    short_lived = samples.evidence(
        engine="liquidity_hunter",
        producer="strategy_intelligence.liquidity_hunter",
        valid_until_ns=T0 + 60 * SEC,
    )
    candidate = samples.candidate()  # valid for 120 s
    check_derivation(candidate, long_lived)
    for inputs in ((long_lived, short_lived), (short_lived, long_lived)):
        error = _refused(candidate, *inputs)
        assert RULE_OUTLIVE in str(error.details["rule"])
    check_derivation(samples.candidate(valid_until_ns=T0 + 60 * SEC), long_lived, short_lived)


@pytest.mark.requirement("S00-ASG-23")
def test_inputs_without_an_expiry_do_not_limit_the_derived_record() -> None:
    # A tick and a registry entry never expire by themselves; a position has no expiry to check.
    check_derivation(samples.evidence(valid_until_ns=T0 + 10_000 * SEC), samples.tick())
    check_derivation(samples.position(), *samples.order_lifecycle())


@pytest.mark.requirement("S00-ASG-23")
def test_an_entry_order_that_outlives_its_approval_is_refused() -> None:
    # Section 3.3 names check_derivation for the whole chain
    # "entry order <= risk approval <= plan <= candidate <= every evidence it uses".
    decision = samples.risk_decision()
    check_derivation(samples.order_event(valid_until_ns=decision.valid_until_ns), decision)
    order = samples.order_event(valid_until_ns=decision.valid_until_ns + 1)
    error = _refused(order, decision)  # type: ignore[arg-type]
    assert RULE_OUTLIVE in str(error.details["rule"])
    assert error.details["input"] == "RiskDecision"


# ============================================================ an order event as the derived record
@pytest.mark.requirement("S00-ASG-23", "S10-ACC-02")
def test_the_sample_entry_order_is_a_valid_derivation_of_its_plan_and_approval() -> None:
    # Rework 2: check_derivation applies the applicable rules to an OrderEvent child; its creation
    # instant is event_time_ns and it has no watermark of its own.
    plan, decision = samples.trade_plan(), samples.risk_decision()
    order = samples.order_event()
    check_derivation(order, decision)
    check_derivation(order, plan, decision)
    # Created exactly when the approval became available: accepted.
    check_derivation(
        samples.order_event(
            event_time_ns=decision.decision_time_ns, receive_time_ns=decision.decision_time_ns
        ),
        decision,
    )


@pytest.mark.requirement("S00-ASG-23", "S10-ACC-02", "COM-07")
@pytest.mark.parametrize(
    ("rule", "order", "source"),
    [
        pytest.param(
            RULE_RUN,
            samples.order_event,
            lambda: samples.risk_decision(run_id=OTHER_RUN_ID),
            id="other-run",
        ),
        pytest.param(
            RULE_CREATED,
            lambda: samples.order_event(event_time_ns=T0 - 1, receive_time_ns=T0 - 1),
            samples.risk_decision,
            id="sent-before-the-approval",
        ),
        pytest.param(
            RULE_OUTLIVE,
            lambda: samples.order_event(valid_until_ns=T0 + 30 * SEC + 1),
            samples.risk_decision,
            id="outlives-the-approval",
        ),
        pytest.param(
            RULE_ORIGIN,
            lambda: samples.order_event(data_origin=DataOrigin.HISTORICAL),
            samples.risk_decision,
            id="better-origin-than-the-approval",
        ),
    ],
)
def test_an_order_event_child_is_refused_by_each_applicable_rule(
    rule: str, order: Callable[[], Any], source: Callable[[], Record]
) -> None:
    error = _refused(order(), source())
    assert rule in str(error.details["rule"])
    assert error.details["contract"] == "OrderEvent"


@pytest.mark.requirement("S00-ASG-23", "S10-ACC-02")
def test_the_watermark_rule_does_not_apply_to_an_order_event_child() -> None:
    # A tick received at the creation instant: a produced record whose watermark lies before it is
    # refused, an order event (no watermark of its own) created at that instant is not.
    order = samples.order_event()
    quote = samples.tick(receive_time_ns=order.event_time_ns, event_time_ns=T0)
    check_derivation(order, quote)
    error = _refused(_evidence_at(order.event_time_ns, order.event_time_ns - 1), quote)
    assert RULE_WATERMARK in str(error.details["rule"])


@pytest.mark.requirement("S00-ASG-23")
@pytest.mark.parametrize(
    "child",
    [samples.tick, samples.clock_reading, samples.model_version, samples.run_manifest],
    ids=["tick", "clock-reading", "model-version", "run-manifest"],
)
def test_only_a_produced_record_or_an_order_event_can_be_a_derived_record(
    child: Callable[[], Record],
) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        check_derivation(child(), samples.tick())  # type: ignore[arg-type]
    assert "produced record or an order event" in str(excinfo.value.details["rule"])
    assert excinfo.value.details["contract"] == type(child()).__name__


# ============================================================ rule: origin not better than the inputs
_TRUST_ORDER = [DataOrigin.BROKER_FEED, DataOrigin.HISTORICAL, DataOrigin.SYNTHETIC_FIXTURE]


@pytest.mark.requirement("COM-07", "M-AUTH-07")
@pytest.mark.parametrize("input_origin", _TRUST_ORDER, ids=lambda origin: f"input-{origin.value}")
@pytest.mark.parametrize("child_origin", _TRUST_ORDER, ids=lambda origin: f"child-{origin.value}")
def test_a_derived_record_never_claims_a_better_origin_than_its_input(
    child_origin: DataOrigin, input_origin: DataOrigin
) -> None:
    child = samples.evidence(data_origin=child_origin)
    tick = _tick(input_origin)
    if _TRUST_ORDER.index(child_origin) >= _TRUST_ORDER.index(input_origin):
        check_derivation(child, tick)
    else:
        error = _refused(child, tick)
        assert RULE_ORIGIN in str(error.details["rule"])


@pytest.mark.requirement("COM-07", "M-AUTH-07")
def test_broker_feed_evidence_cannot_come_from_a_synthetic_fixture_tick() -> None:
    tick = samples.tick()
    assert tick.origin is DataOrigin.SYNTHETIC_FIXTURE
    error = _refused(samples.evidence(data_origin=DataOrigin.BROKER_FEED), tick)
    assert RULE_ORIGIN in str(error.details["rule"])
    _refused(samples.evidence(data_origin=DataOrigin.HISTORICAL), tick)
    check_derivation(samples.evidence(data_origin=DataOrigin.SYNTHETIC_FIXTURE), tick)


@pytest.mark.requirement("COM-07", "M-AUTH-07")
def test_the_least_trustworthy_input_decides() -> None:
    broker, historical, fixture = (_tick(origin) for origin in _TRUST_ORDER)
    for inputs in ((broker, historical), (historical, broker)):
        _refused(samples.evidence(data_origin=DataOrigin.BROKER_FEED), *inputs)
        check_derivation(samples.evidence(data_origin=DataOrigin.HISTORICAL), *inputs)
        check_derivation(samples.evidence(data_origin=DataOrigin.SYNTHETIC_FIXTURE), *inputs)
    for inputs in ((broker, historical, fixture), (fixture, broker, historical)):
        _refused(samples.evidence(data_origin=DataOrigin.BROKER_FEED), *inputs)
        _refused(samples.evidence(data_origin=DataOrigin.HISTORICAL), *inputs)
        check_derivation(samples.evidence(data_origin=DataOrigin.SYNTHETIC_FIXTURE), *inputs)


@pytest.mark.requirement("COM-07", "M-AUTH-07")
def test_the_origin_propagates_through_derived_records() -> None:
    fixture_evidence = samples.evidence()
    for better in (DataOrigin.BROKER_FEED, DataOrigin.HISTORICAL):
        _refused(samples.candidate(data_origin=better), fixture_evidence)
        _refused(samples.trade_plan(data_origin=better), samples.candidate())
        _refused(samples.risk_decision(data_origin=better), samples.trade_plan())
        _refused(samples.position(data_origin=better), *samples.order_lifecycle())
    historical_evidence = samples.evidence(data_origin=DataOrigin.HISTORICAL)
    check_derivation(samples.candidate(data_origin=DataOrigin.HISTORICAL), historical_evidence)
    check_derivation(samples.candidate(), historical_evidence)


# =========================================================================== all inputs, all rules
def _violations() -> dict[str, tuple[Produced, Record]]:
    """One (child, input) pair per rule; each pair violates exactly that rule."""
    evidence = samples.evidence()
    return {
        RULE_RUN: (samples.candidate(), samples.evidence(run_id=OTHER_RUN_ID)),
        RULE_CREATED: (samples.candidate(decision_time_ns=T0 - MS, as_of_time_ns=T0 - MS), evidence),
        RULE_WATERMARK: (samples.candidate(as_of_time_ns=T0 - 2 * MS), evidence),
        RULE_OUTLIVE: (samples.candidate(valid_until_ns=evidence.valid_until_ns + 1), evidence),
        RULE_ORIGIN: (samples.candidate(data_origin=DataOrigin.HISTORICAL), evidence),
    }


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23", "COM-07")
@pytest.mark.parametrize("rule", [RULE_RUN, RULE_CREATED, RULE_WATERMARK, RULE_OUTLIVE, RULE_ORIGIN])
def test_each_rule_is_refused_separately_and_named_in_the_error(rule: str) -> None:
    child, bad_input = _violations()[rule]
    error = _refused(child, bad_input)
    assert rule in str(error.details["rule"])
    assert rule in error.message
    # The error names the violated rule and no other one.
    for other_rule in _violations():
        assert (other_rule in str(error.details["rule"])) == (other_rule == rule)
    document = error.to_dict()
    assert document["code"] == "GIE.CONTRACT.INVALID"
    assert all(
        isinstance(value, (str, int, float, bool)) or value is None for value in error.details.values()
    )


@pytest.mark.requirement("S10-ACC-02", "S00-ASG-23", "COM-07")
@pytest.mark.parametrize("rule", [RULE_RUN, RULE_CREATED, RULE_WATERMARK, RULE_OUTLIVE, RULE_ORIGIN])
def test_every_input_is_checked_wherever_it_stands(rule: str) -> None:
    child, bad_input = _violations()[rule]
    assert isinstance(bad_input, Evidence)
    # A second, valid input of the same child: the model it was scored with.
    good_input = samples.model_version()
    check_derivation(child, good_input)
    _refused(child, good_input, bad_input)
    _refused(child, bad_input, good_input)
    _refused(child, good_input, good_input, bad_input)


@pytest.mark.requirement("S10-ACC-02")
def test_checking_does_not_change_the_records() -> None:
    tick, evidence = samples.tick(), samples.evidence()
    snapshot = (dataclasses.asdict(tick), dataclasses.asdict(evidence))
    check_derivation(evidence, tick)
    _refused(samples.evidence(data_origin=DataOrigin.BROKER_FEED), tick)
    assert (dataclasses.asdict(tick), dataclasses.asdict(evidence)) == snapshot
