"""Research parameters (requirements M-DIR-08, G05-05).

All numerical settings named by the master specification live in
``specifications/research_parameters.toml`` together with their kind, unit and
the requirement they come from.  They are *proposed research parameters*, not
approved production limits; code obtains them here and never hard-codes them.

The loader fails closed.  It refuses a file that is unreadable, not valid TOML,
not labelled as proposed research parameters, that contains an unknown key, a
value of the wrong kind or out of range, that lacks a required parameter, or
whose daily levels are not ordered ``halt < defensive < recovery < 0 < profit
protection``.  A percentage typed where a fraction is expected (``-3`` for
``-0.03``) is therefore an error, not a silently disabled limit.

``parameters_sha256`` identifies the exact parameter set; it belongs in the
configuration hash of every run.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Final

from gie.core.canonical import content_hash, decimal_from_str, decimal_to_str
from gie.core.errors import CanonicalEncodingError, ConfigError

DEFAULT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "specifications" / "research_parameters.toml"
)
EXPECTED_STATUS: Final[str] = "PROPOSED_RESEARCH_PARAMETERS"

KIND_DURATION_NS: Final[str] = "duration_ns"
KIND_FRACTION: Final[str] = "fraction"
KIND_COUNT: Final[str] = "count"
KINDS: Final[tuple[str, ...]] = (KIND_DURATION_NS, KIND_FRACTION, KIND_COUNT)

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"schema_version", "approval_status", "parameters"})
_PARAMETER_KEYS: Final[frozenset[str]] = frozenset({"kind", "value", "unit", "requirement", "description"})

#: Parameters every consumer may rely on, with their kind.  Removing one is a breaking change.
REQUIRED_PARAMETERS: Final[dict[str, str]] = {
    "tick_processing_target_ns": KIND_DURATION_NS,
    "protection_decision_target_ns": KIND_DURATION_NS,
    "tactical_decision_target_ns": KIND_DURATION_NS,
    "daily_defensive_threshold_frac": KIND_FRACTION,
    "daily_recovery_threshold_frac": KIND_FRACTION,
    "daily_emergency_halt_threshold_frac": KIND_FRACTION,
    "daily_profit_protection_threshold_frac": KIND_FRACTION,
    "target_min_trades_per_eligible_day": KIND_COUNT,
    "target_average_daily_net_roi_frac": KIND_FRACTION,
}

_MAX_INT: Final[int] = 2**63 - 1


@dataclass(frozen=True, slots=True)
class ResearchParameter:
    name: str
    kind: str
    value: int | Decimal
    unit: str
    requirement: str
    description: str


class ResearchParameters:
    """Immutable, validated view of the research-parameter file.

    The view cannot be relabelled or re-hashed after it was built: assigning or
    deleting an attribute raises :class:`ConfigError`.  ``parameters_sha256``
    therefore always identifies the values this object returns.
    """

    __slots__ = ("_parameters", "approval_status", "parameters_sha256")

    _parameters: MappingProxyType[str, ResearchParameter]
    approval_status: str
    parameters_sha256: str

    def __init__(self, parameters: dict[str, ResearchParameter], approval_status: str) -> None:
        frozen = MappingProxyType(dict(parameters))
        object.__setattr__(self, "_parameters", frozen)
        object.__setattr__(self, "approval_status", approval_status)
        object.__setattr__(
            self,
            "parameters_sha256",
            content_hash(
                {
                    name: {
                        "kind": parameter.kind,
                        "value": (
                            decimal_to_str(parameter.value)
                            if isinstance(parameter.value, Decimal)
                            else parameter.value
                        ),
                    }
                    for name, parameter in frozen.items()
                }
            ),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise ConfigError("research parameters are immutable", attribute=name)

    def __delattr__(self, name: str) -> None:
        raise ConfigError("research parameters are immutable", attribute=name)

    def get(self, name: str) -> ResearchParameter:
        try:
            return self._parameters[name]
        except KeyError:
            raise ConfigError("unknown research parameter", name=str(name)[:64]) from None

    def value(self, name: str) -> int | Decimal:
        return self.get(name).value

    def decimal(self, name: str) -> Decimal:
        value = self.get(name).value
        if not isinstance(value, Decimal):
            raise ConfigError("research parameter is not a decimal", name=name)
        return value

    def integer(self, name: str) -> int:
        value = self.get(name).value
        if not isinstance(value, int):
            raise ConfigError("research parameter is not an integer", name=name)
        return value

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._parameters))


def _parse_value(name: str, kind: str, raw: object) -> int | Decimal:
    if kind == KIND_FRACTION:
        if not isinstance(raw, str):
            raise ConfigError(
                "a fraction must be written as a decimal string (floats are not exact)",
                name=name,
                actual_type=type(raw).__name__,
            )
        try:
            value = decimal_from_str(raw)
        except CanonicalEncodingError:
            raise ConfigError("research parameter is not a plain decimal string", name=name) from None
        if not -1 < value < 1:
            raise ConfigError(
                "a fraction must lie strictly between -1 and 1 (0.05 means 5 %)", name=name, value=raw
            )
        return value
    if type(raw) is not int:
        raise ConfigError("research parameter must be an integer", name=name, actual_type=type(raw).__name__)
    minimum = 1 if kind == KIND_DURATION_NS else 0
    if not minimum <= raw <= _MAX_INT:
        raise ConfigError("research parameter is out of range", name=name, value=raw, minimum=minimum)
    return raw


def _check_daily_levels(parameters: dict[str, ResearchParameter]) -> None:
    halt = parameters["daily_emergency_halt_threshold_frac"].value
    defensive = parameters["daily_defensive_threshold_frac"].value
    recovery = parameters["daily_recovery_threshold_frac"].value
    profit = parameters["daily_profit_protection_threshold_frac"].value
    if not halt < defensive < recovery < 0 < profit:
        raise ConfigError(
            "daily levels must satisfy halt < defensive < recovery < 0 < profit protection",
            halt=str(halt),
            defensive=str(defensive),
            recovery=str(recovery),
            profit_protection=str(profit),
        )


def load_research_parameters(path: Path | None = None) -> ResearchParameters:
    """Load and validate the research-parameter file (fails closed on any defect)."""
    source = DEFAULT_PATH if path is None else path
    try:
        with source.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError("research parameter file cannot be read", path=str(source)) from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
        # ValueError: tomllib leaks it for an integer literal too long to convert.
        raise ConfigError("research parameter file is not valid UTF-8 TOML", path=str(source)) from exc
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError("unknown top-level key in research parameter file", key=unknown[0])
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ConfigError("unsupported research parameter schema_version", path=str(source))
    status = document.get("approval_status")
    if status != EXPECTED_STATUS:
        raise ConfigError(
            "research parameters must be labelled PROPOSED_RESEARCH_PARAMETERS in this project phase",
            path=str(source),
        )
    tables = document.get("parameters")
    if not isinstance(tables, dict) or not tables:
        raise ConfigError("research parameter file defines no parameters", path=str(source))
    parameters: dict[str, ResearchParameter] = {}
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise ConfigError("research parameter must be a table", name=name)
        missing = sorted(_PARAMETER_KEYS - set(table))
        if missing:
            raise ConfigError("research parameter is incomplete", name=name, missing=",".join(missing))
        extra = sorted(set(table) - _PARAMETER_KEYS)
        if extra:
            raise ConfigError("unknown key in research parameter", name=name, key=extra[0])
        for key in ("unit", "requirement", "description"):
            if not isinstance(table[key], str) or not table[key].strip():
                raise ConfigError(
                    "research parameter text field must be a non-empty string", name=name, field=key
                )
        kind = table["kind"]
        if kind not in KINDS:
            raise ConfigError("unknown research parameter kind", name=name, kind=str(kind)[:32])
        parameters[name] = ResearchParameter(
            name=name,
            kind=kind,
            value=_parse_value(name, kind, table["value"]),
            unit=table["unit"],
            requirement=table["requirement"],
            description=table["description"],
        )
    for name, kind in REQUIRED_PARAMETERS.items():
        if name not in parameters:
            raise ConfigError("required research parameter is missing", name=name)
        if parameters[name].kind != kind:
            raise ConfigError("required research parameter has the wrong kind", name=name, expected=kind)
    _check_daily_levels(parameters)
    return ResearchParameters(parameters, status)
