"""Deterministic identifiers (requirement S00-ASG-22, "IDs").

Every GIE identifier is ``<prefix>_<32 lower-case hex characters>`` where the
hex part is the first 128 bits of the SHA-256 of the canonical JSON array of
the *identity parts*.  Identifiers are therefore

* **deterministic** - replaying the same inputs yields the same IDs, which is
  what makes replay byte-for-byte reproducible and orders idempotent;
* **content derived** - two producers that describe the same market event with
  the same identity parts obtain the same ID, which is what exact evidence
  de-duplication relies on;
* **opaque** - consumers must never parse meaning out of the hex part.

Random identifiers (``uuid4``), wall-clock derived identifiers and process
counters that are not persisted are forbidden in any path that feeds replay.

Identity parts may be strings, integers, decimals, ``None`` and nested
lists/tuples of those.  Floats are rejected because their text form is not a
safe identity.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from gie.core.canonical import canonical_bytes, decimal_to_str
from gie.core.errors import ContractValidationError

IdPart = str | int | Decimal | None | Sequence["IdPart"]

#: Registered prefixes.  Adding a prefix is a backwards-compatible (minor) change.
ID_PREFIXES: Final[dict[str, str]] = {
    "run": "one replay or paper run",
    "mev": "market event (shared identity used for evidence de-duplication)",
    "opp": "opportunity (stable identity of a setup in one direction and horizon)",
    "evd": "evidence record",
    "cnd": "candidate",
    "grp": "group of alternative trade plans",
    "pln": "trade plan",
    "rsk": "risk decision",
    "rsv": "risk reservation",
    "ord": "client order id (idempotency key)",
    "brk": "order id assigned by the simulated broker",
    "oev": "order event",
    "pos": "position",
    "mdl": "model version",
}

ID_HEX_LENGTH: Final[int] = 32
ID_PATTERN: Final[str] = r"^[a-z]{3}_[0-9a-f]{32}$"
_ID_RE: Final[re.Pattern[str]] = re.compile(ID_PATTERN)


def id_pattern(prefix: str) -> str:
    """Regular expression (as text) matching identifiers with the given prefix."""
    _check_prefix(prefix)
    return rf"^{prefix}_[0-9a-f]{{{ID_HEX_LENGTH}}}$"


def _check_prefix(prefix: str) -> None:
    if prefix not in ID_PREFIXES:
        raise ContractValidationError("unknown identifier prefix", prefix=str(prefix))


def _normalise(part: IdPart, path: str) -> object:
    if part is None or isinstance(part, str):
        return part
    if isinstance(part, bool):
        raise ContractValidationError("bool is not a valid identity part", path=path)
    if isinstance(part, int):
        return part
    if isinstance(part, Decimal):
        return {"d": decimal_to_str(part)}
    if isinstance(part, (tuple, list)):
        return [_normalise(item, f"{path}[{i}]") for i, item in enumerate(part)]
    raise ContractValidationError(
        "unsupported identity part (floats are forbidden in identifiers)",
        path=path,
        actual_type=type(part).__name__,
    )


def make_id(prefix: str, *parts: IdPart) -> str:
    """Build the deterministic identifier for *prefix* and the given identity parts."""
    _check_prefix(prefix)
    if not parts:
        raise ContractValidationError("an identifier needs at least one identity part", prefix=prefix)
    payload = [prefix, [_normalise(part, f"parts[{i}]") for i, part in enumerate(parts)]]
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return f"{prefix}_{digest[:ID_HEX_LENGTH]}"


def is_valid_id(value: object, prefix: str | None = None) -> bool:
    """True when *value* is a well-formed identifier (optionally of a given prefix)."""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        return False
    found = value[:3]
    if found not in ID_PREFIXES:
        return False
    return prefix is None or found == prefix


def require_id(value: object, prefix: str, *, path: str = "id") -> str:
    """Return *value* if it is a valid identifier of *prefix*, else raise."""
    _check_prefix(prefix)
    if not is_valid_id(value, prefix):
        raise ContractValidationError(
            "malformed identifier", path=path, expected_prefix=prefix, value=repr(value)[:64]
        )
    assert isinstance(value, str)
    return value


def derive_seed(run_seed: int, purpose: str) -> int:
    """Seed for one named pseudo-random element of a run, derived from the run seed.

    A run has one seed (``RunManifest.seed``).  Every component that needs
    randomness derives its own generator seed with a stable *purpose* text
    (for example ``"backtesting.bootstrap"``), so that adding a consumer never
    changes the numbers another consumer draws.  The result is in ``0 .. 2**63-1``.
    """
    if isinstance(run_seed, bool) or not isinstance(run_seed, int) or not 0 <= run_seed <= 2**63 - 1:
        raise ContractValidationError("run_seed must be an int in 0 .. 2**63-1")
    if not isinstance(purpose, str) or not purpose or not purpose.isprintable() or len(purpose) > 120:
        raise ContractValidationError("purpose must be printable text of 1..120 characters")
    digest = hashlib.sha256(canonical_bytes(["seed", run_seed, purpose])).digest()
    return int.from_bytes(digest[:8], "big") >> 1
