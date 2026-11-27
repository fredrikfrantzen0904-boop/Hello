"""Registry of versioned top-level contracts."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from gie.contracts._spec import Record, record
from gie.core.errors import ContractVersionError, UnknownContractError

_SEMVER_RE: Final[re.Pattern[str]] = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SEMVER_PATTERN: Final[str] = _SEMVER_RE.pattern


@dataclass(frozen=True, slots=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_semver(text: object) -> SemVer:
    if not isinstance(text, str):
        raise ContractVersionError("schema_version must be a string", value=repr(text)[:64])
    match = _SEMVER_RE.match(text)
    if match is None:
        raise ContractVersionError("schema_version must be MAJOR.MINOR.PATCH", value=text[:64])
    return SemVer(int(match[1]), int(match[2]), int(match[3]))


_CONTRACTS: dict[str, type[Record]] = {}

R = TypeVar("R", bound=type[Record])


def contract(name: str, version: str) -> Callable[[R], R]:
    """Class decorator registering a top-level contract.  Apply *above* ``@dataclass``."""
    parse_semver(version)

    def decorate(cls: R) -> R:
        finalised = record(cls)
        if name in _CONTRACTS:
            raise TypeError(f"contract {name!r} is already registered")
        finalised.CONTRACT = name
        finalised.VERSION = version
        _CONTRACTS[name] = finalised
        return finalised

    return decorate


def get_contract(name: object) -> type[Record]:
    if not isinstance(name, str) or name not in _CONTRACTS:
        raise UnknownContractError("unknown contract", contract=repr(name)[:64])
    return _CONTRACTS[name]


def all_contracts() -> dict[str, type[Record]]:
    """Registered contracts by name, in registration order."""
    return dict(_CONTRACTS)


def contract_version(record_type: type[Record]) -> SemVer:
    if record_type.VERSION is None:
        raise UnknownContractError("not a top-level contract", contract=record_type.__name__)
    return parse_semver(record_type.VERSION)
