"""Golden contract messages (acceptance test S00-ACC-01) and their maintenance tool.

``tools/make_golden.py`` keeps the canonical JSON of every labelled sample variant under
``tests/gie/golden/<Contract>-<version>/<variant>.json``.  According to its module docstring and to
``specifications/SHARED_CONTRACTS.md`` section 8, golden files of a released contract version are
the compatibility reference: every later reader of the same MAJOR version must decode them, and
files of the current version must re-encode byte for byte.

The tests below check the committed files and prove, on a copy in a temporary directory, that
tampering with a golden message (changed value, removed optional key, added unknown key, changed
formatting, missing or surplus file) is detected by decoding or by ``make_golden.check``.

Every golden message is a SYNTHETIC FIXTURE produced from ``tests/gie/samples.py``: invented test
data, not market data, not broker data and not evidence of any trading performance.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from gie.contracts import Record, from_json, from_wire, to_bytes, to_json, to_wire
from gie.contracts.registry import all_contracts, parse_semver
from gie.contracts.schema_gen import APPROVED, json_schema, load_lock, load_schema
from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import (
    CanonicalEncodingError,
    ContractValidationError,
    ContractVersionError,
    UnknownContractError,
)
from tests.gie import samples
from tools import make_golden

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "tests" / "gie" / "golden"

CONTRACT_REQUIREMENT = {
    "Tick": "S00-ASG-11",
    "Evidence": "S00-ASG-12",
    "Candidate": "S00-ASG-13",
    "TradePlan": "S00-ASG-14",
    "RiskDecision": "S00-ASG-15",
    "OrderEvent": "S00-ASG-16",
    "Position": "S00-ASG-17",
    "ModelVersion": "S00-ASG-18",
    "Clock": "S00-ASG-19",
}
DIRECTORY_NAME = re.compile(r"^(?P<contract>[A-Za-z]+)-(?P<version>\d+\.\d+\.\d+)$")


def _marks(contract: str) -> list[pytest.MarkDecorator]:
    requirement = CONTRACT_REQUIREMENT.get(contract)
    return [pytest.mark.requirement(requirement)] if requirement else []


#: Every committed golden file, discovered on disk (not derived from the samples).
GOLDEN_FILES = sorted(GOLDEN_DIR.glob("*/*.json"))
#: The golden run trace (change notice 12): the sample run, one canonical message per line.
GOLDEN_TRACE = GOLDEN_DIR / "traces" / "run_story.jsonl"
TRACE_NAME = "traces/run_story.jsonl"
FILE_PARAMS = [
    pytest.param(path, id=f"{path.parent.name}/{path.stem}", marks=_marks(path.parent.name.split("-")[0]))
    for path in GOLDEN_FILES
]
VARIANT_PARAMS = [
    pytest.param(contract, variant, id=f"{contract}-{variant}", marks=_marks(contract))
    for contract, by_variant in samples.variants().items()
    for variant in by_variant
]


def _current_version(contract: str) -> str:
    version = all_contracts()[contract].VERSION
    assert version is not None
    return version


def _directory(path: Path) -> tuple[str, str]:
    match = DIRECTORY_NAME.fullmatch(path.parent.name)
    assert match, f"{path.parent.name} is not <Contract>-<version>"
    return match["contract"], match["version"]


@pytest.fixture
def golden_copy(tmp_path: Path) -> Path:
    """A private copy of the committed golden tree that a test may damage."""
    root = tmp_path / "golden"
    shutil.copytree(GOLDEN_DIR, root)
    return root


def _rewrite(path: Path, change: Any) -> dict[str, Any]:
    """Apply *change* to the parsed message and store it again in canonical form."""
    message: dict[str, Any] = strict_loads(path.read_text(encoding="utf-8"))
    change(message)
    path.write_text(canonical_dumps(message) + "\n", encoding="utf-8", newline="\n")
    return message


def _assert_same_json(parsed: Any, wire: Any, path: str = "<root>") -> None:
    """*parsed* (text read with ``strict_loads``) is the JSON value *wire* of the record.

    The text does not say whether a number without a fraction was an integer or a double
    (``gie.core.canonical``): an integral double is read back as an integer literal, and only as
    its canonical rendering.  Every other value must be identical, type included.
    """
    if isinstance(wire, dict):
        assert type(parsed) is dict, path
        assert set(parsed) == set(wire), path
        for key, value in wire.items():
            _assert_same_json(parsed[key], value, f"{path}.{key}")
    elif isinstance(wire, list):
        assert type(parsed) is list, path
        assert len(parsed) == len(wire), path
        for index, (left, right) in enumerate(zip(parsed, wire, strict=True)):
            _assert_same_json(left, right, f"{path}[{index}]")
    elif type(wire) is float and type(parsed) is int:
        assert str(parsed) == canonical_dumps(wire), path
        assert float(parsed) == wire, path
    else:
        assert type(parsed) is type(wire), path
        assert parsed == wire, path


def _problems_for(root: Path, path: Path) -> list[str]:
    name = path.relative_to(root).as_posix()
    return [problem for problem in make_golden.check(root) if problem.startswith(name + ":")]


# =========================================================================== committed files
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_check_reports_no_problem_for_the_committed_golden_messages() -> None:
    assert make_golden.check() == []
    assert make_golden.check(GOLDEN_DIR) == []
    assert make_golden.GOLDEN_DIR == GOLDEN_DIR


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_command_line_check_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert make_golden.main(["--check"]) == 0
    captured = capsys.readouterr()
    assert f"golden messages consistent: {len(VARIANT_PARAMS)} files" in captured.out
    assert captured.err == ""
    with pytest.raises(SystemExit) as stopped:
        make_golden.main([])
    assert stopped.value.code == 2, "one of --check / --write is required"
    capsys.readouterr()


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_golden_tree_holds_exactly_one_file_per_sample_variant() -> None:
    expected = {
        GOLDEN_DIR / f"{contract}-{_current_version(contract)}" / f"{variant}.json"
        for contract, by_variant in samples.variants().items()
        for variant in by_variant
    }
    assert len(expected) >= 45
    assert set(GOLDEN_FILES) == expected
    assert set(make_golden.expected_files()) == expected
    # Besides one directory per contract version only the directory of the golden run trace exists.
    assert {path.name for path in GOLDEN_DIR.iterdir()} == {path.parent.name for path in expected} | {
        make_golden.TRACE_DIR
    }
    assert sorted(path.name for path in (GOLDEN_DIR / make_golden.TRACE_DIR).iterdir()) == [
        make_golden.TRACE_FILE
    ]
    # Every one of the ten contracts has golden messages.
    assert {_directory(path)[0] for path in GOLDEN_FILES} == set(all_contracts()) & set(samples.variants())
    assert len({_directory(path)[0] for path in GOLDEN_FILES}) == 10


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_every_sample_variant_has_its_golden_file(contract: str, variant: str) -> None:
    path = make_golden.golden_path(contract, _current_version(contract), variant)
    assert path == GOLDEN_DIR / f"{contract}-{_current_version(contract)}" / f"{variant}.json"
    assert path.is_file(), f"{path} is missing (python -m tools.make_golden --write)"
    sample = samples.variants()[contract][variant]
    assert path.read_bytes() == to_bytes(sample) + b"\n", "the sample still encodes to the golden bytes"
    assert from_json(path.read_bytes()) == sample
    assert make_golden.expected_files()[path] == to_json(sample) + "\n"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_golden_file_decodes_and_re_encodes_byte_for_byte(path: Path) -> None:
    contract, version = _directory(path)
    data = path.read_bytes()
    text = data.decode("utf-8")
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1, "one canonical JSON line"
    assert not data.startswith(b"\xef\xbb\xbf"), "no byte order mark"
    decoded: Record = from_json(data)
    assert type(decoded) is all_contracts()[contract]
    assert from_json(text) == decoded
    assert from_json(text, expect=all_contracts()[contract]) == decoded
    assert to_json(decoded) + "\n" == text
    assert to_bytes(decoded) + b"\n" == data
    assert canonical_dumps(strict_loads(text)) + "\n" == text
    # The envelope says what the directory says.
    message = json.loads(text)
    assert message["contract"] == contract
    assert message["schema_version"] == version
    assert parse_semver(version).major == parse_semver(_current_version(contract)).major
    assert from_wire(message) == decoded
    assert from_wire(strict_loads(text)) == decoded
    _assert_same_json(strict_loads(text), to_wire(decoded))
    assert canonical_dumps(to_wire(decoded)) + "\n" == text


@pytest.mark.requirement("S00-ACC-01", "S00-DEL-02")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_golden_file_validates_against_the_schema_of_its_version(path: Path) -> None:
    """``schema_gen``: 'A message must be validated against the schema of the version it declares.'"""
    contract, version = _directory(path)
    schema = load_schema(contract, version)
    assert schema["$id"] == f"urn:gie:contract:{contract}:{version}"
    assert schema["title"] == f"{contract} {version}"
    jsonschema.Draft202012Validator.check_schema(schema)
    instance = strict_loads(path.read_text(encoding="utf-8"))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(instance))
    assert errors == []
    if version == _current_version(contract):
        assert schema == json_schema(all_contracts()[contract]), "published schema equals the generated one"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_golden_file_writes_every_field_and_is_labelled_synthetic(path: Path) -> None:
    contract, _version = _directory(path)
    message = json.loads(path.read_text(encoding="utf-8"))
    declared = {spec.name for spec in all_contracts()[contract].FIELD_SPECS}
    assert set(message) == declared | {"contract", "schema_version"}
    optional = {spec.name for spec in all_contracts()[contract].FIELD_SPECS if not spec.required}
    assert {key for key, value in message.items() if value is None} <= optional
    for key in ("origin", "data_origin", "training_data_origin"):
        if message.get(key) is not None:
            assert message[key] == "SYNTHETIC_FIXTURE", "golden messages are fixtures, never market data"
    assert message.get("mode", "REPLAY") in ("REPLAY", "PAPER")
    assert message.get("venue", "SIMULATED") == "SIMULATED"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_golden_messages_include_null_and_non_null_examples_of_optional_fields() -> None:
    nulls = sum(text.count(":null") for text in (path.read_text(encoding="utf-8") for path in GOLDEN_FILES))
    assert nulls > 100, "optional fields are written as explicit null in the golden messages"
    tick = json.loads((GOLDEN_DIR / f"Tick-{_current_version('Tick')}" / "typical.json").read_text("utf-8"))
    assert tick["bid_volume"] is None
    assert tick["ask_volume"] is None
    measured = json.loads(
        (GOLDEN_DIR / f"Tick-{_current_version('Tick')}" / "measured_receive_time.json").read_text("utf-8")
    )
    assert measured["bid_volume"] == "3.5"
    assert measured["ask_volume"] == "1"


# =========================================================================== tampering is detected
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_an_untouched_copy_of_the_golden_tree_is_consistent(golden_copy: Path) -> None:
    """The baseline of the tampering tests below: without damage ``check`` reports nothing."""
    assert golden_copy != GOLDEN_DIR
    assert make_golden.check(golden_copy) == []
    assert sorted(path.relative_to(golden_copy) for path in golden_copy.glob("*/*.json")) == sorted(
        path.relative_to(GOLDEN_DIR) for path in GOLDEN_FILES
    )


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_a_changed_value_in_any_golden_file_is_detected(path: Path, golden_copy: Path) -> None:
    target = golden_copy / path.relative_to(GOLDEN_DIR)
    _rewrite(target, lambda message: message.__setitem__("seq", message["seq"] + 1))
    problems = _problems_for(golden_copy, target)
    assert problems, "a golden message that differs from its sample must be reported"
    assert any(
        "no longer encodes to the golden bytes" in problem or "no longer decodes" in problem
        for problem in problems
    )
    assert path.read_bytes() != target.read_bytes(), "only the copy was changed"


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_a_removed_optional_key_is_detected(path: Path, golden_copy: Path) -> None:
    contract, _version = _directory(path)
    optional = [spec.name for spec in all_contracts()[contract].FIELD_SPECS if not spec.required]
    if not optional:
        assert contract == "Clock", "only the Clock contract has no optional field"
        required = all_contracts()[contract].FIELD_SPECS[0].name
        target = golden_copy / path.relative_to(GOLDEN_DIR)
        _rewrite(target, lambda message: message.pop(required))
        with pytest.raises(ContractValidationError) as caught:
            from_json(target.read_text(encoding="utf-8"))
        assert caught.value.details["path"] == required
        assert any("no longer decodes" in problem for problem in _problems_for(golden_copy, target))
        return
    target = golden_copy / path.relative_to(GOLDEN_DIR)
    original = target.read_text(encoding="utf-8")
    for name in optional:
        _rewrite(target, lambda message, name=name: message.pop(name))
        with pytest.raises(ContractValidationError) as caught:
            from_json(target.read_text(encoding="utf-8"))
        assert caught.value.details["path"] == name
        assert "explicit null" in str(caught.value.details["rule"])
        target.write_text(original, encoding="utf-8", newline="\n")
    # ... and the checker reports the damaged file (shown for the first and the last optional key).
    for name in {optional[0], optional[-1]}:
        _rewrite(target, lambda message, name=name: message.pop(name))
        problems = _problems_for(golden_copy, target)
        assert any("no longer decodes" in problem and name in problem for problem in problems), name
        target.write_text(original, encoding="utf-8", newline="\n")
    assert _problems_for(golden_copy, target) == []


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("path", FILE_PARAMS)
def test_an_added_unknown_key_is_detected(path: Path, golden_copy: Path) -> None:
    target = golden_copy / path.relative_to(GOLDEN_DIR)
    _rewrite(target, lambda message: message.__setitem__("zzz_unknown", None))
    with pytest.raises(ContractValidationError) as caught:
        from_json(target.read_text(encoding="utf-8"))
    assert caught.value.details == {"path": "zzz_unknown", "rule": "unknown field"}
    problems = _problems_for(golden_copy, target)
    assert any("no longer decodes" in problem for problem in problems)
    assert any("zzz_unknown" in problem for problem in problems)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    ("damage", "error", "report"),
    [
        (lambda text: text.rstrip("\n"), None, "byte for byte"),
        (lambda text: text + "\n", None, "byte for byte"),
        (lambda text: json.dumps(json.loads(text), indent=2) + "\n", None, "byte for byte"),
        (lambda text: json.dumps(json.loads(text)) + "\n", None, "byte for byte"),
        (lambda text: text.replace('"bid":"2650.1"', '"bid":"2650.10"'), None, "byte for byte"),
        (lambda text: text.replace('"seq":0', '"seq":0.0'), ContractValidationError, "no longer decodes"),
        (
            lambda text: text.replace('"bid":"2650.1"', '"bid":2650.1'),
            ContractValidationError,
            "no longer decodes",
        ),
        (
            lambda text: text.replace('"bid":"2650.1"', '"bid":"2650.40"'),
            ContractValidationError,
            "no longer decodes",
        ),
        (
            lambda text: text.replace("SYNTHETIC_FIXTURE", "LIVE"),
            ContractValidationError,
            "no longer decodes",
        ),
        (
            lambda text: text.replace('"contract":"Tick"', '"contract":"Tock"'),
            UnknownContractError,
            "no longer decodes",
        ),
        (
            lambda text: text.replace('"schema_version":"1', '"schema_version":"2'),
            ContractVersionError,
            "no longer decodes",
        ),
        (lambda text: text[: len(text) // 2], CanonicalEncodingError, "no longer decodes"),
        (lambda text: "", CanonicalEncodingError, "no longer decodes"),
        (lambda text: text.rstrip("\n")[:-1] + ',"seq":0}\n', CanonicalEncodingError, "no longer decodes"),
        (lambda text: "[" + text.rstrip("\n") + "]\n", ContractValidationError, "no longer decodes"),
    ],
    ids=[
        "no-trailing-newline",
        "two-trailing-newlines",
        "pretty-printed",
        "spaces-after-separators",
        "decimal-with-trailing-zero",
        "float-token-for-integer",
        "number-for-decimal",
        "invariant-violated",
        "unknown-enum-value",
        "unknown-contract",
        "other-major-version",
        "truncated",
        "empty",
        "duplicate-key",
        "wrapped-in-array",
    ],
)
def test_other_damage_to_a_golden_file_is_detected(
    damage: Any, error: type[Exception] | None, report: str, golden_copy: Path
) -> None:
    target = golden_copy / f"Tick-{_current_version('Tick')}" / "typical.json"
    original = target.read_text(encoding="utf-8")
    damaged = damage(original)
    assert damaged != original, "the damage applies to the fixture"
    target.write_text(damaged, encoding="utf-8", newline="\n")
    if error is None:
        assert from_json(damaged) == samples.variants()["Tick"]["typical"], "still the same record ..."
        assert to_json(from_json(damaged)) + "\n" != damaged, "... but not its canonical bytes"
    else:
        with pytest.raises(error) as caught:
            from_json(damaged)
        assert type(caught.value) is error
    problems = _problems_for(golden_copy, target)
    assert any(report in problem for problem in problems), problems


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_missing_and_surplus_golden_files_are_detected(golden_copy: Path) -> None:
    directory = golden_copy / f"Tick-{_current_version('Tick')}"
    typical = directory / "typical.json"
    text = typical.read_text(encoding="utf-8")
    typical.unlink()
    assert make_golden.check(golden_copy) == [
        f"{directory.name}/typical.json: golden file is missing (run --write)"
    ]
    typical.write_text(text, encoding="utf-8", newline="\n")
    surplus = directory / "not_a_sample_variant.json"
    surplus.write_text(text, encoding="utf-8", newline="\n")
    assert make_golden.check(golden_copy) == [
        f"{directory.name}/not_a_sample_variant.json: golden file has no sample variant"
    ]
    surplus.unlink()
    shutil.rmtree(golden_copy / f"Clock-{_current_version('Clock')}")
    problems = make_golden.check(golden_copy)
    assert len(problems) == len(samples.variants()["Clock"])
    assert all("golden file is missing" in problem for problem in problems)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("version", ["0.9.0", "2.0.0", "10.0.0"])
def test_a_golden_directory_of_another_major_is_listed_and_not_decoded(
    golden_copy: Path, version: str
) -> None:
    """``make_golden``: 'A directory of another MAJOR version than the current one is not decoded
    here ... - it is listed as a note, never silently ignored.'  The current reader refuses such a
    message (ContractVersionError), so decoding it would prove nothing."""
    assert make_golden.other_major_directories() == []
    assert make_golden.other_major_directories(golden_copy) == []
    text = (golden_copy / f"Tick-{_current_version('Tick')}" / "typical.json").read_text(encoding="utf-8")
    other = text.replace(f'"schema_version":"{_current_version("Tick")}"', f'"schema_version":"{version}"')
    with pytest.raises(ContractVersionError):
        from_json(other)
    directory = golden_copy / f"Tick-{version}"
    directory.mkdir()
    (directory / "typical.json").write_bytes(other.encode("utf-8"))
    (directory / "not_even_json.json").write_bytes(b"{ this is not decoded")
    assert make_golden.other_major_directories(golden_copy) == [directory.name]
    assert make_golden.check(golden_copy) == []
    # A second contract with another MAJOR is listed too, in name order.
    (golden_copy / "Position-7.0.0").mkdir()
    assert make_golden.other_major_directories(golden_copy) == sorted([directory.name, "Position-7.0.0"])


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("version", ["1.0.1", "1.1.0", "1.0.0"])
def test_a_golden_directory_of_the_current_major_is_decoded(golden_copy: Path, version: str) -> None:
    """The counterpart: within the current MAJOR every golden file must decode, so damage is reported."""
    directory = golden_copy / f"Tick-{version}"
    directory.mkdir(exist_ok=True)
    (directory / "damaged.json").write_bytes(b'{"contract":"Tick","schema_version":"1.0.0"')
    assert directory.name not in make_golden.other_major_directories(golden_copy)
    problems = _problems_for(golden_copy, directory / "damaged.json")
    assert any("no longer decodes" in problem for problem in problems), problems


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_a_golden_file_must_be_a_message_of_the_contract_and_version_of_its_directory(
    golden_copy: Path,
) -> None:
    """A file kept as the golden message of ``<Contract>-<version>`` is the compatibility reference of
    that contract version (section 8: 'Golden messages of every released version ... must decode
    forever within their MAJOR').  A file whose envelope names another contract or another version
    proves nothing about the version its directory claims, so ``check`` must report it.
    """
    position = (golden_copy / f"Position-{_current_version('Position')}" / "open.json").read_text("utf-8")
    tick = (golden_copy / f"Tick-{_current_version('Tick')}" / "typical.json").read_text("utf-8")
    released = golden_copy / "Tick-1.0.1"
    released.mkdir()
    (released / "wrong_contract.json").write_text(position, encoding="utf-8", newline="\n")
    (released / "wrong_version.json").write_text(tick, encoding="utf-8", newline="\n")
    assert json.loads(position)["contract"] == "Position"
    assert json.loads(tick)["schema_version"] != "1.0.1"
    problems = make_golden.check(golden_copy)
    assert any(problem.startswith("Tick-1.0.1/wrong_contract.json") for problem in problems), problems
    assert any(problem.startswith("Tick-1.0.1/wrong_version.json") for problem in problems), problems


# =========================================================================== the writer
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_write_adds_missing_files_and_changes_nothing_else(golden_copy: Path, tmp_path: Path) -> None:
    assert make_golden.write(force=False, root=golden_copy) == [], "nothing to do on a consistent tree"
    directory = golden_copy / f"Tick-{_current_version('Tick')}"
    typical = directory / "typical.json"
    original = typical.read_bytes()
    typical.unlink()
    assert make_golden.write(force=False, root=golden_copy) == [f"{directory.name}/typical.json"]
    assert typical.read_bytes() == original
    assert make_golden.check(golden_copy) == []
    # An empty directory is filled completely and matches the committed tree byte for byte.
    fresh = tmp_path / "fresh"
    written = make_golden.write(force=False, root=fresh)
    assert len(written) == len(GOLDEN_FILES) + 1, "every golden message and the golden run trace"
    assert f"{make_golden.TRACE_DIR}/{make_golden.TRACE_FILE}" in written
    assert make_golden.check(fresh) == []
    for path in [*GOLDEN_FILES, GOLDEN_TRACE]:
        assert (fresh / path.relative_to(GOLDEN_DIR)).read_bytes() == path.read_bytes()
        assert b"\r" not in (fresh / path.relative_to(GOLDEN_DIR)).read_bytes(), "LF on every platform"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_write_never_rewrites_a_changed_golden_file_without_force(golden_copy: Path) -> None:
    typical = golden_copy / f"Tick-{_current_version('Tick')}" / "typical.json"
    original = typical.read_bytes()
    _rewrite(typical, lambda message: message.__setitem__("seq", 5))
    changed = typical.read_bytes()
    assert changed != original
    with pytest.raises(SystemExit) as stopped:
        make_golden.write(force=False, root=golden_copy)
    assert "differs from the sample" in str(stopped.value)
    assert typical.read_bytes() == changed, "the differing file was left alone"
    if load_lock().get("approval_status") == APPROVED:
        # After owner approval a released golden message may not be rewritten even with --force.
        with pytest.raises(SystemExit) as refused:
            make_golden.write(force=True, root=golden_copy)
        assert "may not be rewritten" in str(refused.value)
        assert typical.read_bytes() == changed
    else:
        # While the contract set is still PROPOSED, --force restores the sample bytes.
        assert make_golden.write(force=True, root=golden_copy) == [f"{typical.parent.name}/typical.json"]
        assert typical.read_bytes() == original
        assert make_golden.check(golden_copy) == []


# =========================================================================== bytes and line ends
@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    "relative",
    ["Tick-1.0.0/typical.json", "TradePlan-1.0.0/float_edge_values.json", TRACE_NAME],
)
def test_a_golden_file_with_crlf_line_ends_is_reported(golden_copy: Path, relative: str) -> None:
    """Change notice 12: golden files are read and written as bytes with LF on every platform; a file
    whose line ends were converted is a different file, not a harmless variant."""
    target = golden_copy / relative
    original = target.read_bytes()
    assert original.endswith(b"\n")
    assert b"\r" not in original
    target.write_bytes(original.replace(b"\n", b"\r\n"))
    problems = _problems_for(golden_copy, target)
    assert problems, "a CRLF golden file must be reported"
    assert all("\r" not in problem for problem in problems)
    with pytest.raises(SystemExit) as stopped:
        make_golden.write(force=False, root=golden_copy)
    assert "differs from the sample" in str(stopped.value)
    assert target.read_bytes() == original.replace(b"\n", b"\r\n"), "left alone without --force"
    if load_lock().get("approval_status") != APPROVED:
        assert make_golden.write(force=True, root=golden_copy) == [relative]
        assert target.read_bytes() == original, "rewritten with LF"
        assert make_golden.check(golden_copy) == []


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_every_committed_golden_file_uses_lf_line_ends() -> None:
    for path in [*GOLDEN_FILES, GOLDEN_TRACE]:
        data = path.read_bytes()
        assert b"\r" not in data, path
        assert data.endswith(b"\n"), path
        assert not data.endswith(b"\n\n"), path


# =========================================================================== float edge values
@pytest.mark.requirement("S00-ACC-01", "S00-ASG-14", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
def test_the_float_edge_golden_message_round_trips() -> None:
    """``TradePlan-1.0.0/float_edge_values.json`` carries a double beyond 2**63 (``net_r_stdev``,
    1e19) and one between 2**53 and 2**63 (``expected_net_r_stderr``, the double 1234567890123456768,
    written with the shortest digits as ``1234567890123456800``)."""
    path = GOLDEN_DIR / f"TradePlan-{_current_version('TradePlan')}" / "float_edge_values.json"
    data = path.read_bytes()
    assert b'"net_r_stdev":10000000000000000000,' in data
    assert b'"expected_net_r_stderr":1234567890123456800,' in data
    plan = from_json(data, expect=all_contracts()["TradePlan"])
    assert plan == samples.variants()["TradePlan"]["float_edge_values"]
    for name, double in (("net_r_stdev", 1e19), ("expected_net_r_stderr", 1.2345678901234568e18)):
        value = getattr(plan, name)
        assert type(value) is float, name
        assert value == double, name
        assert type(to_wire(plan)[name]) is float, name
    assert plan.net_r_stdev >= 2.0**63
    assert 2.0**53 < plan.expected_net_r_stderr < 2.0**63
    assert int(plan.expected_net_r_stderr) == 1234567890123456768
    assert to_bytes(plan) + b"\n" == data
    assert from_json(to_json(plan)) == plan


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-14", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"expected_net_r_stderr":1234567890123456800,', '"expected_net_r_stderr":1234567890123456768,'),
        ('"expected_net_r_stderr":1234567890123456800,', '"expected_net_r_stderr":1234567890123456801,'),
        ('"net_r_stdev":10000000000000000000,', '"net_r_stdev":10000000000000000001,'),
    ],
    ids=["exact-value-not-the-rendering", "not-a-double", "beyond-64-bit-not-a-double"],
)
def test_a_float_edge_literal_that_no_double_renders_to_is_refused_and_reported(
    golden_copy: Path, old: str, new: str
) -> None:
    target = golden_copy / f"TradePlan-{_current_version('TradePlan')}" / "float_edge_values.json"
    text = target.read_bytes().decode("utf-8")
    assert text.count(old) == 1
    target.write_bytes(text.replace(old, new).encode("utf-8"))
    with pytest.raises((ContractValidationError, CanonicalEncodingError)):
        from_json(target.read_bytes())
    assert any("no longer decodes" in problem for problem in _problems_for(golden_copy, target))


# =========================================================================== the golden run trace
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_the_golden_trace_is_the_current_encoding_of_the_sample_run() -> None:
    story = samples.run_story()
    assert len(story) == 28
    data = GOLDEN_TRACE.read_bytes()
    assert make_golden.trace_path() == GOLDEN_TRACE
    assert data == b"".join(to_bytes(record) + b"\n" for record in story)
    assert data.decode("utf-8") == make_golden.expected_trace()
    assert make_golden.trace_problems() == []
    assert make_golden.trace_problems(GOLDEN_DIR) == []


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_every_line_of_the_golden_trace_decodes_and_a_fresh_journal_accepts_them_in_order() -> None:
    from gie.contracts import MemoryJournal

    lines = GOLDEN_TRACE.read_bytes().split(b"\n")
    assert lines[-1] == b"", "the trace ends with a newline"
    records = [from_json(line) for line in lines[:-1]]
    assert records == list(samples.run_story())
    assert [to_bytes(record) for record in records] == lines[:-1], "every line re-encodes byte for byte"
    assert type(records[0]) is all_contracts()["RunManifest"], "the manifest opens the run"
    journal = MemoryJournal()
    for record in records:
        journal.publish(record)
    for record in records:
        for key in ("origin", "data_origin"):
            if hasattr(record, key):
                assert getattr(record, key).value == "SYNTHETIC_FIXTURE"


def _trace_lines(root: Path) -> list[str]:
    return (root / TRACE_NAME).read_bytes().decode("utf-8").splitlines(keepends=True)


def _store_trace(root: Path, lines: list[str]) -> None:
    (root / TRACE_NAME).write_bytes("".join(lines).encode("utf-8"))


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_trace_problems_reports_a_missing_trace(golden_copy: Path) -> None:
    assert make_golden.trace_problems(golden_copy) == []
    (golden_copy / TRACE_NAME).unlink()
    expected = [f"{TRACE_NAME}: golden trace is missing (run --write)"]
    assert make_golden.trace_problems(golden_copy) == expected
    assert make_golden.check(golden_copy) == expected
    assert make_golden.write(force=False, root=golden_copy) == [TRACE_NAME]
    assert (golden_copy / TRACE_NAME).read_bytes() == GOLDEN_TRACE.read_bytes()
    assert make_golden.check(golden_copy) == []


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_trace_problems_reports_a_changed_line(golden_copy: Path) -> None:
    lines = _trace_lines(golden_copy)
    # Still canonical JSON of a valid record that the journal accepts, but not the sample's bytes.
    record = from_json(lines[0])
    message = strict_loads(lines[0])
    message["label"] = message["label"] + "x"
    lines[0] = canonical_dumps(message) + "\n"
    assert lines[0] != to_json(record) + "\n"
    _store_trace(golden_copy, lines)
    problems = make_golden.trace_problems(golden_copy)
    assert problems[0] == (
        f"{TRACE_NAME}: the sample run no longer encodes to the golden trace (contract or sample changed)"
    )
    # The changed manifest no longer matches its run id (RUN-1): the journal refuses the first line.
    assert len(problems) == 2, problems
    assert problems[1].startswith(f"{TRACE_NAME}:1: not accepted by the journal")
    assert problems == [
        problem for problem in make_golden.check(golden_copy) if problem.startswith("traces/")
    ]


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_trace_problems_reports_a_line_order_the_journal_refuses(golden_copy: Path) -> None:
    lines = _trace_lines(golden_copy)
    # Line 4 is the candidate and line 5 the plan built on it: the plan before its candidate is refused
    # by the referential ledger of the journal.
    assert '"contract":"Candidate"' in lines[3]
    assert '"contract":"TradePlan"' in lines[4]
    lines[3], lines[4] = lines[4], lines[3]
    _store_trace(golden_copy, lines)
    problems = make_golden.trace_problems(golden_copy)
    assert len(problems) == 2, problems
    assert "no longer encodes to the golden trace" in problems[0]
    assert problems[1].startswith(f"{TRACE_NAME}:4: not accepted by the journal: ")
    assert "GIE." in problems[1], "the journal's error code is named"


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    ("damage", "line"),
    [
        (lambda lines: [*lines[:4], '{"contract":"TradePlan"\n', *lines[5:]], 5),
        (lambda lines: lines[1:], 1),
        (lambda lines: [*lines[:2], *lines[3:]], 3),
    ],
    ids=["undecodable-line", "manifest-missing", "evidence-missing"],
)
def test_trace_problems_reports_lines_the_journal_cannot_take(
    golden_copy: Path, damage: Any, line: int
) -> None:
    _store_trace(golden_copy, damage(_trace_lines(golden_copy)))
    problems = make_golden.trace_problems(golden_copy)
    assert any(
        problem.startswith(f"{TRACE_NAME}:{line}: not accepted by the journal") for problem in problems
    ), problems
    assert GOLDEN_TRACE.read_bytes() == b"".join(to_bytes(record) + b"\n" for record in samples.run_story())
