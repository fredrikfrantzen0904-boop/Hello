"""Independent tests for the ``ModelVersion`` contract and the model registry rules.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - the fields, the invariants MDL-1 .. MDL-9 and the table
  "Model status transitions";
* ``specifications/SHARED_CONTRACTS.md`` section 9.1 ("a model is ACTIVE only with a recorded owner
  approval and its evidence; a model fitted on fixture data is never *validated*") and section 9.2
  ("a model reaches ACTIVE only through SHADOW; a re-activation names the rolled-back version and
  needs a new approval; one ACTIVE version per model family");
* the docstrings of ``gie/contracts/model_version.py``;
* owner specification G07 (shadow validation, versioned promotion with owner approval, rollback)
  and brief 09 (leakage tests, promotion rejection for weak evidence, rollback).

SYNTHETIC FIXTURE: every model, hash, approver name, decision reference, metric and timestamp in
this file is invented test data built with ``tests/gie/samples.py``.  ``FIXTURE-OWNER`` and
``D-99x`` are not owner decisions; nothing here is market data, a trained model or evidence of
performance.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from gie.contracts import identity
from gie.contracts.codec import from_wire, to_wire
from gie.contracts.common import DataOrigin, NamedValue
from gie.contracts.model_version import (
    MODEL_STATUS_TRANSITIONS,
    ContractRef,
    ModelKind,
    ModelStatus,
    ModelVersion,
    RegistryHistory,
    is_out_of_sample,
    validate_model_transition,
    validate_registry_history,
)
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.core.errors import ContractValidationError, GieError, IllegalTransitionError
from gie.core.timeutil import NS_PER_SECOND
from tests.gie import samples

RESEARCH = ModelStatus.RESEARCH
SHADOW = ModelStatus.SHADOW
ACTIVE = ModelStatus.ACTIVE
RETIRED = ModelStatus.RETIRED
ROLLED_BACK = ModelStatus.ROLLED_BACK
REJECTED = ModelStatus.REJECTED

DAY = 86_400 * NS_PER_SECOND
#: SYNTHETIC FIXTURE instants of the sample model (see tests/gie/samples.py).
CREATED = samples.T0 - 7200 * NS_PER_SECOND
BASE = samples.T0 - 3600 * NS_PER_SECOND
INFORMATION_END = samples.T0 - 30 * DAY
EMBARGO = DAY

#: SYNTHETIC FIXTURE activation evidence and approval (not an owner decision).
ACTIVATION: dict[str, Any] = {
    "evaluation_report_uri": "models/research/fixture_net_r/0.1.0/model_card.md",
    "evaluation_report_sha256": "56" * 32,
    "evaluation_data_origin": samples.ORIGIN,
    "promotion_criteria_sha256": "78" * 32,
    "shadow_run_ids": (samples.RUN_ID,),
    "approved_by": samples.FIXTURE_APPROVER,
    "approval_reference": samples.FIXTURE_DECISION,
}
OTHER_DECISION = "D-998"
OTHER_ID = identity.model_version_id("fixture_other", "0.1.0", "cd" * 32)

#: The transition table as published in FIELDS.md ("Model status transitions").
SPEC_TRANSITIONS: dict[ModelStatus | None, frozenset[ModelStatus]] = {
    None: frozenset({RESEARCH}),
    RESEARCH: frozenset({REJECTED, SHADOW}),
    SHADOW: frozenset({ACTIVE, REJECTED}),
    ACTIVE: frozenset({RETIRED, ROLLED_BACK}),
    RETIRED: frozenset({ACTIVE}),
    ROLLED_BACK: frozenset(),
    REJECTED: frozenset(),
}
#: Lowest revision at which a status can be reached (MDL-5).
MIN_REVISION = {RESEARCH: 0, SHADOW: 1, REJECTED: 1, ACTIVE: 2, RETIRED: 3, ROLLED_BACK: 3}


def _entry(status: ModelStatus, revision: int, **overrides: Any) -> ModelVersion:
    """A registry entry of the sample model in *status* (SYNTHETIC FIXTURE)."""
    fields: dict[str, Any] = {
        "status": status,
        "revision": revision,
        "seq": revision,
        "status_time_ns": BASE + revision * NS_PER_SECOND,
    }
    if status is ACTIVE:
        fields.update(ACTIVATION)
    if status in (REJECTED, RETIRED, ROLLED_BACK):
        fields["status_reasons"] = ("FIXTURE_REASON",)
    if status is ROLLED_BACK:
        fields.update(approved_by=samples.FIXTURE_APPROVER, approval_reference=OTHER_DECISION)
    fields.update(overrides)
    return samples.model_version(**fields)


def _wire_with(record: ModelVersion, overrides: dict[str, Any]) -> dict[str, Any]:
    """Wire form of a valid record with some fields replaced (to build an invalid message)."""
    wire = to_wire(record)
    types = {spec.name: spec.ftype for spec in ModelVersion.FIELD_SPECS}
    for name, value in overrides.items():
        wire[name] = types[name].encode(value)
    return wire


def _invariant(excinfo: pytest.ExceptionInfo[ContractValidationError]) -> object:
    return excinfo.value.details.get("invariant")


# =========================================================================== invariants MDL-1..9
VIOLATIONS = [
    pytest.param("MDL-1", RESEARCH, 0, {"model_version_id": OTHER_ID}, id="MDL-1-foreign-id"),
    pytest.param(
        "MDL-1",
        RESEARCH,
        0,
        {"model_version_id": identity.model_version_id(samples.MODEL_NAME, "0.1.1", samples.ARTIFACT_SHA)},
        id="MDL-1-id-of-another-semver",
    ),
    pytest.param(
        "MDL-1",
        RESEARCH,
        0,
        {"model_version_id": identity.model_version_id(samples.MODEL_NAME, "0.1.0", "ac" * 32)},
        id="MDL-1-id-of-another-artifact",
    ),
    pytest.param("MDL-2", ACTIVE, 2, {"approved_by": None}, id="MDL-2-active-without-approver"),
    pytest.param("MDL-2", ACTIVE, 2, {"approval_reference": None}, id="MDL-2-active-without-reference"),
    pytest.param(
        "MDL-2",
        ACTIVE,
        2,
        {"approved_by": None, "approval_reference": None},
        id="MDL-2-active-without-approval",
    ),
    pytest.param("MDL-2", ROLLED_BACK, 3, {"approved_by": None}, id="MDL-2-rollback-without-approver"),
    pytest.param(
        "MDL-2", ROLLED_BACK, 3, {"approval_reference": None}, id="MDL-2-rollback-without-reference"
    ),
    pytest.param("MDL-3", RESEARCH, 0, {"training_data_start_ns": None}, id="MDL-3-start-missing"),
    pytest.param("MDL-3", RESEARCH, 0, {"training_information_end_ns": None}, id="MDL-3-end-missing"),
    pytest.param("MDL-3", RESEARCH, 0, {"training_data_origin": None}, id="MDL-3-origin-missing"),
    pytest.param("MDL-3", RESEARCH, 0, {"training_data_sha256": None}, id="MDL-3-data-hash-missing"),
    pytest.param(
        "MDL-3",
        RESEARCH,
        0,
        {
            "training_data_start_ns": None,
            "training_information_end_ns": None,
            "training_data_origin": None,
            "training_data_sha256": None,
        },
        id="MDL-3-embargo-without-training-data",
    ),
    pytest.param(
        "MDL-3",
        RESEARCH,
        0,
        {"training_data_start_ns": INFORMATION_END + 1},
        id="MDL-3-start-after-information-end",
    ),
    pytest.param(
        "MDL-3",
        RESEARCH,
        0,
        {"training_information_end_ns": CREATED + 1},
        id="MDL-3-information-end-after-artifact-frozen",
    ),
    pytest.param("MDL-4", RESEARCH, 0, {"created_time_ns": BASE + 1}, id="MDL-4-created-after-status"),
    pytest.param("MDL-4", SHADOW, 1, {"status_time_ns": CREATED - 1}, id="MDL-4-status-before-created"),
    pytest.param("MDL-5", RESEARCH, 1, {}, id="MDL-5-research-at-revision-1"),
    pytest.param("MDL-5", SHADOW, 0, {}, id="MDL-5-shadow-at-revision-0"),
    pytest.param("MDL-5", REJECTED, 0, {}, id="MDL-5-rejected-at-revision-0"),
    pytest.param("MDL-5", ACTIVE, 0, {}, id="MDL-5-active-at-revision-0"),
    pytest.param("MDL-5", ACTIVE, 1, {}, id="MDL-5-active-at-revision-1"),
    pytest.param("MDL-5", RETIRED, 2, {}, id="MDL-5-retired-at-revision-2"),
    pytest.param("MDL-5", ROLLED_BACK, 2, {}, id="MDL-5-rolled-back-at-revision-2"),
    pytest.param("MDL-6", REJECTED, 1, {"status_reasons": ()}, id="MDL-6-rejected-without-reason"),
    pytest.param("MDL-6", RETIRED, 3, {"status_reasons": ()}, id="MDL-6-retired-without-reason"),
    pytest.param("MDL-6", ROLLED_BACK, 3, {"status_reasons": ()}, id="MDL-6-rolled-back-without-reason"),
    pytest.param("MDL-7", ACTIVE, 2, {"evaluation_report_uri": None}, id="MDL-7-no-report"),
    pytest.param("MDL-7", ACTIVE, 2, {"evaluation_report_sha256": None}, id="MDL-7-no-report-hash"),
    pytest.param("MDL-7", ACTIVE, 2, {"promotion_criteria_sha256": None}, id="MDL-7-no-criteria-hash"),
    pytest.param("MDL-7", ACTIVE, 2, {"shadow_run_ids": ()}, id="MDL-7-no-shadow-run"),
    pytest.param("MDL-7", ACTIVE, 2, {"evaluation_data_origin": None}, id="MDL-7-no-evaluation-data-origin"),
    pytest.param("MDL-8", SHADOW, 1, {"replaces_model_version_id": OTHER_ID}, id="MDL-8-replaces-on-shadow"),
    pytest.param(
        "MDL-8", RETIRED, 3, {"replaces_model_version_id": OTHER_ID}, id="MDL-8-replaces-on-retired"
    ),
    pytest.param(
        "MDL-8", RESEARCH, 0, {"rollback_of_model_version_id": OTHER_ID}, id="MDL-8-rollback-of-on-research"
    ),
    pytest.param(
        "MDL-8",
        ROLLED_BACK,
        3,
        {"rollback_of_model_version_id": OTHER_ID},
        id="MDL-8-rollback-of-on-rolled-back",
    ),
    pytest.param(
        "MDL-8", ACTIVE, 2, {"replaces_model_version_id": samples.MODEL_ID}, id="MDL-8-replaces-itself"
    ),
    pytest.param(
        "MDL-8", ACTIVE, 4, {"rollback_of_model_version_id": samples.MODEL_ID}, id="MDL-8-rollback-of-itself"
    ),
    pytest.param(
        "MDL-9", RESEARCH, 0, {"kind": ModelKind.COMPOSITE}, id="MDL-9-composite-without-components"
    ),
    pytest.param(
        "MDL-9",
        RESEARCH,
        0,
        {"component_model_version_ids": (OTHER_ID,)},
        id="MDL-9-components-on-non-composite",
    ),
    pytest.param(
        "MDL-9",
        RESEARCH,
        0,
        {"kind": ModelKind.COMPOSITE, "component_model_version_ids": (OTHER_ID, samples.MODEL_ID)},
        id="MDL-9-composite-contains-itself",
    ),
]


@pytest.mark.requirement("S00-ASG-18", "G07-10")
@pytest.mark.parametrize(("invariant", "status", "revision", "overrides"), VIOLATIONS)
def test_violating_record_cannot_be_constructed(
    invariant: str, status: ModelStatus, revision: int, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        _entry(status, revision, **overrides)
    assert _invariant(excinfo) == invariant
    assert excinfo.value.details["contract"] == "ModelVersion"


@pytest.mark.requirement("S00-ASG-18", "G07-10")
@pytest.mark.parametrize(("invariant", "status", "revision", "overrides"), VIOLATIONS)
def test_violating_record_cannot_be_decoded(
    invariant: str, status: ModelStatus, revision: int, overrides: dict[str, Any]
) -> None:
    # The valid neighbour of the violating record: same status at its lowest legal revision.
    valid = _entry(status, max(revision, MIN_REVISION[status]) if status is not RESEARCH else 0)
    wire = _wire_with(valid, {"revision": revision, **overrides})
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert _invariant(excinfo) == invariant


def _composite(**overrides: Any) -> ModelVersion:
    return samples.model_version(
        model_name="fixture_combo",
        kind=ModelKind.COMPOSITE,
        component_model_version_ids=(OTHER_ID, samples.MODEL_ID),
        **overrides,
    )


def _ruleset(**overrides: Any) -> ModelVersion:
    fields: dict[str, Any] = {
        "model_name": "fixture_ruleset",
        "kind": ModelKind.RULESET,
        "training_data_origin": None,
        "training_data_sha256": None,
        "training_config_sha256": None,
        "training_data_start_ns": None,
        "training_information_end_ns": None,
        "embargo_ns": None,
        "split_spec_sha256": None,
        "feature_schema_sha256": None,
        "seed": None,
        "metrics": (),
    }
    fields.update(overrides)
    return samples.model_version(**fields)


ACCEPTED = [
    pytest.param("MDL-1", lambda: _entry(RESEARCH, 0), id="MDL-1-sample"),
    pytest.param(
        "MDL-1",
        lambda: samples.model_version(model_name="fixture_other", artifact_sha256="cd" * 32),
        id="MDL-1-other-model-recomputed-id",
    ),
    pytest.param("MDL-2", lambda: _entry(ACTIVE, 2), id="MDL-2-active-with-approval"),
    pytest.param("MDL-2", lambda: _entry(ROLLED_BACK, 3), id="MDL-2-rolled-back-with-approval"),
    pytest.param("MDL-2", lambda: _entry(SHADOW, 1), id="MDL-2-shadow-needs-no-approval"),
    pytest.param("MDL-2", lambda: _entry(RETIRED, 3), id="MDL-2-retired-needs-no-approval"),
    pytest.param("MDL-3", lambda: _entry(RESEARCH, 0), id="MDL-3-all-training-fields"),
    pytest.param("MDL-3", _ruleset, id="MDL-3-no-training-data-at-all"),
    pytest.param(
        "MDL-3",
        lambda: _entry(RESEARCH, 0, training_data_start_ns=INFORMATION_END),
        id="MDL-3-start-equals-information-end",
    ),
    pytest.param(
        "MDL-3",
        lambda: _entry(RESEARCH, 0, training_information_end_ns=CREATED),
        id="MDL-3-information-end-equals-created",
    ),
    pytest.param("MDL-3", lambda: _entry(RESEARCH, 0, embargo_ns=None), id="MDL-3-data-without-embargo"),
    pytest.param("MDL-3", lambda: _entry(RESEARCH, 0, embargo_ns=0), id="MDL-3-zero-embargo"),
    pytest.param(
        "MDL-4", lambda: _entry(RESEARCH, 0, created_time_ns=BASE), id="MDL-4-created-equals-status"
    ),
    pytest.param("MDL-4", lambda: _entry(RESEARCH, 0), id="MDL-4-created-before-status"),
    pytest.param("MDL-5", lambda: _entry(RESEARCH, 0), id="MDL-5-research-0"),
    pytest.param("MDL-5", lambda: _entry(SHADOW, 1), id="MDL-5-shadow-1"),
    pytest.param("MDL-5", lambda: _entry(SHADOW, 7), id="MDL-5-shadow-7"),
    pytest.param("MDL-5", lambda: _entry(REJECTED, 1), id="MDL-5-rejected-1"),
    pytest.param("MDL-5", lambda: _entry(REJECTED, 2), id="MDL-5-rejected-2"),
    pytest.param("MDL-5", lambda: _entry(ACTIVE, 2), id="MDL-5-active-2"),
    pytest.param(
        "MDL-5", lambda: _entry(ACTIVE, 4, rollback_of_model_version_id=OTHER_ID), id="MDL-5-active-4"
    ),
    pytest.param("MDL-5", lambda: _entry(RETIRED, 3), id="MDL-5-retired-3"),
    pytest.param("MDL-5", lambda: _entry(ROLLED_BACK, 3), id="MDL-5-rolled-back-3"),
    pytest.param("MDL-6", lambda: _entry(REJECTED, 1), id="MDL-6-rejected-with-reason"),
    pytest.param("MDL-6", lambda: _entry(RETIRED, 3), id="MDL-6-retired-with-reason"),
    pytest.param("MDL-6", lambda: _entry(ROLLED_BACK, 3), id="MDL-6-rolled-back-with-reason"),
    pytest.param("MDL-6", lambda: _entry(SHADOW, 1, status_reasons=()), id="MDL-6-shadow-without-reason"),
    pytest.param(
        "MDL-6",
        lambda: _entry(SHADOW, 1, status_reasons=("PASSED_RESEARCH_GATE",)),
        id="MDL-6-shadow-with-reason",
    ),
    pytest.param("MDL-7", lambda: _entry(ACTIVE, 2), id="MDL-7-active-with-evidence"),
    pytest.param(
        "MDL-7",
        lambda: _entry(ACTIVE, 2, shadow_run_ids=(samples.RUN_ID, identity.run_id("PAPER", "1" * 64))),
        id="MDL-7-active-with-two-shadow-runs",
    ),
    pytest.param("MDL-7", lambda: _entry(SHADOW, 1), id="MDL-7-shadow-without-evidence"),
    pytest.param(
        "MDL-8", lambda: _entry(ACTIVE, 2, replaces_model_version_id=OTHER_ID), id="MDL-8-active-replaces"
    ),
    pytest.param(
        "MDL-8",
        lambda: _entry(ACTIVE, 4, replaces_model_version_id=OTHER_ID, rollback_of_model_version_id=OTHER_ID),
        id="MDL-8-reactivation-names-rolled-back-version",
    ),
    pytest.param("MDL-8", lambda: _entry(ACTIVE, 2), id="MDL-8-first-activation-without-links"),
    pytest.param("MDL-9", _composite, id="MDL-9-composite-with-components"),
    pytest.param("MDL-9", lambda: _entry(RESEARCH, 0), id="MDL-9-plain-model-without-components"),
]


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize(("invariant", "build"), ACCEPTED)
def test_conforming_record_is_accepted_and_round_trips(invariant: str, build: Any) -> None:
    model = build()
    assert type(model) is ModelVersion
    assert invariant in {rule.id for rule in ModelVersion.ALL_INVARIANTS}
    assert from_wire(to_wire(model)) == model


@pytest.mark.requirement("S00-ASG-18")
def test_every_invariant_of_the_field_reference_has_an_accepted_and_a_rejected_case() -> None:
    documented = {f"MDL-{number}" for number in range(1, 10)}
    assert {rule.id for rule in ModelVersion.ALL_INVARIANTS} == documented
    assert {case.values[0] for case in VIOLATIONS} == documented
    assert {case.values[0] for case in ACCEPTED} == documented


@pytest.mark.requirement("S00-ASG-18", "S00-ASG-22")
def test_model_version_id_is_recomputable_from_name_version_and_artifact() -> None:
    model = samples.model_version()
    assert model.model_version_id == identity.model_version_id(
        model.model_name, model.semver, model.artifact_sha256
    )
    assert model.model_version_id.startswith("mdl_")
    other_artifact = samples.model_version(artifact_sha256="ac" * 32)
    other_version = samples.model_version(semver="0.1.1")
    assert len({model.model_version_id, other_artifact.model_version_id, other_version.model_version_id}) == 3


@pytest.mark.requirement("S00-ASG-18", "S00-ASG-25")
def test_stream_and_ordering_time_of_a_registry_entry() -> None:
    model = _entry(SHADOW, 1)
    assert stream_name(model) == "model_version/learning.registry"
    assert ordering_time_ns(model) == model.status_time_ns
    key = ordering_key(model)
    assert (key.time_ns, key.rank, key.stream, key.seq) == (model.status_time_ns, 90, stream_name(model), 1)


# =========================================================================== field rules
@pytest.mark.requirement("G07-10", "S00-ASG-18")
@pytest.mark.parametrize(
    "name",
    ["ab", "FIXTURE-OWNER", "Fixture Owner", "fixture.owner@example.invalid", "A.", "x" * 64, "Owner 2"],
)
def test_approver_name_accepted(name: str) -> None:
    assert _entry(ACTIVE, 2, approved_by=name).approved_by == name


@pytest.mark.requirement("G07-10", "S00-ASG-18")
@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "  ",
        "o",
        " owner",
        "owner ",
        " owner ",
        "\towner",
        "owner\n",
        "own\ner",
        "-owner",
        ".owner",
        "owner-",
        "owner_",
        "owner;drop",
        "x" * 65,
        "owner\u00a0",
        "owner\u200b",
    ],
)
def test_approver_name_refused(name: str) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        _entry(ACTIVE, 2, approved_by=name)
    assert excinfo.value.details["path"] == "approved_by"


@pytest.mark.requirement("G07-10", "S00-ASG-18")
@pytest.mark.parametrize("name", [None, 7, b"FIXTURE-OWNER", ("FIXTURE-OWNER",), True])
def test_approver_that_is_not_a_name_never_activates(name: object) -> None:
    with pytest.raises(ContractValidationError):
        _entry(ACTIVE, 2, approved_by=name)


@pytest.mark.requirement("G07-10", "S00-ASG-18")
@pytest.mark.parametrize("reference", ["D-000", "D-007", "D-999", "D-1234", "D-12345"])
def test_decision_reference_accepted(reference: str) -> None:
    assert _entry(ACTIVE, 2, approval_reference=reference).approval_reference == reference


@pytest.mark.requirement("G07-10", "S00-ASG-18")
@pytest.mark.parametrize(
    "reference",
    [
        "",
        "D-1",
        "D-12",
        "D-123456",
        "d-123",
        "D123",
        "D_123",
        "D-12a",
        "CR-001",
        "B-001",
        " D-123",
        "D-123 ",
        "D--12",
    ],
)
def test_decision_reference_refused(reference: str) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        _entry(ACTIVE, 2, approval_reference=reference)
    assert excinfo.value.details["path"] == "approval_reference"


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize(
    "uri",
    [
        "https://example.invalid/model.bin",
        "s3://fixture-bucket/model.bin",
        "fixture-user@example.invalid:models/model.bin",
        "/models/model.bin",
        "../models/model.bin",
        "models/../../model.bin",
        "models//model.bin",
        "models/model.bin?version=1",
        "models\\model.bin",
        "models/model bin",
        "",
        "m" * 201,
    ],
)
def test_artifact_location_is_a_plain_repository_relative_path(uri: str) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        samples.model_version(artifact_uri=uri)
    assert excinfo.value.details["path"] == "artifact_uri"
    with pytest.raises(ContractValidationError) as excinfo:
        _entry(ACTIVE, 2, evaluation_report_uri=uri)
    assert excinfo.value.details["path"] == "evaluation_report_uri"


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_sha256", "AB" * 32),
        ("artifact_sha256", "ab" * 31),
        ("artifact_sha256", "zz" * 32),
        ("code_commit", "0123456789ABCDEF0123456789abcdef01234567"),
        ("code_commit", "0123456"),
        ("code_commit", "g" * 40),
        ("training_data_sha256", "ef" * 33),
        ("model_name", "Fixture"),
        ("model_name", "f"),
        ("semver", "1.0"),
        ("semver", "01.0.0"),
        ("semver", "v1.0.0"),
        ("revision", -1),
        ("revision", True),
        ("revision", 1.0),
        ("seed", -1),
        ("embargo_ns", -1),
        ("status_time_ns", -1),
        ("status_time_ns", 2**63),
        ("kind", "NET_R_MODEL"),
        ("status", "RESEARCH"),
        ("status_reasons", ("lower_case",)),
        ("status_reasons", ("DUPLICATE_REASON", "DUPLICATE_REASON")),
        ("shadow_run_ids", (samples.RUN_ID, samples.RUN_ID)),
        ("shadow_run_ids", (samples.MODEL_ID,)),
        ("component_model_version_ids", (samples.RUN_ID,)),
        ("derived_from_model_version_id", samples.RUN_ID),
        (
            "metrics",
            (NamedValue(name="oos_mean_net_r", value=0.0), NamedValue(name="oos_mean_net_r", value=1.0)),
        ),
        ("metrics", ({"name": "oos_mean_net_r", "value": 0.0},)),
        (
            "contract_versions",
            (ContractRef(contract="Tick", version="1.0.0"), ContractRef(contract="Tick", version="1.1.0")),
        ),
    ],
)
def test_malformed_field_is_refused_with_its_path(field: str, value: object) -> None:
    # Built from a valid record so that the record itself (not the sample builder) refuses.
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(samples.model_version(), **{field: value})
    assert str(excinfo.value.details["path"]).startswith(field)


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize(
    ("contract", "version"),
    [
        ("tick", "1.0.0"),
        ("T", "1.0.0"),
        ("Trade Plan", "1.0.0"),
        ("Tick", "1.0"),
        ("Tick", "1.0.0-rc1"),
        ("", "1.0.0"),
    ],
)
def test_contract_reference_is_a_contract_name_and_a_semantic_version(contract: str, version: str) -> None:
    assert ContractRef(contract="TradePlan", version="1.2.3").version == "1.2.3"
    with pytest.raises(ContractValidationError):
        ContractRef(contract=contract, version=version)


@pytest.mark.requirement("S00-ASG-18")
def test_unordered_lists_are_stored_in_their_canonical_order() -> None:
    second_run = identity.run_id("PAPER", "1" * 64)
    runs = tuple(sorted((samples.RUN_ID, second_run)))
    refs = (ContractRef(contract="Tick", version="1.0.0"), ContractRef(contract="Evidence", version="1.0.0"))
    one = _entry(ACTIVE, 2, shadow_run_ids=runs, contract_versions=refs, status_reasons=("B_CODE", "A_CODE"))
    two = _entry(
        ACTIVE,
        2,
        shadow_run_ids=tuple(reversed(runs)),
        contract_versions=tuple(reversed(refs)),
        status_reasons=("A_CODE", "B_CODE"),
    )
    assert one == two
    assert one.shadow_run_ids == runs
    assert [ref.contract for ref in one.contract_versions] == ["Evidence", "Tick"]
    assert one.status_reasons == ("A_CODE", "B_CODE")
    assert to_wire(one) == to_wire(two)


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize("value", ["LIVE", "PRODUCTION", "active", "", None, 2])
def test_unknown_status_vocabulary_is_refused_when_decoding(value: object) -> None:
    wire = to_wire(samples.model_version())
    wire["status"] = value
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == "status"


@pytest.mark.requirement("S00-ASG-18")
def test_a_record_is_immutable() -> None:
    model = samples.model_version()
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.status = ACTIVE  # type: ignore[misc]
    assert model.status is RESEARCH


# =========================================================================== validated
@pytest.mark.requirement("G07-10", "COM-07", "M-AUTH-07")
def test_active_model_fitted_on_fixture_data_is_never_validated() -> None:
    active = samples.model_lifecycle()[2]
    assert active.status is ACTIVE
    assert active.training_data_origin is DataOrigin.SYNTHETIC_FIXTURE
    assert active.validated is False


REAL_ORIGINS = [origin for origin in DataOrigin if origin is not DataOrigin.SYNTHETIC_FIXTURE]


@pytest.mark.requirement("G07-10")
@pytest.mark.parametrize("training", REAL_ORIGINS, ids=lambda origin: f"trained-{origin.value}")
@pytest.mark.parametrize("evaluation", REAL_ORIGINS, ids=lambda origin: f"evaluated-{origin.value}")
def test_active_model_fitted_and_evaluated_on_real_data_is_validated(
    training: DataOrigin, evaluation: DataOrigin
) -> None:
    model = _entry(ACTIVE, 2, training_data_origin=training, evaluation_data_origin=evaluation)
    assert model.validated is True


@pytest.mark.requirement("G07-10", "COM-07")
@pytest.mark.parametrize("training", REAL_ORIGINS, ids=lambda origin: origin.value)
def test_active_model_evaluated_on_fixture_data_is_never_validated(training: DataOrigin) -> None:
    # Rework 2: 'validated' needs a non-fixture training AND evaluation origin.
    model = _entry(
        ACTIVE, 2, training_data_origin=training, evaluation_data_origin=DataOrigin.SYNTHETIC_FIXTURE
    )
    assert model.validated is False
    assert _entry(ACTIVE, 2, training_data_origin=training).validated is False  # ACTIVATION: fixture


@pytest.mark.requirement("G07-10", "COM-07")
@pytest.mark.parametrize("evaluation", REAL_ORIGINS, ids=lambda origin: origin.value)
def test_active_model_fitted_on_fixture_data_is_not_validated_by_a_real_evaluation(
    evaluation: DataOrigin,
) -> None:
    model = _entry(ACTIVE, 2, evaluation_data_origin=evaluation)
    assert model.training_data_origin is DataOrigin.SYNTHETIC_FIXTURE
    assert model.validated is False


@pytest.mark.requirement("G07-10")
def test_a_rule_set_is_validated_by_its_evaluation_alone() -> None:
    # A rule set uses no training data; its validation rests on the evaluation origin.
    assert _ruleset(**{**_active_fields(), "evaluation_data_origin": DataOrigin.HISTORICAL}).validated
    assert _ruleset(**_active_fields()).validated is False  # evaluated on fixture data


def _active_fields() -> dict[str, Any]:
    return {
        **ACTIVATION,
        "status": ACTIVE,
        "revision": 2,
        "seq": 2,
        "status_time_ns": BASE + 2 * NS_PER_SECOND,
    }


@pytest.mark.requirement("G07-10", "S00-ASG-18")
def test_the_evaluation_data_origin_is_mandatory_for_active_only() -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        _entry(ACTIVE, 2, training_data_origin=DataOrigin.HISTORICAL, evaluation_data_origin=None)
    assert _invariant(excinfo) == "MDL-7"
    assert "evaluation data" in str(excinfo.value.details["rule"])
    # A version that is not ACTIVE may state it or not.
    assert _entry(SHADOW, 1).evaluation_data_origin is None
    assert _entry(SHADOW, 1, evaluation_data_origin=DataOrigin.HISTORICAL).validated is False
    # ... and it may change between status revisions (it is not part of the immutable artifact).
    validate_model_transition(_entry(SHADOW, 1), _entry(ACTIVE, 2))


@pytest.mark.requirement("G07-09", "G07-10")
@pytest.mark.parametrize(
    ("status", "revision"),
    [(RESEARCH, 0), (SHADOW, 1), (REJECTED, 1), (RETIRED, 3), (ROLLED_BACK, 3)],
)
def test_only_an_active_version_can_be_validated(status: ModelStatus, revision: int) -> None:
    for training in DataOrigin:
        for evaluation in (None, *DataOrigin):
            model = _entry(status, revision, training_data_origin=training, evaluation_data_origin=evaluation)
            assert model.validated is False


@pytest.mark.requirement("COM-07", "M-AUTH-07")
def test_no_sample_model_claims_to_be_validated() -> None:
    models = samples.variants()["ModelVersion"]
    assert len(models) >= 4
    for name, model in models.items():
        assert isinstance(model, ModelVersion)
        assert model.training_data_origin in (None, DataOrigin.SYNTHETIC_FIXTURE), name
        assert model.validated is False, name


# =========================================================================== transition table
@pytest.mark.requirement("S00-ASG-18", "G07-09", "G07-11")
def test_transition_table_is_the_published_one() -> None:
    assert dict(MODEL_STATUS_TRANSITIONS) == SPEC_TRANSITIONS
    assert set(MODEL_STATUS_TRANSITIONS) == {None, *ModelStatus}


def _pair(before: ModelStatus | None, after: ModelStatus) -> tuple[ModelVersion | None, ModelVersion]:
    """A (previous, entry) pair that is correct in everything except possibly the status change."""
    if before is None:
        return None, _entry(after, MIN_REVISION[after])
    previous = _entry(before, MIN_REVISION[before])
    revision = max(previous.revision + 1, MIN_REVISION[after])
    extra: dict[str, Any] = {}
    if after is ACTIVE and before is RETIRED:
        extra = {"rollback_of_model_version_id": OTHER_ID, "approval_reference": OTHER_DECISION}
    if after is RESEARCH:
        revision = 0
        extra = {"status_time_ns": previous.status_time_ns + NS_PER_SECOND}
    return previous, _entry(after, revision, **extra)


ALL_PAIRS = [(before, after) for before in (None, *ModelStatus) for after in ModelStatus]


@pytest.mark.requirement("S00-ASG-18", "G07-09", "G07-10", "G07-11")
@pytest.mark.parametrize(
    ("before", "after"),
    [pair for pair in ALL_PAIRS if pair[1] in SPEC_TRANSITIONS[pair[0]]],
    ids=lambda value: "unregistered" if value is None else value.value,
)
def test_every_allowed_row_of_the_transition_table_is_accepted(
    before: ModelStatus | None, after: ModelStatus
) -> None:
    previous, entry = _pair(before, after)
    assert entry.revision == (0 if previous is None else previous.revision + 1)
    validate_model_transition(previous, entry)


@pytest.mark.requirement("S00-ASG-18", "G07-09", "G07-10", "G07-11")
@pytest.mark.parametrize(
    ("before", "after"),
    [pair for pair in ALL_PAIRS if pair[1] not in SPEC_TRANSITIONS[pair[0]]],
    ids=lambda value: "unregistered" if value is None else value.value,
)
def test_every_other_status_change_is_refused(before: ModelStatus | None, after: ModelStatus) -> None:
    previous, entry = _pair(before, after)
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_model_transition(previous, entry)
    assert excinfo.value.code == "GIE.STATE.ILLEGAL_TRANSITION"
    assert excinfo.value.details["model_version_id"] == entry.model_version_id


@pytest.mark.requirement("G07-09")
def test_active_is_first_reached_only_from_shadow() -> None:
    for before in (None, RESEARCH, REJECTED, ROLLED_BACK, ACTIVE):
        previous, entry = _pair(before, ACTIVE)
        with pytest.raises(IllegalTransitionError):
            validate_model_transition(previous, entry)
    validate_model_transition(*_pair(SHADOW, ACTIVE))


@pytest.mark.requirement("G07-10", "G07-11")
@pytest.mark.parametrize("final", [REJECTED, ROLLED_BACK])
def test_rejected_and_rolled_back_are_final(final: ModelStatus) -> None:
    assert SPEC_TRANSITIONS[final] == frozenset()
    for after in ModelStatus:
        previous, entry = _pair(final, after)
        with pytest.raises(IllegalTransitionError):
            validate_model_transition(previous, entry)


@pytest.mark.requirement("S00-ASG-18")
def test_first_registry_entry_is_research_at_revision_0() -> None:
    validate_model_transition(None, _entry(RESEARCH, 0))
    for status in (SHADOW, ACTIVE, REJECTED, RETIRED, ROLLED_BACK):
        with pytest.raises(IllegalTransitionError):
            validate_model_transition(None, _entry(status, MIN_REVISION[status]))


# =========================================================================== transition details
IMMUTABLE_CHANGES = [
    pytest.param({"model_name": "fixture_other"}, id="model_name"),
    pytest.param({"semver": "0.1.1"}, id="semver"),
    pytest.param({"artifact_sha256": "ac" * 32}, id="artifact_sha256"),
    pytest.param(
        {"model_version_id": OTHER_ID, "model_name": "fixture_other", "artifact_sha256": "cd" * 32}, id="id"
    ),
    pytest.param({"kind": ModelKind.PAYOFF_MODEL}, id="kind"),
    pytest.param({"created_time_ns": CREATED + 1}, id="created_time_ns"),
    pytest.param({"artifact_uri": "models/research/fixture_net_r/0.1.0/other.bin"}, id="artifact_uri"),
    pytest.param({"code_commit": "f" * 40}, id="code_commit"),
    pytest.param({"training_data_origin": DataOrigin.HISTORICAL}, id="training_data_origin"),
    pytest.param({"training_data_sha256": "aa" * 32}, id="training_data_sha256"),
    pytest.param({"training_config_sha256": "aa" * 32}, id="training_config_sha256"),
    pytest.param({"training_config_sha256": None}, id="training_config_sha256-removed"),
    pytest.param({"training_data_start_ns": INFORMATION_END - DAY}, id="training_data_start_ns"),
    pytest.param({"training_information_end_ns": INFORMATION_END - 1}, id="training_information_end_ns"),
    pytest.param({"embargo_ns": EMBARGO - 1}, id="embargo_ns-shortened"),
    pytest.param({"embargo_ns": None}, id="embargo_ns-removed"),
    pytest.param({"split_spec_sha256": "aa" * 32}, id="split_spec_sha256"),
    pytest.param({"feature_schema_sha256": "aa" * 32}, id="feature_schema_sha256"),
    pytest.param(
        {"contract_versions": (ContractRef(contract="Tick", version="1.0.0"),)}, id="contract_versions"
    ),
    pytest.param({"derived_from_model_version_id": OTHER_ID}, id="derived_from_model_version_id"),
    pytest.param({"seed": 8}, id="seed"),
    pytest.param({"seed": None}, id="seed-removed"),
]


@pytest.mark.requirement("S00-ASG-18", "G07-10")
@pytest.mark.parametrize("change", IMMUTABLE_CHANGES)
def test_the_artifact_of_a_model_version_is_immutable(change: dict[str, Any]) -> None:
    previous = _entry(RESEARCH, 0)
    validate_model_transition(previous, _entry(SHADOW, 1))
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_model_transition(previous, _entry(SHADOW, 1, **change))
    assert "immutable" in str(excinfo.value.details["rule"])


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_components_of_a_composite_are_immutable() -> None:
    previous = _composite()
    third = identity.model_version_id("fixture_third", "0.1.0", "ee" * 32)
    kept = {"seq": 1, "revision": 1, "status": SHADOW, "status_time_ns": BASE + NS_PER_SECOND}
    validate_model_transition(previous, _composite(**kept))
    with pytest.raises(IllegalTransitionError):
        validate_model_transition(
            previous,
            samples.model_version(
                model_name="fixture_combo",
                kind=ModelKind.COMPOSITE,
                component_model_version_ids=(OTHER_ID, third),
                **kept,
            ),
        )


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize("revision", [1, 3, 4, 9])
def test_revision_increases_by_exactly_one(revision: int) -> None:
    previous = _entry(SHADOW, 1)
    validate_model_transition(previous, _entry(REJECTED, 2))
    with pytest.raises(IllegalTransitionError):
        validate_model_transition(
            previous, _entry(REJECTED, revision, status_time_ns=BASE + 60 * NS_PER_SECOND)
        )


@pytest.mark.requirement("S00-ASG-18", "S00-ASG-25")
@pytest.mark.parametrize("delta", [0, -1, -NS_PER_SECOND])
def test_status_time_strictly_increases(delta: int) -> None:
    previous = _entry(SHADOW, 1)
    validate_model_transition(previous, _entry(ACTIVE, 2, status_time_ns=previous.status_time_ns + 1))
    with pytest.raises(IllegalTransitionError):
        validate_model_transition(previous, _entry(ACTIVE, 2, status_time_ns=previous.status_time_ns + delta))


@pytest.mark.requirement("G07-11")
def test_rollback_of_is_set_exactly_on_a_reactivation() -> None:
    retired = _entry(RETIRED, 3)
    reactivated = {"rollback_of_model_version_id": OTHER_ID, "approval_reference": OTHER_DECISION}
    validate_model_transition(retired, _entry(ACTIVE, 4, **reactivated))
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_model_transition(retired, _entry(ACTIVE, 4, approval_reference=OTHER_DECISION))
    assert "rollback_of_model_version_id" in str(excinfo.value.details["rule"])
    # A first activation is not a rollback and may not claim to be one.
    shadow = _entry(SHADOW, 1)
    validate_model_transition(shadow, _entry(ACTIVE, 2))
    with pytest.raises(IllegalTransitionError):
        validate_model_transition(shadow, _entry(ACTIVE, 2, rollback_of_model_version_id=OTHER_ID))


@pytest.mark.requirement("S00-ASG-18", "G07-09", "G07-10", "G07-11")
def test_sample_lifecycle_and_a_full_rollback_path_are_valid_step_by_step() -> None:
    previous: ModelVersion | None = None
    for entry in samples.model_lifecycle():
        validate_model_transition(previous, entry)
        previous = entry
    path = [
        _entry(RESEARCH, 0),
        _entry(SHADOW, 1),
        _entry(ACTIVE, 2),
        _entry(RETIRED, 3),
        _entry(ACTIVE, 4, rollback_of_model_version_id=OTHER_ID, approval_reference=OTHER_DECISION),
        _entry(ROLLED_BACK, 5),
    ]
    previous = None
    for entry in path:
        validate_model_transition(previous, entry)
        previous = entry


@pytest.mark.requirement("S00-ASG-18")
def test_a_refused_transition_is_a_gie_error_with_journalable_details() -> None:
    with pytest.raises(GieError) as excinfo:
        validate_model_transition(_entry(RESEARCH, 0), _entry(ACTIVE, 2))
    document = excinfo.value.to_dict()
    assert document["code"] == "GIE.STATE.ILLEGAL_TRANSITION"
    assert excinfo.value.details["revision"] == 2
    assert isinstance(excinfo.value.details["rule"], str)


# =========================================================================== registry history
A: dict[str, Any] = {"artifact_sha256": "aa" * 32, "semver": "0.1.0"}
B: dict[str, Any] = {"artifact_sha256": "bb" * 32, "semver": "0.2.0"}
C: dict[str, Any] = {"artifact_sha256": "cc" * 32, "semver": "0.3.0"}
A_ID = identity.model_version_id(samples.MODEL_NAME, "0.1.0", "aa" * 32)
B_ID = identity.model_version_id(samples.MODEL_NAME, "0.2.0", "bb" * 32)
C_ID = identity.model_version_id(samples.MODEL_NAME, "0.3.0", "cc" * 32)
#: A version of another model family (SYNTHETIC FIXTURE).
X: dict[str, Any] = {
    "model_name": "fixture_payoff",
    "kind": ModelKind.PAYOFF_MODEL,
    "artifact_sha256": "dd" * 32,
}
X_ID = identity.model_version_id("fixture_payoff", "0.1.0", "dd" * 32)


def _reg(
    version: dict[str, Any], seq: int, status: ModelStatus, revision: int, **overrides: Any
) -> ModelVersion:
    fields = {**version, "seq": seq, "status_time_ns": BASE + seq * NS_PER_SECOND, **overrides}
    return _entry(status, revision, **fields)


def _promotion() -> list[ModelVersion]:
    """A research/shadow/active, B research/shadow, A retired, B active replacing A."""
    return [
        _reg(A, 0, RESEARCH, 0),
        _reg(A, 1, SHADOW, 1),
        _reg(A, 2, ACTIVE, 2),
        _reg(B, 3, RESEARCH, 0),
        _reg(B, 4, SHADOW, 1),
        _reg(A, 5, RETIRED, 3),
        _reg(B, 6, ACTIVE, 2, replaces_model_version_id=A_ID),
    ]


def _reactivation(seq: int = 8, **overrides: Any) -> ModelVersion:
    fields: dict[str, Any] = {
        "replaces_model_version_id": B_ID,
        "rollback_of_model_version_id": B_ID,
        "approval_reference": OTHER_DECISION,
    }
    fields.update(overrides)
    return _reg(A, seq, ACTIVE, 4, **fields)


def _rolled_back() -> list[ModelVersion]:
    """The promotion followed by the rollback of B (A is still RETIRED)."""
    return [*_promotion(), _reg(B, 7, ROLLED_BACK, 3)]


def _refused(history: list[ModelVersion]) -> IllegalTransitionError:
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_registry_history(history)
    assert excinfo.value.details["model_version_id"] == history[-1].model_version_id
    assert excinfo.value.details["revision"] == history[-1].revision
    return excinfo.value


@pytest.mark.requirement("G07-09", "G07-10")
def test_history_of_a_valid_promotion_is_accepted() -> None:
    history = _promotion()
    assert [entry.status for entry in history] == [
        RESEARCH,
        SHADOW,
        ACTIVE,
        RESEARCH,
        SHADOW,
        RETIRED,
        ACTIVE,
    ]
    validate_registry_history(history)
    for cut in range(len(history) + 1):
        validate_registry_history(history[:cut])


@pytest.mark.requirement("G07-11")
def test_history_of_a_valid_rollback_is_accepted() -> None:
    history = [*_rolled_back(), _reactivation()]
    assert history[-1].rollback_of_model_version_id == B_ID
    assert history[-1].approval_reference != history[2].approval_reference
    validate_registry_history(history)


@pytest.mark.requirement("G07-10", "G07-11")
def test_reactivation_with_the_approval_of_the_first_activation_is_refused() -> None:
    error = _refused([*_rolled_back(), _reactivation(approval_reference=samples.FIXTURE_DECISION)])
    assert "approval" in str(error.details["rule"])


@pytest.mark.requirement("G07-10", "G07-11")
def test_every_activation_of_a_version_needs_an_unused_approval() -> None:
    # A is re-activated (D-998), retired again, C is promoted and rolled back, and A is
    # re-activated a second time: neither of its two earlier references may be reused.
    history = [
        *_rolled_back(),
        _reactivation(),
        _reg(C, 9, RESEARCH, 0),
        _reg(C, 10, SHADOW, 1),
        _reg(A, 11, RETIRED, 5),
        _reg(C, 12, ACTIVE, 2, replaces_model_version_id=A_ID),
        _reg(C, 13, ROLLED_BACK, 3),
    ]
    again = {"replaces_model_version_id": C_ID, "rollback_of_model_version_id": C_ID}
    validate_registry_history([*history, _reg(A, 14, ACTIVE, 6, approval_reference="D-997", **again)])
    for used in (samples.FIXTURE_DECISION, OTHER_DECISION):
        _refused([*history, _reg(A, 14, ACTIVE, 6, approval_reference=used, **again)])


@pytest.mark.requirement("G07-11")
def test_reactivation_must_name_the_rolled_back_version() -> None:
    _refused([*_rolled_back(), _reactivation(rollback_of_model_version_id=None)])


@pytest.mark.requirement("G07-11")
def test_reactivation_naming_a_version_that_was_not_rolled_back_is_refused() -> None:
    # B was merely RETIRED, not rolled back: there is no rollback that could re-activate A.
    history = [*_promotion(), _reg(B, 7, RETIRED, 3), _reactivation()]
    _refused(history)


@pytest.mark.requirement("G07-11")
def test_reactivation_naming_an_unregistered_version_is_refused() -> None:
    _refused([*_rolled_back(), _reactivation(rollback_of_model_version_id=OTHER_ID)])


@pytest.mark.requirement("G07-11")
def test_reactivation_naming_a_rolled_back_version_of_another_family_is_refused() -> None:
    # "A re-activation names a successor that really was rolled back": a rolled-back version of an
    # unrelated model family is not a successor of A and cannot justify its re-activation.
    history = [
        *_promotion(),
        _reg(B, 7, RETIRED, 3),
        _reg(X, 8, RESEARCH, 0),
        _reg(X, 9, SHADOW, 1),
        _reg(X, 10, ACTIVE, 2),
        _reg(X, 11, ROLLED_BACK, 3),
    ]
    validate_registry_history(history)
    _refused([*history, _reactivation(12, rollback_of_model_version_id=X_ID)])


@pytest.mark.requirement("G07-10")
def test_two_active_versions_of_one_family_are_refused() -> None:
    history = _promotion()
    del history[5]  # A is never retired
    history[5] = _reg(B, 5, ACTIVE, 2, replaces_model_version_id=A_ID)
    assert [entry.status for entry in history][-1] is ACTIVE
    _refused(history)


@pytest.mark.requirement("G07-10", "G07-11")
def test_reactivation_while_the_successor_is_still_active_is_refused() -> None:
    _refused([*_promotion(), _reactivation(7)])


@pytest.mark.requirement("G07-10")
@pytest.mark.parametrize("replaces", [None, C_ID, X_ID, OTHER_ID])
def test_activation_must_name_the_version_it_replaces(replaces: str | None) -> None:
    history = _promotion()
    history[6] = _reg(B, 6, ACTIVE, 2, replaces_model_version_id=replaces)
    error = _refused(history)
    assert "replaces_model_version_id" in str(error.details["rule"])


@pytest.mark.requirement("G07-10")
def test_first_activation_of_a_family_replaces_nothing() -> None:
    history = _promotion()[:3]
    validate_registry_history(history)
    history[2] = _reg(A, 2, ACTIVE, 2, replaces_model_version_id=B_ID)
    _refused(history)


@pytest.mark.requirement("G07-10")
def test_each_model_family_has_its_own_active_version() -> None:
    history = [
        *_promotion(),
        _reg(X, 7, RESEARCH, 0),
        _reg(X, 8, SHADOW, 1),
        _reg(X, 9, ACTIVE, 2),
    ]
    validate_registry_history(history)
    # The predecessor named by an activation is the one of the same family.
    history[9] = _reg(X, 9, ACTIVE, 2, replaces_model_version_id=B_ID)
    _refused(history)


@pytest.mark.requirement("S00-ASG-18", "S00-ASG-25")
@pytest.mark.parametrize("seq", [0, 1, 3, 9])
def test_registry_sequence_numbers_are_gap_free(seq: int) -> None:
    validate_registry_history([_reg(A, 0, RESEARCH, 0), _reg(A, 1, SHADOW, 1), _reg(A, 2, ACTIVE, 2)])
    history = [
        _reg(A, 0, RESEARCH, 0),
        _reg(A, 1, SHADOW, 1),
        _reg(A, seq, ACTIVE, 2, status_time_ns=BASE + 60 * NS_PER_SECOND),
    ]
    error = _refused(history)
    assert "seq" in str(error.details["rule"])


@pytest.mark.requirement("S00-ASG-18")
def test_a_registry_history_may_start_at_any_sequence_number() -> None:
    validate_registry_history([_reg(A, 40, RESEARCH, 0), _reg(A, 41, SHADOW, 1)])
    validate_registry_history([])


@pytest.mark.requirement("G07-09", "G07-10", "G07-11")
def test_per_version_rules_hold_inside_a_history() -> None:
    # Skipping shadow validation.
    _refused([_reg(A, 0, RESEARCH, 0), _reg(A, 1, ACTIVE, 2)])
    # A rolled-back version can never be activated again.
    _refused([*_rolled_back(), _reg(B, 8, ACTIVE, 4, approval_reference="D-997")])
    # A rejected version can never be shadowed again.
    _refused([_reg(A, 0, RESEARCH, 0), _reg(A, 1, REJECTED, 1), _reg(A, 2, SHADOW, 2)])
    # An entry of a version that was never registered.
    _refused([_reg(A, 0, RESEARCH, 0), _reg(B, 1, SHADOW, 1)])


COMBO: dict[str, Any] = {
    "model_name": "fixture_combo",
    "kind": ModelKind.COMPOSITE,
    "artifact_sha256": "ee" * 32,
    "component_model_version_ids": (A_ID,),
}


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_composite_with_registered_components_is_accepted() -> None:
    validate_registry_history(
        [
            _reg(A, 0, RESEARCH, 0),
            _reg(COMBO, 1, RESEARCH, 0),
            _reg(COMBO, 2, SHADOW, 1),
            _reg(COMBO, 3, ACTIVE, 2),
        ]
    )


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_a_component_registered_after_the_composite_does_not_repair_it() -> None:
    # Rework 2: components are registered before the composite.  The refusal names the composite at
    # its first registry entry, whatever follows.
    history = [_reg(COMBO, 0, RESEARCH, 0), _reg(A, 1, RESEARCH, 0), _reg(COMBO, 2, SHADOW, 1)]
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_registry_history(history)
    assert excinfo.value.details["model_version_id"] == history[0].model_version_id
    assert excinfo.value.details["revision"] == 0
    assert "component" in str(excinfo.value.details["rule"])


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_registering_a_composite_with_an_unregistered_component_is_refused() -> None:
    # "component versions of a composite are registered" is a rule of the history, not only of the
    # activation: a composite cannot enter the registry before the versions it is built from.
    _refused([_reg(COMBO, 0, RESEARCH, 0)])


# =========================================================================== RegistryHistory
@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_registry_history_check_validates_without_recording_and_admit_records() -> None:
    history = RegistryHistory()
    research = _reg(A, 0, RESEARCH, 0)
    assert history.check(research) is True
    assert history.model(A_ID) is None  # check changes nothing
    assert history.admit(research) is True
    assert history.model(A_ID) == research
    # The identical newest entry again is a repeat: accepted, not recorded twice.
    assert history.check(research) is False
    assert history.admit(research) is False
    assert history.model(A_ID) == research
    assert history.active(ModelKind.NET_R_MODEL, samples.MODEL_NAME) is None


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_registry_history_active_follows_activation_retirement_and_rollback() -> None:
    history = RegistryHistory()
    family = (ModelKind.NET_R_MODEL, samples.MODEL_NAME)
    seen: list[str | None] = []
    for entry in [*_rolled_back(), _reactivation()]:
        history.admit(entry)
        active = history.active(*family)
        seen.append(None if active is None else active.model_version_id)
    #          A res A sh  A act  B res  B sh  A ret B act  B rb  A re-act
    assert seen == [None, None, A_ID, A_ID, A_ID, None, B_ID, None, A_ID]
    assert history.model(B_ID) is not None
    assert history.model(B_ID).status is ROLLED_BACK  # type: ignore[union-attr]
    assert history.active(ModelKind.PAYOFF_MODEL, samples.MODEL_NAME) is None  # another kind
    assert history.active(ModelKind.NET_R_MODEL, "fixture_other") is None  # another name


@pytest.mark.requirement("G07-10")
def test_registry_history_refuses_a_second_active_version_and_check_leaves_it_unchanged() -> None:
    history = RegistryHistory()
    for entry in _promotion()[:5]:  # A ACTIVE, B SHADOW
        history.admit(entry)
    second = _reg(B, 5, ACTIVE, 2, replaces_model_version_id=A_ID)
    with pytest.raises(IllegalTransitionError) as excinfo:
        history.check(second)
    assert "ACTIVE" in str(excinfo.value.details["rule"])
    with pytest.raises(IllegalTransitionError):
        history.admit(second)
    # A refused entry changes nothing.
    assert history.model(B_ID) == _promotion()[4]
    assert history.active(ModelKind.NET_R_MODEL, samples.MODEL_NAME) == _promotion()[2]
    # Same name, other kind: another family, which may have its own ACTIVE version.
    other_kind = {"kind": ModelKind.PAYOFF_MODEL, "artifact_sha256": "ef" * 32}
    for seq, (status, revision) in enumerate([(RESEARCH, 0), (SHADOW, 1), (ACTIVE, 2)], start=5):
        assert history.admit(_reg(other_kind, seq, status, revision)) is True
    assert history.active(ModelKind.PAYOFF_MODEL, samples.MODEL_NAME) is not None


@pytest.mark.requirement("G07-11")
def test_a_rollback_names_the_version_that_was_active_immediately_before() -> None:
    # A active -> B replaces A -> B rolled back -> C replaces B -> C rolled back.  Re-activating A is
    # a rollback of C (ACTIVE immediately before); B was rolled back too, but earlier.
    history = [
        *_rolled_back(),
        _reg(C, 8, RESEARCH, 0),
        _reg(C, 9, SHADOW, 1),
        _reg(C, 10, ACTIVE, 2, replaces_model_version_id=B_ID),
        _reg(C, 11, ROLLED_BACK, 3),
    ]
    validate_registry_history(history)
    accepted = _reactivation(12, replaces_model_version_id=C_ID, rollback_of_model_version_id=C_ID)
    validate_registry_history([*history, accepted])
    for wrong in (B_ID, X_ID, OTHER_ID):
        error = _refused(
            [*history, _reactivation(12, replaces_model_version_id=C_ID, rollback_of_model_version_id=wrong)]
        )
        assert "rollback_of_model_version_id" in str(error.details["rule"])


@pytest.mark.requirement("S00-ASG-18", "G07-10")
def test_registry_history_components_before_the_composite() -> None:
    history = RegistryHistory()
    composite = _reg(COMBO, 1, RESEARCH, 0)
    with pytest.raises(IllegalTransitionError) as excinfo:
        history.check(composite)
    assert "component" in str(excinfo.value.details["rule"])
    assert history.admit(_reg(A, 0, RESEARCH, 0)) is True
    assert history.admit(composite) is True


@pytest.mark.requirement("S00-ASG-18")
@pytest.mark.parametrize(
    "not_a_model",
    [None, "mdl_0", to_wire(samples.model_version()), samples.tick()],
    ids=["none", "text", "wire-dict", "tick"],
)
def test_the_registry_refuses_what_is_not_a_model_version_with_a_gie_error(not_a_model: object) -> None:
    # CLAUDE.md: errors derive from GieError and fail closed.  The change notice is silent on the error
    # class here; anything else than a GieError escapes the documented error handling.
    with pytest.raises(GieError):
        RegistryHistory().check(not_a_model)  # type: ignore[arg-type]
    with pytest.raises(GieError):
        validate_registry_history([not_a_model])  # type: ignore[list-item]


# =========================================================================== out of sample
BOUNDARY = INFORMATION_END + EMBARGO


@pytest.mark.requirement("S00-ASG-18", "S10-ACC-02")
@pytest.mark.parametrize(
    ("as_of_time_ns", "expected"),
    [
        (0, False),
        (INFORMATION_END - 1, False),
        (INFORMATION_END, False),
        (INFORMATION_END + 1, False),
        (BOUNDARY - 1, False),
        (BOUNDARY, False),
        (BOUNDARY + 1, True),
        (samples.T0, True),
    ],
)
def test_out_of_sample_starts_strictly_after_information_end_plus_embargo(
    as_of_time_ns: int, expected: bool
) -> None:
    model = samples.model_version()
    assert (model.training_information_end_ns, model.embargo_ns) == (INFORMATION_END, EMBARGO)
    assert is_out_of_sample(as_of_time_ns, model) is expected


@pytest.mark.requirement("S00-ASG-18", "S10-ACC-02")
@pytest.mark.parametrize("embargo", [None, 0])
def test_without_embargo_the_boundary_is_the_information_end(embargo: int | None) -> None:
    model = samples.model_version(embargo_ns=embargo)
    assert is_out_of_sample(INFORMATION_END, model) is False
    assert is_out_of_sample(INFORMATION_END + 1, model) is True


@pytest.mark.requirement("S00-ASG-18", "S10-ACC-02")
def test_a_longer_embargo_moves_the_boundary() -> None:
    model = samples.model_version(embargo_ns=7 * DAY)
    assert is_out_of_sample(INFORMATION_END + 7 * DAY, model) is False
    assert is_out_of_sample(INFORMATION_END + 7 * DAY + 1, model) is True


@pytest.mark.requirement("S00-ASG-18", "S10-ACC-02")
@pytest.mark.parametrize("as_of_time_ns", [0, 1, samples.T0, 2**63 - 1])
def test_a_rule_set_that_used_no_data_is_always_out_of_sample(as_of_time_ns: int) -> None:
    ruleset = samples.variants()["ModelVersion"]["ruleset_baseline"]
    assert isinstance(ruleset, ModelVersion)
    assert ruleset.training_information_end_ns is None
    assert is_out_of_sample(as_of_time_ns, ruleset) is True


@pytest.mark.requirement("S10-ACC-02")
def test_leakage_check_with_a_candidate_watermark() -> None:
    candidate = samples.candidate()
    clean = samples.model_version()
    assert is_out_of_sample(candidate.as_of_time_ns, clean) is True
    # A model whose information (labels included) ends inside the embargo before the candidate.
    leaking = samples.model_version(
        training_information_end_ns=candidate.as_of_time_ns - EMBARGO,
        created_time_ns=candidate.as_of_time_ns,
        status_time_ns=candidate.as_of_time_ns,
    )
    assert is_out_of_sample(candidate.as_of_time_ns, leaking) is False
    assert is_out_of_sample(candidate.as_of_time_ns + 1, leaking) is True
