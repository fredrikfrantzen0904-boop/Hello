"""Independent tests for the run contracts: ``RunManifest``, ``RunRole`` and ``RunContext``.

Expected behaviour is taken from the specification, not from the implementation:

* ``specifications/contracts/FIELDS.md`` - section "RunManifest 1.0.0" with the invariants
  RUN-1 .. RUN-4 and the enumerations ``RunMode`` (REPLAY, PAPER - no live member) and ``RunRole``;
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
from gie.contracts.ordering import ordering_key, ordering_time_ns, stream_name
from gie.contracts.run import RunContext, RunManifest, RunRole
from gie.core.errors import ConfigError, ContractValidationError, GieError, LiveRoutingLockedError
from gie.core.params import ResearchParameters, load_research_parameters
from gie.core.timeutil import NS_PER_SECOND
from tests.gie import samples

T0 = samples.T0
OTHER_RUN_ID = identity.run_id("REPLAY", "1" * 64, "other")
START = T0 - 3600 * NS_PER_SECOND


def _replay(**overrides: Any) -> RunManifest:
    return samples.run_manifest(**overrides)


def _paper(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "mode": RunMode.PAPER,
        "role": RunRole.PRIMARY,
        "label": "fixture-paper",
        "data_start_ns": None,
        "data_end_ns": None,
        "seed": None,
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


SHADOW_RUN_ID = identity.run_id("REPLAY", samples.CONFIG_SHA, "fixture-shadow")


def _invariant(excinfo: pytest.ExceptionInfo[ContractValidationError]) -> object:
    return excinfo.value.details.get("invariant")


# =========================================================================== invariants RUN-1..4
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
    pytest.param("RUN-2", _paper, id="RUN-2-paper-without-period"),
    pytest.param("RUN-3", _shadow, id="RUN-3-shadow-names-its-primary"),
    pytest.param("RUN-3", _replay, id="RUN-3-research-without-primary"),
    pytest.param("RUN-3", _paper, id="RUN-3-primary-without-primary"),
    pytest.param(
        "RUN-3",
        lambda: _paper(role=RunRole.SHADOW, label="fixture-paper-shadow", shadow_of_run_id=OTHER_RUN_ID),
        id="RUN-3-paper-shadow",
    ),
    pytest.param("RUN-4", _replay, id="RUN-4-seq-0"),
]

VIOLATIONS: list[Any] = [
    pytest.param("RUN-1", _replay, {"run_id": OTHER_RUN_ID}, id="RUN-1-foreign-run-id"),
    pytest.param("RUN-1", _replay, {"config_sha256": "1" * 64}, id="RUN-1-other-configuration-same-id"),
    pytest.param("RUN-1", _replay, {"label": "other"}, id="RUN-1-other-label-same-id"),
    pytest.param(
        "RUN-1",
        lambda: _paper(label=samples.RUN_LABEL),
        {"run_id": samples.RUN_ID},
        id="RUN-1-paper-run-claims-the-replay-id",
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
    pytest.param("RUN-2", _paper, {"data_start_ns": START}, id="RUN-2-paper-with-start"),
    pytest.param("RUN-2", _paper, {"data_end_ns": T0}, id="RUN-2-paper-with-end"),
    pytest.param("RUN-2", _paper, {"data_start_ns": START, "data_end_ns": T0}, id="RUN-2-paper-with-period"),
    pytest.param("RUN-3", _shadow, {"shadow_of_run_id": None}, id="RUN-3-shadow-without-primary"),
    pytest.param("RUN-3", _shadow, {"shadow_of_run_id": SHADOW_RUN_ID}, id="RUN-3-shadow-of-itself"),
    pytest.param("RUN-3", _replay, {"shadow_of_run_id": OTHER_RUN_ID}, id="RUN-3-research-with-primary"),
    pytest.param("RUN-3", _paper, {"shadow_of_run_id": OTHER_RUN_ID}, id="RUN-3-primary-with-primary"),
    pytest.param("RUN-3", _shadow, {"role": RunRole.PRIMARY}, id="RUN-3-role-changed-to-primary"),
    pytest.param("RUN-4", _replay, {"seq": 1}, id="RUN-4-seq-1"),
    pytest.param("RUN-4", _paper, {"seq": 7}, id="RUN-4-seq-7"),
]


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build"), ACCEPTED)
def test_conforming_manifest_is_accepted_and_round_trips(
    invariant: str, build: Callable[[], RunManifest]
) -> None:
    manifest = build()
    assert type(manifest) is RunManifest
    assert invariant in {rule.id for rule in RunManifest.ALL_INVARIANTS}
    assert from_wire(to_wire(manifest)) == manifest
    assert from_json(to_json(manifest), RunManifest) == manifest


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_manifest_cannot_be_constructed(
    invariant: str, build: Callable[[], RunManifest], change: dict[str, Any]
) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(build(), **change)
    assert _invariant(excinfo) == invariant
    assert excinfo.value.details["contract"] == "RunManifest"


@pytest.mark.requirement("S00-ASG-22", "M-AUTH-07")
@pytest.mark.parametrize(("invariant", "build", "change"), VIOLATIONS)
def test_violating_manifest_cannot_be_decoded(
    invariant: str, build: Callable[[], RunManifest], change: dict[str, Any]
) -> None:
    wire = to_wire(build())
    for name, value in change.items():
        wire[name] = getattr(value, "value", value)
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert _invariant(excinfo) == invariant


@pytest.mark.requirement("S00-ASG-22")
def test_every_invariant_of_the_field_reference_has_an_accepted_and_a_rejected_case() -> None:
    documented = {"RUN-1", "RUN-2", "RUN-3", "RUN-4"}
    assert {rule.id for rule in RunManifest.ALL_INVARIANTS} == documented
    assert {case.values[0] for case in ACCEPTED} == documented
    assert {case.values[0] for case in VIOLATIONS} == documented


# =========================================================================== run identity
@pytest.mark.requirement("S00-ASG-22")
def test_run_id_is_derived_from_mode_configuration_hash_and_label() -> None:
    manifest = _replay()
    assert manifest.run_id == identity.run_id("REPLAY", samples.CONFIG_SHA, samples.RUN_LABEL)
    assert manifest.run_id == identity.run_id(RunMode.REPLAY, samples.CONFIG_SHA, samples.RUN_LABEL)
    assert manifest.run_id == samples.RUN_ID
    assert manifest.run_id.startswith("run_")
    variants = {
        manifest.run_id,
        _replay(label="other").run_id,
        _replay(config_sha256="1" * 64).run_id,
        _paper(label=samples.RUN_LABEL).run_id,
    }
    assert len(variants) == 4


@pytest.mark.requirement("S00-ASG-22", "S10-ACC-01")
def test_run_id_does_not_depend_on_when_or_by_whom_the_manifest_was_written() -> None:
    first = _replay()
    second = _replay(created_time_ns=first.created_time_ns + 5 * NS_PER_SECOND, producer="app.other", seed=8)
    assert second.run_id == first.run_id
    assert second != first


# =========================================================================== no live mode
@pytest.mark.requirement("M-AUTH-07")
def test_run_mode_and_run_role_vocabularies() -> None:
    assert [mode.value for mode in RunMode] == ["REPLAY", "PAPER"]
    assert [role.value for role in RunRole] == ["PRIMARY", "SHADOW", "RESEARCH"]
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
        identity.run_id(mode, samples.CONFIG_SHA, samples.RUN_LABEL)
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
        ("contract_set_version", "1.0"),
        ("contract_set_version", "latest"),
        ("parameters_sha256", "0" * 65),
        ("parameters_sha256", None),
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
        ("model_version_ids", (samples.MODEL_ID, samples.MODEL_ID)),
        ("model_version_ids", (samples.RUN_ID,)),
        ("shadow_of_run_id", samples.MODEL_ID),
        ("created_time_ns", -1),
        ("created_time_ns", None),
        ("producer", "App.Run"),
        ("run_id", "run_123"),
    ],
)
def test_malformed_field_is_refused_with_its_path(field: str, value: object) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        dataclasses.replace(_replay(), **{field: value})
    assert str(excinfo.value.details["path"]).startswith(field)


@pytest.mark.requirement("M-AUTH-07")
@pytest.mark.parametrize(
    "missing",
    ["config_sha256", "parameters_sha256", "data_origin", "data_start_ns", "seed", "shadow_of_run_id"],
)
def test_a_manifest_with_a_missing_field_cannot_be_decoded(missing: str) -> None:
    wire = to_wire(_replay())
    del wire[missing]
    with pytest.raises(ContractValidationError) as excinfo:
        from_wire(wire)
    assert excinfo.value.details["path"] == missing


@pytest.mark.requirement("M-AUTH-07")
def test_unordered_lists_are_stored_in_their_canonical_order() -> None:
    second_model = identity.model_version_id("fixture_other", "0.1.0", "cd" * 32)
    models = tuple(sorted((samples.MODEL_ID, second_model)))
    one = _replay(data_sources=("fixture", "another_feed"), model_version_ids=models)
    two = _replay(data_sources=("another_feed", "fixture"), model_version_ids=tuple(reversed(models)))
    assert one == two
    assert one.data_sources == ("another_feed", "fixture")
    assert one.model_version_ids == models
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
        assert manifest.run_id == identity.run_id(manifest.mode, manifest.config_sha256, manifest.label), name
        assert manifest.seq == 0, name
    assert manifests["paper_primary"].data_start_ns is None  # type: ignore[attr-defined]
    assert manifests["replay_shadow"].shadow_of_run_id == samples.RUN_ID  # type: ignore[attr-defined]


@pytest.mark.requirement("S00-ASG-25")
def test_stream_ordering_time_and_rank_of_a_manifest() -> None:
    manifest = _replay()
    assert stream_name(manifest) == "run_manifest/app.run"
    assert ordering_time_ns(manifest) == manifest.created_time_ns
    key = ordering_key(manifest)
    assert (key.time_ns, key.rank, key.stream, key.seq) == (
        manifest.created_time_ns,
        0,
        "run_manifest/app.run",
        0,
    )


@pytest.mark.requirement("M-AUTH-07")
def test_wire_form_of_a_manifest() -> None:
    wire = to_wire(_paper())
    assert wire["contract"] == "RunManifest"
    assert wire["schema_version"] == "1.0.0"
    assert wire["mode"] == "PAPER"
    assert wire["role"] == "PRIMARY"
    # Optional fields are always written, as explicit null.
    for name in ("data_start_ns", "data_end_ns", "seed", "shadow_of_run_id"):
        assert name in wire
        assert wire[name] is None


# =========================================================================== RunContext
def _system_clock() -> SystemClock:
    return SystemClock(wall_source=lambda: T0, monotonic_source=lambda: 1_000)


def _other_parameters() -> ResearchParameters:
    """The default parameter set with one value changed (SYNTHETIC FIXTURE, not a proposal)."""
    default = load_research_parameters()
    parameters = {name: default.get(name) for name in default.names()}
    changed = parameters["tick_processing_target_ns"]
    assert isinstance(changed.value, int)
    parameters[changed.name] = dataclasses.replace(changed, value=changed.value + 1)
    return ResearchParameters(parameters, default.approval_status)


@pytest.mark.requirement("S00-ASG-19", "S10-ACC-01")
def test_replay_context_hands_out_manifest_clock_scheduler_parameters_and_sink() -> None:
    manifest, clock, parameters, journal = (
        _replay(),
        ReplayClock(T0),
        load_research_parameters(),
        MemoryJournal(),
    )
    context = RunContext(manifest, clock, clock, parameters, journal)
    assert context.manifest is manifest
    assert context.clock is clock
    assert context.scheduler is clock
    assert context.parameters is parameters
    assert context.sink is journal
    assert context.run_id == samples.RUN_ID
    assert context.mode is RunMode.REPLAY
    assert context.clock.mode is ClockMode.REPLAY
    assert context.sink.publish(manifest) == 0


@pytest.mark.requirement("S00-ASG-19")
def test_paper_context_uses_the_system_clock() -> None:
    clock = _system_clock()
    context = RunContext(
        _paper(), clock, PollingScheduler(clock), load_research_parameters(), MemoryJournal()
    )
    assert context.mode is RunMode.PAPER
    assert context.clock.mode is ClockMode.SYSTEM
    assert context.run_id == identity.run_id("PAPER", samples.CONFIG_SHA, "fixture-paper")


@pytest.mark.requirement("S00-ASG-19", "S10-ACC-02")
def test_a_replay_run_refuses_the_system_clock() -> None:
    clock = _system_clock()
    with pytest.raises(ConfigError) as excinfo:
        RunContext(_replay(), clock, PollingScheduler(clock), load_research_parameters(), MemoryJournal())
    assert excinfo.value.code == "GIE.CONFIG.INVALID"


@pytest.mark.requirement("S00-ASG-19")
def test_a_paper_run_refuses_a_replay_clock() -> None:
    clock = ReplayClock(T0)
    with pytest.raises(ConfigError):
        RunContext(_paper(), clock, clock, load_research_parameters(), MemoryJournal())


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


@pytest.mark.requirement("S00-ASG-19")
def test_a_context_cannot_be_rewired_after_construction() -> None:
    clock = ReplayClock(T0)
    context = RunContext(_replay(), clock, clock, load_research_parameters(), MemoryJournal())
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.clock = _system_clock()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.manifest = _paper()  # type: ignore[misc]
    assert context.clock is clock
