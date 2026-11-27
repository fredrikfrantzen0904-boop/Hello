"""Independent tests for ``gie.core.params`` (requirements M-DIR-08, G05-05).

Two things are verified:

1. The real file ``specifications/research_parameters.toml`` carries exactly the numbers that
   the master specification states in sections G01, G03 and G05
   (``specifications/extracted/MASTER.txt``).  Each expected number is written out below with
   the sentence it comes from, and is additionally re-derived by parsing that sentence out of
   the specification text, so neither the file nor this test can drift from the source unnoticed.
2. The loader fails closed (``ConfigError``) on every defective file.  The defective files are
   synthetic fixtures written to ``tmp_path``; they are not research parameters.
"""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from gie.core.errors import ConfigError, GieError
from gie.core.params import (
    DEFAULT_PATH,
    EXPECTED_STATUS,
    ResearchParameter,
    ResearchParameters,
    load_research_parameters,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PARAMETER_FILE = REPO_ROOT / "specifications" / "research_parameters.toml"
MASTER_TEXT = REPO_ROOT / "specifications" / "extracted" / "MASTER.txt"
REQUIREMENTS_MATRIX = REPO_ROOT / "specifications" / "requirements_matrix.yaml"

NS_PER_MS = 1_000_000

#: name -> (exact expected value, requirement id, MASTER section, sentence of the specification)
EXPECTED: dict[str, tuple[int | Decimal, str, str, str]] = {
    "tick_processing_target_ns": (1_000_000, "G03-07", "G03", "tick processing <1 ms"),
    "protection_decision_target_ns": (5_000_000, "G03-08", "G03", "protection decision <5 ms"),
    "tactical_decision_target_ns": (20_000_000, "G03-09", "G03", "tactical <20 ms"),
    "daily_defensive_threshold_frac": (Decimal("-0.02"), "G05-02", "G05", "defensive at -2%"),
    "daily_recovery_threshold_frac": (Decimal("-0.0175"), "G05-03", "G05", "proposed recovery at -1.75%"),
    "daily_emergency_halt_threshold_frac": (Decimal("-0.03"), "G05-04", "G05", "emergency halt at -3%"),
    "daily_profit_protection_threshold_frac": (Decimal("0.05"), "G05-06", "G05", "Above +5% protect"),
    "target_min_trades_per_eligible_day": (1, "G01-07", "G01", "at least one valid trade per eligible day"),
    "target_average_daily_net_roi_frac": (
        Decimal("0.05"),
        "G01-08",
        "G01",
        "at least 5% average daily net ROI",
    ),
}


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
    expected, requirement, section, sentence = EXPECTED[name]
    value = params.value(name)
    assert type(value) is type(expected)  # int stays int (never bool), fractions are Decimal
    assert value == expected
    assert sentence in _master_section(section)

    parameter = params.get(name)
    assert isinstance(parameter, ResearchParameter)
    assert parameter.name == name
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
        if name.endswith("_frac"):
            assert type(value) is Decimal
            assert value.is_finite()
            assert -1 < value < 1
            assert "fraction" in params.get(name).unit


@pytest.mark.requirement("M-DIR-08")
def test_every_parameter_cites_an_existing_requirement_of_its_section(params: ResearchParameters) -> None:
    matrix = yaml.safe_load(REQUIREMENTS_MATRIX.read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in matrix["requirements"]}
    for name in params.names():
        parameter = params.get(name)
        _, requirement, section, _ = EXPECTED[name]
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
        for name in params.names():
            assert other.get(name) == params.get(name)


# =========================================================================== accessors
@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_typed_accessors(params: ResearchParameters) -> None:
    assert params.integer("tick_processing_target_ns") == 1_000_000
    assert type(params.integer("tick_processing_target_ns")) is int
    assert params.decimal("daily_defensive_threshold_frac") == Decimal("-0.02")
    assert type(params.decimal("daily_defensive_threshold_frac")) is Decimal
    assert params.value("tick_processing_target_ns") == 1_000_000
    assert params.value("daily_defensive_threshold_frac") == Decimal("-0.02")


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
    assert params.decimal("daily_emergency_halt_threshold_frac") == Decimal("-0.03")

    source = {"synthetic": ResearchParameter("synthetic", 1, "ns", "G03-07", "synthetic fixture")}
    view = ResearchParameters(source, EXPECTED_STATUS)
    source["added_later"] = ResearchParameter("added_later", 2, "ns", "G03-07", "synthetic fixture")
    del source["synthetic"]
    assert view.names() == ("synthetic",)
    assert view.integer("synthetic") == 1


# =========================================================================== defective files
#: Synthetic fixture: a minimal valid file.  Each defect test changes exactly one thing in it.
VALID_SYNTHETIC_FILE = """\
# synthetic fixture - not the real research parameters
schema_version = 1
approval_status = "PROPOSED_RESEARCH_PARAMETERS"

[parameters.synthetic_limit_ns]
value = 250
unit = "ns"
requirement = "G03-07"
description = "synthetic fixture: integer parameter"

[parameters.synthetic_threshold_frac]
value = "-0.0125"
unit = "fraction"
requirement = "G05-02"
description = "synthetic fixture: decimal parameter"
"""


def _write(tmp_path: Path, content: str | bytes) -> Path:
    path = tmp_path / "research_parameters.toml"
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


def _assert_refused(path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        load_research_parameters(path)
    assert excinfo.value.code == "GIE.CONFIG.INVALID"
    assert isinstance(excinfo.value, GieError)
    return excinfo.value


@pytest.mark.requirement("M-DIR-08", "G05-05")
def test_valid_synthetic_file_loads(tmp_path: Path) -> None:
    """Positive control: without a defect the synthetic file is accepted."""
    loaded = load_research_parameters(_write(tmp_path, VALID_SYNTHETIC_FILE))
    assert loaded.names() == ("synthetic_limit_ns", "synthetic_threshold_frac")
    assert loaded.approval_status == "PROPOSED_RESEARCH_PARAMETERS"
    assert loaded.integer("synthetic_limit_ns") == 250
    assert type(loaded.value("synthetic_limit_ns")) is int
    assert loaded.decimal("synthetic_threshold_frac") == Decimal("-0.0125")
    assert type(loaded.value("synthetic_threshold_frac")) is Decimal
    assert loaded.get("synthetic_limit_ns") == ResearchParameter(
        name="synthetic_limit_ns",
        value=250,
        unit="ns",
        requirement="G03-07",
        description="synthetic fixture: integer parameter",
    )


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20")
@pytest.mark.parametrize(
    ("toml_value", "expected"),
    [
        ("0", 0),
        ("-7", -7),
        ("1_000_000", 1_000_000),
        ('"0.1"', Decimal("0.1")),
        ('"-0.0175"', Decimal("-0.0175")),
        ('"3"', Decimal("3")),
        ('"0.05"', Decimal("0.05")),
    ],
)
def test_values_are_exact_integers_or_exact_decimals(
    tmp_path: Path, toml_value: str, expected: int | Decimal
) -> None:
    loaded = load_research_parameters(_write(tmp_path, _mutated("value = 250", f"value = {toml_value}")))
    value = loaded.value("synthetic_limit_ns")
    assert type(value) is type(expected)
    assert value == expected
    if isinstance(expected, Decimal):
        assert loaded.decimal("synthetic_limit_ns") == expected
        # exact: a decimal string never passes through a binary float
        assert str(loaded.decimal("synthetic_limit_ns")) == toml_value.strip('"')
    else:
        assert loaded.integer("synthetic_limit_ns") == expected


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_missing_file_is_refused(tmp_path: Path) -> None:
    error = _assert_refused(tmp_path / "does_not_exist.toml")
    assert "does_not_exist.toml" in str(error)


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_directory_instead_of_file_is_refused(tmp_path: Path) -> None:
    _assert_refused(tmp_path)


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content",
    [
        "schema_version = ",
        "schema_version = 1\napproval_status = PROPOSED_RESEARCH_PARAMETERS\n",
        "[parameters\n",
        "this is not toml",
        '{"schema_version": 1}',
        VALID_SYNTHETIC_FILE + "\n[parameters.synthetic_limit_ns]\nvalue = 1\n",  # table defined twice
        VALID_SYNTHETIC_FILE.replace('unit = "ns"', 'unit = "ns'),  # unterminated string
        "schema_version = 1\nschema_version = 1\n",  # duplicate key
    ],
    ids=[
        "no-value",
        "bare-string",
        "open-table",
        "prose",
        "json",
        "duplicate-table",
        "open-string",
        "duplicate-key",
    ],
)
def test_invalid_toml_is_refused(tmp_path: Path, content: str) -> None:
    _assert_refused(_write(tmp_path, content))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_file_that_is_not_utf8_is_refused_with_a_config_error(tmp_path: Path) -> None:
    """TOML is UTF-8; any other bytes are a defective file and must fail closed as ConfigError."""
    content = VALID_SYNTHETIC_FILE.encode("utf-8").replace(
        b"integer parameter", b"integer parameter \xff\xfe"
    )
    assert content != VALID_SYNTHETIC_FILE.encode("utf-8")
    _assert_refused(_write(tmp_path, content))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "content", ["", "\n", "# only a comment\n", "   \n\t\n"], ids=["empty", "newline", "comment", "blank"]
)
def test_empty_file_is_refused(tmp_path: Path, content: str) -> None:
    _assert_refused(_write(tmp_path, content))


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
        "",  # key removed
        "schema_versions = 1",  # misspelt key
    ],
    ids=["2", "0", "-1", "11", "string-1", "string-v1", "list", "missing", "misspelt"],
)
def test_wrong_schema_version_is_refused(tmp_path: Path, replacement: str) -> None:
    _assert_refused(_write(tmp_path, _mutated("schema_version = 1", replacement)))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "replacement", ["schema_version = true", "schema_version = 1.0"], ids=["bool", "float"]
)
def test_schema_version_of_the_wrong_type_is_refused(tmp_path: Path, replacement: str) -> None:
    """The schema version is the integer 1; ``true`` and ``1.0`` are different TOML values."""
    _assert_refused(_write(tmp_path, _mutated("schema_version = 1", replacement)))


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
        'approval_state = "PROPOSED_RESEARCH_PARAMETERS"',  # misspelt key
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
        "misspelt",
    ],
)
def test_any_other_approval_status_is_refused(tmp_path: Path, replacement: str) -> None:
    """In this project phase the numbers are proposed research parameters and nothing else."""
    _assert_refused(
        _write(tmp_path, _mutated('approval_status = "PROPOSED_RESEARCH_PARAMETERS"', replacement))
    )


