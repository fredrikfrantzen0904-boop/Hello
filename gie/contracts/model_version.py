"""ModelVersion contract - one immutable, versioned model (or rule set) and its status."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    DurationNs,
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
    fld,
    inv,
    record,
)
from gie.contracts.common import (
    CODE_PATTERN,
    SHA256_PATTERN,
    DataOrigin,
    NamedValue,
    Sequenced,
    reason_codes_field,
)
from gie.contracts.registry import SEMVER_PATTERN, contract
from gie.core.errors import IllegalTransitionError

GIT_COMMIT_PATTERN: Final[str] = r"^[0-9a-f]{40}$"
#: Repository-relative location.  No scheme, no credentials, no parent traversal.
ARTIFACT_URI_PATTERN: Final[str] = r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}$"
CONTRACT_NAME_PATTERN: Final[str] = r"^[A-Z][A-Za-z0-9]{1,47}$"
#: Who authorised a status: a name without leading or trailing blanks.
APPROVER_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,62}[A-Za-z0-9.]$"
DECISION_REFERENCE_PATTERN: Final[str] = r"^D-[0-9]{3,5}$"


class ModelKind(str, Enum):
    RULESET = "RULESET"
    NET_R_MODEL = "NET_R_MODEL"
    PAYOFF_MODEL = "PAYOFF_MODEL"
    SIMILARITY_INDEX = "SIMILARITY_INDEX"
    DESTINATION_MODEL = "DESTINATION_MODEL"
    COMPOSITE = "COMPOSITE"


class ModelStatus(str, Enum):
    """Promotion status (G07).  Only ACTIVE versions can count as *validated*."""

    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


#: ``status before -> statuses allowed after``.  ``None`` is "not registered yet".
#:
#: * ACTIVE is first reached only from SHADOW: shadow validation is mandatory (G07-09).
#: * Promotion of a successor moves the previous ACTIVE version to RETIRED.
#: * Rollback (G07-11) moves the faulty ACTIVE version to ROLLED_BACK and re-activates a
#:   RETIRED version; that is the only way a RETIRED version becomes ACTIVE again, it must
#:   name the rolled-back version, and like every activation it needs its own recorded
#:   owner approval.
#: * A ROLLED_BACK or REJECTED version can never be activated again.
MODEL_STATUS_TRANSITIONS: Final[dict[ModelStatus | None, frozenset[ModelStatus]]] = {
    None: frozenset({ModelStatus.RESEARCH}),
    ModelStatus.RESEARCH: frozenset({ModelStatus.SHADOW, ModelStatus.REJECTED}),
    ModelStatus.SHADOW: frozenset({ModelStatus.ACTIVE, ModelStatus.REJECTED}),
    ModelStatus.ACTIVE: frozenset({ModelStatus.RETIRED, ModelStatus.ROLLED_BACK}),
    ModelStatus.RETIRED: frozenset({ModelStatus.ACTIVE}),
    ModelStatus.ROLLED_BACK: frozenset(),
    ModelStatus.REJECTED: frozenset(),
}

_OWNER_DECISIONS: Final[tuple[ModelStatus, ...]] = (ModelStatus.ACTIVE, ModelStatus.ROLLED_BACK)
_REASONED: Final[tuple[ModelStatus, ...]] = (
    ModelStatus.REJECTED,
    ModelStatus.ROLLED_BACK,
    ModelStatus.RETIRED,
)
_MIN_REVISION: Final[dict[ModelStatus, int]] = {
    ModelStatus.RESEARCH: 0,
    ModelStatus.SHADOW: 1,
    ModelStatus.REJECTED: 1,
    ModelStatus.ACTIVE: 2,
    ModelStatus.RETIRED: 3,
    ModelStatus.ROLLED_BACK: 3,
}
_SHA = Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256")
_URI = Str(pattern=ARTIFACT_URI_PATTERN, max_len=200, label="relative path")


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class ContractRef(Record):
    """A contract version a model was built against."""

    contract: str = fld(
        Str(pattern=CONTRACT_NAME_PATTERN, max_len=48, label="contract name"), "Contract name."
    )
    version: str = fld(Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Contract version.")


def _training_fields(model: ModelVersion) -> bool:
    used = model.training_data_start_ns is not None
    together = (
        (model.training_information_end_ns is not None) == used
        and (model.training_data_origin is not None) == used
        and (model.training_data_sha256 is not None) == used
    )
    if not together:
        return False
    if not used:
        return model.embargo_ns is None
    assert model.training_data_start_ns is not None
    assert model.training_information_end_ns is not None
    return model.training_data_start_ns <= model.training_information_end_ns <= model.created_time_ns


def _activation_evidence(model: ModelVersion) -> bool:
    if model.status is not ModelStatus.ACTIVE:
        return True
    return (
        model.evaluation_report_uri is not None
        and model.evaluation_report_sha256 is not None
        and model.promotion_criteria_sha256 is not None
        and model.evaluation_data_origin is not None
        and bool(model.shadow_run_ids)
    )


@final
@contract("ModelVersion", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVersion(Sequenced):
    """Registry entry of one model artifact and its promotion status.

    Stream: ``model_version/<producer>`` (the model registry).  Ordering time:
    ``status_time_ns``.  Registry times are always readings of the system clock,
    never of a replay clock; a replay takes the models it uses from its run
    configuration, not from the registry status of the moment.

    The artifact is immutable: a status change is a new record with
    ``revision + 1`` and the same ``model_version_id``.  Activation and rollback
    are owner decisions: the record must cite the decision-log entry and the
    evidence (evaluation report, shadow runs, promotion criteria) it rests on;
    the registry refuses a transition whose cited evidence does not exist.
    """

    model_version_id: str = fld(Id("mdl"), "identity.model_version_id(model_name, semver, artifact_sha256).")
    revision: int = fld(Int(ge=0, label="revision"), "Status revision of this model version: 0, 1, 2, ...")
    model_name: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Model family name.")
    semver: str = fld(Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Version of the model.")
    kind: ModelKind = fld(EnumOf(ModelKind), "What the model is.")
    status: ModelStatus = fld(EnumOf(ModelStatus), "Promotion status.")
    status_reasons: tuple[str, ...] = reason_codes_field(
        "Why the status was set; mandatory for REJECTED, ROLLED_BACK and RETIRED."
    )
    status_time_ns: int = fld(
        TimeNs(), "When this status was recorded (system clock).", unit="ns since epoch, UTC"
    )
    created_time_ns: int = fld(
        TimeNs(), "When the artifact was frozen (system clock).", unit="ns since epoch, UTC"
    )
    artifact_sha256: str = fld(_SHA, "SHA-256 of the frozen artifact.")
    artifact_uri: str = fld(
        _URI, "Repository-relative location of the artifact (never a URL with credentials)."
    )
    code_commit: str = fld(
        Str(pattern=GIT_COMMIT_PATTERN, min_len=40, max_len=40, label="git commit"),
        "Commit of the code that produced the artifact.",
    )
    training_data_origin: DataOrigin | None = fld(
        Opt(EnumOf(DataOrigin)),
        "Least trustworthy origin of the data used to produce the artifact; null only when no data "
        "was used.  A model fitted on SYNTHETIC_FIXTURE data is never validated.",
    )
    training_data_sha256: str | None = fld(
        Opt(_SHA), "Hash of the manifest of the data set used; null only when no data was used."
    )
    training_config_sha256: str | None = fld(
        Opt(_SHA), "Hash of the complete training configuration (hyper-parameters, seeds, environment)."
    )
    training_data_start_ns: int | None = fld(
        Opt(TimeNs()), "Earliest event time used; null when no data was used.", unit="ns since epoch, UTC"
    )
    training_information_end_ns: int | None = fld(
        Opt(TimeNs()),
        "Latest availability instant of ANY information used to produce the artifact: features, "
        "labels (including the end of their outcome windows), transforms, feature and "
        "hyper-parameter selection.  A prediction is out of sample only after this instant plus the embargo.",
        unit="ns since epoch, UTC",
    )
    embargo_ns: int | None = fld(
        Opt(DurationNs()), "Embargo applied after training_information_end_ns.", unit="ns"
    )
    split_spec_sha256: str | None = fld(
        Opt(_SHA), "Hash of the purged walk-forward split specification the model was validated with."
    )
    feature_schema_sha256: str | None = fld(Opt(_SHA), "Hash of the feature dictionary the model expects.")
    contract_versions: tuple[ContractRef, ...] = fld(
        ListOf(
            Obj(ContractRef),
            max_items=32,
            unique_key=lambda item: item.contract,
            canonical_sort=lambda item: item.contract,
            sorted_label="sorted by contract",
        ),
        "Contract versions the model was built against.",
    )
    derived_from_model_version_id: str | None = fld(
        Opt(Id("mdl")), "Lineage: the version this one was derived from, if any."
    )
    component_model_version_ids: tuple[str, ...] = fld(
        ListOf(
            Id("mdl"),
            max_items=32,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Component versions of a COMPOSITE model; empty otherwise.",
    )
    seed: int | None = fld(Opt(Int(ge=0, label="seed")), "Random seed used for fitting.")
    metrics: tuple[NamedValue, ...] = fld(
        ListOf(
            Obj(NamedValue),
            max_items=64,
            unique_key=lambda item: item.name,
            canonical_sort=lambda item: item.name,
            sorted_label="sorted by name",
        ),
        "Headline validation metrics (the full results are in the evaluation report).",
    )
    evaluation_report_uri: str | None = fld(
        Opt(_URI), "Model card / shadow report this status rests on; mandatory for ACTIVE."
    )
    evaluation_report_sha256: str | None = fld(Opt(_SHA), "SHA-256 of that report; mandatory for ACTIVE.")
    evaluation_data_origin: DataOrigin | None = fld(
        Opt(EnumOf(DataOrigin)),
        "Least trustworthy origin of the data the evaluation report and the shadow runs rest on; "
        "mandatory for ACTIVE.  A version evaluated on SYNTHETIC_FIXTURE data is never validated.",
    )
    promotion_criteria_sha256: str | None = fld(
        Opt(_SHA), "Hash of the promotion criteria the report was judged against; mandatory for ACTIVE."
    )
    shadow_run_ids: tuple[str, ...] = fld(
        ListOf(
            Id("run"),
            max_items=64,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Runs in which the version was shadow-validated; at least one for ACTIVE.",
    )
    replaces_model_version_id: str | None = fld(
        Opt(Id("mdl")),
        "On an ACTIVE entry: the version that was ACTIVE before it (null if none was).",
    )
    rollback_of_model_version_id: str | None = fld(
        Opt(Id("mdl")),
        "On a re-activation by rollback: the version that was rolled back; null on a first activation.",
    )
    approved_by: str | None = fld(
        Opt(Str(pattern=APPROVER_PATTERN, min_len=2, max_len=64, label="name")),
        "Who authorised the status; mandatory for ACTIVE and ROLLED_BACK.",
    )
    approval_reference: str | None = fld(
        Opt(Str(pattern=DECISION_REFERENCE_PATTERN, max_len=8, label="decision id")),
        "Decision-log entry recording the approval; mandatory for ACTIVE and ROLLED_BACK.",
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "MDL-1",
            "model_version_id equals identity.model_version_id(model_name, semver, artifact_sha256)",
            lambda r: (
                r.model_version_id == identity.model_version_id(r.model_name, r.semver, r.artifact_sha256)
            ),
        ),
        inv(
            "MDL-2",
            "status ACTIVE or ROLLED_BACK requires approved_by and approval_reference (owner decision)",
            lambda r: (
                r.status not in _OWNER_DECISIONS
                or (r.approved_by is not None and r.approval_reference is not None)
            ),
        ),
        inv(
            "MDL-3",
            "training_data_start_ns, training_information_end_ns, training_data_origin and "
            "training_data_sha256 are all set (with start <= information end <= created_time_ns) or "
            "all null; embargo_ns needs them",
            _training_fields,
        ),
        inv(
            "MDL-4",
            "created_time_ns <= status_time_ns",
            lambda r: r.created_time_ns <= r.status_time_ns,
        ),
        inv(
            "MDL-5",
            "the revision is consistent with the path to the status: RESEARCH is revision 0, SHADOW "
            "and REJECTED need revision >= 1, ACTIVE >= 2, RETIRED and ROLLED_BACK >= 3",
            lambda r: (
                r.revision >= _MIN_REVISION[r.status]
                and (r.revision == 0) == (r.status is ModelStatus.RESEARCH)
            ),
        ),
        inv(
            "MDL-6",
            "status_reasons is non-empty for REJECTED, ROLLED_BACK and RETIRED",
            lambda r: r.status not in _REASONED or bool(r.status_reasons),
        ),
        inv(
            "MDL-7",
            "ACTIVE cites its evidence: evaluation report with hash, promotion criteria hash, the "
            "origin of the evaluation data and at least one shadow run",
            _activation_evidence,
        ),
        inv(
            "MDL-8",
            "replaces_model_version_id and rollback_of_model_version_id appear only on ACTIVE entries "
            "and never name the version itself",
            lambda r: all(
                value is None or (r.status is ModelStatus.ACTIVE and value != r.model_version_id)
                for value in (r.replaces_model_version_id, r.rollback_of_model_version_id)
            ),
        ),
        inv(
            "MDL-9",
            "component_model_version_ids is non-empty if and only if kind is COMPOSITE, and never "
            "contains the version itself",
            lambda r: (
                bool(r.component_model_version_ids) == (r.kind is ModelKind.COMPOSITE)
                and r.model_version_id not in r.component_model_version_ids
            ),
        ),
    )

    @property
    def validated(self) -> bool:
        """True only for an owner-approved ACTIVE version neither fitted nor evaluated on fixture data.

        A rule set uses no training data, so its validation rests on its
        evaluation alone; that is why the origin of the evaluation data is
        recorded separately.
        """
        return (
            self.status is ModelStatus.ACTIVE
            and self.training_data_origin is not DataOrigin.SYNTHETIC_FIXTURE
            and self.evaluation_data_origin is not None
            and self.evaluation_data_origin is not DataOrigin.SYNTHETIC_FIXTURE
        )


#: Fields that never change during the registry history of one model version.
MODEL_IMMUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "model_version_id",
    "model_name",
    "semver",
    "kind",
    "created_time_ns",
    "artifact_sha256",
    "artifact_uri",
    "code_commit",
    "training_data_origin",
    "training_data_sha256",
    "training_config_sha256",
    "training_data_start_ns",
    "training_information_end_ns",
    "embargo_ns",
    "split_spec_sha256",
    "feature_schema_sha256",
    "contract_versions",
    "derived_from_model_version_id",
    "component_model_version_ids",
    "seed",
)


def validate_model_transition(previous: ModelVersion | None, entry: ModelVersion) -> None:
    """Check that *entry* may follow *previous* in the registry history of one model version."""

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal model transition: {rule}",
            model_version_id=entry.model_version_id,
            revision=entry.revision,
            rule=rule,
        )

    before = None if previous is None else previous.status
    if entry.status not in MODEL_STATUS_TRANSITIONS[before]:
        raise fail(
            f"{'unregistered' if before is None else before.value} -> {entry.status.value} is not allowed"
        )
    if previous is None:
        if entry.revision != 0:
            raise fail("the first registry entry has revision 0")
        return
    if entry.revision != previous.revision + 1:
        raise fail("revision must increase by exactly 1")
    for name in MODEL_IMMUTABLE_FIELDS:
        if getattr(previous, name) != getattr(entry, name):
            raise fail(f"{name} may not change: a model version is immutable")
    if entry.status_time_ns <= previous.status_time_ns:
        raise fail("status_time_ns must increase with every status change")
    if entry.status is ModelStatus.ACTIVE:
        reactivation = previous.status is ModelStatus.RETIRED
        if reactivation != (entry.rollback_of_model_version_id is not None):
            raise fail("rollback_of_model_version_id is set exactly when a RETIRED version is re-activated")


class RegistryHistory:
    """The family-level rules of the model registry, checked one entry at a time.

    Beyond the per-version rules of :func:`validate_model_transition`, for every
    model family (``kind``, ``model_name``):

    * at most one version is ACTIVE at any time;
    * an activation names, in ``replaces_model_version_id``, the version of the
      family that was ACTIVE before it (none for the first);
    * a re-activation of a RETIRED version is a rollback: its
      ``rollback_of_model_version_id`` names exactly that last ACTIVE version of
      the same family, and that version has been ROLLED_BACK;
    * every activation carries an approval reference that was not used for an
      activation of this version before;
    * the components of a composite version are registered before it is.

    ``check`` validates an entry without recording it; ``admit`` validates and
    records it.  The lifecycle ledger keeps one instance per run, so these rules
    hold for every journal; :func:`validate_registry_history` applies them to a
    complete history.
    """

    __slots__ = ("_active", "_approvals", "_last_active", "_latest")

    def __init__(self) -> None:
        self._latest: dict[str, ModelVersion] = {}
        self._active: dict[tuple[ModelKind, str], str] = {}
        self._last_active: dict[tuple[ModelKind, str], str] = {}
        self._approvals: dict[str, set[str]] = {}

    def model(self, model_version_id: str) -> ModelVersion | None:
        """Newest registry entry of a model version, if it is registered."""
        return self._latest.get(model_version_id)

    def active(self, kind: ModelKind, model_name: str) -> ModelVersion | None:
        """The ACTIVE version of a model family, if any."""
        active_id = self._active.get((kind, model_name))
        return None if active_id is None else self._latest[active_id]

    def check(self, entry: ModelVersion) -> bool:
        """Validate *entry*; returns False when it is the newest entry of its version again."""
        if type(entry) is not ModelVersion:
            raise TypeError("the model registry holds ModelVersion records")

        def fail(rule: str) -> IllegalTransitionError:
            return IllegalTransitionError(
                f"illegal registry history: {rule}",
                model_version_id=entry.model_version_id,
                revision=entry.revision,
                rule=rule,
            )

        previous = self._latest.get(entry.model_version_id)
        if previous is not None and previous == entry:
            return False
        validate_model_transition(previous, entry)
        family = (entry.kind, entry.model_name)
        for component in entry.component_model_version_ids:
            if component not in self._latest:
                raise fail("a component of a composite model is not registered")
        if entry.status is ModelStatus.ACTIVE:
            if family in self._active:
                raise fail("the currently ACTIVE version must be RETIRED or ROLLED_BACK first")
            before = self._last_active.get(family)
            if entry.replaces_model_version_id != before:
                raise fail("replaces_model_version_id must name the version that was ACTIVE before")
            assert entry.approval_reference is not None
            if entry.approval_reference in self._approvals.get(entry.model_version_id, ()):
                raise fail("an activation needs its own approval; this reference was used before")
            rolled_back = entry.rollback_of_model_version_id
            if rolled_back is not None and (
                rolled_back != before or self._latest[rolled_back].status is not ModelStatus.ROLLED_BACK
            ):
                raise fail(
                    "rollback_of_model_version_id must name the version of this family that was ACTIVE "
                    "last and has been ROLLED_BACK"
                )
        return True

    def admit(self, entry: ModelVersion) -> bool:
        """Validate *entry* and make it the newest entry of its version.  Returns False for a repeat."""
        if not self.check(entry):
            return False
        family = (entry.kind, entry.model_name)
        if entry.status is ModelStatus.ACTIVE:
            assert entry.approval_reference is not None
            self._approvals.setdefault(entry.model_version_id, set()).add(entry.approval_reference)
            self._active[family] = entry.model_version_id
            self._last_active[family] = entry.model_version_id
        elif self._active.get(family) == entry.model_version_id:
            del self._active[family]
        self._latest[entry.model_version_id] = entry
        return True


def validate_registry_history(entries: Sequence[ModelVersion]) -> None:
    """Check a complete registry history (all versions, in registry order).

    Applies :class:`RegistryHistory` to every entry in turn and, because the
    history is complete, also requires the registry ``seq`` to increase by
    exactly 1.
    """
    history = RegistryHistory()
    last_seq: int | None = None
    for entry in entries:
        if type(entry) is not ModelVersion:
            raise TypeError("validate_registry_history expects ModelVersion records")
        if last_seq is not None and entry.seq != last_seq + 1:
            raise IllegalTransitionError(
                "illegal registry history: registry seq must increase by exactly 1",
                model_version_id=entry.model_version_id,
                revision=entry.revision,
                rule="registry seq must increase by exactly 1",
            )
        last_seq = entry.seq
        history.admit(entry)


def is_out_of_sample(as_of_time_ns: int, model: ModelVersion) -> bool:
    """True when a prediction with the given market-data watermark is out of sample for *model*.

    A model that used no data (a rule set) is out of sample for every instant.
    Otherwise the watermark must lie after ``training_information_end_ns`` plus
    the embargo.  Use it with ``Candidate.as_of_time_ns`` in leakage checks.
    """
    if model.training_information_end_ns is None:
        return True
    return as_of_time_ns > model.training_information_end_ns + (model.embargo_ns or 0)
