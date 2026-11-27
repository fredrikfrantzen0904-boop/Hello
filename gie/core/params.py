"""Research parameters (requirements M-DIR-08, G05-05).

All numerical settings named by the master specification live in
``specifications/research_parameters.toml`` together with their unit and the
requirement they come from.  They are *proposed research parameters*, not
approved production limits; code obtains them here and never hard-codes them.

Integer parameters are returned as ``int``; fractional ones are written as
strings in the file and returned as exact :class:`~decimal.Decimal`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from gie.core.errors import ConfigError

DEFAULT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "specifications" / "research_parameters.toml"
)
EXPECTED_STATUS: Final[str] = "PROPOSED_RESEARCH_PARAMETERS"


@dataclass(frozen=True, slots=True)
class ResearchParameter:
    name: str
    value: int | Decimal
    unit: str
    requirement: str
    description: str


class ResearchParameters:
    """Immutable, validated view of the research-parameter file."""

    def __init__(self, parameters: dict[str, ResearchParameter], approval_status: str) -> None:
        self._parameters: Final[dict[str, ResearchParameter]] = dict(parameters)
        self.approval_status: Final[str] = approval_status

    def get(self, name: str) -> ResearchParameter:
        try:
            return self._parameters[name]
        except KeyError:
            raise ConfigError("unknown research parameter", name=name) from None

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


def _parse_value(name: str, raw: object) -> int | Decimal:
    if isinstance(raw, bool):
        raise ConfigError("research parameter value must be an int or a decimal string", name=name)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ConfigError("research parameter is not a valid decimal", name=name) from None
        if not value.is_finite():
            raise ConfigError("research parameter must be finite", name=name)
        return value
    raise ConfigError(
        "research parameter value must be an int or a decimal string (floats are not exact)",
        name=name,
        actual_type=type(raw).__name__,
    )


def load_research_parameters(path: Path | None = None) -> ResearchParameters:
    """Load and validate the research-parameter file (fails closed on any defect)."""
    source = DEFAULT_PATH if path is None else path
    try:
        with source.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError("research parameter file cannot be read", path=str(source)) from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("research parameter file is not valid UTF-8 TOML", path=str(source)) from exc
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
        missing = [key for key in ("value", "unit", "requirement", "description") if key not in table]
        if missing:
            raise ConfigError("research parameter is incomplete", name=name, missing=",".join(missing))
        for key in ("unit", "requirement", "description"):
            if not isinstance(table[key], str) or not table[key].strip():
                raise ConfigError(
                    "research parameter text field must be a non-empty string", name=name, field=key
                )
        parameters[name] = ResearchParameter(
            name=name,
            value=_parse_value(name, table["value"]),
            unit=table["unit"],
            requirement=table["requirement"],
            description=table["description"],
        )
    return ResearchParameters(parameters, status)
