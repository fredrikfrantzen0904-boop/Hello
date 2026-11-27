"""Shared error taxonomy (requirement S00-ASG-24, "error semantics").

Rules that every GIE component follows:

* **Fail closed.**  When any :class:`GieError` is raised while a component is
  deciding whether to *create or increase* risk, the component must not create
  that risk.  Actions that only *reduce* risk (cancelling a pending entry,
  closing a position, tightening a stop) stay permitted.
* **Never coerce silently.**  Invalid input is rejected with an error that names
  the contract, the field path and the violated rule; it is never repaired,
  clamped or defaulted.
* **Stable codes.**  ``code`` is a machine-readable identifier that is part of the
  shared contract surface; changing or removing a code is a breaking change.
* **Structured details.**  ``details`` only carries JSON scalars so every error
  can be journaled and shown on the dashboard without custom handling.
"""

from __future__ import annotations

from typing import ClassVar, Final

DetailValue = str | int | float | bool | None


class GieError(Exception):
    """Base class of every error raised by GIE code."""

    code: ClassVar[str] = "GIE.ERROR"

    def __init__(self, message: str, **details: DetailValue) -> None:
        super().__init__(message)
        self.message: Final[str] = message
        self.details: Final[dict[str, DetailValue]] = dict(details)

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable form used in journals and evidence files."""
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def __str__(self) -> str:
        if not self.details:
            return f"[{self.code}] {self.message}"
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
        return f"[{self.code}] {self.message} ({rendered})"


# --------------------------------------------------------------------------- contracts
class ContractError(GieError):
    """Any problem with a contract message."""

    code = "GIE.CONTRACT.ERROR"


class ContractValidationError(ContractError):
    """A field constraint or cross-field invariant is violated."""

    code = "GIE.CONTRACT.INVALID"


class UnknownContractError(ContractError):
    """The ``contract`` name is not present in the registry."""

    code = "GIE.CONTRACT.UNKNOWN"


class ContractVersionError(ContractError):
    """The message version cannot be read by this reader (see compatibility rules)."""

    code = "GIE.CONTRACT.VERSION"


class CanonicalEncodingError(ContractError):
    """A value cannot be represented in canonical JSON (NaN, unsupported type, bad JSON)."""

    code = "GIE.CONTRACT.ENCODING"


# --------------------------------------------------------------------------- time
class TimeError(GieError):
    code = "GIE.TIME.ERROR"


class StaleDataError(TimeError):
    """Data is older than its freshness limit; it must not drive a risk-increasing decision."""

    code = "GIE.TIME.STALE"


class FutureDataError(TimeError):
    """Data claims to be available later than *now*: clock skew or look-ahead leakage."""

    code = "GIE.TIME.FUTURE"


class ClockRegressionError(TimeError):
    """A clock was asked to move backwards."""

    code = "GIE.TIME.CLOCK_REGRESSION"


# --------------------------------------------------------------------------- sequencing
class SequenceError(GieError):
    code = "GIE.SEQ.ERROR"


class OutOfOrderError(SequenceError):
    """An event arrived with a sequence number lower than one already accepted."""

    code = "GIE.SEQ.OUT_OF_ORDER"


class SequenceConflictError(SequenceError):
    """Two different payloads claim the same stream position (not an idempotent duplicate)."""

    code = "GIE.SEQ.CONFLICT"


class SequenceGapError(SequenceError):
    """One or more sequence numbers are missing from a stream that must be gap-free."""

    code = "GIE.SEQ.GAP"


# --------------------------------------------------------------------------- state / routing / config
class IllegalTransitionError(GieError):
    """A lifecycle transition (order, position, model) is not permitted."""

    code = "GIE.STATE.ILLEGAL_TRANSITION"


class LiveRoutingLockedError(GieError):
    """Real order routing was requested.  It is locked for the whole project phase."""

    code = "GIE.ROUTING.LIVE_LOCKED"


class ConfigError(GieError):
    """Configuration or research-parameter input is missing or invalid."""

    code = "GIE.CONFIG.INVALID"


ALL_ERROR_TYPES: Final[tuple[type[GieError], ...]] = (
    GieError,
    ContractError,
    ContractValidationError,
    UnknownContractError,
    ContractVersionError,
    CanonicalEncodingError,
    TimeError,
    StaleDataError,
    FutureDataError,
    ClockRegressionError,
    SequenceError,
    OutOfOrderError,
    SequenceConflictError,
    SequenceGapError,
    IllegalTransitionError,
    LiveRoutingLockedError,
    ConfigError,
)

ERROR_CODES: Final[dict[str, type[GieError]]] = {t.code: t for t in ALL_ERROR_TYPES}
