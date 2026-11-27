"""Independent tests for ``gie.core.params`` (requirements M-DIR-08, G05-05).

Three things are verified:

1. The real file ``specifications/research_parameters.toml`` carries exactly the numbers that
   the master specification states in sections G01, G03 and G05
   (``specifications/extracted/MASTER.txt``).  Each expected number is written out below with
   the sentence it comes from, and is additionally re-derived by parsing that sentence out of
   the specification text, so neither the file nor this test can drift from the source unnoticed.
2. The loader is strict and fails closed (``ConfigError``), as the module docstring and the header
   of the parameter file document after the independent review:

   * every parameter has a ``kind``: ``duration_ns`` (integer nanoseconds > 0), ``fraction``
     (exact decimal string in the canonical decimal grammar with ``|value| < 1``; "0.05 means
     5 %; never 5") or ``count`` (integer >= 0);
   * unknown top-level keys and unknown keys of a parameter are refused;
   * every parameter of ``REQUIRED_PARAMETERS`` must be present with its documented kind;
   * the daily levels must satisfy ``halt < defensive < recovery < 0 < profit protection``.
3. ``ResearchParameters.parameters_sha256`` identifies the parameter set: it changes when any
   value or kind changes and is stable otherwise.

The defective and the alternative files are synthetic fixtures written to ``tmp_path``.  Their
numbers are invented for the tests (deliberately different from the specification); they are
not research parameters and not market data.  Every defect test changes exactly one thing in a
file that is otherwise accepted (see the positive controls).
"""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import ConfigError, GieError
from gie.core.params import (
    DEFAULT_PATH,
    EXPECTED_STATUS,
    KIND_COUNT,
    KIND_DURATION_NS,
    KIND_FRACTION,
    KINDS,
    REQUIRED_PARAMETERS,
    ResearchParameter,
    ResearchParameters,
    load_research_parameters,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PARAMETER_FILE = REPO_ROOT / "specifications" / "research_parameters.toml"
MASTER_TEXT = REPO_ROOT / "specifications" / "extracted" / "MASTER.txt"
REQUIREMENTS_MATRIX = REPO_ROOT / "specifications" / "requirements_matrix.yaml"

NS_PER_MS = 1_000_000
INT64_MAX = 2**63 - 1

#: name -> (exact expected value, kind, requirement id, MASTER section, sentence of the specification)
EXPECTED: dict[str, tuple[int | Decimal, str, str, str, str]] = {
    "tick_processing_target_ns": (1_000_000, "duration_ns", "G03-07", "G03", "tick processing <1 ms"),
    "protection_decision_target_ns": (
        5_000_000,
        "duration_ns",
        "G03-08",
        "G03",
        "protection decision <5 ms",
    ),
    "tactical_decision_target_ns": (20_000_000, "duration_ns", "G03-09", "G03", "tactical <20 ms"),
    "daily_defensive_threshold_frac": (Decimal("-0.02"), "fraction", "G05-02", "G05", "defensive at -2%"),
    "daily_recovery_threshold_frac": (
        Decimal("-0.0175"),
        "fraction",
        "G05-03",
        "G05",
        "proposed recovery at -1.75%",
    ),
    "daily_emergency_halt_threshold_frac": (
        Decimal("-0.03"),
        "fraction",
        "G05-04",
        "G05",
        "emergency halt at -3%",
    ),
    "daily_profit_protection_threshold_frac": (
        Decimal("0.05"),
        "fraction",
        "G05-06",
        "G05",
        "Above +5% protect",
    ),
    "target_min_trades_per_eligible_day": (
        1,
        "count",
        "G01-07",
        "G01",
        "at least one valid trade per eligible day",
    ),
    "target_average_daily_net_roi_frac": (
        Decimal("0.05"),
        "fraction",
        "G01-08",
        "G01",
        "at least 5% average daily net ROI",
    ),
}

DAILY_LEVEL_RULE = "halt < defensive < recovery < 0 < profit protection"


@pytest.fixture(scope="module")
def params() -> ResearchParameters:
    return load_research_parameters()


def _master_section(section_id: str) -> str:
    """Text of one ``Gnn | Title`` section of the master specification, whitespace-normalised."""
    text = MASTER_TEXT.read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*{section_id} \| [^\n]*\n(.*?)(?=^\s*G[0-9]{{2}} \| |^\s*Implementation directive)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"section {section_id} not found in {MASTER_TEXT}"
    return " ".join(match.group(1).split())


def _percent_as_fraction(percent_text: str) -> Decimal:
    """``"-1.75"`` (percent) -> ``Decimal("-0.0175")``, exactly."""
    return Decimal(percent_text).scaleb(-2)


# =========================================================================== the real file
@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_default_path_is_the_specification_file() -> None:
    assert DEFAULT_PATH == PARAMETER_FILE
    assert DEFAULT_PATH.is_file()


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_real_file_is_labelled_as_proposed_research_parameters(params: ResearchParameters) -> None:
    assert EXPECTED_STATUS == "PROPOSED_RESEARCH_PARAMETERS"
    assert params.approval_status == "PROPOSED_RESEARCH_PARAMETERS"


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_real_file_defines_exactly_the_parameters_checked_here(params: ResearchParameters) -> None:
    """A parameter added to the file must be added here with its source, or this test fails."""
    assert params.names() == tuple(sorted(EXPECTED))
    assert isinstance(params.names(), tuple)


@pytest.mark.requirement("M-DIR-08", "G05-05")
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_value_equals_the_number_in_the_master_specification(
    params: ResearchParameters, name: str
) -> None:
    expected, kind, requirement, section, sentence = EXPECTED[name]
    value = params.value(name)
    assert type(value) is type(expected)  # int stays int (never bool), fractions are Decimal
    assert value == expected
    assert sentence in _master_section(section)

    parameter = params.get(name)
    assert isinstance(parameter, ResearchParameter)
    assert parameter.name == name
    assert parameter.kind == kind
    assert parameter.value == expected
    assert parameter.requirement == requirement
    assert parameter.unit.strip()
    assert parameter.description.strip()


@pytest.mark.requirement("M-DIR-08")
def test_g03_latency_targets_are_derived_from_the_specification_text(params: ResearchParameters) -> None:
    section = _master_section("G03")
    tick = re.search(r"tick processing <(\d+) ms", section)
    protection = re.search(r"protection decision <(\d+) ms", section)
    tactical = re.search(r"tactical <(\d+) ms", section)
    assert tick is not None
    assert protection is not None
    assert tactical is not None
    assert (tick.group(1), protection.group(1), tactical.group(1)) == ("1", "5", "20")
    assert params.integer("tick_processing_target_ns") == int(tick.group(1)) * NS_PER_MS == 1_000_000
    assert (
        params.integer("protection_decision_target_ns") == int(protection.group(1)) * NS_PER_MS == 5_000_000
    )
    assert params.integer("tactical_decision_target_ns") == int(tactical.group(1)) * NS_PER_MS == 20_000_000
    for name in ("tick_processing_target_ns", "protection_decision_target_ns", "tactical_decision_target_ns"):
        assert params.get(name).unit == "ns"
        assert params.get(name).kind == "duration_ns"


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_g05_daily_thresholds_are_derived_from_the_specification_text(params: ResearchParameters) -> None:
    section = _master_section("G05")
    defensive = re.search(r"defensive at (-\d+(?:\.\d+)?)%", section)
    recovery = re.search(r"recovery at (-\d+(?:\.\d+)?)%", section)
    halt = re.search(r"emergency halt at (-\d+(?:\.\d+)?)%", section)
    protect = re.search(r"Above \+(\d+(?:\.\d+)?)% protect", section)
    assert defensive is not None
    assert recovery is not None
    assert halt is not None
    assert protect is not None
    assert (defensive.group(1), recovery.group(1), halt.group(1), protect.group(1)) == (
        "-2",
        "-1.75",
        "-3",
        "5",
    )

    assert params.decimal("daily_defensive_threshold_frac") == _percent_as_fraction(defensive.group(1))
    assert params.decimal("daily_recovery_threshold_frac") == _percent_as_fraction(recovery.group(1))
    assert params.decimal("daily_emergency_halt_threshold_frac") == _percent_as_fraction(halt.group(1))
    assert params.decimal("daily_profit_protection_threshold_frac") == _percent_as_fraction(protect.group(1))

    assert params.decimal("daily_defensive_threshold_frac") == Decimal("-0.02")
    assert params.decimal("daily_recovery_threshold_frac") == Decimal("-0.0175")
    assert params.decimal("daily_emergency_halt_threshold_frac") == Decimal("-0.03")
    assert params.decimal("daily_profit_protection_threshold_frac") == Decimal("0.05")


@pytest.mark.requirement("M-DIR-08")
def test_g01_research_goals_are_derived_from_the_specification_text(params: ResearchParameters) -> None:
    section = _master_section("G01")
    assert "at least one valid trade per eligible day" in section
    roi = re.search(r"at least (\d+(?:\.\d+)?)% average daily net ROI", section)
    assert roi is not None
    assert roi.group(1) == "5"
    assert params.integer("target_min_trades_per_eligible_day") == 1
    assert params.decimal("target_average_daily_net_roi_frac") == _percent_as_fraction(roi.group(1))
    assert params.decimal("target_average_daily_net_roi_frac") == Decimal("0.05")
    assert "research goals, not established performance" in section


@pytest.mark.requirement("G05-05")
def test_daily_thresholds_are_ordered_as_the_specification_describes(params: ResearchParameters) -> None:
    """G05: halt (-3 %) < defensive (-2 %) < recovery (-1.75 %) < 0 < profit protection (+5 %)."""
    halt = params.decimal("daily_emergency_halt_threshold_frac")
    defensive = params.decimal("daily_defensive_threshold_frac")
    recovery = params.decimal("daily_recovery_threshold_frac")
    protect = params.decimal("daily_profit_protection_threshold_frac")
    assert halt < defensive < recovery < 0 < protect
    # exact decimal arithmetic: the recovery level is a quarter of a percent above the defensive one
    assert recovery - defensive == Decimal("0.0025")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
def test_units_follow_the_naming_rules(params: ResearchParameters) -> None:
    """``*_ns`` are integer nanoseconds; ``*_frac`` are exact decimal fractions, never percent."""
    for name in params.names():
        value = params.value(name)
        assert not isinstance(value, (bool, float))
        if name.endswith("_ns"):
            assert type(value) is int
            assert value > 0
            assert params.get(name).unit == "ns"
            assert params.get(name).kind == "duration_ns"
        if name.endswith("_frac"):
            assert type(value) is Decimal
            assert value.is_finite()
            assert -1 < value < 1
            assert "fraction" in params.get(name).unit
            assert params.get(name).kind == "fraction"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
def test_the_value_of_every_real_parameter_fits_its_kind(params: ResearchParameters) -> None:
    """The documented meaning of the three kinds, checked on every parameter of the real file."""
    seen: set[str] = set()
    for name in params.names():
        parameter = params.get(name)
        seen.add(parameter.kind)
        if parameter.kind == "duration_ns":
            assert type(parameter.value) is int
            assert 0 < parameter.value <= INT64_MAX
        elif parameter.kind == "fraction":
            assert type(parameter.value) is Decimal
            assert abs(parameter.value) < 1
        else:
            assert parameter.kind == "count"
            assert type(parameter.value) is int
            assert 0 <= parameter.value <= INT64_MAX
    assert seen == {"duration_ns", "fraction", "count"}


@pytest.mark.requirement("M-DIR-08")
def test_every_parameter_cites_an_existing_requirement_of_its_section(params: ResearchParameters) -> None:
    matrix = yaml.safe_load(REQUIREMENTS_MATRIX.read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in matrix["requirements"]}
    for name in params.names():
        parameter = params.get(name)
        _, _, requirement, section, _ = EXPECTED[name]
        assert parameter.requirement == requirement
        assert requirement in by_id, f"{name}: unknown requirement {requirement}"
        assert by_id[requirement]["section"].startswith(section + " | ")
        assert by_id[requirement]["source"] == "MASTER.pdf"


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_loading_is_repeatable(params: ResearchParameters) -> None:
    again = load_research_parameters()
    explicit = load_research_parameters(PARAMETER_FILE)
    for other in (again, explicit):
        assert other.names() == params.names()
        assert other.approval_status == params.approval_status
        assert other.parameters_sha256 == params.parameters_sha256
        for name in params.names():
            assert other.get(name) == params.get(name)


# =========================================================================== kinds and required parameters
@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
def test_the_three_documented_kinds() -> None:
    assert (KIND_DURATION_NS, KIND_FRACTION, KIND_COUNT) == ("duration_ns", "fraction", "count")
    assert isinstance(KINDS, tuple)
    assert len(KINDS) == 3
    assert set(KINDS) == {"duration_ns", "fraction", "count"}


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_required_parameters_are_the_nine_numbers_of_the_master_specification() -> None:
    """Every number named by G01, G03 and G05 is required, with the kind its unit implies.

    "Removing one is a breaking change": a parameter dropped from ``REQUIRED_PARAMETERS`` (or a
    changed kind) must fail here.
    """
    assert {name: kind for name, (_, kind, _, _, _) in EXPECTED.items()} == REQUIRED_PARAMETERS
    assert len(REQUIRED_PARAMETERS) == 9
    assert set(REQUIRED_PARAMETERS.values()) <= set(KINDS)
    for name in (
        "daily_emergency_halt_threshold_frac",
        "daily_defensive_threshold_frac",
        "daily_recovery_threshold_frac",
        "daily_profit_protection_threshold_frac",
    ):
        assert REQUIRED_PARAMETERS[name] == "fraction"


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_the_real_file_satisfies_the_required_parameters(params: ResearchParameters) -> None:
    for name, kind in REQUIRED_PARAMETERS.items():
        assert name in params.names()
        assert params.get(name).kind == kind


@pytest.mark.requirement("M-DIR-08")
def test_research_parameter_fields() -> None:
    assert [field.name for field in dataclasses.fields(ResearchParameter)] == [
        "name",
        "kind",
        "value",
        "unit",
        "requirement",
        "description",
    ]


# =========================================================================== accessors
@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_typed_accessors(params: ResearchParameters) -> None:
    assert params.integer("tick_processing_target_ns") == 1_000_000
    assert type(params.integer("tick_processing_target_ns")) is int
    assert params.decimal("daily_defensive_threshold_frac") == Decimal("-0.02")
    assert type(params.decimal("daily_defensive_threshold_frac")) is Decimal
    assert params.value("tick_processing_target_ns") == 1_000_000
    assert params.value("daily_defensive_threshold_frac") == Decimal("-0.02")
    for name in params.names():
        if params.get(name).kind == "fraction":
            assert params.decimal(name) == params.value(name)
        else:
            assert params.integer(name) == params.value(name)


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_typed_accessors_refuse_the_other_type(params: ResearchParameters) -> None:
    with pytest.raises(ConfigError) as as_integer:
        params.integer("daily_defensive_threshold_frac")
    assert as_integer.value.code == "GIE.CONFIG.INVALID"
    assert "daily_defensive_threshold_frac" in str(as_integer.value)
    with pytest.raises(ConfigError) as as_decimal:
        params.decimal("tick_processing_target_ns")
    assert as_decimal.value.code == "GIE.CONFIG.INVALID"
    assert "tick_processing_target_ns" in str(as_decimal.value)
    with pytest.raises(ConfigError):
        params.decimal("target_min_trades_per_eligible_day")  # a count is not a fraction


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "name",
    [
        "",
        "unknown",
        "tick_processing_target",
        "TICK_PROCESSING_TARGET_NS",
        " tick_processing_target_ns",
        "value",
        "kind",
    ],
)
def test_unknown_parameter_name_is_a_config_error(params: ResearchParameters, name: str) -> None:
    for accessor in (params.get, params.value, params.integer, params.decimal):
        with pytest.raises(ConfigError) as excinfo:
            accessor(name)
        assert excinfo.value.code == "GIE.CONFIG.INVALID"
        assert isinstance(excinfo.value, GieError)
        assert not isinstance(excinfo.value, KeyError)
        assert excinfo.value.details["name"] == name


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_cannot_be_modified_through_the_view(params: ResearchParameters) -> None:
    parameter = params.get("daily_emergency_halt_threshold_frac")
    with pytest.raises(dataclasses.FrozenInstanceError):
        parameter.value = Decimal("-0.5")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parameter.unit = "percent"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parameter.kind = "count"  # type: ignore[misc]
    assert params.decimal("daily_emergency_halt_threshold_frac") == Decimal("-0.03")

    source = {
        "synthetic": ResearchParameter("synthetic", "duration_ns", 1, "ns", "G03-07", "synthetic fixture")
    }
    view = ResearchParameters(source, EXPECTED_STATUS)
    hash_before = view.parameters_sha256
    source["added_later"] = ResearchParameter(
        "added_later", "duration_ns", 2, "ns", "G03-07", "synthetic fixture"
    )
    del source["synthetic"]
    assert view.names() == ("synthetic",)
    assert view.integer("synthetic") == 1
    assert view.parameters_sha256 == hash_before