@pytest.mark.requirement("M-DIR-08", "S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "toml_value",
    [
        "0.05",
        "250.0",
        "-0.02",
        "1e3",
        "inf",
        "nan",
        "true",
        "false",
        "[250]",
        "{ amount = 250 }",
        "2026-11-27",
        "09:30:00",
        '"abc"',
        '""',
        '"5%"',
        '"0,05"',
        '"0.05.1"',
        '"NaN"',
        '"sNaN"',
        '"Infinity"',
        '"-Infinity"',
    ],
)
def test_value_that_is_not_an_exact_number_is_refused(tmp_path: Path, toml_value: str) -> None:
    error = _assert_refused(_write(tmp_path, _mutated("value = 250", f"value = {toml_value}")))
    assert "synthetic_limit_ns" in str(error)  # the error names the offending parameter


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    "line",
    [
        "value = 250\n",
        'unit = "ns"\n',
        'requirement = "G03-07"\n',
        'description = "synthetic fixture: integer parameter"\n',
    ],
    ids=["value", "unit", "requirement", "description"],
)
def test_missing_key_is_refused(tmp_path: Path, line: str) -> None:
    error = _assert_refused(_write(tmp_path, _mutated(line, "")))
    assert "synthetic_limit_ns" in str(error)
    assert line.partition(" = ")[0] in str(error)  # the error names the missing key


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('unit = "ns"', 'unit = ""'),
        ('unit = "ns"', 'unit = "   "'),
        ('unit = "ns"', "unit = 5"),
        ('unit = "ns"', 'unit = ["ns"]'),
        ('requirement = "G03-07"', 'requirement = ""'),
        ('requirement = "G03-07"', 'requirement = "\\t"'),
        ('requirement = "G03-07"', "requirement = 307"),
        ('description = "synthetic fixture: integer parameter"', 'description = ""'),
        ('description = "synthetic fixture: integer parameter"', 'description = " "'),
        ('description = "synthetic fixture: integer parameter"', "description = false"),
    ],
)
def test_empty_or_non_text_metadata_is_refused(tmp_path: Path, old: str, new: str) -> None:
    error = _assert_refused(_write(tmp_path, _mutated(old, new)))
    assert "synthetic_limit_ns" in str(error)


