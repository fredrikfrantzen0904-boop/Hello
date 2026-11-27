"""Encoding and decoding of contract records (wire form = canonical JSON object).

Envelope: every top-level record is a JSON object that carries ``contract``
(the contract name) and ``schema_version`` (``MAJOR.MINOR.PATCH``) next to its
fields.  All fields are always written, optional ones as explicit ``null``.

Reader compatibility rules (see ``specifications/SHARED_CONTRACTS.md`` section 8):

* a different MAJOR version is refused (:class:`ContractVersionError`);
* the same MAJOR with a MINOR not newer than the reader's is decoded strictly:
  an unknown field is an error, a missing optional field is ``null``;
* the same MAJOR with a *newer* MINOR is decoded tolerantly: unknown fields are
  ignored, because a newer minor version may only add optional fields.  Unknown
  enum values are still refused (fail closed);
* PATCH never affects decoding.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from gie.contracts._spec import (
    ENVELOPE_CONTRACT,
    ENVELOPE_VERSION,
    DecodeContext,
    Record,
    decode_fields,
    encode_fields,
)
from gie.contracts.registry import contract_version, get_contract, parse_semver
from gie.core.canonical import canonical_bytes, canonical_dumps, content_hash, strict_loads
from gie.core.errors import ContractValidationError, ContractVersionError, UnknownContractError

T = TypeVar("T", bound=Record)


def to_wire(value: Record) -> dict[str, Any]:
    """Record -> JSON-compatible ``dict`` including the envelope."""
    record_type = type(value)
    if record_type.CONTRACT is None or record_type.VERSION is None:
        raise UnknownContractError(
            "only top-level contracts can be encoded with an envelope", contract=record_type.__name__
        )
    out: dict[str, Any] = {
        ENVELOPE_CONTRACT: record_type.CONTRACT,
        ENVELOPE_VERSION: record_type.VERSION,
    }
    out.update(encode_fields(value))
    return out


def to_json(value: Record) -> str:
    """Record -> canonical JSON text."""
    return canonical_dumps(to_wire(value))


def to_bytes(value: Record) -> bytes:
    """Record -> canonical UTF-8 bytes (the form that is journaled and hashed)."""
    return canonical_bytes(to_wire(value))


def fingerprint(value: Record) -> str:
    """SHA-256 (hex) of the canonical bytes; used for idempotent duplicate detection."""
    return content_hash(to_wire(value))


def from_wire(data: Any, expect: type[T] | None = None) -> T:
    """JSON-compatible ``dict`` -> validated record.

    ``expect`` pins the contract type; a message of another contract is refused.
    """
    if not isinstance(data, Mapping):
        raise ContractValidationError(
            "<root>: must be a JSON object", path="<root>", actual_type=type(data).__name__
        )
    record_type = get_contract(data.get(ENVELOPE_CONTRACT))
    if expect is not None and record_type is not expect:
        raise ContractValidationError(
            "unexpected contract",
            path=ENVELOPE_CONTRACT,
            expected=expect.CONTRACT,
            actual=record_type.CONTRACT,
        )
    reader = contract_version(record_type)
    if ENVELOPE_VERSION not in data:
        raise ContractVersionError("schema_version is missing", contract=record_type.CONTRACT)
    message = parse_semver(data[ENVELOPE_VERSION])
    if message.major != reader.major:
        raise ContractVersionError(
            "incompatible MAJOR version",
            contract=record_type.CONTRACT,
            message_version=str(message),
            reader_version=str(reader),
        )
    ctx = DecodeContext(ignore_unknown_fields=message.minor > reader.minor)
    return cast(T, decode_fields(record_type, data, "", ctx, envelope=True))


def from_json(text: str | bytes, expect: type[T] | None = None) -> T:
    """Canonical (or any strict) JSON text -> validated record."""
    return from_wire(strict_loads(text), expect)