# =========================================================================== synthetic files
#: Synthetic fixture, invented numbers: (name, kind, TOML literal of the value, unit, requirement).
#: The nine required parameters plus one further parameter of every kind, so that kinds and
#: ranges can be exercised without touching the rules for the required ones.
_SYNTHETIC_PARAMETERS: tuple[tuple[str, str, str, str, str], ...] = (
    ("tick_processing_target_ns", "duration_ns", "250", "ns", "G03-07"),
    ("protection_decision_target_ns", "duration_ns", "750", "ns", "G03-08"),
    ("tactical_decision_target_ns", "duration_ns", "1_500", "ns", "G03-09"),
    ("daily_defensive_threshold_frac", "fraction", '"-0.3"', "fraction of equity", "G05-02"),
    ("daily_recovery_threshold_frac", "fraction", '"-0.2"', "fraction of equity", "G05-03"),
    ("daily_emergency_halt_threshold_frac", "fraction", '"-0.4"', "fraction of equity", "G05-04"),
    ("daily_profit_protection_threshold_frac", "fraction", '"0.1"', "fraction of equity", "G05-06"),
    ("target_min_trades_per_eligible_day", "count", "7", "trades", "G01-07"),
    ("target_average_daily_net_roi_frac", "fraction", '"0.125"', "fraction per day", "G01-08"),
    ("synthetic_extra_ns", "duration_ns", "40", "ns", "G03-07"),
    ("synthetic_extra_frac", "fraction", '"0.25"', "fraction", "G05-02"),
    ("synthetic_extra_count", "count", "3", "items", "G01-07"),
)
_SYNTHETIC_NAMES = tuple(row[0] for row in _SYNTHETIC_PARAMETERS)
_SYNTHETIC_VALUES: dict[str, int | Decimal] = {
    "tick_processing_target_ns": 250,
    "protection_decision_target_ns": 750,
    "tactical_decision_target_ns": 1500,
    "daily_defensive_threshold_frac": Decimal("-0.3"),
    "daily_recovery_threshold_frac": Decimal("-0.2"),
    "daily_emergency_halt_threshold_frac": Decimal("-0.4"),
    "daily_profit_protection_threshold_frac": Decimal("0.1"),
    "target_min_trades_per_eligible_day": 7,
    "target_average_daily_net_roi_frac": Decimal("0.125"),
    "synthetic_extra_ns": 40,
    "synthetic_extra_frac": Decimal("0.25"),
    "synthetic_extra_count": 3,
}