_HEADER = 'schema_version = 1\napproval_status = "PROPOSED_RESEARCH_PARAMETERS"\n'


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
        _HEADER + "\n[parameters]\nsynthetic_limit_ns = 250\n",  # a bare value instead of a table
        _HEADER + "\n[[parameters]]\nvalue = 250\n",  # array of tables
        _HEADER + "\n[parameter.synthetic_limit_ns]\nvalue = 250\nunit = 'ns'\nrequirement = 'G03-07'\n"
        "description = 'synthetic fixture'\n",  # misspelt top-level table
    ],
    ids=[
        "no-table",
        "empty-table",
        "int",
        "string",
        "empty-array",
        "empty-inline-table",
        "bare-value",
        "array-of-tables",
        "misspelt-table",
    ],
)
def test_file_without_parameters_is_refused(tmp_path: Path, content: str) -> None:
    _assert_refused(_write(tmp_path, content))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_one_defective_parameter_refuses_the_whole_file(tmp_path: Path) -> None:
    """Fail closed: no partial view with the valid parameters is returned."""
    defective_last = _mutated('value = "-0.0125"', "value = -0.0125")
    _assert_refused(_write(tmp_path, defective_last))
    defective_first = _mutated("value = 250", "value = 250.5")
    _assert_refused(_write(tmp_path, defective_first))


@pytest.mark.requirement("M-DIR-08", "S00-ASG-24")
def test_errors_of_the_loader_are_journalable(tmp_path: Path) -> None:
    error = _assert_refused(_write(tmp_path, _mutated("value = 250", "value = 0.5")))
    journal_form: dict[str, Any] = error.to_dict()
    assert journal_form["code"] == "GIE.CONFIG.INVALID"
    assert isinstance(journal_form["message"], str)
    assert all(
        isinstance(item, (str, int, float, bool, type(None))) for item in journal_form["details"].values()
    )
