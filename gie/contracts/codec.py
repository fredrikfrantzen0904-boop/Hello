"""Encoding and decoding of contract records (wire form = canonical JSON object).

Envelope: every top-level record is a JSON object that carries ``contract``
(the contract name) and ``schema_version`` (``MAJOR.MINOR.PATCH``) next to its
fields.  All fields are always written, optional ones as explicit ``null``.

Reader compatibility rules (see ``specifications/SHARED_CONTRACTS.md`` section 8):

* a different MAJOR version is refused (:class:`ContractVersionError`);
* the same MAJOR with a MINOR not newer than the reader's is decoded strictly:
  an unknown field is an error, and so is a missing field that the message's
  version already has (writers always write optional fields, as ``null``); only
  a field introduced by a later MINOR than the message's may be absent;
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
    # Exact registered type: a subclass that skipped validation must not pass as the contract.
    if get_contract(record_type.CONTRACT) is not record_type:
        raise UnknownContractError(
            "record type is not the registered contract class", contract=record_type.__name__
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
    """Content identity of a record: SHA-256 (hex) of contract name, MAJOR version and fields.

    MINOR and PATCH are left out on purpose: they never change the meaning of
    existing fields, so the same record written by two compatible versions has
    the same fingerprint and is recognised as an idempotent duplicate.
    """
    wire = to_wire(value)
    major = parse_semver(wire.pop(ENVELOPE_VERSION)).major
    return content_hash({"contract": wire.pop(ENVELOPE_CONTRACT), "major": major, "fields": wire})


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
    ctx = DecodeContext(
        ignore_unknown_fields=message.minor > reader.minor,
        message_version=(message.major, message.minor),
    )
    return cast(T, decode_fields(record_type, data, "", ctx, envelope=True))


def from_json(text: str | bytes, expect: type[T] | None = None) -> T:
    """Canonical (or any strict) JSON text -> validated record."""
    return from_wire(strict_loads(text), expect)