_HEADER = (
    "# synthetic fixture - not the real research parameters\n"
    "schema_version = 1\n"
    'approval_status = "PROPOSED_RESEARCH_PARAMETERS"\n'
)


def _table(name: str, kind: str, value: str, unit: str, requirement: str) -> str:
    return (
        f"[parameters.{name}]\n"
        f'kind = "{kind}"\n'
        f"value = {value}\n"
        f'unit = "{unit}"\n'
        f'requirement = "{requirement}"\n'
        f'description = "synthetic fixture: {name}"\n'
    )


_TABLES: dict[str, str] = {row[0]: _table(*row) for row in _SYNTHETIC_PARAMETERS}

#: A valid synthetic file.  Each defect test changes exactly one thing in it.
VALID_SYNTHETIC_FILE = _HEADER + "".join("\n" + table for table in _TABLES.values())


def _write(tmp_path: Path, content: str | bytes, name: str = "research_parameters.toml") -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _mutated(old: str, new: str) -> str:
    """The valid synthetic file with exactly one occurrence of *old* replaced by *new*."""
    assert VALID_SYNTHETIC_FILE.count(old) == 1, f"mutation target {old!r} must occur exactly once"
    mutated = VALID_SYNTHETIC_FILE.replace(old, new)
    assert mutated != VALID_SYNTHETIC_FILE
    return mutated


def _edited(name: str, key: str, replacement: str | None) -> str:
    """The valid synthetic file with the *key* line of one parameter replaced (``None``: removed)."""
    lines = _TABLES[name].splitlines()
    positions = [index for index, line in enumerate(lines) if line.startswith(f"{key} = ")]
    assert len(positions) == 1, (name, key)
    if replacement is None:
        del lines[positions[0]]
    else:
        lines[positions[0]] = replacement
    return _mutated(_TABLES[name], "\n".join(lines) + "\n")


def _with_value(name: str, toml_literal: str) -> str:
    return _edited(name, "value", f"value = {toml_literal}")


def _without_table(name: str) -> str:
    return _mutated("\n" + _TABLES[name], "")


def _assert_refused(path: Path, reason: str | None = None) -> ConfigError:
    """The file is refused with ``ConfigError``; *reason* is a fragment of the violated rule."""
    with pytest.raises(ConfigError) as excinfo:
        load_research_parameters(path)
    error = excinfo.value
    assert error.code == "GIE.CONFIG.INVALID"
    assert isinstance(error, GieError)
    if reason is not None:
        assert reason in error.message, error.message
    return error


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_valid_synthetic_file_loads(tmp_path: Path) -> None:
    """Positive control: without a defect the synthetic file is accepted, value by value."""
    loaded = load_research_parameters(_write(tmp_path, VALID_SYNTHETIC_FILE))
    assert loaded.names() == tuple(sorted(_SYNTHETIC_NAMES))
    assert loaded.approval_status == "PROPOSED_RESEARCH_PARAMETERS"
    for name, kind, _, unit, requirement in _SYNTHETIC_PARAMETERS:
        expected = _SYNTHETIC_VALUES[name]
        assert loaded.value(name) == expected
        assert type(loaded.value(name)) is type(expected)
        assert loaded.get(name) == ResearchParameter(
            name=name,
            kind=kind,
            value=expected,
            unit=unit,
            requirement=requirement,
            description=f"synthetic fixture: {name}",
        )
    assert loaded.integer("synthetic_extra_ns") == 40
    assert loaded.integer("synthetic_extra_count") == 3
    assert loaded.decimal("synthetic_extra_frac") == Decimal("0.25")
    assert set(REQUIRED_PARAMETERS) < set(loaded.names())


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_a_file_with_only_the_required_parameters_loads(tmp_path: Path) -> None:
    content = _HEADER + "".join("\n" + _TABLES[name] for name in REQUIRED_PARAMETERS)
    loaded = load_research_parameters(_write(tmp_path, content))
    assert loaded.names() == tuple(sorted(REQUIRED_PARAMETERS))


