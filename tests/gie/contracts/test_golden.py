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
    path.write_text(canonical_dumps(message) + "\n", encoding="utf-8")
    return message


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
    assert {path.name for path in GOLDEN_DIR.iterdir()} == {path.parent.name for path in expected}
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
    assert to_wire(decoded) == strict_loads(text)


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
        target.write_text(original, encoding="utf-8")
    # ... and the checker reports the damaged file (shown for the first and the last optional key).
    for name in {optional[0], optional[-1]}:
        _rewrite(target, lambda message, name=name: message.pop(name))
        problems = _problems_for(golden_copy, target)
        assert any("no longer decodes" in problem and name in problem for problem in problems), name
        target.write_text(original, encoding="utf-8")
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
    target.write_text(damaged, encoding="utf-8")
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
    typical.write_text(text, encoding="utf-8")
    surplus = directory / "not_a_sample_variant.json"
    surplus.write_text(text, encoding="utf-8")
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
def test_a_golden_file_of_an_unreadable_version_is_reported(golden_copy: Path) -> None:
    """Golden files of other directories only need to decode; another MAJOR never does."""
    text = (golden_copy / f"Tick-{_current_version('Tick')}" / "typical.json").read_text(encoding="utf-8")
    old = golden_copy / "Tick-0.9.0"
    old.mkdir()
    (old / "typical.json").write_text(
        text.replace(f'"schema_version":"{_current_version("Tick")}"', '"schema_version":"0.9.0"'),
        encoding="utf-8",
    )
    problems = make_golden.check(golden_copy)
    assert len(problems) == 1
    assert problems[0].startswith("Tick-0.9.0/typical.json: no longer decodes")
    assert "GIE.CONTRACT.VERSION" in problems[0]


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
    (released / "wrong_contract.json").write_text(position, encoding="utf-8")
    (released / "wrong_version.json").write_text(tick, encoding="utf-8")
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
    assert len(written) == len(GOLDEN_FILES)
    assert make_golden.check(fresh) == []
    for path in GOLDEN_FILES:
        assert (fresh / path.relative_to(GOLDEN_DIR)).read_bytes() == path.read_bytes()


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
