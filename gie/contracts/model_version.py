"""ModelVersion contract - one immutable, versioned model (or rule set) and its status."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from gie.contracts import identity
from gie.contracts._spec import (
    EnumOf,
    Id,
    Int,
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
from gie.contracts.common import CODE_PATTERN, NamedValue, Sequenced
from gie.contracts.registry import SEMVER_PATTERN, contract
from gie.core.errors import IllegalTransitionError

SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
GIT_COMMIT_PATTERN: Final[str] = r"^[0-9a-f]{40}$"
#: Relative artifact location.  No scheme, no credentials, no parent traversal.
ARTIFACT_URI_PATTERN: Final[str] = r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}$"
CONTRACT_NAME_PATTERN: Final[str] = r"^[A-Z][A-Za-z0-9]{1,47}$"


class ModelKind(str, Enum):
    RULESET = "RULESET"
    NET_R_MODEL = "NET_R_MODEL"
    PAYOFF_MODEL = "PAYOFF_MODEL"
    SIMILARITY_INDEX = "SIMILARITY_INDEX"
    DESTINATION_MODEL = "DESTINATION_MODEL"
    COMPOSITE = "COMPOSITE"


class ModelStatus(str, Enum):
    """Promotion status (G07).  Only ACTIVE versions count as *validated*."""

    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


#: ``status before -> statuses allowed after``.  ``None`` is "not registered yet".
#: ACTIVE is reachable only from SHADOW (shadow validation is mandatory), and a
#: ROLLED_BACK or REJECTED version can never be reactivated.
MODEL_STATUS_TRANSITIONS: Final[dict[ModelStatus | None, frozenset[ModelStatus]]] = {
    None: frozenset({ModelStatus.RESEARCH}),
    ModelStatus.RESEARCH: frozenset({ModelStatus.SHADOW, ModelStatus.REJECTED}),
    ModelStatus.SHADOW: frozenset({ModelStatus.ACTIVE, ModelStatus.REJECTED}),
    ModelStatus.ACTIVE: frozenset({ModelStatus.RETIRED, ModelStatus.ROLLED_BACK}),
    ModelStatus.RETIRED: frozenset(),
    ModelStatus.ROLLED_BACK: frozenset(),
    ModelStatus.REJECTED: frozenset(),
}

_OWNER_DECISIONS = (ModelStatus.ACTIVE, ModelStatus.ROLLED_BACK)


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class ContractRef(Record):
    """A contract version a model was built against."""

    contract: str = fld(
        Str(pattern=CONTRACT_NAME_PATTERN, max_len=48, label="contract name"), "Contract name."
    )
    version: str = fld(Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Contract version.")


@contract("ModelVersion", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVersion(Sequenced):
    """Registry entry of one model artifact and its promotion status.

    Stream: ``model_version/<producer>`` (the model registry).  Ordering time:
    ``status_time_ns``.  The artifact itself is immutable: a status change is a
    new record with ``revision + 1`` and the same ``model_version_id``.
    Promotion to ACTIVE and rollback are owner decisions and must cite the
    decision-log entry that records the approval.
    """

    model_version_id: str = fld(Id("mdl"), "identity.model_version_id(model_name, semver, artifact_sha256).")
    revision: int = fld(Int(ge=0, label="revision"), "Status revision of this model version: 0, 1, 2, ...")
    model_name: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Model family name.")
    semver: str = fld(Str(pattern=SEMVER_PATTERN, max_len=32, label="semver"), "Version of the model.")
    kind: ModelKind = fld(EnumOf(ModelKind), "What the model is.")
    status: ModelStatus = fld(EnumOf(ModelStatus), "Promotion status.")
    status_time_ns: int = fld(TimeNs(), "When this status was recorded.", unit="ns since epoch, UTC")
    created_time_ns: int = fld(TimeNs(), "When the artifact was frozen.", unit="ns since epoch, UTC")
    artifact_sha256: str = fld(
        Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256"), "SHA-256 of the frozen artifact."
    )
    artifact_uri: str = fld(
        Str(pattern=ARTIFACT_URI_PATTERN, max_len=200, label="relative path"),
        "Repository-relative location of the artifact (never a URL with credentials).",
    )
    code_commit: str = fld(
        Str(pattern=GIT_COMMIT_PATTERN, min_len=40, max_len=40, label="git commit"),
        "Commit of the code that produced the artifact.",
    )
    training_data_start_ns: int | None = fld(
        Opt(TimeNs()), "Earliest event time used for fitting; null for rule sets.", unit="ns since epoch, UTC"
    )
    training_data_end_ns: int | None = fld(
        Opt(TimeNs()),
        "Latest event time used for fitting; predictions for earlier instants are in-sample.",
        unit="ns since epoch, UTC",
    )
    feature_schema_sha256: str | None = fld(
        Opt(Str(pattern=SHA256_PATTERN, min_len=64, max_len=64, label="sha256")),
        "Hash of the feature dictionary the model expects.",
    )
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
    parent_model_version_id: str | None = fld(
        Opt(Id("mdl")), "Version this one was derived from, and the rollback target."
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
        "Headline validation metrics (full results live in the model card).",
    )
    approved_by: str | None = fld(
        Opt(Str(max_len=64, label="name")), "Who authorised the status; mandatory for ACTIVE and ROLLED_BACK."
    )
    approval_reference: str | None = fld(
        Opt(Str(pattern=r"^D-[0-9]{3,5}$", max_len=8, label="decision id")),
        "Decision-log entry recording the approval; mandatory for ACTIVE and ROLLED_BACK.",
    )

    INVARIANTS = (
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
            "training_data_start_ns and training_data_end_ns are both null or both set with start <= end "
            "<= created_time_ns",
            lambda r: (
                r.training_data_end_ns is None
                if r.training_data_start_ns is None
                else (
                    r.training_data_end_ns is not None
                    and r.training_data_start_ns <= r.training_data_end_ns <= r.created_time_ns
                )
            ),
        ),
        inv(
            "MDL-4",
            "created_time_ns <= status_time_ns",
            lambda r: r.created_time_ns <= r.status_time_ns,
        ),
        inv(
            "MDL-5",
            "revision 0 has status RESEARCH",
            lambda r: r.revision != 0 or r.status is ModelStatus.RESEARCH,
        ),
        inv(
            "MDL-6",
            "status ROLLED_BACK requires parent_model_version_id (the version rolled back to)",
            lambda r: r.status is not ModelStatus.ROLLED_BACK or r.parent_model_version_id is not None,
        ),
    )

    @property
    def validated(self) -> bool:
        """True only for an owner-approved ACTIVE version."""
        return self.status is ModelStatus.ACTIVE


_MODEL_IMMUTABLE = (
    "model_version_id",
    "model_name",
    "semver",
    "kind",
    "created_time_ns",
    "artifact_sha256",
    "artifact_uri",
    "code_commit",
    "training_data_start_ns",
    "training_data_end_ns",
    "feature_schema_sha256",
    "contract_versions",
    "parent_model_version_id",
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
    for name in _MODEL_IMMUTABLE:
        if getattr(previous, name) != getattr(entry, name):
            raise fail(f"{name} may not change: a model version is immutable")
    if entry.status_time_ns < previous.status_time_ns:
        raise fail("status_time_ns may not decrease")