# --------------------------------------------------------------------------- kind: duration_ns
@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
@pytest.mark.parametrize(
    ("toml_value", "expected"),
    [("1", 1), ("41", 41), ("1_000_000", 1_000_000), ("9223372036854775807", INT64_MAX)],
    ids=["smallest", "plain", "underscores", "int64-max"],
)
def test_a_duration_is_an_integer_of_nanoseconds_above_zero(
    tmp_path: Path, toml_value: str, expected: int
) -> None:
    loaded = load_research_parameters(_write(tmp_path, _with_value("synthetic_extra_ns", toml_value)))
    assert loaded.get("synthetic_extra_ns").kind == "duration_ns"
    assert loaded.integer("synthetic_extra_ns") == expected
    assert type(loaded.value("synthetic_extra_ns")) is int


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "toml_value",
    ["0", "-1", "-40", "9223372036854775808", "18446744073709551616", "-9223372036854775808"],
    ids=["zero", "minus-one", "negative", "int64-max+1", "2**64", "int64-min"],
)
def test_a_duration_out_of_range_is_refused(tmp_path: Path, toml_value: str) -> None:
    """A zero or negative duration would switch a limit off or invert it; nothing is clamped."""
    error = _assert_refused(_write(tmp_path, _with_value("synthetic_extra_ns", toml_value)), "range")
    assert error.details["name"] == "synthetic_extra_ns"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "toml_value",
    ["0", "-1", "9223372036854775808"],
    ids=["zero", "minus-one", "int64-max+1"],
)
def test_a_required_duration_out_of_range_is_refused(tmp_path: Path, toml_value: str) -> None:
    error = _assert_refused(_write(tmp_path, _with_value("tick_processing_target_ns", toml_value)), "range")
    assert error.details["name"] == "tick_processing_target_ns"


# --------------------------------------------------------------------------- kind: count
@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
@pytest.mark.parametrize(
    ("toml_value", "expected"),
    [("0", 0), ("1", 1), ("4", 4), ("9223372036854775807", INT64_MAX)],
    ids=["zero", "one", "plain", "int64-max"],
)
def test_a_count_is_a_non_negative_integer(tmp_path: Path, toml_value: str, expected: int) -> None:
    loaded = load_research_parameters(_write(tmp_path, _with_value("synthetic_extra_count", toml_value)))
    assert loaded.get("synthetic_extra_count").kind == "count"
    assert loaded.integer("synthetic_extra_count") == expected
    assert type(loaded.value("synthetic_extra_count")) is int


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "toml_value",
    ["-1", "-3", "9223372036854775808", "-9223372036854775808"],
    ids=["minus-one", "negative", "int64-max+1", "int64-min"],
)
def test_a_count_out_of_range_is_refused(tmp_path: Path, toml_value: str) -> None:
    error = _assert_refused(_write(tmp_path, _with_value("synthetic_extra_count", toml_value)), "range")
    assert error.details["name"] == "synthetic_extra_count"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("name", ["synthetic_extra_ns", "synthetic_extra_count"])
@pytest.mark.parametrize(
    "toml_value",
    [
        "40.0",
        "0.5",
        "1e3",
        "inf",
        "nan",
        "true",
        "false",
        '"40"',
        '"0.25"',
        '""',
        "[40]",
        "{ amount = 40 }",
        "2026-11-27",
        "09:30:00",
        "2026-11-27T09:30:00Z",
    ],
)
def test_an_integer_kind_refuses_everything_that_is_not_an_integer(
    tmp_path: Path, name: str, toml_value: str
) -> None:
    """Nothing is coerced: ``40.0``, ``true`` and ``"40"`` are not the integer 40."""
    error = _assert_refused(_write(tmp_path, _with_value(name, toml_value)), "integer")
    assert error.details["name"] == name
    assert name in str(error)  # the error names the offending parameter


# --------------------------------------------------------------------------- kind: fraction
@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
@pytest.mark.parametrize(
    "text",
    [
        "0",
        "0.75",
        "-0.25",
        "0.05",
        "-0.0175",
        "0.999999999999",
        "-0.999999999999",
        "0.000000000001",
        "0.250",
        "0.0",
        "-0",
    ],
)
def test_a_fraction_is_an_exact_decimal_string_strictly_between_minus_one_and_one(
    tmp_path: Path, text: str
) -> None:
    loaded = load_research_parameters(_write(tmp_path, _with_value("synthetic_extra_frac", f'"{text}"')))
    value = loaded.decimal("synthetic_extra_frac")
    assert loaded.get("synthetic_extra_frac").kind == "fraction"
    assert type(value) is Decimal
    assert value == Decimal(text)  # exact: a decimal string never passes through a binary float
    assert -1 < value < 1
    assert loaded.value("synthetic_extra_frac") == value


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    ["1", "-1", "1.0", "-1.000", "1.000000000001", "5", "-3", "2.5", "100", "-175", "123456789012345678.5"],
)
def test_a_fraction_of_magnitude_one_or_more_is_refused(tmp_path: Path, text: str) -> None:
    """``0.05`` means 5 %; a percentage typed as ``5`` is an error, not a limit of 500 %."""
    error = _assert_refused(
        _write(tmp_path, _with_value("synthetic_extra_frac", f'"{text}"')), "between -1 and 1"
    )
    assert error.details["name"] == "synthetic_extra_frac"


@pytest.mark.requirement("M-DIR-08", "G05-05", "S00-ASG-24")
@pytest.mark.parametrize(
    ("name", "percent_text"),
    [
        ("daily_emergency_halt_threshold_frac", "-3"),
        ("daily_emergency_halt_threshold_frac", "-40"),
        ("daily_defensive_threshold_frac", "-2"),
        ("daily_recovery_threshold_frac", "-1.75"),
        ("daily_profit_protection_threshold_frac", "5"),
        ("daily_profit_protection_threshold_frac", "1"),
        ("target_average_daily_net_roi_frac", "5"),
    ],
)
def test_a_percentage_typed_for_a_required_fraction_is_refused(
    tmp_path: Path, name: str, percent_text: str
) -> None:
    """Module docstring: ``-3`` for ``-0.03`` is an error, not a silently disabled limit.

    With ``-3`` the halt level would still be "below the defensive level", so only the range of the
    kind can catch it - a halt at -300 % would never trigger.
    """
    error = _assert_refused(_write(tmp_path, _with_value(name, f'"{percent_text}"')), "between -1 and 1")
    assert error.details["name"] == name


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "toml_value",
    ["0.25", "-0.02", "0", "1", "0.0", "1e-2", "inf", "nan", "true", '["0.25"]', '{ value = "0.25" }'],
)
def test_a_fraction_that_is_not_a_string_is_refused(tmp_path: Path, toml_value: str) -> None:
    """A TOML float is a binary float and not exact; an integer is not a fraction either."""
    error = _assert_refused(
        _write(tmp_path, _with_value("synthetic_extra_frac", toml_value)), "decimal string"
    )
    assert error.details["name"] == "synthetic_extra_frac"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "abc",
        "+0.25",
        ".25",
        "0.",
        "-.25",
        "-",
        "00.25",
        "01",
        "2.5e-1",
        "25E-2",
        " 0.25",
        "0.25 ",
        "0.25\\n",
        "0,25",
        "0.25.1",
        "25%",
        "0.25%",
        "1/4",
        "0x1",
        "1_0",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "0.0000000000001",  # 13 fractional digits: beyond the canonical decimal grammar
        "\N{FULLWIDTH DIGIT ZERO}.\N{FULLWIDTH DIGIT TWO}\N{FULLWIDTH DIGIT FIVE}",
        "\N{MINUS SIGN}0.25",  # U+2212 instead of the ASCII hyphen-minus
    ],
)
def test_a_fraction_outside_the_canonical_decimal_grammar_is_refused(tmp_path: Path, text: str) -> None:
    error = _assert_refused(
        _write(tmp_path, _with_value("synthetic_extra_frac", f'"{text}"')), "decimal string"
    )
    assert error.details["name"] == "synthetic_extra_frac"


# --------------------------------------------------------------------------- kind itself
@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "replacement",
    [
        'kind = "percent"',
        'kind = "duration"',
        'kind = "duration_ms"',
        'kind = "COUNT"',
        'kind = "Count"',
        'kind = "count "',
        'kind = " count"',
        'kind = "int"',
        'kind = ""',
        "kind = 3",
        "kind = true",
        'kind = ["count"]',
        'kind = { name = "count" }',
    ],
)
def test_an_unknown_kind_is_refused(tmp_path: Path, replacement: str) -> None:
    """Unknown vocabulary is refused: nothing is guessed from the value or the name."""
    error = _assert_refused(_write(tmp_path, _edited("synthetic_extra_count", "kind", replacement)), "kind")
    assert error.details["name"] == "synthetic_extra_count"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize("name", _SYNTHETIC_NAMES)
