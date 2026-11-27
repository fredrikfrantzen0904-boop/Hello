"""RunManifest contract and the run context handed to every component."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import EnumOf, Id, Int, Invariant, ListOf, Opt, Str, TimeNs, fld, inv
from gie.contracts.common import SHA256_PATTERN, DataOrigin, RunMode, Sequenced
from gie.contracts.model_version import GIT_COMMIT_PATTERN
from gie.contracts.registry import SEMVER_PATTERN, contract
from gie.contracts.tick import SOURCE_PATTERN
from gie.core.clock import Clock, ClockMode, Scheduler
from gie.core.errors import ConfigError
from gie.core.params import ResearchParameters

if TYPE_CHECKING:
    from gie.contracts.interfaces import RecordSink

LABEL_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_SHA = Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256")


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


@final
@contract("RunManifest", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class RunManifest(Sequenced):
    """Identity and reproducibility record of one run; the first record of its journal.

    Stream: ``run_manifest/<producer>`` (one record, ``seq`` 0).  Ordering time:
    ``created_time_ns``.  ``run_id`` is derived from the mode, the hash of the
    complete run configuration and the label, and the manifest verifies it, so a
    run id cannot be claimed for another configuration.  The configuration hash
    covers everything listed here plus whatever else influences the run.
    """

    run_id: str = fld(Id("run"), "identity.run_id(mode, config_sha256, label).")
    mode: RunMode = fld(EnumOf(RunMode), "REPLAY or PAPER.  No live mode exists (routing lock).")
    role: RunRole = fld(EnumOf(RunRole), "PRIMARY, SHADOW or RESEARCH.")
    label: str = fld(Str(pattern=LABEL_PATTERN, max_len=64, label="label"), "Operator label of the run.")
    config_sha256: str = fld(_SHA, "SHA-256 of the complete run configuration (canonical JSON).")
    code_commit: str = fld(
        Str(pattern=GIT_COMMIT_PATTERN, min_len=40, max_len=40, label="git commit"),
        "Commit of the code that executes the run.",
    )
    contract_set_version: str = fld(
        Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Version of the contract set in use."
    )
    parameters_sha256: str = fld(_SHA, "Hash of the research-parameter set (gie.core.params).")
    data_origin: DataOrigin = fld(
        EnumOf(DataOrigin), "Least trustworthy origin of the market data the run will use."
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
        "Tick sources (versioned feed ids) the run reads.",
    )
    data_start_ns: int | None = fld(
        Opt(TimeNs()), "Start of the replayed period; null for a paper run.", unit="ns since epoch, UTC"
    )
    data_end_ns: int | None = fld(
        Opt(TimeNs()),
        "End (exclusive) of the replayed period; null for a paper run.",
        unit="ns since epoch, UTC",
    )
    seed: int | None = fld(Opt(Int(ge=0, label="seed")), "Seed of every pseudo-random element of the run.")
    model_version_ids: tuple[str, ...] = fld(
        ListOf(
            Id("mdl"),
            max_items=64,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Model versions the run uses (its model schedule comes from the configuration, not from "
        "the registry status of the moment).",
    )
    shadow_of_run_id: str | None = fld(
        Opt(Id("run")), "Primary run a SHADOW run is compared with; null otherwise."
    )
    created_time_ns: int = fld(
        TimeNs(), "When the run was created (system clock, also for replays).", unit="ns since epoch, UTC"
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "RUN-1",
            "run_id equals identity.run_id(mode, config_sha256, label)",
            lambda r: r.run_id == identity.run_id(r.mode, r.config_sha256, r.label),
        ),
        inv(
            "RUN-2",
            "a REPLAY run states its data period (start < end); a PAPER run has none",
            lambda r: (
                r.data_start_ns is not None and r.data_end_ns is not None and r.data_start_ns < r.data_end_ns
                if r.mode is RunMode.REPLAY
                else r.data_start_ns is None and r.data_end_ns is None
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
    )


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything a component needs from its run, injected by the composition root.

    Components take the clock, the scheduler, the research parameters and their
    output sink from here and from nowhere else; that is what makes a replay run
    and a paper run execute the same code.
    """

    manifest: RunManifest
    clock: Clock
    scheduler: Scheduler
    parameters: ResearchParameters
    sink: RecordSink

    def __post_init__(self) -> None:
        if type(self.manifest) is not RunManifest:
            raise ConfigError("RunContext needs a RunManifest")
        replay_clock = self.clock.mode is ClockMode.REPLAY
        if replay_clock != (self.manifest.mode is RunMode.REPLAY):
            raise ConfigError("a REPLAY run needs a replay clock and a PAPER run the system clock")
        if self.parameters.parameters_sha256 != self.manifest.parameters_sha256:
            raise ConfigError("research parameters differ from the ones named in the run manifest")

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def mode(self) -> RunMode:
        return self.manifest.mode
