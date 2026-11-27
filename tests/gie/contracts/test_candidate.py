"""Independent tests for the ``Candidate`` contract (``gie/contracts/candidate.py``) and its nested
value objects ``Destination``, ``QuantilePoint`` and ``Contribution``.

ALL DATA IN THIS MODULE IS SYNTHETIC FIXTURE DATA.  Every record is built with the labelled
builders of ``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); setups, estimates, model
ids and instants are invented.  Nothing here is a model output or evidence of performance.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - "Candidate 1.0.0" with the invariants CAUSAL-1 and
  CND-1 .. CND-11, ``Destination`` (DEST-1), ``Contribution`` (CONTRIB-1), ``QuantilePoint`` and
  ``CandidateStatus``;
* ``specifications/SHARED_CONTRACTS.md`` - section 2 (an unscored candidate has null estimates,
  never 0; the two meanings of uncertainty; only XAUUSD can be planned), section 4 (the opportunity
  id is stable across evidence revisions, engines and scorers; the candidate id adds scorer and
  revision), section 5 (error semantics) and section 9.1 (retrieved historical cases are prior-only);
* the class docstrings of ``gie.contracts.candidate`` (final statuses are final).
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools
from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    CANDIDATE_STATUS_TRANSITIONS,
    Candidate,
    CandidateStatus,
    Contribution,
    DataOrigin,
    Destination,
    Horizon,
    MarketSession,
    ModelStatus,
    QuantilePoint,
    RunMode,
    Side,
    from_json,
    from_wire,
    identity,
    ordering_time_ns,
    stream_name,
    to_json,
    to_wire,
    validate_candidate_transition,
)
from gie.core.errors import ContractValidationError, GieError, IllegalTransitionError
from gie.core.freshness import is_expired
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
AS_OF = T0 - NS_PER_MS
SHADOW_MODEL = identity.model_version_id("fixture_shadow", "0.2.0", "cd" * 32)
BASELINE_MODEL = identity.model_version_id("fixture_ruleset", "1.0.0", "ee" * 32)
OTHER_EVENT = identity.market_event_id("breakout", "XAUUSD", "M15", T0 - 900 * NS_PER_SECOND, [D("2651.20")])
OTHER_RUN = identity.run_id("REPLAY", "1" * 64)

Build = Callable[[], object]

#: Fields that exist only on a scored candidate, with a valid value each.
MODEL_ONLY_VALUES: dict[str, Any] = {
    "net_r_stdev": 1.1,
    "expected_net_r_stderr": 0.12,
    "support_count": 84,
    "win_probability_frac": 0.48,
    "expected_win_r": 1.5,
    "expected_loss_r": 1.0,
    "expected_cost_r": 0.08,
    "expected_holding_time_ns": 900 * NS_PER_SECOND,
    "quality_score": 0.6,
    "memory_cutoff_time_ns": T0 - 86_400 * NS_PER_SECOND,
    "baseline_r": 0.05,
}
SCORED_TOGETHER = ("model_version_id", "model_status", "risk_unit_price", "expected_net_r")


# --------------------------------------------------------------------------- labelled fixture builders
def _scored(**overrides: Any) -> Candidate:
    return samples.candidate(**overrides)


def _unscored(**overrides: Any) -> Candidate:
    return samples.unscored_candidate(**overrides)


def _with_status(status: CandidateStatus, revision: int = 0, **overrides: Any) -> Candidate:
    fields: dict[str, Any] = {"status": status, "revision": revision, "seq": revision}
    if status in (CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN):
        fields["status_reasons"] = ("FIXTURE_REASON",)
    fields.update(overrides)
    return samples.candidate(**fields)


def _changed(record: Candidate, **changes: Any) -> Candidate:
    """Copy *record* with single fields replaced; nothing is recomputed (the copy is re-validated)."""
    return dataclasses.replace(record, **changes)


def _quantiles(*points: tuple[float, float]) -> tuple[QuantilePoint, ...]:
    return tuple(QuantilePoint(q_frac=q, value_r=value) for q, value in points)


def _destination(scenario: str = "reversal", label: str = "prior_high", **overrides: Any) -> Destination:
    fields: dict[str, Any] = {
        "scenario": scenario,
        "label": label,
        "price": D("2655.00"),
        "probability_frac": 0.4,
        "horizon_ns": 1800 * NS_PER_SECOND,
    }
    fields.update(overrides)
    return Destination(**fields)


def _evidence_id(engine: str = "sweep_reversal", revision: int = 0) -> str:
    return samples.evidence(
        engine=engine, producer=f"strategy_intelligence.{engine}", revision=revision
    ).evidence_id


def _opportunity(
    setup: str = "sweep_reversal_long",
    side: str = "BUY",
    horizon: str = "SCALP",
    events: tuple[str, ...] | None = None,
) -> str:
    return identity.opportunity_id(setup, side, horizon, events or (samples.evidence().market_event_id,))


def _invariant_error(build: Build, invariant_id: str, contract: str = "Candidate") -> ContractValidationError:
    with pytest.raises(ContractValidationError) as info:
        build()
    error = info.value
    assert error.details.get("invariant") == invariant_id, str(error)
    assert error.details.get("contract") == contract
    assert error.code == "GIE.CONTRACT.INVALID"
    return error


def _field_error(build: Build, path: str) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as info:
        build()
    error = info.value
    assert error.details.get("path") == path, str(error)
    assert "invariant" not in error.details
    return error


# =========================================================================== inventory
CANDIDATE_INVARIANTS = ("CAUSAL-1", *(f"CND-{number}" for number in range(1, 12)))


@pytest.mark.requirement("S00-ASG-13")
def test_candidate_declares_exactly_the_invariants_of_the_field_reference() -> None:
    assert tuple(rule.id for rule in Candidate.ALL_INVARIANTS) == CANDIDATE_INVARIANTS
    assert (Candidate.CONTRACT, Candidate.VERSION) == ("Candidate", "1.0.0")
    assert [rule.id for rule in Destination.ALL_INVARIANTS] == ["DEST-1"]
    assert [rule.id for rule in Contribution.ALL_INVARIANTS] == ["CONTRIB-1"]
    assert QuantilePoint.ALL_INVARIANTS == ()


@pytest.mark.requirement("S00-ASG-13")
def test_published_status_vocabulary_and_transition_table() -> None:
    assert [status.value for status in CandidateStatus] == ["AVAILABLE", "WITHDRAWN", "EXPIRED", "REJECTED"]
    assert {
        None: frozenset(CandidateStatus),
        CandidateStatus.AVAILABLE: frozenset(CandidateStatus),
        CandidateStatus.WITHDRAWN: frozenset(),
        CandidateStatus.EXPIRED: frozenset(),
        CandidateStatus.REJECTED: frozenset(),
    } == CANDIDATE_STATUS_TRANSITIONS


@pytest.mark.requirement("S00-ASG-13")
def test_sample_candidates_are_labelled_synthetic_and_immutable() -> None:
    variants = samples.variants()["Candidate"]
    assert len(variants) >= 4
    for name, record in variants.items():
        assert type(record) is Candidate, name
        assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE, name
    with pytest.raises(dataclasses.FrozenInstanceError):
        variants["scored"].expected_net_r = 9.0  # type: ignore[misc]


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-21")
def test_stream_and_ordering_time() -> None:
    record = _scored()
    assert stream_name(record) == "candidate/prediction.scorer"
    assert ordering_time_ns(record) == record.decision_time_ns == T0


# =========================================================================== invariants: accepted
ACCEPTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_equals_decision_time", lambda: _scored(as_of_time_ns=T0)),
    ("CAUSAL-1", "watermark_before_decision_time", lambda: _scored(as_of_time_ns=T0 - 60 * NS_PER_SECOND)),
    ("CND-1", "one_event", _scored),
    (
        "CND-1",
        "two_events",
        lambda: _scored(market_event_ids=(OTHER_EVENT, samples.evidence().market_event_id)),
    ),
    (
        "CND-1",
        "sell_intraday",
        lambda: _scored(side=Side.SELL, horizon=Horizon.INTRADAY, setup_type="fade_high"),
    ),
    ("CND-2", "scored_revision_0", _scored),
    (
        "CND-2",
        "scored_by_shadow_model",
        lambda: _scored(model_version_id=SHADOW_MODEL, model_status=ModelStatus.SHADOW),
    ),
    ("CND-2", "unscored_has_no_scorer", _unscored),
    ("CND-2", "revision_4", lambda: _scored(revision=4, seq=4)),
    ("CND-3", "all_four_set", _scored),
    ("CND-3", "all_four_null", _unscored),
    ("CND-4", "unscored_with_reason", lambda: _unscored(unscored_reason="MODEL_NOT_LOADED")),
    ("CND-4", "scored_without_reason", _scored),
    (
        "CND-5",
        "stdev_only",
        lambda: _scored(expected_net_r_stderr=None, support_count=None, net_r_quantiles=()),
    ),
    ("CND-5", "stderr_only", lambda: _scored(net_r_stdev=None, net_r_quantiles=())),
    (
        "CND-5",
        "two_quantiles_only",
        lambda: _scored(
            net_r_stdev=None,
            expected_net_r_stderr=None,
            net_r_quantiles=_quantiles((0.25, -0.5), (0.75, 1.0)),
        ),
    ),
    ("CND-5", "unscored_states_none", _unscored),
    ("CND-6", "unscored_carries_no_estimates", _unscored),
    ("CND-6", "scored_carries_all_estimates", _scored),
    ("CND-7", "no_quantiles", lambda: _scored(net_r_quantiles=())),
    ("CND-7", "single_quantile", lambda: _scored(net_r_quantiles=_quantiles((0.5, 0.2)))),
    (
        "CND-7",
        "equal_values_allowed",
        lambda: _scored(net_r_quantiles=_quantiles((0.1, -1.0), (0.2, -1.0), (0.9, -1.0))),
    ),
    (
        "CND-7",
        "extreme_probabilities",
        lambda: _scored(net_r_quantiles=_quantiles((1e-9, -5.0), (1 - 1e-9, 9.0))),
    ),
    ("CND-8", "available_expires_one_ns_later", lambda: _scored(valid_until_ns=T0 + 1)),
    ("CND-8", "expired_at_decision_time", lambda: _with_status(CandidateStatus.EXPIRED, valid_until_ns=T0)),
    ("CND-8", "rejected_long_expired", lambda: _with_status(CandidateStatus.REJECTED, valid_until_ns=0)),
    ("CND-9", "available_without_reasons", _scored),
    ("CND-9", "rejected_with_reason", lambda: _with_status(CandidateStatus.REJECTED)),
    (
        "CND-9",
        "withdrawn_with_reasons",
        lambda: _with_status(CandidateStatus.WITHDRAWN, status_reasons=("B_C", "A_B")),
    ),
    ("CND-9", "expired_without_reason", lambda: _with_status(CandidateStatus.EXPIRED)),
    (
        "CND-9",
        "expired_with_reason",
        lambda: _with_status(CandidateStatus.EXPIRED, status_reasons=("AGED_OUT",)),
    ),
    ("CND-10", "revision_0_supersedes_nothing", _scored),
    ("CND-10", "revision_1_names_revision_0", lambda: _scored(revision=1, seq=1)),
    ("CND-10", "unscored_revision_2", lambda: _unscored(revision=2, seq=2)),
    ("CND-11", "cutoff_equals_watermark", lambda: _scored(memory_cutoff_time_ns=AS_OF)),
    ("CND-11", "cutoff_long_before", lambda: _scored(memory_cutoff_time_ns=0)),
    ("CND-11", "no_retrieval", lambda: _scored(memory_cutoff_time_ns=None)),
]


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in ACCEPTED_CASES],
)
def test_invariant_accepts(invariant_id: str, build: Build) -> None:
    record = build()
    assert type(record) is Candidate
    assert record.data_origin is DataOrigin.SYNTHETIC_FIXTURE
    assert record.symbol == "XAUUSD"
    assert invariant_id in CANDIDATE_INVARIANTS
    assert dataclasses.replace(record) == record
    assert from_wire(to_wire(record), Candidate) == record


# =========================================================================== invariants: rejected
def _mixed_scoredness_cases() -> list[tuple[str, str, Build]]:
    """Every combination of the four 'scored together' fields that is neither all set nor all null."""
    cases: list[tuple[str, str, Build]] = []
    for present in itertools.product((True, False), repeat=len(SCORED_TOGETHER)):
        if all(present) or not any(present):
            continue
        nulled = {name: None for name, keep in zip(SCORED_TOGETHER, present, strict=True) if not keep}
        name = "only_" + "_and_".join(n for n, keep in zip(SCORED_TOGETHER, present, strict=True) if keep)
        cases.append(("CND-3", name, lambda nulled=nulled: _scored(**nulled)))
        # The same partial combination on an otherwise unscored candidate.
        kept = {
            "model_version_id": samples.MODEL_ID,
            "model_status": ModelStatus.RESEARCH,
            "risk_unit_price": D("2.00"),
            "expected_net_r": 0.25,
        }
        partial = {name_: kept[name_] for name_, keep in zip(SCORED_TOGETHER, present, strict=True) if keep}
        cases.append(("CND-3", name + "_on_unscored", lambda partial=partial: _unscored(**partial)))
    return cases


REJECTED_CASES: list[tuple[str, str, Build]] = [
    ("CAUSAL-1", "watermark_one_ns_after_decision", lambda: _changed(_scored(), as_of_time_ns=T0 + 1)),
    ("CND-1", "id_of_another_setup", lambda: _scored(opportunity_id=_opportunity(setup="other_setup"))),
    ("CND-1", "id_of_the_other_side", lambda: _scored(opportunity_id=_opportunity(side="SELL"))),
    ("CND-1", "id_of_another_horizon", lambda: _scored(opportunity_id=_opportunity(horizon="INTRADAY"))),
    ("CND-1", "id_of_other_events", lambda: _scored(opportunity_id=_opportunity(events=(OTHER_EVENT,)))),
    ("CND-1", "side_changed_under_the_id", lambda: _changed(_scored(), side=Side.SELL)),
    ("CND-1", "horizon_changed_under_the_id", lambda: _changed(_scored(), horizon=Horizon.INTRADAY)),
    ("CND-1", "setup_changed_under_the_id", lambda: _changed(_scored(), setup_type="other_setup")),
    ("CND-1", "events_changed_under_the_id", lambda: _changed(_scored(), market_event_ids=(OTHER_EVENT,))),
    (
        "CND-1",
        "event_added_under_the_id",
        lambda: _changed(_scored(), market_event_ids=(OTHER_EVENT, samples.evidence().market_event_id)),
    ),
    (
        "CND-2",
        "id_of_another_revision",
        lambda: _changed(_scored(), candidate_id=identity.candidate_id(_opportunity(), samples.MODEL_ID, 1)),
    ),
    (
        "CND-2",
        "id_of_another_model",
        lambda: _changed(_scored(), candidate_id=identity.candidate_id(_opportunity(), SHADOW_MODEL, 0)),
    ),
    (
        "CND-2",
        "scored_with_the_unscored_id",
        lambda: _changed(_scored(), candidate_id=identity.candidate_id(_opportunity(), None, 0)),
    ),
    (
        "CND-2",
        "unscored_with_a_scored_id",
        lambda: _changed(
            _unscored(),
            candidate_id=identity.candidate_id(_unscored().opportunity_id, samples.MODEL_ID, 0),
        ),
    ),
    *_mixed_scoredness_cases(),
    (
        "CND-4",
        "scored_with_unscored_reason",
        lambda: _changed(_scored(), unscored_reason="NO_MODEL_AVAILABLE"),
    ),
    ("CND-4", "unscored_without_reason", lambda: _changed(_unscored(), unscored_reason=None)),
    (
        "CND-5",
        "no_uncertainty_at_all",
        lambda: _changed(_scored(), net_r_stdev=None, expected_net_r_stderr=None, net_r_quantiles=()),
    ),
    (
        "CND-5",
        "a_single_quantile_is_not_a_distribution",
        lambda: _changed(
            _scored(), net_r_stdev=None, expected_net_r_stderr=None, net_r_quantiles=_quantiles((0.5, 0.2))
        ),
    ),
    (
        "CND-5",
        "support_count_alone_is_not_uncertainty",
        lambda: _changed(
            _scored(), net_r_stdev=None, expected_net_r_stderr=None, net_r_quantiles=(), support_count=500
        ),
    ),
    *[
        (
            "CND-6",
            f"unscored_with_{name}",
            lambda name=name, value=value: _changed(_unscored(), **{name: value}),
        )
        for name, value in MODEL_ONLY_VALUES.items()
    ],
    ("CND-6", "unscored_with_zero_stdev", lambda: _changed(_unscored(), net_r_stdev=0.0)),
    (
        "CND-6",
        "unscored_with_quantile",
        lambda: _changed(_unscored(), net_r_quantiles=_quantiles((0.5, 0.2))),
    ),
    (
        "CND-6",
        "unscored_with_quantiles",
        lambda: _changed(_unscored(), net_r_quantiles=_quantiles((0.1, -1), (0.9, 2))),
    ),
    ("CND-6", "unscored_with_destination", lambda: _changed(_unscored(), destinations=(_destination(),))),
    (
        "CND-6",
        "unscored_with_contribution",
        lambda: _changed(
            _unscored(),
            contributions=(Contribution(evidence_id=None, feature="session_london", weight_r=0.05),),
        ),
    ),
    (
        "CND-7",
        "equal_probabilities",
        lambda: _changed(_scored(), net_r_quantiles=_quantiles((0.5, 0.1), (0.5, 0.2))),
    ),
    (
        "CND-7",
        "decreasing_probabilities",
        lambda: _changed(_scored(), net_r_quantiles=_quantiles((0.6, 0.1), (0.5, 0.2))),
    ),
    (
        "CND-7",
        "decreasing_values",
        lambda: _changed(_scored(), net_r_quantiles=_quantiles((0.5, 0.3), (0.6, 0.2))),
    ),
    (
        "CND-7",
        "disorder_in_the_tail",
        lambda: _changed(
            _scored(), net_r_quantiles=_quantiles((0.1, -1.0), (0.5, 0.2), (0.9, 2.0), (0.8, 2.5))
        ),
    ),
    ("CND-8", "available_expires_at_decision_time", lambda: _changed(_scored(), valid_until_ns=T0)),
    ("CND-8", "available_already_expired", lambda: _changed(_scored(), valid_until_ns=T0 - 1)),
    ("CND-9", "available_with_reasons", lambda: _changed(_scored(), status_reasons=("STALE_EVIDENCE",))),
    ("CND-9", "rejected_without_reason", lambda: _changed(_scored(), status=CandidateStatus.REJECTED)),
    ("CND-9", "withdrawn_without_reason", lambda: _changed(_scored(), status=CandidateStatus.WITHDRAWN)),
    (
        "CND-10",
        "revision_0_names_a_predecessor",
        lambda: _changed(_scored(), supersedes_candidate_id=_scored().candidate_id),
    ),
    (
        "CND-10",
        "revision_1_names_nothing",
        lambda: _changed(_scored(revision=1, seq=1), supersedes_candidate_id=None),
    ),
    (
        "CND-10",
        "revision_1_names_itself",
        lambda: _changed(
            _scored(revision=1, seq=1), supersedes_candidate_id=_scored(revision=1, seq=1).candidate_id
        ),
    ),
    (
        "CND-10",
        "revision_2_skips_revision_1",
        lambda: _changed(_scored(revision=2, seq=2), supersedes_candidate_id=_scored().candidate_id),
    ),
    (
        "CND-10",
        "revision_1_names_another_scorers_candidate",
        lambda: _changed(
            _scored(revision=1, seq=1),
            supersedes_candidate_id=identity.candidate_id(_opportunity(), SHADOW_MODEL, 0),
        ),
    ),
    ("CND-11", "cutoff_one_ns_after_watermark", lambda: _changed(_scored(), memory_cutoff_time_ns=AS_OF + 1)),
    (
        "CND-11",
        "cutoff_at_decision_time_after_watermark",
        lambda: _changed(_scored(), memory_cutoff_time_ns=T0),
    ),
    (
        "CND-11",
        "cutoff_in_the_future",
        lambda: _changed(_scored(), memory_cutoff_time_ns=T0 + 86_400 * NS_PER_SECOND),
    ),
]


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize(
    ("invariant_id", "build"),
    [pytest.param(invariant, build, id=f"{invariant}__{name}") for invariant, name, build in REJECTED_CASES],
)
def test_invariant_rejects(invariant_id: str, build: Build) -> None:
    error = _invariant_error(build, invariant_id)
    rule = next(rule for rule in Candidate.ALL_INVARIANTS if rule.id == invariant_id)
    assert error.details["rule"] == rule.text
    assert invariant_id in error.message


@pytest.mark.requirement("S00-ASG-13")
def test_every_invariant_has_an_accepted_and_a_rejected_case() -> None:
    assert {invariant for invariant, _, _ in ACCEPTED_CASES} == set(CANDIDATE_INVARIANTS)
    assert {invariant for invariant, _, _ in REJECTED_CASES} == set(CANDIDATE_INVARIANTS)
    # 2**4 combinations of the "scored together" fields: 2 valid, 14 mixed (each tried on both bases).
    assert sum(1 for invariant, _, _ in REJECTED_CASES if invariant == "CND-3") == 28


@pytest.mark.requirement("S00-ASG-13")
def test_decoding_enforces_the_invariants() -> None:
    wire = to_wire(_unscored())
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "expected_net_r": 0}, Candidate)
    assert info.value.details["invariant"] == "CND-3"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**wire, "quality_score": 0}, Candidate)
    assert info.value.details["invariant"] == "CND-6"
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(_scored()), "memory_cutoff_time_ns": T0}, Candidate)
    assert info.value.details["invariant"] == "CND-11"


# =========================================================================== identity
@pytest.mark.requirement("S00-ASG-13", "S00-ASG-22")
def test_opportunity_id_is_stable_across_evidence_revisions_engines_and_scorers() -> None:
    views = [
        _scored(),
        _scored(evidence_ids=(_evidence_id(revision=1),)),
        _scored(evidence_ids=(_evidence_id("liquidity_hunter"), _evidence_id("sweep_reversal", 3))),
        _scored(model_version_id=SHADOW_MODEL, model_status=ModelStatus.SHADOW),
        _scored(model_version_id=BASELINE_MODEL, model_status=ModelStatus.ACTIVE, expected_net_r=-0.4),
        _unscored(side=Side.BUY, setup_type="sweep_reversal_long"),
        _scored(revision=1, seq=1),
        _scored(revision=2, seq=2, status=CandidateStatus.EXPIRED),
        _scored(session=MarketSession.NEW_YORK, reference_price=D("2700"), producer="prediction.other"),
    ]
    assert {view.opportunity_id for view in views} == {_opportunity()}
    assert views[0].opportunity_id == identity.opportunity_id(
        "sweep_reversal_long", "BUY", "SCALP", [samples.evidence().market_event_id]
    )


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-22")
def test_candidate_id_is_per_scorer_and_revision() -> None:
    scorers: list[tuple[str | None, ModelStatus | None]] = [
        (samples.MODEL_ID, ModelStatus.RESEARCH),
        (SHADOW_MODEL, ModelStatus.SHADOW),
        (BASELINE_MODEL, ModelStatus.ACTIVE),
    ]
    records = [
        _scored(model_version_id=model, model_status=status, revision=revision, seq=revision)
        for (model, status), revision in itertools.product(scorers, range(3))
    ]
    records += [
        _unscored(side=Side.BUY, setup_type="sweep_reversal_long", revision=r, seq=r) for r in range(3)
    ]
    assert len({record.opportunity_id for record in records}) == 1
    assert len({record.candidate_id for record in records}) == len(records) == 12
    for record in records:
        assert record.candidate_id == identity.candidate_id(
            record.opportunity_id, record.model_version_id, record.revision
        )
        expected_previous = (
            None
            if record.revision == 0
            else identity.candidate_id(record.opportunity_id, record.model_version_id, record.revision - 1)
        )
        assert record.supersedes_candidate_id == expected_previous


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-22")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"setup_type": "sweep_reversal_short"}, id="setup"),
        pytest.param({"side": Side.SELL}, id="side"),
        pytest.param({"horizon": Horizon.INTRADAY}, id="horizon"),
        pytest.param({"market_event_ids": (OTHER_EVENT,)}, id="other_event"),
        pytest.param(
            {"market_event_ids": (OTHER_EVENT, samples.evidence().market_event_id)}, id="additional_event"
        ),
    ],
)
def test_a_different_opportunity_obtains_a_different_id(overrides: dict[str, Any]) -> None:
    reference = _scored()
    other = _scored(**overrides)
    assert other.opportunity_id != reference.opportunity_id
    assert other.candidate_id != reference.candidate_id


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-22")
def test_market_events_and_evidence_ids_are_unique_and_canonically_sorted() -> None:
    event = samples.evidence().market_event_id
    forward = _scored(
        market_event_ids=(event, OTHER_EVENT), evidence_ids=(_evidence_id(), _evidence_id("cavemen"))
    )
    backward = _scored(
        market_event_ids=(OTHER_EVENT, event), evidence_ids=(_evidence_id("cavemen"), _evidence_id())
    )
    assert forward == backward
    assert forward.opportunity_id == backward.opportunity_id
    assert list(forward.market_event_ids) == sorted([event, OTHER_EVENT])
    assert list(forward.evidence_ids) == sorted([_evidence_id(), _evidence_id("cavemen")])
    assert to_json(forward) == to_json(backward)
    duplicate = _field_error(
        lambda: _changed(_scored(), market_event_ids=(event, event)), "market_event_ids[1]"
    )
    assert duplicate.details["rule"] == "duplicate item"
    _field_error(
        lambda: _changed(_scored(), evidence_ids=(_evidence_id(), _evidence_id())), "evidence_ids[1]"
    )
    _field_error(lambda: _changed(_scored(), market_event_ids=()), "market_event_ids")
    _field_error(lambda: _changed(_scored(), evidence_ids=()), "evidence_ids")
    # Identifier kinds are not interchangeable.
    _field_error(lambda: _changed(_scored(), market_event_ids=(_evidence_id(),)), "market_event_ids[0]")
    _field_error(lambda: _changed(_scored(), evidence_ids=(event,)), "evidence_ids[0]")
    _field_error(lambda: _changed(_scored(), model_version_id=event), "model_version_id")
    _field_error(lambda: _changed(_scored(), opportunity_id=event), "opportunity_id")
    _field_error(lambda: _changed(_scored(), candidate_id="cnd_" + "G" * 32), "candidate_id")


# =========================================================================== scored-ness (CND-3 .. CND-6)
@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
def test_an_unscored_candidate_has_null_estimates_never_zero() -> None:
    record = _unscored()
    assert record.scored is False
    assert record.unscored_reason == "NO_MODEL_AVAILABLE"
    wire = to_wire(record)
    for name in (*SCORED_TOGETHER, *MODEL_ONLY_VALUES):
        assert getattr(record, name) is None, name
        assert wire[name] is None, name
    assert (record.net_r_quantiles, record.destinations, record.contributions) == ((), (), ())
    # Zero is a number, not "unknown": it is refused wherever the estimate does not exist.
    _invariant_error(lambda: _changed(record, expected_net_r=0.0), "CND-3")
    _invariant_error(lambda: _changed(record, expected_cost_r=0.0), "CND-6")
    _invariant_error(lambda: _changed(record, support_count=0), "CND-6")
    _invariant_error(lambda: _changed(record, baseline_r=0.0), "CND-6")


@pytest.mark.requirement("S00-ASG-13")
def test_a_scored_candidate_is_scored_and_may_have_a_negative_or_zero_expectation() -> None:
    assert _scored().scored is True
    for value in (-2.5, 0.0, 1e-9, 7.0):
        assert _scored(expected_net_r=value).expected_net_r == value
    for bad in (float("nan"), float("inf"), float("-inf"), "0.25", True, D("0.25")):
        _field_error(lambda bad=bad: _changed(_scored(), expected_net_r=bad), "expected_net_r")


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
@pytest.mark.parametrize(
    ("stdev", "stderr", "quantile_count"),
    list(itertools.product((None, 1.1), (None, 0.12), (0, 1, 2, 3))),
)
def test_uncertainty_rule_over_every_combination(
    stdev: float | None, stderr: float | None, quantile_count: int
) -> None:
    points = _quantiles((0.1, -1.0), (0.5, 0.2), (0.9, 2.0))[:quantile_count]
    stated = stdev is not None or stderr is not None or quantile_count >= 2

    def build() -> Candidate:
        return _scored(net_r_stdev=stdev, expected_net_r_stderr=stderr, net_r_quantiles=points)

    if stated:
        record = build()
        assert (record.net_r_stdev, record.expected_net_r_stderr, len(record.net_r_quantiles)) == (
            stdev,
            stderr,
            quantile_count,
        )
    else:
        _invariant_error(build, "CND-5")


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
def test_zero_uncertainty_is_a_statement_and_negative_uncertainty_is_invalid() -> None:
    record = _scored(net_r_stdev=0.0, expected_net_r_stderr=None, net_r_quantiles=())
    assert record.net_r_stdev == 0.0
    for field in (
        "net_r_stdev",
        "expected_net_r_stderr",
        "expected_win_r",
        "expected_loss_r",
        "expected_cost_r",
    ):
        _field_error(lambda field=field: _changed(_scored(), **{field: -0.01}), field)
    for field in ("win_probability_frac", "quality_score"):
        _field_error(lambda field=field: _changed(_scored(), **{field: 1.01}), field)
        _field_error(lambda field=field: _changed(_scored(), **{field: -0.01}), field)
    _field_error(lambda: _changed(_scored(), support_count=-1), "support_count")
    _field_error(lambda: _changed(_scored(), support_count=84.0), "support_count")
    _field_error(lambda: _changed(_scored(), expected_holding_time_ns=-1), "expected_holding_time_ns")


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("reason", ["no model", "no_model_available", "N", "", "X" * 49, 7])
def test_unscored_reason_is_a_machine_reason_code(reason: object) -> None:
    _field_error(lambda: _changed(_unscored(), unscored_reason=reason), "unscored_reason")


# =========================================================================== quantiles
@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("q_frac", [0.0, 1.0, -0.1, 1.1, float("nan"), True, "0.5"])
def test_quantile_probability_lies_strictly_between_0_and_1(q_frac: object) -> None:
    _field_error(lambda: QuantilePoint(q_frac=q_frac, value_r=0.0), "q_frac")  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-13")
def test_quantile_values_are_finite_and_the_order_is_not_repaired() -> None:
    _field_error(lambda: QuantilePoint(q_frac=0.5, value_r=float("inf")), "value_r")
    _field_error(lambda: QuantilePoint(q_frac=0.5, value_r=float("nan")), "value_r")
    ascending = _quantiles((0.1, -1.0), (0.5, 0.2), (0.9, 2.0))
    assert _scored(net_r_quantiles=ascending).net_r_quantiles == ascending
    # Unlike the canonically sorted lists, a mis-ordered distribution is an error, never re-sorted.
    for order in itertools.permutations(ascending):
        if order != ascending:
            _invariant_error(lambda order=order: _changed(_scored(), net_r_quantiles=order), "CND-7")
    _field_error(lambda: _changed(_scored(), net_r_quantiles=((0.5, 0.2),)), "net_r_quantiles[0]")


# =========================================================================== memory cutoff, status, expiry
@pytest.mark.requirement("S00-ASG-13", "S00-ASG-21")
def test_retrieved_historical_cases_are_prior_only() -> None:
    record = _scored(
        decision_time_ns=T0, as_of_time_ns=T0 - NS_PER_SECOND, memory_cutoff_time_ns=T0 - NS_PER_SECOND
    )
    assert record.memory_cutoff_time_ns == record.as_of_time_ns
    # Between the watermark and the decision time is still too late: the bound is the watermark.
    for late in (T0 - NS_PER_SECOND + 1, T0 - 1, T0, T0 + 1):
        _invariant_error(lambda late=late: _changed(record, memory_cutoff_time_ns=late), "CND-11")
    _field_error(lambda: _changed(record, memory_cutoff_time_ns=-1), "memory_cutoff_time_ns")


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("status", list(CandidateStatus))
@pytest.mark.parametrize("reasons", [(), ("FIXTURE_REASON",), ("B_REASON", "A_REASON")])
def test_status_and_reasons_matrix(status: CandidateStatus, reasons: tuple[str, ...]) -> None:
    needs_reason = status in (CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN)
    forbids_reason = status is CandidateStatus.AVAILABLE
    valid = (bool(reasons) or not needs_reason) and not (reasons and forbids_reason)

    def build() -> Candidate:
        return _changed(_scored(), status=status, status_reasons=reasons)

    if valid:
        assert build().status_reasons == tuple(sorted(reasons))
    else:
        _invariant_error(build, "CND-9")


@pytest.mark.requirement("S00-ASG-13")
def test_status_reasons_are_unique_reason_codes() -> None:
    withdrawn = _with_status(CandidateStatus.WITHDRAWN)
    duplicate = _field_error(lambda: _changed(withdrawn, status_reasons=("A_B", "A_B")), "status_reasons[1]")
    assert duplicate.details["rule"] == "duplicate item"
    _field_error(lambda: _changed(withdrawn, status_reasons=("stale evidence",)), "status_reasons[0]")
    _field_error(lambda: _changed(withdrawn, status_reasons="STALE_EVIDENCE"), "status_reasons")
    _field_error(lambda: _changed(withdrawn, status="WITHDRAWN"), "status")


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-23")
def test_an_available_candidate_is_unexpired_at_creation_and_expired_at_its_expiry_instant() -> None:
    record = _scored(valid_until_ns=T0 + 120 * NS_PER_SECOND)
    assert is_expired(now_ns=record.decision_time_ns, valid_until_ns=record.valid_until_ns) is False
    assert is_expired(now_ns=record.valid_until_ns - 1, valid_until_ns=record.valid_until_ns) is False
    assert is_expired(now_ns=record.valid_until_ns, valid_until_ns=record.valid_until_ns) is True
    _invariant_error(lambda: _changed(record, valid_until_ns=record.decision_time_ns), "CND-8")
    for status in (CandidateStatus.EXPIRED, CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN):
        closed = _with_status(status, valid_until_ns=T0 - 1)
        assert is_expired(now_ns=closed.decision_time_ns, valid_until_ns=closed.valid_until_ns) is True


# =========================================================================== destinations (DEST-1)
@pytest.mark.requirement("S00-ASG-13")
def test_dest1_accepts_probability_and_horizon_together_or_not_at_all() -> None:
    estimated = _destination(probability_frac=0.0, horizon_ns=0)
    assert (estimated.probability_frac, estimated.horizon_ns) == (0.0, 0)
    certain = _destination(probability_frac=1.0, horizon_ns=1)
    assert certain.probability_frac == 1.0
    bare = _destination(probability_frac=None, horizon_ns=None)
    assert (bare.probability_frac, bare.horizon_ns) == (None, None)
    assert _scored(destinations=(estimated,)).destinations == (estimated,)


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize(
    ("probability", "horizon"),
    [(0.4, None), (0.0, None), (None, 1800 * NS_PER_SECOND), (None, 0)],
    ids=["probability_only", "zero_probability_only", "horizon_only", "zero_horizon_only"],
)
def test_dest1_rejects_a_probability_without_horizon_and_vice_versa(
    probability: float | None, horizon: int | None
) -> None:
    error = _invariant_error(
        lambda: _destination(probability_frac=probability, horizon_ns=horizon),
        "DEST-1",
        contract="Destination",
    )
    assert error.details["rule"] == "probability_frac and horizon_ns are both set or both null"


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
def test_destination_fields_are_strict() -> None:
    for price in (D("0"), D("-2655"), 2655.0, 2655, "2655", D("NaN")):
        _field_error(lambda price=price: _destination(price=price), "price")
    for probability in (-0.01, 1.01, float("nan"), "0.4"):
        _field_error(
            lambda probability=probability: _destination(probability_frac=probability), "probability_frac"
        )
    _field_error(lambda: _destination(horizon_ns=-1), "horizon_ns")
    _field_error(lambda: _destination(horizon_ns=1.5), "horizon_ns")
    _field_error(lambda: _destination(scenario="Reversal"), "scenario")
    _field_error(lambda: _destination(label="prior high"), "label")


@pytest.mark.requirement("S00-ASG-13")
def test_destinations_are_unique_by_scenario_and_label_and_canonically_sorted() -> None:
    supplied = (
        _destination("reversal", "prior_high"),
        _destination("continuation", "session_high", price=D("2661.50")),
        _destination("reversal", "equal_lows", price=D("2646.00"), probability_frac=None, horizon_ns=None),
        _destination("continuation", "prior_high", price=D("2655.00")),
    )
    expected = [
        ("continuation", "prior_high"),
        ("continuation", "session_high"),
        ("reversal", "equal_lows"),
        ("reversal", "prior_high"),
    ]
    reference = _scored(destinations=supplied)
    for order in itertools.permutations(supplied):
        record = _scored(destinations=order)
        assert [(item.scenario, item.label) for item in record.destinations] == expected
        assert record == reference
        assert to_json(record) == to_json(reference)
    # Same (scenario, label) twice is a duplicate even with another price or probability.
    duplicate = (_destination(), _destination(price=D("2656.00"), probability_frac=0.1))
    error = _field_error(lambda: _changed(_scored(), destinations=duplicate), "destinations[1]")
    assert error.details["rule"] == "duplicate item"
    # The same price under two labels, or the same label under two scenarios, is not a duplicate.
    same_price = (_destination("reversal", "prior_high"), _destination("reversal", "round_number"))
    assert len(_scored(destinations=same_price).destinations) == 2


@pytest.mark.requirement("S00-ASG-13")
def test_a_nested_destination_violation_is_reported_when_decoding() -> None:
    wire = to_wire(_scored())
    wire["destinations"] = [{**wire["destinations"][0], "horizon_ns": None}]
    with pytest.raises(ContractValidationError) as info:
        from_wire(wire, Candidate)
    assert info.value.details["invariant"] == "DEST-1"
    assert info.value.details["path"] == "destinations[0]"


# =========================================================================== contributions (CONTRIB-1)
@pytest.mark.requirement("S00-ASG-13")
def test_contrib1_accepts_exactly_one_driver() -> None:
    by_evidence = Contribution(evidence_id=_evidence_id(), feature=None, weight_r=-0.15)
    by_feature = Contribution(evidence_id=None, feature="session_london", weight_r=0.05)
    assert by_evidence.key == _evidence_id()
    assert by_feature.key == "feature:session_london"
    assert by_evidence.weight_r == -0.15


@pytest.mark.requirement("S00-ASG-13")
def test_contrib1_rejects_both_drivers_and_no_driver() -> None:
    for evidence_id, feature in ((_evidence_id(), "session_london"), (None, None)):
        error = _invariant_error(
            lambda evidence_id=evidence_id, feature=feature: Contribution(
                evidence_id=evidence_id, feature=feature, weight_r=0.1
            ),
            "CONTRIB-1",
            contract="Contribution",
        )
        assert error.details["rule"] == "exactly one of evidence_id and feature is set"


@pytest.mark.requirement("S00-ASG-13")
def test_contribution_fields_are_strict() -> None:
    _field_error(lambda: Contribution(evidence_id="evd_1", feature=None, weight_r=0.1), "evidence_id")
    _field_error(lambda: Contribution(evidence_id=OTHER_EVENT, feature=None, weight_r=0.1), "evidence_id")
    _field_error(lambda: Contribution(evidence_id=None, feature="Session London", weight_r=0.1), "feature")
    _field_error(lambda: Contribution(evidence_id=None, feature="", weight_r=0.1), "feature")
    for weight in (float("nan"), float("inf"), "0.1", None, D("0.1")):
        _field_error(
            lambda weight=weight: Contribution(evidence_id=None, feature="cost", weight_r=weight), "weight_r"
        )


@pytest.mark.requirement("S00-ASG-13")
def test_contributions_are_unique_by_driver_and_sorted_by_driver() -> None:
    first, second = sorted([_evidence_id(), _evidence_id("cavemen")])
    supplied = (
        Contribution(evidence_id=None, feature="session_london", weight_r=0.05),
        Contribution(evidence_id=second, feature=None, weight_r=0.10),
        Contribution(evidence_id=None, feature="cost", weight_r=-0.08),
        Contribution(evidence_id=first, feature=None, weight_r=0.15),
    )
    expected = [first, second, "feature:cost", "feature:session_london"]
    reference = _scored(contributions=supplied)
    for order in itertools.permutations(supplied):
        record = _scored(contributions=order)
        assert [item.key for item in record.contributions] == expected
        assert record == reference
    for duplicate in (
        (supplied[1], Contribution(evidence_id=second, feature=None, weight_r=-0.3)),
        (supplied[0], Contribution(evidence_id=None, feature="session_london", weight_r=0.0)),
    ):
        error = _field_error(
            lambda duplicate=duplicate: _changed(_scored(), contributions=duplicate), "contributions[1]"
        )
        assert error.details["rule"] == "duplicate item"


# =========================================================================== XAUUSD only, prices, context
@pytest.mark.requirement("S00-ASG-13", "G01-01")
@pytest.mark.parametrize("symbol", ["XAGUSD", "EURUSD", "XAUEUR", "xauusd", "XAUUSD ", "XAUUSD.M", "", None])
def test_a_candidate_can_only_be_about_xauusd(symbol: object) -> None:
    for record in (_scored(), _unscored()):
        error = _field_error(lambda record=record: _changed(record, symbol=symbol), "symbol")
        assert isinstance(error, GieError)
    assert _scored().symbol == _unscored().symbol == "XAUUSD"


@pytest.mark.requirement("S00-ASG-13", "G01-01")
def test_decoding_refuses_a_candidate_for_another_symbol() -> None:
    with pytest.raises(ContractValidationError) as info:
        from_wire({**to_wire(_scored()), "symbol": "XAGUSD"}, Candidate)
    assert info.value.details["path"] == "symbol"
    assert info.value.details["rule"] == "must be one of ['XAUUSD']"


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
def test_prices_are_exact_positive_decimals() -> None:
    for field in ("reference_price", "risk_unit_price"):
        for bad in (D("0"), D("-0.01"), 2650.35, 2650, "2650.35", D("Infinity"), D("0.0000000000001")):
            _field_error(lambda field=field, bad=bad: _changed(_scored(), **{field: bad}), field)
    record = _scored(
        reference_price=D("0.000000000001"), risk_unit_price=D("999999999999999999.999999999999")
    )
    assert record.risk_unit_price == D("999999999999999999.999999999999")


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-20")
def test_construction_does_not_depend_on_the_ambient_decimal_context() -> None:
    fields = {"reference_price": D("2650.123456789012"), "risk_unit_price": D("1.999999999999")}
    outside = _scored(**fields)
    with decimal.localcontext(decimal.Context(prec=3, traps=[decimal.Inexact, decimal.Rounded])):
        inside = _scored(**fields)
        wire = to_json(inside)
    assert inside == outside
    assert wire == to_json(outside)
    assert '"reference_price":"2650.123456789012"' in wire
    assert '"risk_unit_price":"1.999999999999"' in wire


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("name", sorted(samples.variants()["Candidate"]))
def test_every_sample_candidate_survives_the_wire_round_trip(name: str) -> None:
    record = samples.variants()["Candidate"][name]
    assert from_wire(to_wire(record), Candidate) == record
    assert from_json(to_json(record), Candidate) == record
    assert to_json(from_json(to_json(record), Candidate)) == to_json(record)


# =========================================================================== validate_candidate_transition
def _illegal(previous: Candidate | None, revision: Candidate) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as info:
        validate_candidate_transition(previous, revision)
    error = info.value
    assert error.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert error.details["candidate_id"] == revision.candidate_id
    assert error.details["revision"] == revision.revision
    assert isinstance(error.details["rule"], str)
    return error


FINAL_STATUSES = (CandidateStatus.WITHDRAWN, CandidateStatus.EXPIRED, CandidateStatus.REJECTED)


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("first", list(CandidateStatus))
def test_a_chain_may_start_in_any_status_at_revision_0(first: CandidateStatus) -> None:
    validate_candidate_transition(None, _with_status(first))
    error = _illegal(None, _with_status(first, revision=1))
    assert error.details["rule"] == "the first record of a candidate has revision 0"


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("following", list(CandidateStatus))
def test_an_available_candidate_may_be_followed_by_any_status(following: CandidateStatus) -> None:
    validate_candidate_transition(_scored(), _with_status(following, revision=1))


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize("final", FINAL_STATUSES)
@pytest.mark.parametrize("following", list(CandidateStatus))
def test_withdrawn_expired_and_rejected_are_final(final: CandidateStatus, following: CandidateStatus) -> None:
    error = _illegal(_with_status(final), _with_status(following, revision=1))
    assert error.details["rule"] == f"{final.value} is final; no revision may follow it"


@pytest.mark.requirement("S00-ASG-13")
def test_a_whole_chain_of_revisions_is_accepted() -> None:
    chain = [
        _scored(),
        _scored(revision=1, seq=1, expected_net_r=0.4, evidence_ids=(_evidence_id(revision=1),)),
        _scored(revision=2, seq=5, decision_time_ns=T0 + NS_PER_SECOND, as_of_time_ns=T0 + NS_PER_SECOND),
        _with_status(
            CandidateStatus.WITHDRAWN,
            revision=3,
            seq=6,
            decision_time_ns=T0 + NS_PER_SECOND,
            as_of_time_ns=T0 + NS_PER_SECOND,
        ),
    ]
    validate_candidate_transition(None, chain[0])
    for previous, current in itertools.pairwise(chain):
        validate_candidate_transition(previous, current)
    assert len({record.opportunity_id for record in chain}) == 1
    variants = samples.variants()["Candidate"]
    validate_candidate_transition(variants["scored"], variants["rejected_revision"])


@pytest.mark.requirement("S00-ASG-13")
def test_the_revision_number_increases_by_exactly_one() -> None:
    first = _scored()
    for wrong in (first, _scored(revision=2, seq=2), _scored(revision=3, seq=3)):
        assert _illegal(first, wrong).details["rule"] == "revision must increase by exactly 1"
    assert _illegal(_scored(revision=2, seq=2), _scored(revision=1, seq=1)).details["rule"] == (
        "revision must increase by exactly 1"
    )


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-22")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"model_version_id": SHADOW_MODEL, "model_status": ModelStatus.SHADOW}, id="another_scorer"
        ),
        pytest.param({"side": Side.SELL}, id="side"),
        pytest.param({"setup_type": "other_setup"}, id="setup_type"),
        pytest.param({"horizon": Horizon.INTRADAY}, id="horizon"),
        pytest.param({"market_event_ids": (OTHER_EVENT,)}, id="market_event_ids"),
    ],
)
def test_a_revision_of_another_opportunity_or_scorer_cannot_continue_the_chain(
    overrides: dict[str, Any],
) -> None:
    error = _illegal(_scored(), _scored(revision=1, seq=1, **overrides))
    assert error.details["rule"] == "supersedes_candidate_id must name the previous revision"


@pytest.mark.requirement("S00-ASG-13")
def test_a_scored_chain_cannot_continue_an_unscored_one() -> None:
    unscored = _unscored(side=Side.BUY, setup_type="sweep_reversal_long")
    assert unscored.opportunity_id == _scored().opportunity_id
    _illegal(unscored, _scored(revision=1, seq=1))
    _illegal(_scored(), _unscored(side=Side.BUY, setup_type="sweep_reversal_long", revision=1, seq=1))


@pytest.mark.requirement("S00-ASG-13")
@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("run_id", {"run_id": OTHER_RUN}),
        ("mode", {"mode": RunMode.PAPER}),
        ("producer", {"producer": "prediction.scorer_b"}),
    ],
)
def test_run_mode_and_producer_may_not_change_between_revisions(
    field: str, overrides: dict[str, Any]
) -> None:
    error = _illegal(_scored(), _scored(revision=1, seq=1, **overrides))
    assert error.details["rule"] == f"{field} may not change between revisions"


@pytest.mark.requirement("S00-ASG-13", "S00-ASG-21")
def test_times_and_sequence_numbers_do_not_decrease_along_a_chain() -> None:
    first = _scored(seq=4)
    validate_candidate_transition(first, _scored(revision=1, seq=5))
    earlier_decision = _scored(revision=1, seq=5, decision_time_ns=T0 - 1, as_of_time_ns=AS_OF)
    assert _illegal(first, earlier_decision).details["rule"] == "decision_time_ns may not decrease"
    earlier_watermark = _scored(revision=1, seq=5, as_of_time_ns=AS_OF - 1, memory_cutoff_time_ns=None)
    assert _illegal(first, earlier_watermark).details["rule"] == "as_of_time_ns may not decrease"
    assert _illegal(first, _scored(revision=1, seq=3)).details["rule"] == "seq may not decrease"


@pytest.mark.requirement("S00-ASG-13")
def test_an_illegal_transition_error_is_journalable_data() -> None:
    error = _illegal(_with_status(CandidateStatus.REJECTED), _scored(revision=1, seq=1))
    payload = error.to_dict()
    assert payload["code"] == "GIE.STATE.ILLEGAL_TRANSITION"
    assert payload["details"] == dict(error.details)
    assert isinstance(error, GieError)