def test_a_parameter_without_a_kind_is_refused(tmp_path: Path, name: str) -> None:
    error = _assert_refused(_write(tmp_path, _edited(name, "kind", None)))
    assert error.details["name"] == name
    assert "kind" in str(error)  # the error names the missing key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("synthetic_extra_frac", "duration_ns"),  # "0.25" is not an integer
        ("synthetic_extra_frac", "count"),
        ("synthetic_extra_ns", "fraction"),  # 40 is not a decimal string
        ("synthetic_extra_count", "fraction"),
    ],
)
def test_the_value_must_fit_the_declared_kind(tmp_path: Path, name: str, kind: str) -> None:
    error = _assert_refused(_write(tmp_path, _edited(name, "kind", f'kind = "{kind}"')))
    assert error.details["name"] == name


# --------------------------------------------------------------------------- required parameters
@pytest.mark.requirement("M-DIR-08", "G05-05", "S00-ASG-24")
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_missing_required_parameter_is_refused(tmp_path: Path, name: str) -> None:
    error = _assert_refused(_write(tmp_path, _without_table(name)), "required")
    assert error.details["name"] == name


@pytest.mark.requirement("M-DIR-08", "G05-05", "S00-ASG-24")
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_misspelt_required_parameter_is_refused(tmp_path: Path, name: str) -> None:
    """A renamed table is a new, unrequired parameter; the required one is then missing."""
    for misspelt in (name + "s", name.upper(), name.replace("_", "-", 1)):
        content = _mutated(f"[parameters.{name}]", f"[parameters.{misspelt}]")
        error = _assert_refused(_write(tmp_path, content), "required")
        assert error.details["name"] == name


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize("name", ["synthetic_extra_ns", "synthetic_extra_frac", "synthetic_extra_count"])
def test_a_parameter_that_is_not_required_may_be_absent(tmp_path: Path, name: str) -> None:
    """Positive control for the two tests above: only required parameters are required."""
    loaded = load_research_parameters(_write(tmp_path, _without_table(name)))
    assert name not in loaded.names()
    assert len(loaded.names()) == len(_SYNTHETIC_NAMES) - 1


@pytest.mark.requirement("M-DIR-08", "G05-05", "S00-ASG-24")
@pytest.mark.parametrize(
    ("name", "kind", "toml_value"),
    [
        # the value is valid for the wrong kind, so only the kind rule can refuse the file
        ("tick_processing_target_ns", "count", "250"),
        ("protection_decision_target_ns", "count", "750"),
        ("tactical_decision_target_ns", "count", "1_500"),
        ("tick_processing_target_ns", "fraction", '"0.25"'),
        ("target_min_trades_per_eligible_day", "duration_ns", "7"),
        ("target_min_trades_per_eligible_day", "fraction", '"0.7"'),
        ("target_average_daily_net_roi_frac", "count", "0"),
        ("target_average_daily_net_roi_frac", "duration_ns", "5"),
        ("daily_emergency_halt_threshold_frac", "count", "0"),
        ("daily_defensive_threshold_frac", "duration_ns", "2"),
        ("daily_recovery_threshold_frac", "count", "0"),
        ("daily_profit_protection_threshold_frac", "count", "5"),
        ("daily_profit_protection_threshold_frac", "duration_ns", "5"),
    ],
)
def test_a_required_parameter_of_the_wrong_kind_is_refused(
    tmp_path: Path, name: str, kind: str, toml_value: str
) -> None:
    lines = _TABLES[name].splitlines()
    lines[1] = f'kind = "{kind}"'
    lines[2] = f"value = {toml_value}"
    content = _mutated(_TABLES[name], "\n".join(lines) + "\n")
    error = _assert_refused(_write(tmp_path, content), "kind")
    assert error.details["name"] == name
    assert REQUIRED_PARAMETERS[name] in str(error)  # the error names the kind that is required


# --------------------------------------------------------------------------- order of the daily levels
def _with_levels(halt: str, defensive: str, recovery: str, profit: str) -> str:
    """The valid synthetic file with the four daily levels replaced (TOML decimal strings)."""
    content = VALID_SYNTHETIC_FILE
    for name, text in (
        ("daily_emergency_halt_threshold_frac", halt),
        ("daily_defensive_threshold_frac", defensive),
        ("daily_recovery_threshold_frac", recovery),
        ("daily_profit_protection_threshold_frac", profit),
    ):
        old = _TABLES[name]
        assert content.count(old) == 1
        lines = old.splitlines()
        lines[2] = f'value = "{text}"'
        content = content.replace(old, "\n".join(lines) + "\n")
    return content


@pytest.mark.requirement("G05-05", "M-DIR-08")
@pytest.mark.parametrize(
    ("halt", "defensive", "recovery", "profit"),
    [
        ("-0.4", "-0.3", "-0.2", "0.1"),  # the synthetic baseline
        ("-0.03", "-0.02", "-0.0175", "0.05"),  # the proposed levels of the specification
        ("-0.999999999999", "-0.5", "-0.000000000001", "0.000000000001"),  # as wide as a fraction allows
        ("-0.000000000003", "-0.000000000002", "-0.000000000001", "0.999999999999"),
        ("-0.030", "-0.02", "-0.01750", "0.0500"),  # trailing zeros do not change the order
    ],
    ids=["baseline", "specification", "widest", "tightest", "trailing-zeros"],
)
def test_correctly_ordered_daily_levels_are_accepted(
    tmp_path: Path, halt: str, defensive: str, recovery: str, profit: str
) -> None:
    """G05: the recovery and halt levels are configurable - any correctly ordered set loads."""
    loaded = load_research_parameters(_write(tmp_path, _with_levels(halt, defensive, recovery, profit)))
    assert loaded.decimal("daily_emergency_halt_threshold_frac") == Decimal(halt)
    assert loaded.decimal("daily_defensive_threshold_frac") == Decimal(defensive)
    assert loaded.decimal("daily_recovery_threshold_frac") == Decimal(recovery)
    assert loaded.decimal("daily_profit_protection_threshold_frac") == Decimal(profit)
    assert (
        loaded.decimal("daily_emergency_halt_threshold_frac")
        < loaded.decimal("daily_defensive_threshold_frac")
        < loaded.decimal("daily_recovery_threshold_frac")
        < 0
        < loaded.decimal("daily_profit_protection_threshold_frac")
    )


@pytest.mark.requirement("G05-05", "M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    ("halt", "defensive", "recovery", "profit"),
    [
        ("-0.3", "-0.3", "-0.2", "0.1"),
        ("-0.25", "-0.3", "-0.2", "0.1"),
        ("-0.1", "-0.3", "-0.2", "0.1"),
        ("0.4", "-0.3", "-0.2", "0.1"),
        ("0", "-0.3", "-0.2", "0.1"),
        ("-0.4", "-0.2", "-0.2", "0.1"),
        ("-0.4", "-0.1", "-0.2", "0.1"),
        ("-0.4", "-0.5", "-0.2", "0.1"),
        ("-0.4", "0.3", "-0.2", "0.1"),
        ("-0.4", "-0.3", "0", "0.1"),
        ("-0.4", "-0.3", "-0", "0.1"),
        ("-0.4", "-0.3", "0.0", "0.1"),
        ("-0.4", "-0.3", "0.05", "0.1"),
        ("-0.4", "-0.3", "0.2", "0.1"),
        ("-0.4", "-0.3", "-0.35", "0.1"),
        ("-0.4", "-0.3", "-0.2", "0"),
        ("-0.4", "-0.3", "-0.2", "-0"),
        ("-0.4", "-0.3", "-0.2", "-0.1"),
        ("-0.4", "-0.3", "-0.2", "-0.5"),
        ("0.4", "0.3", "0.2", "0.1"),
        ("-0.2", "-0.3", "-0.4", "0.1"),
        ("0.1", "0.2", "0.3", "0.4"),
        ("0", "0", "0", "0"),
        ("-0.0175", "-0.02", "-0.03", "0.05"),  # the proposed levels with halt and recovery swapped
        ("-0.03", "-0.0175", "-0.02", "0.05"),  # ... with defensive and recovery swapped
        ("-0.02", "-0.03", "-0.0175", "0.05"),  # ... with halt and defensive swapped
        ("-0.03", "-0.02", "-0.0175", "-0.05"),  # ... with the sign of profit protection lost
        ("0.03", "0.02", "0.0175", "0.05"),  # ... with the signs of the loss levels lost
    ],
    ids=[
        "halt=defensive",
        "halt-between-defensive-and-recovery",
        "halt-above-recovery",
        "halt-positive",
        "halt-zero",
        "defensive=recovery",
        "defensive-above-recovery",
        "defensive-below-halt",
        "defensive-positive",
        "recovery-zero",
        "recovery-minus-zero",
        "recovery-0.0",
        "recovery-positive",
        "recovery-above-profit",
        "recovery-below-defensive",
        "profit-zero",
        "profit-minus-zero",
        "profit-negative",
        "profit-below-halt",
        "all-positive-descending",
        "loss-levels-reversed",
        "all-positive-ascending",
        "all-zero",
        "spec-halt-recovery-swapped",
        "spec-defensive-recovery-swapped",
        "spec-halt-defensive-swapped",
        "spec-profit-sign-lost",
        "spec-loss-signs-lost",
    ],
)
def test_daily_levels_in_the_wrong_order_are_refused(
    tmp_path: Path, halt: str, defensive: str, recovery: str, profit: str
) -> None:
    """Every level is a valid fraction on its own; only their order is wrong (boundaries included)."""
    error = _assert_refused(
        _write(tmp_path, _with_levels(halt, defensive, recovery, profit)), DAILY_LEVEL_RULE
    )
    # the refusal reports the four levels as exact text, never as binary floats
    assert error.details["halt"] == halt
    assert error.details["defensive"] == defensive
    assert error.details["recovery"] == recovery
    assert error.details["profit_protection"] == profit
    assert strict_loads(canonical_dumps(error.to_dict())) == error.to_dict()


