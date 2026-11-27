"""Independent tests for the run contracts: ``RunManifest``, ``RunRole`` and ``RunContext``.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - section "RunManifest 1.0.0" with the invariants
  RUN-1 .. RUN-5, the nested ``RunModel`` (RMD-1) and the enumerations ``RunMode`` (REPLAY, PAPER -
  no live member), ``RunRole`` and ``RiskRegime``;
* ``reviews/session_00/rework_2_change_notice.md`` sections 1 and 2 (run identity bound to every
  field, ``RunManifest.create``, ``RunContext`` with read-only clock and scheduler views, ``seed_for``);
* ``specifications/SHARED_CONTRACTS.md`` section 4 (``run_id`` is recomputable and verified),
  section 6.1 (stream ``run_manifest/<producer>``, ordering time ``created_time_ns``, rank 0),
  section 9.1 ("No live mode or real venue exists in the vocabulary"; "every record names its run,
  mode and the origin of its data; a run names its configuration") and section 10 (``RunContext``);
* the docstrings of ``gie/contracts/run.py``.

SYNTHETIC FIXTURE: every run, label, hash, commit, model id and timestamp in this file is invented
test data built with ``tests/gie/samples.py``.  No run described here was executed; the paper
"system clock" is a ``SystemClock`` driven by fixed fake sources.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from gie.contracts import identity
from gie.contracts.clock import ClockMode, PollingScheduler, ReplayClock, SystemClock
from gie.contracts.codec import from_json, from_wire, to_json, to_wire
from gie.contracts.common import DataOrigin, RunMode
from gie.contracts.journal import MemoryJournal
from gie.contracts.model_version import ModelStatus
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.contracts.run import (
    MAX_RUN_MODELS,
    RiskRegime,
    RunContext,
    RunManifest,
    RunModel,
    RunRole,
    derive_run_id,
)
from gie.core.clock import ClockView, PaperClock, SchedulerView
from gie.core.errors import ConfigError, ContractValidationError, GieError, LiveRoutingLockedError
from gie.core.ids import derive_seed
from gie.core.params import ResearchParameters, load_research_parameters
from gie.core.timeutil import NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
OTHER_RUN_ID = identity.run_id("REPLAY", "1" * 64)
START = T0 - 3600 * NS_PER_SECOND
PAPER_CREATED = T0 + 86_400 * NS_PER_SECOND
SECOND_MODEL = identity.model_version_id("fixture_other", "0.1.0", "cd" * 32)


def _pin(
    model_version_id: str = samples.MODEL_ID, status: ModelStatus = ModelStatus.ACTIVE, **kw: Any
) -> RunModel:
    """A pinned model entry (SYNTHETIC FIXTURE; fixture models are never validated)."""
    fields: dict[str, Any] = {
        "model_version_id": model_version_id,
        "status": status,
        "validated": False,
        "valid_from_ns": None,
        "valid_until_ns": None,
    }
    fields.update(kw)
    return RunModel(**fields)


def _replay(**overrides: Any) -> RunManifest:
    return samples.run_manifest(**overrides)


def _paper(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "mode": RunMode.PAPER,
        "role": RunRole.PRIMARY,
        "label": "fixture-paper",
        "data_start_ns": None,
        "data_end_ns": None,
        "created_time_ns": PAPER_CREATED,
        "seed": None,
        "models": (_pin(),),  # a PRIMARY run pins ACTIVE models only (RUN-5)
    }
    fields.update(overrides)
    return samples.run_manifest(**fields)


def _shadow(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "role": RunRole.SHADOW,
        "label": "fixture-shadow",
        "shadow_of_run_id": samples.RUN_ID,
    }
    fields.update(overrides)
    return samples.run_manifest(**fields)


def _fields(manifest: RunManifest) -> dict[str, Any]:
    """Every field of *manifest* except ``run_id``."""
    return {
        spec.name: getattr(manifest, spec.name) for spec in RunManifest.FIELD_SPECS if spec.name != "run_id"
    }


def _changed(build: Callable[[], RunManifest], change: dict[str, Any]) -> dict[str, Any]:
    """All fields of the built manifest with *change* applied.

    ``run_id`` is re-derived from the changed content unless the change names it: a violation of
    RUN-2 .. RUN-5 is then not hidden behind RUN-1 (RUN-1 is checked first).
    """
    fields = _fields(build())
    fields.update({name: value for name, value in change.items() if name != "run_id"})
    fields["run_id"] = change["run_id"] if "run_id" in change else derive_run_id(**fields)
    return fields


def _invariant(excinfo: pytest.ExceptionInfo[ContractValidationError]) -> object:
    return excinfo.value.details.get("invariant")


# =========================================================================== invariants RUN-1..5
ACCEPTED: list[Any] = [
    pytest.param("RUN-1", _replay, id="RUN-1-replay-sample"),
    pytest.param("RUN-1", _paper, id="RUN-1-paper-sample"),
    pytest.param(
        "RUN-1", lambda: _replay(config_sha256="1" * 64, label="other"), id="RUN-1-other-configuration"
    ),
    pytest.param("RUN-2", _replay, id="RUN-2-replay-with-period"),
    pytest.param(
        "RUN-2", lambda: _replay(data_start_ns=START, data_end_ns=START + 1), id="RUN-2-shortest-period"
    ),
    pytest.param("RUN-2", lambda: _replay(data_start_ns=0, data_end_ns=1), id="RUN-2-period-at-the-epoch"),
    pytest.param("RUN-2", _paper, id="RUN-2-paper-with-creation-time-without-period"),
    pytest.param("RUN-2", lambda: _paper(created_time_ns=0), id="RUN-2-paper-created-at-the-epoch"),
    pytest.param("RUN-3", _shadow, id="RUN-3-shadow-names-its-primary"),
    pytest.param("RUN-3", _replay, id="RUN-3-research-without-primary"),
    pytest.param("RUN-3", _paper, id="RUN-3-primary-without-primary"),
    pytest.param(
        "RUN-3",
        lambda: _paper(
            role=RunRole.SHADOW,
            label="fixture-paper-shadow",
            shadow_of_run_id=OTHER_RUN_ID,
            models=(_pin(status=ModelStatus.SHADOW),),
        ),
        id="RUN-3-paper-shadow",
    ),
    pytest.param("RUN-4", _replay, id="RUN-4-seq-0"),
    pytest.param(
        "RUN-5",
        lambda: _replay(risk_regime=RiskRegime.UNCONSTRAINED_RESEARCH),
        id="RUN-5-research-run-unconstrained",
    ),
    pytest.param("RUN-5", lambda: _paper(models=()), id="RUN-5-primary-without-models"),
    pytest.param(
        "RUN-5",
        lambda: _paper(models=(_pin(), _pin(SECOND_MODEL, valid_from_ns=PAPER_CREATED))),
        id="RUN-5-primary-with-active-models-only",
    ),
    pytest.param(
        "RUN-5",
        lambda: _shadow(models=(_pin(status=ModelStatus.SHADOW), _pin(SECOND_MODEL, ModelStatus.RESEARCH))),
        id="RUN-5-shadow-may-pin-non-active-models",
    ),
]

VIOLATIONS: list[Any] = [
    pytest.param("RUN-1", _replay, {"run_id": OTHER_RUN_ID}, id="RUN-1-foreign-run-id"),
    pytest.param(
        "RUN-1",
        _replay,
        {"config_sha256": "1" * 64, "run_id": samples.RUN_ID},
        id="RUN-1-other-configuration-same-id",
    ),
    pytest.param(
        "RUN-1", _replay, {"label": "other", "run_id": samples.RUN_ID}, id="RUN-1-other-label-same-id"
    ),
    pytest.param("RUN-1", _replay, {"seed": 8, "run_id": samples.RUN_ID}, id="RUN-1-other-seed-same-id"),
    pytest.param(
        "RUN-1", _replay, {"code_tree_dirty": True, "run_id": samples.RUN_ID}, id="RUN-1-dirty-tree-same-id"
    ),
    pytest.param("RUN-1", _replay, {"models": (), "run_id": samples.RUN_ID}, id="RUN-1-other-models-same-id"),
    pytest.param(
        "RUN-1",
        lambda: _paper(label=samples.RUN_LABEL),
        {"run_id": samples.RUN_ID},
        id="RUN-1-paper-run-claims-the-replay-id",
    ),
    pytest.param(
        "RUN-1",
        _shadow,
        {"shadow_of_run_id": _shadow().run_id, "run_id": _shadow().run_id},
        # The id covers shadow_of_run_id, so no manifest can name itself with a matching id.
        id="RUN-1-shadow-of-itself-cannot-carry-a-matching-id",
    ),
    pytest.param("RUN-2", _replay, {"data_start_ns": None}, id="RUN-2-replay-without-start"),
    pytest.param("RUN-2", _replay, {"data_end_ns": None}, id="RUN-2-replay-without-end"),
    pytest.param(
        "RUN-2", _replay, {"data_start_ns": None, "data_end_ns": None}, id="RUN-2-replay-without-period"
    ),
    pytest.param("RUN-2", _replay, {"data_start_ns": START, "data_end_ns": START}, id="RUN-2-empty-period"),
    pytest.param(
        "RUN-2", _replay, {"data_start_ns": START, "data_end_ns": START - 1}, id="RUN-2-reversed-period"
    ),
    pytest.param("RUN-2", _replay, {"created_time_ns": T0}, id="RUN-2-replay-with-creation-time"),
    pytest.param("RUN-2", _paper, {"created_time_ns": None}, id="RUN-2-paper-without-creation-time"),
    pytest.param("RUN-2", _paper, {"data_start_ns": START}, id="RUN-2-paper-with-start"),
    pytest.param("RUN-2", _paper, {"data_end_ns": T0}, id="RUN-2-paper-with-end"),
    pytest.param("RUN-2", _paper, {"data_start_ns": START, "data_end_ns": T0}, id="RUN-2-paper-with-period"),
    pytest.param("RUN-3", _shadow, {"shadow_of_run_id": None}, id="RUN-3-shadow-without-primary"),
    pytest.param("RUN-3", _replay, {"shadow_of_run_id": OTHER_RUN_ID}, id="RUN-3-research-with-primary"),
    pytest.param("RUN-3", _paper, {"shadow_of_run_id": OTHER_RUN_ID}, id="RUN-3-primary-with-primary"),
    pytest.param("RUN-3", _shadow, {"role": RunRole.RESEARCH}, id="RUN-3-role-changed-to-research"),
    pytest.param("RUN-4", _replay, {"seq": 1}, id="RUN-4-seq-1"),
    pytest.param("RUN-4", _paper, {"seq": 7}, id="RUN-4-seq-7"),
    pytest.param(
        "RUN-5", _paper, {"risk_regime": RiskRegime.UNCONSTRAINED_RESEARCH}, id="RUN-5-primary-unconstrained"
    ),
    pytest.param(
        "RUN-5", _shadow, {"risk_regime": RiskRegime.UNCONSTRAINED_RESEARCH}, id="RUN-5-shadow-unconstrained"
    ),
    pytest.param(
        "RUN-5",
        _paper,
        {"models": (_pin(status=ModelStatus.SHADOW),)},
        id="RUN-5-primary-pins-a-shadow-model",
    ),
    pytest.param(
        "RUN-5",
        _paper,
        {"models": (_pin(), _pin(SECOND_MODEL, ModelStatus.RESEARCH))},
        id="RUN-5-primary-pins-one-research-model",
    ),
    pytest.param(
        "RUN-5",
        _replay,
        {"role": RunRole.PRIMARY},  # the sample replay pins RESEARCH and SHADOW models
        id="RUN-5-primary-replay-with-the-research-models",
    ),
]


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build"), ACCEPTED)
def test_conforming_manifest_is_accepted_and_round_trips(
    invariant: str, build: Callable[[], RunManifest]
) -> None:
    manifest = build()
    assert type(manifest) is RunManifest
    assert invariant in {rule.id for rule in RunManifest.ALL_INVARIANTS}
    assert manifest.run_id == derive_run_id(**_fields(manifest))
    assert from_wire(to_wire(manifest)) == manifest
    assert from_json(to_json(manifest), RunManifest) == manifest


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_manifest_cannot_be_constructed(
    invariant: str, build: Callable[[], RunManifest], change: dict[str, Any]
) -> None:
    fields = _changed(build, change)
    with pytest.raises(ContractValidationError) as excinfo:
        RunManifest(**fields)
    assert _invariant(excinfo) == invariant
    assert excinfo.value.details["contract"] == "RunManifest"
    if "run_id" not in change:
        # RunManifest.create derives the id itself and refuses the same content for the same reason.
        create_fields = {name: value for name, value in fields.items() if name != "run_id"}
        with pytest.raises(ContractValidationError) as excinfo:
            RunManifest.create(**create_fields)
        assert _invariant(excinfo) == invariant


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_manifest_cannot_be_decoded(
    invariant: str, build: Callable[[], RunManifest], change: dict[str, Any]
) -> None:
    fields = _changed(build, change)
    wire = to_wire(build())
    types = {spec.name: spec.ftype for spec in RunManifest.FIELD_SPECS}
    for name, value in fields.items():
        wire[name] = types[name].encode(value)
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert _invariant(excinfo) == invariant


@pytest.mark.requirement("S00-ASG-22")
def test_every_invariant_of_the_field_reference_has_an_accepted_and_a_rejected_case() -> None:
    documented = {"RUN-1", "RUN-2", "RUN-3", "RUN-4", "RUN-5"}
    assert {rule.id for rule in RunManifest.ALL_INVARIANTS} == documented
    assert {case.values[0] for case in ACCEPTED} == documented
    assert {case.values[0] for case in VIOLATIONS} == documented


# =========================================================================== RunModel (RMD-1)
@pytest.mark.requirement("S00-ASG-22", "G07-12")
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="whole-run"),
        pytest.param({"valid_from_ns": T0}, id="from-only"),
        pytest.param({"valid_until_ns": T0}, id="until-only"),
        pytest.param({"valid_from_ns": T0, "valid_until_ns": T0 + 1}, id="shortest-period"),
        pytest.param({"validated": True}, id="validated-active"),
        pytest.param({"status": ModelStatus.RETIRED}, id="retired-not-validated"),
    ],
)
def test_conforming_run_model_is_accepted_and_round_trips(fields: dict[str, Any]) -> None:
    pinned = _pin(**fields)
    manifest = _replay(models=(pinned,))
    assert from_wire(to_wire(manifest)).models == (pinned,)


@pytest.mark.requirement("S00-ASG-22", "G07-12")
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"valid_from_ns": T0, "valid_until_ns": T0}, id="empty-period"),
        pytest.param({"valid_from_ns": T0 + 1, "valid_until_ns": T0}, id="reversed-period"),
        pytest.param({"status": ModelStatus.SHADOW, "validated": True}, id="validated-shadow"),
        pytest.param({"status": ModelStatus.RESEARCH, "validated": True}, id="validated-research"),
        pytest.param({"status": ModelStatus.RETIRED, "validated": True}, id="validated-retired"),
    ],
)
def test_violating_run_model_is_refused_with_rmd_1(fields: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        _pin(**fields)
    assert _invariant(excinfo) == "RMD-1"
    assert excinfo.value.details["contract"] == "RunModel"
    # ... also inside a manifest message.
    wire = to_wire(_replay(models=(_pin(),)))
    wire["models"][0].update({name: getattr(value, "value", value) for name, value in fields.items()})
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert _invariant(excinfo) == "RMD-1"


@pytest.mark.requirement("S00-ASG-22", "G07-12")
def test_run_model_covers_its_period_from_inclusive_until_exclusive() -> None:
    bounded = _pin(valid_from_ns=T0, valid_until_ns=T0 + 10)
    assert [bounded.covers(t) for t in (T0 - 1, T0, T0 + 9, T0 + 10)] == [False, True, True, False]
    assert _pin().covers(0)
    assert _pin().covers(2**63 - 1)
    assert not _pin(valid_from_ns=T0).covers(T0 - 1)
    assert _pin(valid_from_ns=T0).covers(T0)
    assert _pin(valid_until_ns=T0).covers(T0 - 1)
    assert not _pin(valid_until_ns=T0).covers(T0)


@pytest.mark.requirement("S00-ASG-22", "G07-12")
def test_manifest_model_returns_the_pinned_entry_that_covers_the_instant() -> None:
    # A walk-forward schedule: the same version pinned for two folds, another for the fold between.
    first = _pin(samples.MODEL_ID, ModelStatus.RESEARCH, valid_from_ns=START, valid_until_ns=T0)
    second = _pin(SECOND_MODEL, ModelStatus.RESEARCH, valid_from_ns=T0, valid_until_ns=T0 + 10)
    third = _pin(samples.MODEL_ID, ModelStatus.SHADOW, valid_from_ns=T0 + 10)
    manifest = _replay(models=(third, second, first))
    assert manifest.models == (first, second, third)  # canonical order: start of validity, then id
    assert manifest.model(samples.MODEL_ID, START) == first
    assert manifest.model(samples.MODEL_ID, T0 - 1) == first
    assert manifest.model(samples.MODEL_ID, T0) is None  # not pinned in that fold
    assert manifest.model(SECOND_MODEL, T0) == second
    assert manifest.model(SECOND_MODEL, T0 + 10) is None
    assert manifest.model(samples.MODEL_ID, T0 + 10) == third
    assert manifest.model(samples.MODEL_ID, START - 1) is None
    assert manifest.model(samples.MODEL_ID) in (first, third)  # no instant: any pinned entry
    assert manifest.model(samples.SHADOW_MODEL_ID) is None  # not pinned at all
    assert manifest.model(samples.SHADOW_MODEL_ID, T0) is None


@pytest.mark.requirement("S00-ASG-22")
def test_the_number_of_pinned_models_is_bounded() -> None:
    def models(count: int) -> tuple[RunModel, ...]:
        return tuple(
            _pin(samples.MODEL_ID, ModelStatus.RESEARCH, valid_from_ns=index, valid_until_ns=index + 1)
            for index in range(count)
        )

    assert MAX_RUN_MODELS == 4096
    assert len(_replay(models=models(MAX_RUN_MODELS)).models) == MAX_RUN_MODELS
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(_replay(), models=models(MAX_RUN_MODELS + 1))
    assert str(excinfo.value.details["path"]).startswith("models")


# =========================================================================== run identity
ALTERNATIVE_VALUES: dict[str, object] = {
    "producer": "app.other",
    "seq": 1,
    "mode": RunMode.PAPER,
    "role": RunRole.SHADOW,
    "risk_regime": RiskRegime.UNCONSTRAINED_RESEARCH,
    "label": "other",
    "config_sha256": "1" * 64,
    "code_commit": "f" * 40,
    "code_tree_dirty": True,
    "contract_set_version": "1.1.0",
    "parameters_sha256": "f" * 64,
    "data_origin": DataOrigin.HISTORICAL,
    "data_sources": ("another_feed", "fixture"),
    "data_start_ns": START + 1,
    "data_end_ns": T0 + 3600 * NS_PER_SECOND + 1,
    "seed": 8,
    "models": (),
    "shadow_of_run_id": OTHER_RUN_ID,
    "created_time_ns": T0,
}


@pytest.mark.requirement("S00-ASG-22")
def test_every_field_except_run_id_has_an_alternative_value_here() -> None:
    assert set(ALTERNATIVE_VALUES) == set(_fields(_replay()))


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize("field", sorted(ALTERNATIVE_VALUES))
def test_changing_any_single_field_changes_the_run_id(field: str) -> None:
    fields = _fields(_replay())
    base = derive_run_id(**fields)
    assert base == samples.RUN_ID
    fields[field] = ALTERNATIVE_VALUES[field]
    assert derive_run_id(**fields) != base


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("seed", [None, 0, 8])
def test_a_seed_change_including_none_changes_the_run_id(seed: int | None) -> None:
    assert _replay(seed=seed).run_id != samples.RUN_ID
    assert _replay(seed=7).run_id == samples.RUN_ID


@pytest.mark.requirement("S00-ASG-22")
def test_run_id_is_derived_from_the_mode_and_the_hash_of_every_other_field() -> None:
    manifest = _replay()
    assert manifest.run_id == samples.RUN_ID
    assert manifest.run_id.startswith("run_")
    assert manifest.run_id == derive_run_id(**_fields(manifest))
    # RunManifest.create derives the id; the constructor accepts exactly that id.
    assert RunManifest.create(**_fields(manifest)) == manifest
    assert RunManifest(run_id=manifest.run_id, **_fields(manifest)) == manifest
    # identity.run_id takes the mode and a SHA-256: the same hash in another mode is another run.
    replay_id = identity.run_id(RunMode.REPLAY, "0" * 64)
    assert replay_id == identity.run_id("REPLAY", "0" * 64)
    assert replay_id != identity.run_id(RunMode.PAPER, "0" * 64)
    assert replay_id != identity.run_id(RunMode.REPLAY, "1" * 64)


@pytest.mark.requirement("S00-ASG-22", "S10-ACC-01")
def test_identical_manifests_are_the_same_run_and_a_paper_run_is_unique_by_its_start() -> None:
    # Two replays with identical manifests are the same run (byte-identical journal).
    assert _replay() == _replay()
    assert _replay().run_id == _replay().run_id
    assert to_json(_replay()) == to_json(_replay())
    # A paper run states when it started; another start is another run.
    assert _paper().run_id != _paper(created_time_ns=PAPER_CREATED + 1).run_id


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    ("fields", "error"),
    [
        pytest.param({"bogus": 1}, ContractValidationError, id="unknown-field"),
        pytest.param({"run_id": samples.RUN_ID}, ContractValidationError, id="run-id-is-not-an-input"),
        pytest.param({"config_sha256": "G" * 64}, ContractValidationError, id="malformed-value"),
        pytest.param({"mode": "LIVE"}, GieError, id="live-mode"),
    ],
)
def test_derive_run_id_refuses_unknown_fields_and_malformed_values(
    fields: dict[str, Any], error: type[GieError]
) -> None:
    base = _fields(_replay())
    base.update(fields)
    with pytest.raises(error):
        derive_run_id(**base)


@pytest.mark.requirement("S00-ASG-22")
def test_derive_run_id_refuses_a_missing_required_field() -> None:
    fields = _fields(_replay())
    del fields["config_sha256"]
    with pytest.raises(ContractValidationError) as excinfo:
        derive_run_id(**fields)
    assert excinfo.value.details["path"] == "config_sha256"


# =========================================================================== no live mode
@pytest.mark.requirement("M-AUTH-07")
def test_run_mode_role_and_risk_regime_vocabularies() -> None:
    assert [mode.value for mode in RunMode] == ["REPLAY", "PAPER"]
    assert [role.value for role in RunRole] == ["PRIMARY", "SHADOW", "RESEARCH"]
    assert [regime.value for regime in RiskRegime] == ["CHALLENGE_RULES", "UNCONSTRAINED_RESEARCH"]
    assert not hasattr(RunMode, "LIVE")


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize("mode", ["LIVE", "live", "REAL", "DEMO", "PRODUCTION", "REPLAY", "PAPER", None, 1])
def test_a_manifest_cannot_be_constructed_with_anything_but_a_genuine_run_mode(mode: object) -> None:
    # Even the *strings* "REPLAY" and "PAPER" are refused: nothing is coerced into a run mode.
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(_replay(), mode=mode)
    assert excinfo.value.details["path"] == "mode"


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize(
    "mode", ["LIVE", "live", "REAL", "DEMO", "PRODUCTION", "replay", "", None, 1, ["PAPER"]]
)
def test_a_manifest_with_a_live_or_unknown_mode_cannot_be_decoded(mode: object) -> None:
    wire = to_wire(_replay())
    assert from_wire(wire) == _replay()
    wire["mode"] = mode
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == "mode"


@pytest.mark.requirement("M-AUTH-07", "S00-ASG-22")
@pytest.mark.parametrize("mode", ["LIVE", "live", "DEMO", "", None])
def test_no_run_id_can_be_derived_for_a_live_run(mode: object) -> None:
    with pytest.raises(LiveRoutingLockedError) as excinfo:
        identity.run_id(mode, samples.CONFIG_SHA)
    assert excinfo.value.code == "GIE.ROUTING.LIVE_LOCKED"
    # The sample builder derives the id first, so it cannot produce a live manifest either.
    with pytest.raises(GieError):
        samples.run_manifest(mode=mode)


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize("role", ["SHADOW", "LIVE", "PRODUCTION", None])
def test_unknown_role_is_refused(role: object) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(_replay(), role=role)
    assert excinfo.value.details["path"] == "role"
    wire = to_wire(_replay())
    wire["role"] = "LIVE"
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == "role"


# =========================================================================== fields
@pytest.mark.requirement("M-AUTH-07", "COM-07")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label", "Fixture"),
        ("label", "fixture run"),
        ("label", "-fixture"),
        ("label", ""),
        ("label", "x" * 65),
        ("config_sha256", "0" * 63),
        ("config_sha256", "G" * 64),
        ("config_sha256", None),
        ("code_commit", "0123456"),
        ("code_commit", "0123456789ABCDEF0123456789abcdef01234567"),
        ("code_tree_dirty", None),
        ("code_tree_dirty", 0),
        ("code_tree_dirty", "false"),
        ("contract_set_version", "1.0"),
        ("contract_set_version", "latest"),
        ("parameters_sha256", "0" * 65),
        ("parameters_sha256", None),
        ("risk_regime", "CHALLENGE_RULES"),
        ("risk_regime", None),
        ("data_origin", "SYNTHETIC_FIXTURE"),
        ("data_origin", None),
        ("data_sources", ()),
        ("data_sources", ("fixture", "fixture")),
        ("data_sources", ("Fixture Feed",)),
        ("data_sources", "fixture"),
        ("data_start_ns", -1),
        ("data_end_ns", 2**63),
        ("seed", -1),
        ("seed", 1.5),
        (
            "models",
            (_pin(samples.MODEL_ID, ModelStatus.RESEARCH), _pin(samples.MODEL_ID, ModelStatus.SHADOW)),
        ),
        ("models", (samples.MODEL_ID,)),
        ("models", None),
        ("shadow_of_run_id", samples.MODEL_ID),
        ("created_time_ns", -1),
        ("producer", "App.Run"),
        ("run_id", "run_123"),
    ],
)
def test_malformed_field_is_refused_with_its_path(field: str, value: object) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(_replay(), **{field: value})
    assert str(excinfo.value.details["path"]).startswith(field)


@pytest.mark.requirement("M-AUTH-07")
def test_model_version_ids_is_gone() -> None:
    # Rework 2: the list of model ids was replaced by the pinned entries in ``models``.
    assert "model_version_ids" not in {spec.name for spec in RunManifest.FIELD_SPECS}
    wire = to_wire(_replay())
    wire["model_version_ids"] = [samples.MODEL_ID]
    with pytest.raises(ContractValidationError):
        from_wire(wire)


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize(
    "missing",
    [
        "config_sha256",
        "parameters_sha256",
        "data_origin",
        "data_start_ns",
        "seed",
        "shadow_of_run_id",
        "risk_regime",
        "code_tree_dirty",
        "models",
        "created_time_ns",
    ],
)
def test_a_manifest_with_a_missing_field_cannot_be_decoded(missing: str) -> None:
    wire = to_wire(_replay())
    del wire[missing]
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == missing


@pytest.mark.requirement("M-AUTH-07")
def test_unordered_lists_are_stored_in_their_canonical_order() -> None:
    models = (_pin(samples.MODEL_ID, ModelStatus.RESEARCH), _pin(SECOND_MODEL, ModelStatus.RESEARCH))
    ordered = tuple(sorted(models, key=lambda item: item.model_version_id))
    one = _replay(data_sources=("fixture", "another_feed"), models=models)
    two = _replay(data_sources=("another_feed", "fixture"), models=tuple(reversed(models)))
    assert one == two
    assert one.run_id == two.run_id  # the identity is computed over the canonical form
    assert one.data_sources == ("another_feed", "fixture")
    assert one.models == ordered
    assert to_json(one) == to_json(two)


@pytest.mark.requirement("COM-07", "M-AUTH-07")
def test_sample_manifests_name_their_configuration_and_are_labelled_synthetic() -> None:
    manifests = samples.variants()["RunManifest"]
    assert set(manifests) == {"replay_research", "paper_primary", "replay_shadow"}
    parameters = load_research_parameters()
    for name, manifest in manifests.items():
        assert isinstance(manifest, RunManifest), name
        assert manifest.data_origin is DataOrigin.SYNTHETIC_FIXTURE, name
        assert manifest.parameters_sha256 == parameters.parameters_sha256, name
        assert manifest.run_id == derive_run_id(**_fields(manifest)), name
        assert manifest.seq == 0, name
        assert all(not pinned.validated for pinned in manifest.models), name  # fixture models
    paper = manifests["paper_primary"]
    assert isinstance(paper, RunManifest)
    assert paper.data_start_ns is None
    assert paper.created_time_ns is not None
    assert all(pinned.status is ModelStatus.ACTIVE for pinned in paper.models)
    assert manifests["replay_shadow"].shadow_of_run_id == samples.RUN_ID  # type: ignore[attr-defined]
    assert manifests["replay_research"].created_time_ns is None  # type: ignore[attr-defined]


@pytest.mark.requirement("S00-ASG-25")
def test_stream_ordering_time_and_rank_of_a_manifest() -> None:
    replay, paper = _replay(), _paper()
    assert stream_name(replay) == "run_manifest/app.run"
    # Ordering time: the start of the data period for a replay, the creation time for a paper run.
    assert ordering_time_ns(replay) == replay.data_start_ns == START
    assert ordering_time_ns(paper) == paper.created_time_ns == PAPER_CREATED
    key = ordering_key(replay)
    assert (key.time_ns, key.rank, key.stream, key.seq) == (START, 0, "run_manifest/app.run", 0)


@pytest.mark.requirement("M-AUTH-07")
def test_wire_form_of_a_manifest() -> None:
    wire = to_wire(_paper())
    assert wire["contract"] == "RunManifest"
    assert wire["schema_version"] == "1.0.0"
    assert wire["mode"] == "PAPER"
    assert wire["role"] == "PRIMARY"
    assert wire["risk_regime"] == "CHALLENGE_RULES"
    assert wire["code_tree_dirty"] is False
    assert wire["created_time_ns"] == PAPER_CREATED
    assert wire["models"][0]["status"] == "ACTIVE"
    # Optional fields are always written, as explicit null.
    for name in ("data_start_ns", "data_end_ns", "seed", "shadow_of_run_id"):
        assert name in wire
        assert wire[name] is None
    assert to_wire(_replay())["created_time_ns"] is None


# =========================================================================== RunContext
def _system_clock() -> SystemClock:
    return SystemClock(wall_source=lambda: T0, monotonic_source=lambda: 1_000)


def _paper_clock() -> PaperClock:
    return PaperClock(_system_clock())


def _other_parameters() -> ResearchParameters:
    """The default parameter set with one value changed (SYNTHETIC FIXTURE, not a proposal)."""
    default = load_research_parameters()
    parameters = {name: default.get(name) for name in default.names()}
    changed = parameters["tick_processing_target_ns"]
    assert isinstance(changed.value, int)
    parameters[changed.name] = dataclasses.replace(changed, value=changed.value + 1)
    return ResearchParameters(parameters, default.approval_status)


@pytest.mark.requirement("S00-ASG-19", "S10-ACC-01")
def test_replay_context_hands_out_manifest_views_parameters_and_sink() -> None:
    manifest, clock, parameters, journal = (
        _replay(),
        ReplayClock(T0),
        load_research_parameters(),
        MemoryJournal(),
    )
    context = RunContext(manifest, clock, clock, parameters, journal)
    assert context.manifest is manifest
    # Rework 2: clock and scheduler are wrapped in read-only views.
    assert type(context.clock) is ClockView
    assert type(context.scheduler) is SchedulerView
    assert context.clock.now_ns() == clock.now_ns() == T0
    assert context.parameters is parameters
    assert context.sink is journal
    assert context.run_id == manifest.run_id == samples.RUN_ID
    assert context.mode is RunMode.REPLAY
    assert context.clock.mode is ClockMode.REPLAY
    assert context.sink.publish(manifest) == 0


@pytest.mark.requirement("S00-ASG-19")
def test_paper_context_uses_the_paper_clock() -> None:
    clock = _paper_clock()
    context = RunContext(
        _paper(), clock, PollingScheduler(clock), load_research_parameters(), MemoryJournal()
    )
    assert context.mode is RunMode.PAPER
    assert context.clock.mode is ClockMode.SYSTEM
    assert context.clock.now_ns() == T0
    assert context.run_id == _paper().run_id


@pytest.mark.requirement("S00-ASG-19", "S10-ACC-02")
def test_a_replay_run_refuses_the_paper_clock() -> None:
    clock = _paper_clock()
    with pytest.raises(ConfigError) as excinfo:
        RunContext(_replay(), clock, PollingScheduler(clock), load_research_parameters(), MemoryJournal())
    assert excinfo.value.code == "GIE.CONFIG.INVALID"


@pytest.mark.requirement("S00-ASG-19")
def test_a_paper_run_refuses_a_replay_clock() -> None:
    clock = ReplayClock(T0)
    with pytest.raises(ConfigError):
        RunContext(_paper(), clock, clock, load_research_parameters(), MemoryJournal())


@pytest.mark.requirement("S00-ASG-19")
def test_a_component_cannot_move_time_or_fire_timers_through_the_context() -> None:
    clock = ReplayClock(T0)
    context = RunContext(_replay(), clock, clock, load_research_parameters(), MemoryJournal())
    fired: list[int] = []
    handle = context.scheduler.call_after(10, fired.append)
    context.scheduler.call_at(T0 + 20, fired.append)
    cancelled = context.scheduler.call_at(T0 + 30, fired.append)
    context.scheduler.cancel(cancelled)
    for name in ("advance_to", "advance_by", "begin_step", "wall_ns", "call_at", "run_due"):
        assert not hasattr(context.clock, name), name
    for name in ("advance_to", "advance_by", "run_due", "next_timer_ns", "pending_timers", "now_ns"):
        assert not hasattr(context.scheduler, name), name
    assert context.clock.now_ns() == T0
    assert fired == []
    # Only the run loop, which holds the clock itself, moves time and fires the timers.
    clock.advance_to(T0 + 100)
    assert fired == [T0 + 10, T0 + 20]
    assert context.clock.now_ns() == T0 + 100
    assert isinstance(handle, int)


@pytest.mark.requirement("S00-ASG-19")
def test_a_paper_component_cannot_begin_a_step_or_poll_through_the_context() -> None:
    walls = iter([T0, T0 + 50, T0 + 50, T0 + 50, T0 + 50, T0 + 50])
    paper = PaperClock(SystemClock(wall_source=lambda: next(walls), monotonic_source=lambda: 1))
    scheduler = PollingScheduler(paper)
    context = RunContext(_paper(), paper, scheduler, load_research_parameters(), MemoryJournal())
    fired: list[int] = []
    context.scheduler.call_after(10, fired.append)
    assert not hasattr(context.clock, "begin_step")
    assert not hasattr(context.scheduler, "run_due")
    with pytest.raises(GieError):
        context.scheduler.call_at(T0 - 1, fired.append)  # a timer in the past is refused, as in replay
    assert scheduler.run_due() == 1
    assert fired == [T0 + 10]
    assert context.clock.now_ns() == T0 + 10  # step time follows the timer's due instant


@pytest.mark.requirement("S00-ASG-19")
def test_views_of_views_do_not_nest() -> None:
    clock = ReplayClock(T0)
    context = RunContext(
        _replay(), ClockView(clock), SchedulerView(clock), load_research_parameters(), MemoryJournal()
    )
    assert type(context.clock) is ClockView
    assert context.clock.now_ns() == T0
    with pytest.raises(ContractValidationError):
        ClockView(object())  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        SchedulerView(object())  # type: ignore[arg-type]


@pytest.mark.requirement("M-AUTH-07")
def test_parameters_with_another_hash_than_the_manifest_are_refused() -> None:
    clock = ReplayClock(T0)
    other = _other_parameters()
    assert other.parameters_sha256 != load_research_parameters().parameters_sha256
    with pytest.raises(ConfigError) as excinfo:
        RunContext(_replay(), clock, clock, other, MemoryJournal())
    assert "parameters" in excinfo.value.message
    # ... and the other way round: a manifest that names another parameter set.
    with pytest.raises(ConfigError):
        RunContext(
            _replay(parameters_sha256="f" * 64), clock, clock, load_research_parameters(), MemoryJournal()
        )
    # The check is by content: a manifest naming the changed set accepts exactly that set.
    matching = _replay(parameters_sha256=other.parameters_sha256)
    assert RunContext(matching, clock, clock, other, MemoryJournal()).parameters is other


@pytest.mark.requirement("M-AUTH-07")
def test_an_equal_parameter_set_loaded_twice_is_accepted() -> None:
    clock = ReplayClock(T0)
    first, second = load_research_parameters(), load_research_parameters()
    assert first is not second
    assert RunContext(_replay(), clock, clock, second, MemoryJournal()).parameters is second


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize(
    "not_a_manifest",
    [None, "run", samples.RUN_ID, to_wire(samples.run_manifest()), samples.clock_reading(), samples.tick()],
    ids=["none", "text", "run-id", "wire-dict", "clock-reading", "tick"],
)
def test_a_context_needs_a_genuine_manifest(not_a_manifest: object) -> None:
    clock = ReplayClock(T0)
    with pytest.raises(ConfigError):
        RunContext(not_a_manifest, clock, clock, load_research_parameters(), MemoryJournal())  # type: ignore[arg-type]


@pytest.mark.requirement("S00-ASG-19", "M-AUTH-07")
def test_every_missing_or_foreign_argument_is_a_configuration_error() -> None:
    # Fail closed with the documented error (ConfigError, GIE.CONFIG.INVALID).
    clock, parameters = ReplayClock(T0), load_research_parameters()
    for not_a_clock in (None, object(), "REPLAY"):
        with pytest.raises(ConfigError):
            RunContext(_replay(), not_a_clock, clock, parameters, MemoryJournal())  # type: ignore[arg-type]
    for not_a_scheduler in (None, object(), _system_clock()):
        with pytest.raises(ConfigError):
            RunContext(_replay(), clock, not_a_scheduler, parameters, MemoryJournal())  # type: ignore[arg-type]
    for not_parameters in (None, parameters.parameters_sha256, {}):
        with pytest.raises(ConfigError):
            RunContext(_replay(), clock, clock, not_parameters, MemoryJournal())  # type: ignore[arg-type]
    with pytest.raises(ConfigError) as excinfo:
        RunContext(_replay(), clock, clock, parameters, None)  # type: ignore[arg-type]
    assert excinfo.value.code == "GIE.CONFIG.INVALID"


@pytest.mark.requirement("S00-ASG-19")
def test_a_context_cannot_be_rewired_after_construction() -> None:
    clock = ReplayClock(T0)
    context = RunContext(_replay(), clock, clock, load_research_parameters(), MemoryJournal())
    view = context.clock
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.clock = _system_clock()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.manifest = _paper()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.scheduler = clock  # type: ignore[misc]
    assert context.clock is view


# =========================================================================== seeds
@pytest.mark.requirement("S00-ASG-19", "S00-ASG-22")
def test_seed_for_derives_a_stable_seed_per_purpose_from_the_run_seed() -> None:
    clock = ReplayClock(T0)
    context = RunContext(_replay(), clock, clock, load_research_parameters(), MemoryJournal())
    assert context.manifest.seed == 7
    first = context.seed_for("backtesting.bootstrap")
    assert first == derive_seed(7, "backtesting.bootstrap")
    assert context.seed_for("backtesting.bootstrap") == first  # deterministic
    assert context.seed_for("learning.shuffle") != first  # independent per purpose
    assert 0 <= first <= 2**63 - 1
    other = RunContext(_replay(seed=8), clock, clock, load_research_parameters(), MemoryJournal())
    assert other.seed_for("backtesting.bootstrap") != first
    with pytest.raises(GieError):
        context.seed_for("")


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-22")
def test_a_run_without_a_seed_may_not_use_randomness() -> None:
    clock = _paper_clock()
    context = RunContext(
        _paper(), clock, PollingScheduler(clock), load_research_parameters(), MemoryJournal()
    )
    assert context.manifest.seed is None
    with pytest.raises(ConfigError) as excinfo:
        context.seed_for("backtesting.bootstrap")
    assert excinfo.value.details["purpose"] == "backtesting.bootstrap"
