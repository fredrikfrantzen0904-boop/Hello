"""RunManifest contract and the run context handed to every component."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    Bool,
    EnumOf,
    Id,
    Int,
    Invariant,
    ListOf,
    Obj,
    Opt,
    Record,
    Str,
    TimeNs,
    encode_fields,
    fld,
    inv,
    record,
)
from gie.contracts.common import SHA256_PATTERN, DataOrigin, RunMode, Sequenced
from gie.contracts.model_version import GIT_COMMIT_PATTERN, ModelStatus
from gie.contracts.registry import SEMVER_PATTERN, contract
from gie.contracts.tick import SOURCE_PATTERN
from gie.core.canonical import content_hash
from gie.core.clock import Clock, ClockMode, ClockView, Scheduler, SchedulerView
from gie.core.errors import ConfigError, ContractValidationError
from gie.core.ids import derive_seed
from gie.core.params import ResearchParameters

if TYPE_CHECKING:
    from gie.contracts.interfaces import RecordSink

LABEL_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_SHA = Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256")
#: Enough for a walk-forward study with weekly refits of several model kinds over many years.
MAX_RUN_MODELS: Final[int] = 4096


class RunRole(str, Enum):
    """What a run is for.

    ``PRIMARY`` is the decision stream of record (paper forward run or the
    reference replay).  ``SHADOW`` runs a candidate model next to a primary run
    for comparison; its records never feed the primary run.  ``RESEARCH`` is any
    other experiment.
    """

    PRIMARY = "PRIMARY"
    SHADOW = "SHADOW"
    RESEARCH = "RESEARCH"


class RiskRegime(str, Enum):
    """Which risk rules a run is simulated under (brief 10: report the two separately).

    ``CHALLENGE_RULES``: the daily states of the account (defensive, halt,
    profit protection) are in force and halt trading as they would on the
    account.  ``UNCONSTRAINED_RESEARCH``: the daily states are not enforced, to
    study what the strategy does without them; such a run is research only and
    its results are never reported as a challenge outcome.
    """

    CHALLENGE_RULES = "CHALLENGE_RULES"
    UNCONSTRAINED_RESEARCH = "UNCONSTRAINED_RESEARCH"


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class RunModel(Record):
    """One model version a run is allowed to use, pinned for the whole run.

    The status and the *validated* flag are what the registry said when the run
    was configured.  A promotion, retirement or rollback that happens later does
    not change a running or replayed run: it takes effect in a new run (G07-12,
    no untested online changes).  ``valid_from_ns`` / ``valid_until_ns`` restrict
    the model to a period of the run, which is how a walk-forward schedule is
    written down: each fold's model is valid for its test period only.
    """

    model_version_id: str = fld(Id("mdl"), "The model (or rule set) version.")
    status: ModelStatus = fld(EnumOf(ModelStatus), "Its promotion status as pinned for this run.")
    validated: bool = fld(
        Bool(), "ModelVersion.validated as pinned for this run (ACTIVE and not resting on fixture data)."
    )
    valid_from_ns: int | None = fld(
        Opt(TimeNs()),
        "First instant at which the run may use it; null = from the start.",
        unit="ns since epoch, UTC",
    )
    valid_until_ns: int | None = fld(
        Opt(TimeNs()),
        "End (exclusive) of its use; null = until the end of the run.",
        unit="ns since epoch, UTC",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "RMD-1",
            "valid_from_ns < valid_until_ns when both are given; only an ACTIVE model can be validated",
            lambda r: (
                (r.valid_from_ns is None or r.valid_until_ns is None or r.valid_from_ns < r.valid_until_ns)
                and (not r.validated or r.status is ModelStatus.ACTIVE)
            ),
        ),
    )

    def covers(self, time_ns: int) -> bool:
        """True if the run may use this model for market data as of *time_ns*."""
        return (self.valid_from_ns is None or self.valid_from_ns <= time_ns) and (
            self.valid_until_ns is None or time_ns < self.valid_until_ns
        )


def _content_sha256(wire_fields: dict[str, Any]) -> str:
    return content_hash({name: value for name, value in wire_fields.items() if name != "run_id"})


@final
@contract("RunManifest", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class RunManifest(Sequenced):
    """Identity and reproducibility record of one run; the first record of its journal.

    Stream: ``run_manifest/<producer>`` (one record, ``seq`` 0).  Ordering time:
    ``data_start_ns`` for a replay, ``created_time_ns`` for a paper run.

    ``run_id`` is derived from the mode and the hash of **every other field of
    this record**, and the manifest verifies it.  A run id therefore cannot be
    claimed for a run that differs in role, risk regime, configuration, code,
    data, seed, parameters or models.  Two replays with identical manifests are
    the same run and must produce the same journal, byte for byte; for that
    reason a replay manifest carries no wall-clock time.  A paper run also
    states when it started and is unique.

    ``config_sha256`` is the hash of the run-configuration document of the
    session that drives the run (session 10 for replays); the fields listed here
    are the part of the configuration that the shared contracts themselves rely on.

    A restarted process continues a run with the manifest it finds in the
    journal; building a new manifest starts a new run and a new journal.
    """

    run_id: str = fld(Id("run"), "identity.run_id(mode, hash of all other fields); see derive_run_id.")
    mode: RunMode = fld(EnumOf(RunMode), "REPLAY or PAPER.  No live mode exists (routing lock).")
    role: RunRole = fld(EnumOf(RunRole), "PRIMARY, SHADOW or RESEARCH.")
    risk_regime: RiskRegime = fld(
        EnumOf(RiskRegime), "Whether the daily risk states of the account are enforced in this run."
    )
    label: str = fld(Str(pattern=LABEL_PATTERN, max_len=64, label="label"), "Operator label of the run.")
    config_sha256: str = fld(_SHA, "SHA-256 of the complete run configuration (canonical JSON).")
    code_commit: str = fld(
        Str(pattern=GIT_COMMIT_PATTERN, min_len=40, max_len=40, label="git commit"),
        "Commit of the code that executes the run.",
    )
    code_tree_dirty: bool = fld(
        Bool(),
        "True if the working tree had uncommitted changes: the commit then does not identify the code.",
    )
    contract_set_version: str = fld(
        Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Version of the contract set in use."
    )
    parameters_sha256: str = fld(_SHA, "Hash of the research-parameter set (gie.core.params).")
    data_origin: DataOrigin = fld(
        EnumOf(DataOrigin),
        "Least trustworthy origin of the market data the run uses; no record of the run may claim a "
        "better one.",
    )
    data_sources: tuple[str, ...] = fld(
        ListOf(
            Str(pattern=SOURCE_PATTERN, max_len=64, label="feed id"),
            min_items=1,
            max_items=64,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Tick sources (versioned feed ids) the run reads; ticks of any other source are refused.",
    )
    data_start_ns: int | None = fld(
        Opt(TimeNs()), "Start of the replayed period; null for a paper run.", unit="ns since epoch, UTC"
    )
    data_end_ns: int | None = fld(
        Opt(TimeNs()),
        "End (exclusive) of the replayed period; null for a paper run.",
        unit="ns since epoch, UTC",
    )
    seed: int | None = fld(
        Opt(Int(ge=0, label="seed")),
        "Seed of the run; every pseudo-random element derives its own seed from it "
        "(gie.core.ids.derive_seed).  Null = the run uses no randomness.",
    )
    models: tuple[RunModel, ...] = fld(
        ListOf(
            Obj(RunModel),
            max_items=MAX_RUN_MODELS,
            unique_key=lambda item: (item.model_version_id, item.valid_from_ns),
            canonical_sort=lambda item: (
                -1 if item.valid_from_ns is None else item.valid_from_ns,
                item.model_version_id,
            ),
            sorted_label="sorted by start of validity, then id",
        ),
        "Model versions the run may use, with the status pinned for the run and, optionally, the "
        "period in which each may be used (walk-forward schedule).",
    )
    shadow_of_run_id: str | None = fld(
        Opt(Id("run")), "Primary run a SHADOW run is compared with; null otherwise."
    )
    created_time_ns: int | None = fld(
        Opt(TimeNs()),
        "When a paper run was started (system clock).  Always null for a replay, whose identity and "
        "journal must not depend on when it was executed.",
        unit="ns since epoch, UTC",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "RUN-1",
            "run_id equals identity.run_id(mode, SHA-256 of the canonical form of all other fields)",
            lambda r: r.run_id == identity.run_id(r.mode, _content_sha256(encode_fields(r))),
        ),
        inv(
            "RUN-2",
            "a REPLAY run states its data period (start < end) and no creation time; a PAPER run "
            "states its creation time and no data period",
            lambda r: (
                r.data_start_ns is not None
                and r.data_end_ns is not None
                and r.data_start_ns < r.data_end_ns
                and r.created_time_ns is None
                if r.mode is RunMode.REPLAY
                else r.data_start_ns is None and r.data_end_ns is None and r.created_time_ns is not None
            ),
        ),
        inv(
            "RUN-3",
            "shadow_of_run_id is set if and only if role is SHADOW, and is not the run itself",
            lambda r: (
                (r.shadow_of_run_id is not None) == (r.role is RunRole.SHADOW)
                and r.shadow_of_run_id != r.run_id
            ),
        ),
        inv(
            "RUN-4",
            "the manifest is the first record of its stream (seq 0)",
            lambda r: r.seq == 0,
        ),
        inv(
            "RUN-5",
            "only a RESEARCH run may be UNCONSTRAINED_RESEARCH; a PRIMARY run uses owner-approved "
            "(ACTIVE) models only",
            lambda r: (
                (r.risk_regime is RiskRegime.CHALLENGE_RULES or r.role is RunRole.RESEARCH)
                and (
                    r.role is not RunRole.PRIMARY
                    or all(model.status is ModelStatus.ACTIVE for model in r.models)
                )
            ),
        ),
    )

    @classmethod
    def create(cls, **fields: Any) -> RunManifest:
        """Build a manifest from every field except ``run_id``, which is derived."""
        return cls(run_id=derive_run_id(**fields), **fields)

    def model(self, model_version_id: str, time_ns: int | None = None) -> RunModel | None:
        """The pinned entry of a model, optionally the one valid at *time_ns*; ``None`` if not listed."""
        for entry in self.models:
            if entry.model_version_id == model_version_id and (time_ns is None or entry.covers(time_ns)):
                return entry
        return None


def derive_run_id(**fields: Any) -> str:
    """``run_id`` of the manifest that has these field values (all fields except ``run_id``)."""
    names = {spec.name for spec in RunManifest.FIELD_SPECS} - {"run_id"}
    unknown = sorted(set(fields) - names)
    if unknown:
        raise ContractValidationError("unknown RunManifest field", field=unknown[0])
    wire = {
        spec.name: spec.ftype.encode(spec.ftype.check(fields.get(spec.name), spec.name))
        for spec in RunManifest.FIELD_SPECS
        if spec.name != "run_id"
    }
    return identity.run_id(fields.get("mode"), _content_sha256(wire))


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything a component needs from its run, injected by the composition root.

    Components take the clock, the scheduler, the research parameters and their
    output sink from here and from nowhere else; that is what makes a replay run
    and a paper run execute the same code.  ``clock`` and ``scheduler`` are
    read-only views: a component can read step time and schedule timers, it
    cannot move time or fire timers - only the run loop can.
    """

    manifest: RunManifest
    clock: Clock
    scheduler: Scheduler
    parameters: ResearchParameters
    sink: RecordSink

    def __post_init__(self) -> None:
        if type(self.manifest) is not RunManifest:
            raise ConfigError("RunContext needs a RunManifest")
        if not isinstance(self.clock, Clock):
            raise ConfigError("RunContext needs the clock of the run")
        if not isinstance(self.scheduler, Scheduler):
            raise ConfigError("RunContext needs the scheduler of the run")
        if type(self.parameters) is not ResearchParameters:
            raise ConfigError("RunContext needs the research parameters of the run")
        if self.sink is None:
            raise ConfigError("RunContext needs the record sink (journal) of the run")
        replay_clock = self.clock.mode is ClockMode.REPLAY
        if replay_clock != (self.manifest.mode is RunMode.REPLAY):
            raise ConfigError("a REPLAY run needs a replay clock and a PAPER run the paper clock")
        if self.parameters.parameters_sha256 != self.manifest.parameters_sha256:
            raise ConfigError("research parameters differ from the ones named in the run manifest")
        object.__setattr__(self, "clock", ClockView(self.clock))
        object.__setattr__(self, "scheduler", SchedulerView(self.scheduler))

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def mode(self) -> RunMode:
        return self.manifest.mode

    def seed_for(self, purpose: str) -> int:
        """Seed of one named pseudo-random element of this run (``gie.core.ids.derive_seed``)."""
        if self.manifest.seed is None:
            raise ConfigError("the run declares no seed, so it may not use randomness", purpose=purpose)
        return derive_seed(self.manifest.seed, purpose)