# --------------------------------------------------------------------------- unknown keys
@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    ("addition", "key"),
    [
        ('owner = "synthetic"\n', "owner"),
        ("approved = true\n", "approved"),
        ('approval_state = "APPROVED"\n', "approval_state"),
        ("schema_versions = 1\n", "schema_versions"),
        ("Schema_Version = 1\n", "Schema_Version"),
        ("limits = { daily = 1 }\n", "limits"),
        ('comment = ""\n', "comment"),
    ],
)
def test_an_unknown_top_level_key_is_refused(tmp_path: Path, addition: str, key: str) -> None:
    content = _mutated("schema_version = 1\n", "schema_version = 1\n" + addition)
    error = _assert_refused(_write(tmp_path, content), "unknown top-level key")
    assert error.details["key"] == key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("[metadata]\nauthor = 'synthetic'\n", "metadata"),
        ("[parameter.synthetic_other]\nkind = 'count'\nvalue = 1\n", "parameter"),
        ("[overrides.tick_processing_target_ns]\nvalue = 1\n", "overrides"),
        ("[[history]]\nschema_version = 0\n", "history"),
    ],
    ids=["metadata", "misspelt-parameters", "overrides", "array-of-tables"],
)
def test_an_unknown_top_level_table_is_refused(tmp_path: Path, table: str, key: str) -> None:
    error = _assert_refused(_write(tmp_path, VALID_SYNTHETIC_FILE + "\n" + table), "unknown top-level key")
    assert error.details["key"] == key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize("name", ["tick_processing_target_ns", "synthetic_extra_frac"])
@pytest.mark.parametrize(
    ("addition", "key"),
    [
        ('source = "synthetic"', "source"),
        ("values = 1", "values"),
        ("default = 3", "default"),
        ('Kind = "count"', "Kind"),
        ('units = "ns"', "units"),
        ("minimum = 0", "minimum"),
        ("approved = true", "approved"),
        ('note = ""', "note"),
    ],
)
def test_an_unknown_key_of_a_parameter_is_refused(tmp_path: Path, name: str, addition: str, key: str) -> None:
    content = _mutated(_TABLES[name], _TABLES[name] + addition + "\n")
    error = _assert_refused(_write(tmp_path, content), "unknown key")
    assert error.details["name"] == name
    assert error.details["key"] == key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_a_nested_table_inside_a_parameter_is_refused(tmp_path: Path) -> None:
    content = VALID_SYNTHETIC_FILE + "\n[parameters.synthetic_extra_count.limits]\nmaximum = 5\n"
    error = _assert_refused(_write(tmp_path, content), "unknown key")
    assert error.details["name"] == "synthetic_extra_count"
    assert error.details["key"] == "limits"


# --------------------------------------------------------------------------- unreadable files
@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_missing_file_is_refused(tmp_path: Path) -> None:
    error = _assert_refused(tmp_path / "does_not_exist.toml", "cannot be read")
    assert "does_not_exist.toml" in str(error)


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_directory_instead_of_file_is_refused(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "cannot be read")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content",
    [
        "schema_version = ",
        "schema_version = 1\napproval_status = PROPOSED_RESEARCH_PARAMETERS\n",
        "[parameters\n",
        "this is not toml",
        '{"schema_version": 1}',
        VALID_SYNTHETIC_FILE + "\n" + _TABLES["synthetic_extra_count"],  # table defined twice
        VALID_SYNTHETIC_FILE.replace('unit = "items"', 'unit = "items'),  # unterminated string
        VALID_SYNTHETIC_FILE.replace("value = 3\n", "value = 3\nvalue = 3\n"),  # duplicate key in a table
        "schema_version = 1\n" + VALID_SYNTHETIC_FILE,  # duplicate top-level key
        VALID_SYNTHETIC_FILE.replace("value = 3\n", "value = 03\n"),  # leading zero is not a TOML integer
        VALID_SYNTHETIC_FILE.replace("value = 3\n", "value = 3 items\n"),
    ],
    ids=[
        "no-value",
        "bare-string",
        "open-table",
        "prose",
        "json",
        "duplicate-table",
        "open-string",
        "duplicate-key-in-table",
        "duplicate-top-level-key",
        "leading-zero",
        "trailing-garbage",
    ],
)
def test_invalid_toml_is_refused(tmp_path: Path, content: str) -> None:
    assert content != VALID_SYNTHETIC_FILE
    _assert_refused(_write(tmp_path, content), "not valid UTF-8 TOML")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_file_that_is_not_utf8_is_refused_with_a_config_error(tmp_path: Path) -> None:
    """TOML is UTF-8; any other bytes are a defective file and must fail closed as ConfigError."""
    content = VALID_SYNTHETIC_FILE.encode("utf-8").replace(
        b"synthetic fixture: synthetic_extra_count", b"synthetic fixture: \xff\xfe"
    )
    assert content != VALID_SYNTHETIC_FILE.encode("utf-8")
    _assert_refused(_write(tmp_path, content), "not valid UTF-8 TOML")
    _assert_refused(_write(tmp_path, VALID_SYNTHETIC_FILE.encode("utf-16")), "not valid UTF-8 TOML")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize("name", ["synthetic_extra_ns", "synthetic_extra_count"])
def test_an_integer_too_long_to_convert_is_refused_with_a_config_error(tmp_path: Path, name: str) -> None:
    """Fail closed with the documented error: a defective file is a ``ConfigError``, whatever the defect.

    A 5000-digit integer is "a value ... out of range" (or, for the TOML reader, not a number it
    can convert).  Either way the caller that handles ``ConfigError``/``GieError`` must see it.
    """
    _assert_refused(_write(tmp_path, _with_value(name, "1" + "0" * 5000)))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content", ["", "\n", "# only a comment\n", "   \n\t\n"], ids=["empty", "newline", "comment", "blank"]
)
def test_empty_file_is_refused(tmp_path: Path, content: str) -> None:
    _assert_refused(_write(tmp_path, content))


# --------------------------------------------------------------------------- envelope
@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "replacement",
    [
        "schema_version = 2",
        "schema_version = 0",
        "schema_version = -1",
        "schema_version = 11",
        'schema_version = "1"',
        'schema_version = "v1"',
        "schema_version = [1]",
        "schema_version = true",
        "schema_version = 1.0",
        "",  # key removed
    ],
    ids=["2", "0", "-1", "11", "string-1", "string-v1", "list", "bool", "float", "missing"],
)
def test_wrong_schema_version_is_refused(tmp_path: Path, replacement: str) -> None:
    """The schema version is the integer 1; ``true`` and ``1.0`` are different TOML values."""
    _assert_refused(_write(tmp_path, _mutated("schema_version = 1", replacement)), "schema_version")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "replacement",
    [
        'approval_status = "APPROVED"',
        'approval_status = "APPROVED_PRODUCTION_LIMITS"',
        'approval_status = "proposed_research_parameters"',
        'approval_status = "PROPOSED_RESEARCH_PARAMETERS "',
        'approval_status = " PROPOSED_RESEARCH_PARAMETERS"',
        'approval_status = "PROPOSED"',
        'approval_status = ""',
        "approval_status = 1",
        "approval_status = true",
        'approval_status = ["PROPOSED_RESEARCH_PARAMETERS"]',
        "",  # key removed
    ],
    ids=[
        "approved",
        "approved-production",
        "lower-case",
        "trailing-space",
        "leading-space",
        "truncated",
        "empty",
        "int",
        "bool",
        "list",
        "missing",
    ],
)
def test_any_other_approval_status_is_refused(tmp_path: Path, replacement: str) -> None:
    """In this project phase the numbers are proposed research parameters and nothing else."""
    _assert_refused(
        _write(tmp_path, _mutated('approval_status = "PROPOSED_RESEARCH_PARAMETERS"', replacement)),
        "PROPOSED_RESEARCH_PARAMETERS",
    )


# --------------------------------------------------------------------------- parameter tables
@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "name", ["tick_processing_target_ns", "synthetic_extra_frac", "synthetic_extra_count"]
)
@pytest.mark.parametrize("key", ["kind", "value", "unit", "requirement", "description"])
def test_missing_key_is_refused(tmp_path: Path, name: str, key: str) -> None:
    error = _assert_refused(_write(tmp_path, _edited(name, key, None)), "incomplete")
    assert error.details["name"] == name
    assert error.details["missing"] == key  # the error names the missing key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("unit", 'unit = ""'),
        ("unit", 'unit = "   "'),
        ("unit", "unit = 5"),
        ("unit", 'unit = ["items"]'),
        ("requirement", 'requirement = ""'),
        ("requirement", 'requirement = "\\t"'),
        ("requirement", "requirement = 107"),
        ("description", 'description = ""'),
        ("description", 'description = " "'),
        ("description", "description = false"),
    ],
)
def test_empty_or_non_text_metadata_is_refused(tmp_path: Path, key: str, replacement: str) -> None:
    error = _assert_refused(
        _write(tmp_path, _edited("synthetic_extra_count", key, replacement)), "non-empty string"
    )
    assert error.details["name"] == "synthetic_extra_count"
    assert error.details["field"] == key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content",
    [
        _HEADER,
        _HEADER + "\n[parameters]\n",
        _HEADER + "parameters = 5\n",
        _HEADER + 'parameters = "none"\n',
        _HEADER + "parameters = []\n",
        _HEADER + "parameters = {}\n",
        _HEADER + "\n[[parameters]]\nkind = 'count'\nvalue = 3\n",  # array of tables
    ],
    ids=["no-table", "empty-table", "int", "string", "empty-array", "empty-inline-table", "array-of-tables"],
)
def test_file_without_parameters_is_refused(tmp_path: Path, content: str) -> None:
    _assert_refused(_write(tmp_path, content), "no parameters")


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_a_parameter_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    content = _mutated("\n[parameters.tick_processing_target_ns]", "\n[parameters]\nsynthetic_bare = 250\n")
    assert "[parameters.tick_processing_target_ns]" not in content
    # the remaining keys of that table now belong to [parameters]; TOML still accepts the file
    error = _assert_refused(_write(tmp_path, content), "must be a table")
    assert error.details["name"] in {"synthetic_bare", "kind", "value", "unit", "requirement", "description"}


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_one_defective_parameter_refuses_the_whole_file(tmp_path: Path) -> None:
    """Fail closed: no partial view with the valid parameters is returned."""
    defective_last = _with_value("synthetic_extra_count", "-1")
    assert _assert_refused(_write(tmp_path, defective_last)).details["name"] == "synthetic_extra_count"
    defective_first = _with_value("tick_processing_target_ns", "250.5")
    assert _assert_refused(_write(tmp_path, defective_first)).details["name"] == "tick_processing_target_ns"


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content",
    [
        _with_value("synthetic_extra_frac", "0.5"),
        _with_value("synthetic_extra_frac", '"5"'),
        _with_value("synthetic_extra_ns", "0"),
        _with_value("synthetic_extra_count", "18446744073709551616"),
        _without_table("daily_recovery_threshold_frac"),
        _with_levels("-0.2", "-0.3", "-0.4", "0.1"),
        _edited("synthetic_extra_count", "kind", 'kind = "percent"'),
        _mutated("schema_version = 1\n", "schema_version = 1\nowner = 1\n"),
        "this is not toml",
    ],
    ids=[
        "float-fraction",
        "percent",
        "zero-duration",
        "count-2**64",
        "missing-required",
        "level-order",
        "unknown-kind",
        "unknown-key",
        "not-toml",
    ],
)
def test_errors_of_the_loader_are_journalable(tmp_path: Path, content: str) -> None:
    """Errors are data: the refusal survives canonical JSON (the journal form) unchanged."""
    error = _assert_refused(_write(tmp_path, content))
    journal_form: dict[str, Any] = error.to_dict()
    assert journal_form["code"] == "GIE.CONFIG.INVALID"
    assert isinstance(journal_form["message"], str)
    assert all(
        isinstance(item, (str, int, float, bool, type(None))) for item in journal_form["details"].values()
    )
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


# =========================================================================== parameters_sha256
_SHA256 = re.compile(r"[0-9a-f]{64}")

#: For every parameter of the synthetic file another valid value (the daily levels stay ordered).
_OTHER_VALUES: dict[str, str] = {
    "tick_processing_target_ns": "251",
    "protection_decision_target_ns": "749",
    "tactical_decision_target_ns": "1_501",
    "daily_defensive_threshold_frac": '"-0.31"',
    "daily_recovery_threshold_frac": '"-0.19"',
    "daily_emergency_halt_threshold_frac": '"-0.41"',
    "daily_profit_protection_threshold_frac": '"0.11"',
    "target_min_trades_per_eligible_day": "8",
    "target_average_daily_net_roi_frac": '"0.126"',
    "synthetic_extra_ns": "41",
    "synthetic_extra_frac": '"0.26"',
    "synthetic_extra_count": "2",
}


def _hash_of(tmp_path: Path, content: str, name: str = "other.toml") -> str:
    digest = load_research_parameters(_write(tmp_path, content, name)).parameters_sha256
    assert _SHA256.fullmatch(digest), digest
    return digest


@pytest.fixture
def baseline_hash(tmp_path: Path) -> str:
    return _hash_of(tmp_path, VALID_SYNTHETIC_FILE, "baseline.toml")


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_sha256_is_a_lower_case_sha256(params: ResearchParameters, baseline_hash: str) -> None:
    assert isinstance(params.parameters_sha256, str)
    assert _SHA256.fullmatch(params.parameters_sha256)
    assert _SHA256.fullmatch(baseline_hash)
    # different parameter sets (the real one and the synthetic one) have different hashes
    assert params.parameters_sha256 != baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_sha256_is_reproducible(tmp_path: Path, baseline_hash: str) -> None:
    for attempt in range(3):
        assert _hash_of(tmp_path, VALID_SYNTHETIC_FILE, f"copy_{attempt}.toml") == baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_sha256_identifies_the_real_parameter_set(
    tmp_path: Path, params: ResearchParameters
) -> None:
    """The hash follows the set of (name, kind, value), not the text of the file.

    A file written independently here from the numbers of the master specification (other
    layout, other order, other descriptive text) is the same parameter set as the real file.
    """
    tables = []
    for name in sorted(EXPECTED, reverse=True):
        value, kind, requirement, _, _ = EXPECTED[name]
        literal = f'"{value}"' if isinstance(value, Decimal) else str(value)
        tables.append(_table(name, kind, literal, "synthetic unit text", requirement))
    rewritten = load_research_parameters(_write(tmp_path, _HEADER + "\n" + "\n".join(tables)))
    assert rewritten.names() == params.names()
    assert rewritten.parameters_sha256 == params.parameters_sha256


@pytest.mark.requirement("M-DIR-08", "G05-05")
@pytest.mark.parametrize("name", _SYNTHETIC_NAMES)
def test_parameters_sha256_changes_when_any_value_changes(
    tmp_path: Path, baseline_hash: str, name: str
) -> None:
    changed = _hash_of(tmp_path, _with_value(name, _OTHER_VALUES[name]))
    assert changed != baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_every_single_value_change_gives_its_own_hash(tmp_path: Path, baseline_hash: str) -> None:
    hashes = {
        name: _hash_of(tmp_path, _with_value(name, _OTHER_VALUES[name]), f"{name}.toml")
        for name in _SYNTHETIC_NAMES
    }
    assert len(set(hashes.values()) | {baseline_hash}) == len(_SYNTHETIC_NAMES) + 1


@pytest.mark.requirement("M-DIR-08", "G05-05")
@pytest.mark.parametrize(
    ("name", "kind"),
    [("synthetic_extra_ns", "count"), ("synthetic_extra_count", "duration_ns")],
    ids=["duration-to-count", "count-to-duration"],
)
def test_parameters_sha256_changes_when_a_kind_changes(
    tmp_path: Path, baseline_hash: str, name: str, kind: str
) -> None:
    """Same name, same number, other kind: 40 ns is not 40 items."""
    content = _edited(name, "kind", f'kind = "{kind}"')
    loaded = load_research_parameters(_write(tmp_path, content))
    assert loaded.get(name).kind == kind
    assert loaded.value(name) == _SYNTHETIC_VALUES[name]  # the value itself is unchanged
    assert loaded.parameters_sha256 != baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_sha256_changes_when_values_are_exchanged_between_parameters(
    tmp_path: Path, baseline_hash: str
) -> None:
    """The hash binds each value to its name: the same numbers under other names are another set."""
    content = VALID_SYNTHETIC_FILE
    for name, literal in (("tick_processing_target_ns", "750"), ("protection_decision_target_ns", "250")):
        lines = _TABLES[name].splitlines()
        lines[2] = f"value = {literal}"
        assert content.count(_TABLES[name]) == 1
        content = content.replace(_TABLES[name], "\n".join(lines) + "\n")
    assert _hash_of(tmp_path, content) != baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_parameters_sha256_changes_when_a_parameter_is_added_removed_or_renamed(
    tmp_path: Path, baseline_hash: str
) -> None:
    added = VALID_SYNTHETIC_FILE + "\n" + _table("synthetic_added_count", "count", "0", "items", "G01-07")
    removed = _without_table("synthetic_extra_count")
    renamed = _mutated("[parameters.synthetic_extra_count]", "[parameters.synthetic_renamed_count]")
    hashes = {
        baseline_hash,
        _hash_of(tmp_path, added, "added.toml"),
        _hash_of(tmp_path, removed, "removed.toml"),
        _hash_of(tmp_path, renamed, "renamed.toml"),
    }
    assert len(hashes) == 4


def _reordered() -> str:
    return _HEADER + "".join("\n" + _TABLES[name] for name in reversed(_SYNTHETIC_NAMES))


@pytest.mark.requirement("M-DIR-08", "G05-05")
@pytest.mark.parametrize(
    "content",
    [
        _reordered(),
        VALID_SYNTHETIC_FILE.replace("\n", "\r\n"),
        "# another comment\n\n" + VALID_SYNTHETIC_FILE.replace("\n[", "\n\n# section\n[") + "\n# end\n",
        VALID_SYNTHETIC_FILE.replace("synthetic fixture: ", "synthetic fixture, reworded - "),
        _edited("synthetic_extra_count", "unit", 'unit = "pieces"'),
        _edited("synthetic_extra_count", "requirement", 'requirement = "G01-08"'),
        _with_value("tactical_decision_target_ns", "1500"),  # 1_500 written without the separator
        _with_value("tactical_decision_target_ns", "0x5DC"),  # the same integer in another TOML notation
        _with_value("synthetic_extra_frac", '"0.250"'),  # the same decimal with a trailing zero
        _with_value("synthetic_extra_frac", "'0.25'"),  # the same string as a TOML literal string
        _edited("synthetic_extra_count", "kind", "kind = 'count'"),
    ],
    ids=[
        "tables-reordered",
        "crlf",
        "comments-and-blank-lines",
        "descriptions-reworded",
        "unit-reworded",
        "requirement-changed",
        "integer-without-separator",
        "integer-in-hex",
        "decimal-trailing-zero",
        "literal-string",
        "kind-as-literal-string",
    ],
)
def test_parameters_sha256_is_stable_when_no_value_or_kind_changes(
    tmp_path: Path, baseline_hash: str, content: str
) -> None:
    assert content != VALID_SYNTHETIC_FILE  # the text differs ...
    loaded = load_research_parameters(_write(tmp_path, content, "variant.toml"))
    for name, kind, _, _, _ in _SYNTHETIC_PARAMETERS:  # ... the parameter set does not
        assert loaded.value(name) == _SYNTHETIC_VALUES[name]
        assert type(loaded.value(name)) is type(_SYNTHETIC_VALUES[name])
        assert loaded.get(name).kind == kind
    assert loaded.parameters_sha256 == baseline_hash


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_zero_and_minus_zero_are_the_same_fraction(tmp_path: Path) -> None:
    plain = _hash_of(tmp_path, _with_value("synthetic_extra_frac", '"0"'), "zero.toml")
    signed = _hash_of(tmp_path, _with_value("synthetic_extra_frac", '"-0"'), "minus_zero.toml")
    fractional = _hash_of(tmp_path, _with_value("synthetic_extra_frac", '"0.000"'), "zeros.toml")
    assert plain == signed == fractional


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_the_hash_of_a_view_built_in_code_does_not_depend_on_insertion_order() -> None:
    first = ResearchParameter("synthetic_a_ns", "duration_ns", 5, "ns", "G03-07", "synthetic fixture")
    second = ResearchParameter("synthetic_b_frac", "fraction", Decimal("0.5"), "fraction", "G05-02", "x")
    forward = ResearchParameters({first.name: first, second.name: second}, EXPECTED_STATUS)
    backward = ResearchParameters({second.name: second, first.name: first}, EXPECTED_STATUS)
    assert forward.parameters_sha256 == backward.parameters_sha256
    assert _SHA256.fullmatch(forward.parameters_sha256)
    trailing_zero = dataclasses.replace(second, value=Decimal("0.50"))
    assert (
        ResearchParameters({first.name: first, second.name: trailing_zero}, EXPECTED_STATUS).parameters_sha256
        == forward.parameters_sha256
    )
    other = dataclasses.replace(second, value=Decimal("0.51"))
    assert (
        ResearchParameters({first.name: first, second.name: other}, EXPECTED_STATUS).parameters_sha256
        != forward.parameters_sha256
    )


@pytest.mark.requirement("M-DIR-08", "G05-05")
@pytest.mark.parametrize(
    ("attribute", "value"),
    [("parameters_sha256", "0" * 64), ("approval_status", "APPROVED_PRODUCTION_LIMITS")],
    ids=["hash", "approval-status"],
)
def test_the_validated_view_cannot_be_rehashed_or_relabelled(
    tmp_path: Path, attribute: str, value: str
) -> None:
    """``ResearchParameters`` is documented as an *immutable*, validated view.

    If the hash or the approval label of a loaded view can be overwritten, the hash no longer
    identifies the parameter set that the view actually returns, and research parameters can be
    relabelled as approved limits (M-DIR-08) without touching the file.  A freshly loaded view is
    used so that a successful assignment cannot leak into other tests.
    """
    loaded = load_research_parameters(_write(tmp_path, VALID_SYNTHETIC_FILE))
    before = getattr(loaded, attribute)
    with pytest.raises((AttributeError, TypeError, GieError)):
        setattr(loaded, attribute, value)
    assert getattr(loaded, attribute) == before
