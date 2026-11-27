"""Acceptance test S00-ACC-02 - published JSON Schemas, the schema lock and the generated field reference.

SYNTHETIC FIXTURE DATA ONLY.  Messages are encoded from the labelled synthetic records of
``tests/gie/samples.py``; schema directories that are edited live in pytest's temporary directory and
are copies of ``gie/contracts/schemas`` - the committed files are only ever read.

Expected behaviour: ``specifications/SHARED_CONTRACTS.md`` (revision 2) sections 6.1, 7 and 8, the
transition tables of ``specifications/contracts/FIELDS.md`` and the docstrings of
``gie.contracts.schema_gen`` / ``gie.contracts.behaviour``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from gie.contracts import (
    CANDIDATE_STATUS_TRANSITIONS,
    MODEL_STATUS_TRANSITIONS,
    ORDER_TRANSITIONS,
    PLAN_STATUS_TRANSITIONS,
    Tick,
    all_contracts,
    schema_gen,
    to_wire,
)
from gie.contracts.behaviour import behaviour
from gie.contracts.compat import Change, classify_schema_change
from gie.contracts.registry import SEMVER_PATTERN
from gie.contracts.schema_gen import (
    SCHEMA_DIR,
    check,
    json_schema,
    load_lock,
    render,
    routing_vocabulary_problems,
)
from gie.core.errors import GieError
from tests.gie import samples
from tools import render_contract_docs
from tools.write_schemas import write

pytestmark = [pytest.mark.acceptance("S00-ACC-02"), pytest.mark.requirement("S00-ACC-02", "S00-DEL-02")]

REPO_ROOT = Path(__file__).resolve().parents[3]
FIELDS_MD = REPO_ROOT / "specifications" / "contracts" / "FIELDS.md"

#: The ten shared contracts of SHARED_CONTRACTS.md section 1.
CONTRACT_NAMES = [
    "Candidate",
    "Clock",
    "Evidence",
    "ModelVersion",
    "OrderEvent",
    "Position",
    "RiskDecision",
    "RunManifest",
    "Tick",
    "TradePlan",
]
VERSION = "1.0.0"

#: SHARED_CONTRACTS.md section 6.1: contract -> (stream name, ordering time, rank).  Change notice 2:
#: ``created_time_ns`` is null in a replay, so a manifest is ordered by ``data_start_ns`` in a replay
#: and by ``created_time_ns`` in a paper run (FIELDS.md, RunManifest).
STREAM_TABLE = {
    "RunManifest": ("run_manifest/<producer>", "data_start_ns | created_time_ns", 0),
    "Clock": ("clock/<producer>", "wall_time_ns", 5),
    "Tick": ("tick/<source>/<symbol>", "receive_time_ns", 10),
    "OrderEvent": ("order_event/<producer>", "receive_time_ns", 20),
    "Position": ("position/<producer>", "decision_time_ns", 30),
    "Evidence": ("evidence/<producer>", "decision_time_ns", 40),
    "Candidate": ("candidate/<producer>", "decision_time_ns", 50),
    "TradePlan": ("trade_plan/<producer>", "decision_time_ns", 60),
    "RiskDecision": ("risk_decision/<producer>", "decision_time_ns", 70),
    "ModelVersion": ("model_version/<producer>", "status_time_ns", 90),
}

VARIANTS = [
    pytest.param(name, key, id=f"{name}:{key}") for name, group in samples.variants().items() for key in group
]


# =========================================================================== helpers
def _filename(name: str, version: str = VERSION) -> str:
    return f"{name}-{version}.schema.json"


def _published(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMA_DIR / _filename(name)).read_text(encoding="utf-8"))
    return loaded


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_lock(directory: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / "LOCK.json").read_text(encoding="utf-8"))
    return loaded


def _write_lock(directory: Path, lock: dict[str, Any]) -> None:
    (directory / "LOCK.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def _edit_lock(directory: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    lock = _read_lock(directory)
    mutate(lock)
    _write_lock(directory, lock)


def _edit_schema(
    directory: Path,
    name: str,
    mutate: Callable[[dict[str, Any]], None],
    *,
    relock: bool,
    version: str = VERSION,
) -> None:
    path = directory / _filename(name, version)
    schema = json.loads(path.read_text(encoding="utf-8"))
    mutate(schema)
    path.write_text(render(schema), encoding="utf-8", newline="\n")
    if relock:
        _edit_lock(directory, lambda lock: lock["contracts"][name].__setitem__(version, _sha256(path)))


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


@pytest.fixture
def schema_copy(tmp_path: Path) -> Path:
    """A private copy of the committed schema directory that a test may damage."""
    target = tmp_path / "schemas"
    target.mkdir()
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        (target / path.name).write_bytes(path.read_bytes())
    return target


def _markdown_rows(heading: str) -> list[list[str]]:
    text = FIELDS_MD.read_text(encoding="utf-8")
    section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    assert len(rows) > 1
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[1:]]  # skip the header


def _rows_to_table(rows: list[dict[str, Any]]) -> dict[Any, frozenset[str]]:
    table: dict[Any, frozenset[str]] = {}
    for row in rows:
        key = (row["from"], row["event"]) if "event" in row else row["from"]
        assert key not in table, f"duplicate transition row {key}"
        assert row["to"] == sorted(set(row["to"]))
        table[key] = frozenset(row["to"])
    return table


def _value(member: Any) -> Any:
    return None if member is None else member.value


# =========================================================================== the repository is consistent
def test_check_finds_no_problem_in_the_repository() -> None:
    assert check() == []
    assert check(SCHEMA_DIR) == []


def test_cli_check_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert schema_gen.main(["--check"]) == 0
    captured = capsys.readouterr()
    assert "contract schemas consistent: 10 contracts" in captured.out
    assert "PROBLEM" not in captured.err


def test_cli_check_runs_as_a_module_like_in_ci() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gie.contracts.schema_gen", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "contract schemas consistent: 10 contracts" in result.stdout


def test_cli_check_fails_when_there_are_problems(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(schema_gen, "check", lambda: ["Tick-1.0.0.schema.json: synthetic problem"])
    assert schema_gen.main(["--check"]) == 1
    captured = capsys.readouterr()
    assert "PROBLEM: Tick-1.0.0.schema.json: synthetic problem" in captured.err
    assert "consistent" not in captured.out


@pytest.mark.parametrize("argv", [[], ["--check", "--write"], ["--frobnicate"], ["check"]])
def test_cli_needs_exactly_one_known_action(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        schema_gen.main(argv)
    assert caught.value.code == 2
    capsys.readouterr()


# =========================================================================== the committed lock
def test_committed_lock_is_proposed_and_locks_all_ten_contracts_at_1_0_0() -> None:
    lock = _read_lock(SCHEMA_DIR)
    assert lock["approval_status"] == "PROPOSED"  # owner approval is still pending (review gate)
    assert sorted(lock["contracts"]) == CONTRACT_NAMES
    assert sorted(all_contracts()) == CONTRACT_NAMES
    for name in CONTRACT_NAMES:
        assert list(lock["contracts"][name]) == [VERSION]
    assert load_lock() == lock
    assert load_lock(SCHEMA_DIR) == lock


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_lock_hash_is_the_sha256_of_the_schema_file(name: str) -> None:
    digest = _read_lock(SCHEMA_DIR)["contracts"][name][VERSION]
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == _sha256(SCHEMA_DIR / _filename(name))


def test_schema_directory_holds_exactly_the_locked_schemas_and_the_lock() -> None:
    found = sorted(path.name for path in SCHEMA_DIR.iterdir() if path.name != "__pycache__")
    assert found == sorted(["LOCK.json", *(_filename(name) for name in CONTRACT_NAMES)])


def test_load_lock_of_a_directory_without_lock_is_an_empty_proposed_lock(tmp_path: Path) -> None:
    assert load_lock(tmp_path) == {"lock_version": 1, "approval_status": "PROPOSED", "contracts": {}}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_committed_schema_is_exactly_what_the_code_generates(name: str) -> None:
    generated = json_schema(all_contracts()[name])
    path = SCHEMA_DIR / _filename(name)
    assert path.read_text(encoding="utf-8") == render(generated)
    assert _published(name) == generated
    assert classify_schema_change(_published(name), generated) == (Change.NONE, [])


def test_render_is_canonical_text() -> None:
    schema = json_schema(Tick)
    text = render(schema)
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    assert json.loads(text) == schema
    shuffled = dict(reversed(list(schema.items())))
    assert render(shuffled) == text
    assert render(copy.deepcopy(schema)) == text


def test_generating_a_schema_twice_gives_equal_results() -> None:
    for record_type in all_contracts().values():
        assert json_schema(record_type) == json_schema(record_type)
        assert behaviour(record_type) == behaviour(record_type)


def test_a_generated_schema_does_not_share_state_with_the_generator() -> None:
    # A caller that edits a schema it was given (a diff tool, a test) must not change what the
    # generator publishes afterwards.
    first = json_schema(Tick)
    pristine = copy.deepcopy(first)
    identity_rules = first["x-gie-behaviour"]["identity"]
    try:
        identity_rules["zz_tampered"] = "not a rule"
        first["properties"]["bid"]["description"] = "tampered"
        first["required"].append("zz_tampered")
        first["x-gie-invariants"].clear()
        assert json_schema(Tick) == pristine
    finally:
        identity_rules.pop("zz_tampered", None)  # never leave shared state damaged for other tests


@pytest.mark.parametrize("target", [samples.evidence().levels[0], samples.trade_plan().targets[0]])
def test_only_top_level_contracts_have_a_schema(target: Any) -> None:
    with pytest.raises((ValueError, GieError), match="not a top-level contract"):
        json_schema(type(target))


# =========================================================================== content of every schema
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_generated_schema_is_valid_json_schema_2020_12(name: str) -> None:
    schema = json_schema(all_contracts()[name])
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator.check_schema(_published(name))


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_schema_envelope_and_structure(name: str) -> None:
    schema = _published(name)
    assert schema["$id"] == f"urn:gie:contract:{name}:{VERSION}"
    assert schema["title"] == f"{name} {VERSION}"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["description"].strip()
    assert schema["properties"]["contract"]["const"] == name
    assert schema["properties"]["schema_version"]["type"] == "string"
    assert schema["properties"]["schema_version"]["pattern"] == SEMVER_PATTERN
    assert schema["required"][:2] == ["contract", "schema_version"]
    assert len(schema["required"]) == len(set(schema["required"]))
    assert set(schema["required"]) <= set(schema["properties"])
    for field, fragment in schema["properties"].items():
        assert fragment.get("description", "").strip(), f"{name}.{field} has no description"
    assert {item["id"] for item in schema["x-gie-invariants"]}
    assert all(item["rule"].strip() for item in schema["x-gie-invariants"])
    assert len({item["id"] for item in schema["x-gie-invariants"]}) == len(schema["x-gie-invariants"])


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_schema_says_that_it_is_necessary_but_not_sufficient(name: str) -> None:
    comment = _published(name)["$comment"]
    assert comment.startswith("Necessary but not sufficient")
    for carried_elsewhere in ("decimal bounds", "canonical order", "invariants", "lifecycle rules", "64-bit"):
        assert carried_elsewhere in comment


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_required_lists_exactly_the_non_optional_fields(name: str) -> None:
    schema = _published(name)
    specs = all_contracts()[name].FIELD_SPECS
    assert list(schema["properties"]) != []
    assert set(schema["properties"]) == {"contract", "schema_version", *(spec.name for spec in specs)}
    expected_required = ["contract", "schema_version", *(spec.name for spec in specs if spec.required)]
    assert schema["required"] == expected_required
    for spec in specs:
        fragment = schema["properties"][spec.name]
        nullable = "anyOf" in fragment and {"type": "null"} in fragment["anyOf"]
        assert nullable is (not spec.required), f"{name}.{spec.name}"
        assert fragment["description"] == spec.doc
        assert fragment.get("x-gie-unit") == spec.unit


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_nested_objects_are_closed_and_documented(name: str) -> None:
    schema = _published(name)
    for nested_name, nested in schema.get("$defs", {}).items():
        assert nested["type"] == "object", nested_name
        assert nested["additionalProperties"] is False, nested_name
        assert set(nested["required"]) <= set(nested["properties"]), nested_name
        assert all(fragment.get("description", "").strip() for fragment in nested["properties"].values())
    text = json.dumps(schema)
    for reference in set(re.findall(r'"\$ref": "#/\$defs/(\w+)"', text)):
        assert reference in schema["$defs"]
    assert set(schema.get("$defs", {})) == set(re.findall(r'"\$ref": "#/\$defs/(\w+)"', text))


@pytest.mark.parametrize(
    ("version", "valid"),
    [
        ("1.0.0", True),
        ("0.0.0", True),
        ("10.20.30", True),
        ("999999999.999999999.999999999", True),
        ("1.0", False),
        ("v1.0.0", False),
        ("01.0.0", False),
        ("1.0.0-rc1", False),
        ("1.0.9999999999", False),
        ("", False),
        (" 1.0.0", False),
    ],
)
def test_published_schema_version_pattern(version: str, valid: bool) -> None:
    fragment = _published("Tick")["properties"]["schema_version"]
    assert jsonschema.Draft202012Validator(fragment).is_valid(version) is valid


@pytest.mark.parametrize(("name", "key"), VARIANTS)
def test_every_sample_message_satisfies_the_published_schema(name: str, key: str) -> None:
    # "Necessary": a message accepted by the reference implementation must pass the schema.
    wire = to_wire(samples.variants()[name][key])
    errors = sorted(jsonschema.Draft202012Validator(_published(name)).iter_errors(wire), key=str)
    assert errors == []


def test_sample_variants_cover_every_contract() -> None:
    assert sorted(samples.variants()) == CONTRACT_NAMES
    assert len(VARIANTS) >= 40


def _set(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda wire: wire.__setitem__(field, value)


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize(
    ("name", "damage"),
    [
        pytest.param("OrderEvent", _set("mode", "LIVE"), id="live-mode"),
        pytest.param("OrderEvent", _set("venue", "LIVE"), id="live-venue"),
        pytest.param("OrderEvent", _set("venue", "MT5"), id="real-venue"),
        pytest.param("RunManifest", _set("mode", "LIVE"), id="live-run"),
        pytest.param("Clock", _set("mode", "DEMO"), id="demo-mode"),
        pytest.param("Tick", _set("zz_unknown", 1), id="unknown-field"),
        pytest.param("Tick", lambda wire: wire.pop("bid"), id="missing-required-field"),
        pytest.param("Tick", _set("contract", "Tock"), id="other-contract-name"),
        pytest.param("Tick", _set("schema_version", "1.0"), id="malformed-version"),
        pytest.param("Tick", _set("bid", 2650.1), id="price-as-a-float"),
        pytest.param("Tick", _set("seq", -1), id="negative-seq"),
        pytest.param("Tick", _set("origin", "ZZ_UNKNOWN"), id="unknown-enum-value"),
        pytest.param(
            "Evidence", lambda wire: wire["levels"][0].__setitem__("zz", 1), id="unknown-nested-field"
        ),
    ],
)
def test_published_schema_refuses_damaged_messages(
    name: str, damage: Callable[[dict[str, Any]], Any]
) -> None:
    wire = to_wire(samples.BUILDERS[name]())
    validator = jsonschema.Draft202012Validator(_published(name))
    assert validator.is_valid(wire)
    damage(wire)
    assert not validator.is_valid(wire)


# =========================================================================== behavioural surface
@pytest.mark.requirement("S00-ASG-25")
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_behaviour_publishes_stream_ordering_time_and_rank_of_section_6_1(name: str) -> None:
    stream, ordering_time, rank = STREAM_TABLE[name]
    for published in (_published(name)["x-gie-behaviour"], behaviour(all_contracts()[name])):
        assert {"stream", "ordering_time", "rank", "identity"} <= set(published)
        assert published["stream"] == stream
        assert published["ordering_time"] == ordering_time
        assert published["rank"]["value"] == rank
        assert isinstance(published["rank"]["name"], str)
        assert published["rank"]["name"]
        assert isinstance(published["identity"], dict)
        assert published["identity"]
    alternatives = ordering_time.split(" | ")
    schema = _published(name)
    for field in alternatives:
        fragment = schema["properties"][field]
        if len(alternatives) == 1:
            assert field in schema["required"]
            assert fragment["type"] == "integer"
        else:
            # Each alternative may be null; RUN-2 makes exactly one of them present.
            assert field not in schema["required"]
            assert fragment["anyOf"][0]["type"] == "integer"
            assert {"type": "null"} in fragment["anyOf"]


@pytest.mark.requirement("S00-ASG-25")
def test_a_manifest_is_ordered_by_its_data_start_in_a_replay_and_by_its_creation_in_a_paper_run() -> None:
    from gie.contracts.ordering import ordering_time_ns

    manifests = samples.variants()["RunManifest"]
    replay, paper = manifests["replay_research"], manifests["paper_primary"]
    assert replay.created_time_ns is None
    assert replay.data_start_ns is not None
    assert ordering_time_ns(replay) == replay.data_start_ns
    assert paper.data_start_ns is None
    assert paper.created_time_ns is not None
    assert ordering_time_ns(paper) == paper.created_time_ns


@pytest.mark.requirement("S00-ASG-25")
def test_stream_ranks_are_distinct() -> None:
    ranks = [
        json_schema(record_type)["x-gie-behaviour"]["rank"]["value"]
        for record_type in all_contracts().values()
    ]
    assert sorted(ranks) == [0, 5, 10, 20, 30, 40, 50, 60, 70, 90]


@pytest.mark.requirement("S00-ASG-25")
def test_order_event_publishes_both_ranks_and_its_request_events() -> None:
    published = _published("OrderEvent")["x-gie-behaviour"]
    assert published["rank"] == {"name": "ORDER_REPORT", "value": 20}
    assert published["rank_of_requests"] == {"name": "ORDER_REQUEST", "value": 80}
    assert published["request_events"] == ["CANCEL_REQUESTED", "REPLACE_REQUESTED", "SUBMIT_REQUESTED"]
    event_types = _published("OrderEvent")["properties"]["event_type"]["enum"]
    assert published["request_events"] == sorted(
        value for value in event_types if value.endswith("_REQUESTED")
    )
    assert published["terminal_states"] == ["CANCELED", "EXPIRED", "FILLED", "REJECTED"]


def test_behaviour_is_plain_json_data() -> None:
    for record_type in all_contracts().values():
        published = behaviour(record_type)
        assert json.loads(json.dumps(published)) == published


#: Change notice 6: 'PENDING_CANCEL stays PENDING_CANCEL on a late ACCEPTED, REPLACED or
#: REPLACE_REJECTED; (PENDING_CANCEL, REJECTED) -> REJECTED'.
LATE_REPORTS_WHILE_CANCELLING = {
    ("PENDING_CANCEL", "ACCEPTED"): frozenset({"PENDING_CANCEL"}),
    ("PENDING_CANCEL", "REPLACED"): frozenset({"PENDING_CANCEL"}),
    ("PENDING_CANCEL", "REPLACE_REJECTED"): frozenset({"PENDING_CANCEL"}),
    ("PENDING_CANCEL", "REJECTED"): frozenset({"REJECTED"}),
}


def test_order_event_behaviour_carries_the_full_transition_table() -> None:
    rows = _published("OrderEvent")["x-gie-behaviour"]["transitions"]
    # 36 transitions of rework 1 plus the four of change notice 6 (late reports during a cancellation).
    assert len(rows) == len(ORDER_TRANSITIONS) == 40
    table = _rows_to_table(rows)
    for key, after in LATE_REPORTS_WHILE_CANCELLING.items():
        assert table[key] == after, key
    expected = {
        (_value(before), event.value): frozenset(state.value for state in after)
        for (before, event), after in ORDER_TRANSITIONS.items()
    }
    assert table == expected
    assert rows == sorted(rows, key=lambda row: (row["from"] or "", row["event"]))
    assert json_schema(all_contracts()["OrderEvent"])["x-gie-behaviour"]["transitions"] == rows


def test_order_event_transition_table_matches_the_field_reference() -> None:
    documented: dict[Any, frozenset[str]] = {}
    for before, event, after in _markdown_rows("Order state machine"):
        key = (None if before == "(none)" else before, event)
        assert key not in documented
        documented[key] = frozenset(state.strip() for state in after.split(","))
    assert len(documented) == 40
    for key, after in LATE_REPORTS_WHILE_CANCELLING.items():
        assert documented[key] == after, key
    assert _rows_to_table(_published("OrderEvent")["x-gie-behaviour"]["transitions"]) == documented


def test_no_published_transition_leaves_a_terminal_order_state() -> None:
    published = _published("OrderEvent")["x-gie-behaviour"]
    terminal = set(published["terminal_states"])
    states = set(_published("OrderEvent")["properties"]["order_state"]["enum"])
    events = set(_published("OrderEvent")["properties"]["event_type"]["enum"])
    assert terminal < states
    for row in published["transitions"]:
        assert row["from"] not in terminal
        assert row["from"] is None or row["from"] in states
        assert row["event"] in events
        assert row["to"]
        assert set(row["to"]) <= states
    assert {row["from"] for row in published["transitions"]} == {None, *(states - terminal)}
    assert {row["event"] for row in published["transitions"]} == events
    assert [row for row in published["transitions"] if row["from"] is None] == [
        {"from": None, "event": "SUBMIT_REQUESTED", "to": ["PENDING_SUBMIT"]}
    ]


@pytest.mark.parametrize(
    ("name", "status_field", "code_table"),
    [
        ("ModelVersion", "status", MODEL_STATUS_TRANSITIONS),
        ("TradePlan", "status", PLAN_STATUS_TRANSITIONS),
        ("Candidate", "status", CANDIDATE_STATUS_TRANSITIONS),
    ],
)
def test_status_transition_tables_are_published(
    name: str, status_field: str, code_table: dict[Any, Any]
) -> None:
    rows = _published(name)["x-gie-behaviour"]["transitions"]
    statuses = set(_published(name)["properties"][status_field]["enum"])
    table = _rows_to_table(rows)
    assert table == {
        _value(before): frozenset(s.value for s in after) for before, after in code_table.items()
    }
    assert set(table) == {None, *statuses}  # one row per status and one for "no record yet"
    assert all(after <= statuses for after in table.values())
    assert table[None], "a first record must be possible"


def test_model_status_transitions_match_the_field_reference() -> None:
    documented: dict[Any, frozenset[str]] = {}
    for before, after in _markdown_rows("Model status transitions"):
        key = None if before == "(unregistered)" else before
        states = frozenset() if after == "(terminal)" else frozenset(s.strip() for s in after.split(","))
        documented[key] = states
    assert _rows_to_table(_published("ModelVersion")["x-gie-behaviour"]["transitions"]) == documented
    # G07-09 / section 9.2: a model reaches ACTIVE only through SHADOW (or back from RETIRED by rollback).
    assert {before for before, after in documented.items() if "ACTIVE" in after} == {"SHADOW", "RETIRED"}
    assert documented[None] == {"RESEARCH"}


def test_final_plan_and_candidate_statuses_are_final() -> None:
    plan = _rows_to_table(_published("TradePlan")["x-gie-behaviour"]["transitions"])
    candidate = _rows_to_table(_published("Candidate")["x-gie-behaviour"]["transitions"])
    for final in ("WITHDRAWN", "EXPIRED", "REJECTED"):
        assert plan[final] == frozenset()
        assert candidate[final] == frozenset()
    assert "PREPARED" not in plan["ARMED"]
    assert "PREPARED" not in plan["SELECTED"]
    assert "ARMED" not in plan["SELECTED"]


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        ("OrderEvent", ["immutable_fields", "replaceable_fields"]),
        ("Position", ["immutable_fields", "immutable_fields_gie_owner"]),
        ("ModelVersion", ["immutable_fields"]),
    ],
)
def test_immutable_and_replaceable_fields_are_fields_of_the_contract(name: str, keys: list[str]) -> None:
    schema = _published(name)
    seen: list[str] = []
    for key in keys:
        fields = schema["x-gie-behaviour"][key]
        assert fields, key
        assert set(fields) <= set(schema["properties"]), key
        seen += fields
    assert len(seen) == len(set(seen)), "a field is listed twice or is both immutable and replaceable"


@pytest.mark.requirement("G08-03", "G08-10")
def test_run_mode_and_venue_of_an_order_can_never_change() -> None:
    immutable = _published("OrderEvent")["x-gie-behaviour"]["immutable_fields"]
    assert {"client_order_id", "run_id", "mode", "venue", "symbol", "side", "intent"} <= set(immutable)


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    ("name", "identifiers"),
    [
        ("RunManifest", ["run_id"]),
        ("Evidence", ["market_event_id", "evidence_id"]),
        ("Candidate", ["opportunity_id", "candidate_id"]),
        ("TradePlan", ["plan_id", "alternative_group_id"]),
        ("RiskDecision", ["decision_id", "reservation_id"]),
        ("OrderEvent", ["client_order_id", "order_event_id"]),
        ("Position", ["position_id"]),
        ("ModelVersion", ["model_version_id"]),
    ],
)
def test_identity_rule_of_every_verified_identifier_is_published(name: str, identifiers: list[str]) -> None:
    # Section 4: the table of identity parts "is published in every schema under x-gie-behaviour".
    schema = _published(name)
    rules = schema["x-gie-behaviour"]["identity"]
    for identifier in identifiers:
        assert identifier in schema["properties"]
        assert rules[identifier].strip()
        assert "(" in rules[identifier]


# =========================================================================== routing vocabulary
@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_published_run_mode_and_venue_vocabulary_is_the_locked_one(name: str) -> None:
    schema = _published(name)
    assert routing_vocabulary_problems(name, schema) == []
    if "mode" in schema["properties"]:
        assert schema["properties"]["mode"] == {
            "type": "string",
            "enum": ["REPLAY", "PAPER"],
            "description": schema["properties"]["mode"]["description"],
        }
    if "venue" in schema["properties"]:
        assert schema["properties"]["venue"]["enum"] == ["SIMULATED"]

    def enums(node: Any) -> list[list[Any]]:
        if isinstance(node, dict):
            own = [node["enum"]] if isinstance(node.get("enum"), list) else []
            return own + [found for value in node.values() for found in enums(value)]
        if isinstance(node, list):
            return [found for value in node for found in enums(value)]
        return []

    for values in enums(schema["properties"]) + enums(schema.get("$defs", {})):
        assert not {"LIVE", "REAL", "DEMO", "PRODUCTION"} & {str(value).upper() for value in values}


def test_run_mode_is_carried_by_every_run_scoped_contract() -> None:
    with_mode = [name for name in CONTRACT_NAMES if "mode" in _published(name)["properties"]]
    assert with_mode == [name for name in CONTRACT_NAMES if name not in ("Tick", "ModelVersion")]
    assert [name for name in CONTRACT_NAMES if "venue" in _published(name)["properties"]] == ["OrderEvent"]


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize(
    ("field", "fragment"),
    [
        pytest.param("mode", {"type": "string", "enum": ["REPLAY", "PAPER", "LIVE"]}, id="mode-LIVE-added"),
        pytest.param("mode", {"type": "string", "enum": ["LIVE", "REPLAY", "PAPER"]}, id="mode-LIVE-first"),
        pytest.param("mode", {"type": "string", "enum": ["REPLAY"]}, id="mode-value-removed"),
        pytest.param("mode", {"type": "string", "enum": ["PAPER", "REPLAY"]}, id="mode-reordered"),
        pytest.param("mode", {"type": "string", "enum": ["REPLAY", "PAPER", "PAPER"]}, id="mode-duplicate"),
        pytest.param("mode", {"type": "string", "enum": []}, id="mode-empty"),
        pytest.param("mode", {"type": "string"}, id="mode-open-string"),
        pytest.param("mode", {"$ref": "#/$defs/RunMode"}, id="mode-behind-a-reference"),
        pytest.param(
            "mode",
            {"anyOf": [{"type": "string", "enum": ["REPLAY", "PAPER", "LIVE"]}, {"type": "null"}]},
            id="mode-optional",
        ),
        pytest.param("mode", {"const": "LIVE"}, id="mode-const"),
        pytest.param("venue", {"type": "string", "enum": ["SIMULATED", "LIVE"]}, id="venue-LIVE-added"),
        pytest.param("venue", {"type": "string", "enum": ["MT5"]}, id="venue-replaced"),
        pytest.param("venue", {"type": "string", "enum": []}, id="venue-empty"),
        pytest.param("venue", {"type": "string"}, id="venue-open-string"),
    ],
)
def test_routing_vocabulary_problems_reports_every_deviation(field: str, fragment: dict[str, Any]) -> None:
    schema = copy.deepcopy(_published("OrderEvent"))
    schema["properties"][field] = fragment
    problems = routing_vocabulary_problems("OrderEvent", schema)
    assert len(problems) == 1
    assert problems[0].startswith("OrderEvent: routing-lock vocabulary changed")
    assert field in problems[0]
    other = "venue" if field == "mode" else "mode"
    assert f"{other} must be" not in problems[0]


@pytest.mark.requirement("G08-03", "G08-10")
def test_routing_vocabulary_problems_checks_a_newly_introduced_field_too() -> None:
    schema = copy.deepcopy(_published("TradePlan"))
    assert "venue" not in schema["properties"]
    schema["properties"]["venue"] = {"anyOf": [{"enum": ["SIMULATED", "LIVE"]}, {"type": "null"}]}
    assert len(routing_vocabulary_problems("TradePlan", schema)) == 1
    both = copy.deepcopy(_published("OrderEvent"))
    both["properties"]["mode"]["enum"].append("LIVE")
    both["properties"]["venue"]["enum"].append("LIVE")
    assert len(routing_vocabulary_problems("OrderEvent", both)) == 2
    assert routing_vocabulary_problems("Tick", _published("Tick")) == []
    assert routing_vocabulary_problems("Empty", {}) == []


# =========================================================================== check() on damaged copies
def test_untouched_copy_of_the_schema_directory_is_consistent(schema_copy: Path) -> None:
    assert check(schema_copy) == []
    assert _snapshot(schema_copy) == {
        path.name: path.read_bytes() for path in sorted(SCHEMA_DIR.glob("*.json"))
    }


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda text: text.replace("Best bid.", "Best bid, reworded."), id="description"),
        pytest.param(lambda text: text + "\n", id="trailing-newline"),
        pytest.param(lambda text: text.replace("\n", "\r\n"), id="line-endings"),
        pytest.param(lambda text: text.replace('"minimum": 0', '"minimum": 1', 1), id="bound"),
        pytest.param(lambda text: json.dumps(json.loads(text), sort_keys=True), id="reformatted"),
    ],
)
def test_edited_released_schema_is_reported(schema_copy: Path, edit: Callable[[str], str]) -> None:
    path = schema_copy / _filename("Tick")
    original = path.read_bytes().decode("utf-8")
    edited = edit(original)
    assert edited != original
    path.write_bytes(edited.encode("utf-8"))
    problems = check(schema_copy)
    assert f"{path.name}: released schema was modified (hash differs from LOCK.json)" in problems
    assert all(problem.startswith(path.name) for problem in problems)


def test_edit_with_a_recomputed_lock_hash_is_still_reported(schema_copy: Path) -> None:
    def reword(schema: dict[str, Any]) -> None:
        schema["properties"]["bid"]["description"] = "Best bid, reworded."

    _edit_schema(schema_copy, "Tick", reword, relock=True)
    problems = check(schema_copy)
    assert len(problems) == 1
    assert problems[0].startswith("Tick-1.0.0.schema.json: committed schema differs from the code")


@pytest.mark.parametrize("name", ["Tick", "OrderEvent", "RunManifest"])
def test_missing_schema_file_is_reported(schema_copy: Path, name: str) -> None:
    (schema_copy / _filename(name)).unlink()
    problems = check(schema_copy)
    assert f"{_filename(name)}: locked schema file is missing" in problems
    assert f"{_filename(name)}: schema of the current version is not published" in problems
    assert all(problem.startswith(_filename(name)) for problem in problems)


@pytest.mark.parametrize(
    "extra",
    ["Extra.json", "Tick-0.9.0.schema.json", "Tick-1.0.1.schema.json", "Tock-1.0.0.schema.json", "lock.json"],
)
def test_unexpected_json_file_is_reported(schema_copy: Path, extra: str) -> None:
    replaces_the_lock = (schema_copy / extra).exists()  # "lock.json" on a file system that ignores case
    (schema_copy / extra).write_text("{}\n", encoding="utf-8", newline="\n")
    problems = check(schema_copy)
    if replaces_the_lock:
        # There the name is the lock itself: the write destroyed the lock, and that is reported.
        assert problems
        assert all(problem.startswith("LOCK.json: ") for problem in problems)
    else:
        assert problems == [f"{extra}: unexpected file in the schema directory"]


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize(
    ("name", "field", "values"),
    [
        ("OrderEvent", "mode", ["REPLAY", "PAPER", "LIVE"]),
        ("OrderEvent", "venue", ["SIMULATED", "LIVE"]),
        ("OrderEvent", "venue", ["LIVE"]),
        ("RunManifest", "mode", ["REPLAY", "PAPER", "LIVE"]),
        ("RiskDecision", "mode", ["PAPER", "REPLAY"]),
        ("Clock", "mode", ["REPLAY"]),
    ],
)
def test_changed_routing_vocabulary_is_reported_although_the_lock_is_only_proposed(
    schema_copy: Path, name: str, field: str, values: list[str]
) -> None:
    assert _read_lock(schema_copy)["approval_status"] == "PROPOSED"
    _edit_schema(
        schema_copy, name, lambda schema: schema["properties"][field].__setitem__("enum", values), relock=True
    )
    problems = check(schema_copy)
    routing = [problem for problem in problems if "routing-lock vocabulary changed" in problem]
    assert len(routing) == 1
    assert routing[0].startswith(_filename(name))
    assert field in routing[0]
    assert str(values) in routing[0]


@pytest.mark.parametrize(
    "status", ["DRAFT", "approved", "Approved", "PROPOSED ", "", None, 1, True, ["APPROVED"]]
)
def test_invalid_approval_status_is_reported(schema_copy: Path, status: Any) -> None:
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", status))
    assert check(schema_copy) == ["LOCK.json: approval_status must be PROPOSED or APPROVED"]


def test_missing_approval_status_is_reported(schema_copy: Path) -> None:
    _edit_lock(schema_copy, lambda lock: lock.pop("approval_status"))
    assert check(schema_copy) == ["LOCK.json: approval_status must be PROPOSED or APPROVED"]


def test_approved_lock_with_unchanged_schemas_is_consistent(schema_copy: Path) -> None:
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", "APPROVED"))
    assert check(schema_copy) == []


def test_missing_lock_file_leaves_every_contract_unlocked(schema_copy: Path) -> None:
    (schema_copy / "LOCK.json").unlink()
    problems = check(schema_copy)
    assert sorted(problems) == sorted(f"LOCK.json: {name} {VERSION} is not locked" for name in CONTRACT_NAMES)


def test_contract_missing_from_the_lock_is_reported(schema_copy: Path) -> None:
    _edit_lock(schema_copy, lambda lock: lock["contracts"].pop("Tick"))
    assert check(schema_copy) == ["LOCK.json: Tick 1.0.0 is not locked"]


def test_contract_that_disappeared_from_the_code_is_reported(schema_copy: Path) -> None:
    ghost = schema_copy / _filename("Ghost")
    ghost.write_text("{}\n", encoding="utf-8", newline="\n")
    _edit_lock(schema_copy, lambda lock: lock["contracts"].__setitem__("Ghost", {VERSION: _sha256(ghost)}))
    problems = check(schema_copy)
    assert any(
        problem.startswith("LOCK.json: contract Ghost is locked but no longer registered")
        for problem in problems
    )


def test_locked_version_newer_than_the_code_is_reported(schema_copy: Path) -> None:
    newer = schema_copy / _filename("Tick", "1.1.0")
    newer.write_bytes((schema_copy / _filename("Tick")).read_bytes())
    _edit_lock(schema_copy, lambda lock: lock["contracts"]["Tick"].__setitem__("1.1.0", _sha256(newer)))
    assert check(schema_copy) == ["Tick: locked version 1.1.0 is newer than the code (1.0.0)"]


# =========================================================================== write()
def test_write_into_an_empty_directory_reproduces_the_committed_files_byte_for_byte(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    written = write(target)
    assert sorted(written) == sorted(["LOCK.json", *(_filename(name) for name in CONTRACT_NAMES)])
    assert written[-1] == "LOCK.json"
    assert _snapshot(target) == {path.name: path.read_bytes() for path in sorted(SCHEMA_DIR.glob("*.json"))}
    assert check(target) == []


def test_write_is_idempotent_and_creates_missing_directories(tmp_path: Path) -> None:
    target = tmp_path / "does" / "not" / "exist" / "yet"
    assert len(write(target)) == 11
    before = _snapshot(target)
    assert write(target) == []
    assert _snapshot(target) == before


def test_write_corrects_a_schema_in_place_while_the_lock_is_proposed(schema_copy: Path) -> None:
    # Section 8: while the status is PROPOSED version 1.0.0 may still be corrected in place.
    _edit_schema(schema_copy, "Tick", lambda schema: schema["properties"].pop("ask_volume"), relock=True)
    assert check(schema_copy) != []
    assert sorted(write(schema_copy)) == ["LOCK.json", _filename("Tick")]
    assert check(schema_copy) == []
    assert _snapshot(schema_copy) == {
        path.name: path.read_bytes() for path in sorted(SCHEMA_DIR.glob("*.json"))
    }


@pytest.mark.parametrize("name", ["OrderEvent", "Tick", "RunManifest"])
def test_write_refuses_to_overwrite_a_changed_schema_once_the_lock_is_approved(
    schema_copy: Path, name: str
) -> None:
    # The released schema (file and lock agree) differs from what the code generates now.
    _edit_schema(schema_copy, name, lambda schema: schema["properties"].pop("seq"), relock=True)
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", "APPROVED"))
    before = _snapshot(schema_copy)
    with pytest.raises(SystemExit) as caught:
        write(schema_copy)
    message = str(caught.value)
    assert _filename(name) in message
    assert "bump the version" in message
    assert caught.value.code not in (0, None)
    assert _snapshot(schema_copy) == before  # nothing was overwritten, the lock is untouched


def test_write_refuses_when_only_the_locked_hash_disagrees_with_the_code(schema_copy: Path) -> None:
    _edit_lock(schema_copy, lambda lock: lock["contracts"]["Tick"].__setitem__(VERSION, "0" * 64))
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", "APPROVED"))
    before = _snapshot(schema_copy)
    with pytest.raises(SystemExit):
        write(schema_copy)
    assert _snapshot(schema_copy) == before


def test_write_with_an_approved_lock_and_unchanged_contracts_changes_nothing(schema_copy: Path) -> None:
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", "APPROVED"))
    before = _snapshot(schema_copy)
    assert write(schema_copy) == []
    assert _snapshot(schema_copy) == before
    assert _read_lock(schema_copy)["approval_status"] == "APPROVED"


# --------------------------------------------------------------------------- new version beside the old
@pytest.mark.parametrize("new_version", ["1.0.1", "1.1.0", "2.0.0"])
def test_version_bump_adds_a_file_and_leaves_the_released_schema_untouched(
    schema_copy: Path, monkeypatch: pytest.MonkeyPatch, new_version: str
) -> None:
    _edit_lock(schema_copy, lambda lock: lock.__setitem__("approval_status", "APPROVED"))
    released = _snapshot(schema_copy)
    monkeypatch.setattr(Tick, "VERSION", new_version)  # the code declares a new version (restored afterwards)
    written = write(schema_copy)
    assert sorted(written) == ["LOCK.json", _filename("Tick", new_version)]
    after = _snapshot(schema_copy)
    for filename, content in released.items():
        if filename != "LOCK.json":
            assert after[filename] == content
    lock = _read_lock(schema_copy)
    assert lock["approval_status"] == "APPROVED"
    assert lock["contracts"]["Tick"] == {
        VERSION: _sha256(schema_copy / _filename("Tick")),
        new_version: _sha256(schema_copy / _filename("Tick", new_version)),
    }
    new_schema = json.loads(after[_filename("Tick", new_version)])
    assert new_schema["$id"] == f"urn:gie:contract:Tick:{new_version}"
    assert check(schema_copy) == []


def _released_had_extra_optional_field(schema: dict[str, Any]) -> None:
    schema["properties"]["zz_legacy"] = {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "x"}


def _released_lacked_a_flag(schema: dict[str, Any]) -> None:
    schema["properties"]["flags"]["items"]["enum"].remove("OUTLIER")


def _released_had_other_wording(schema: dict[str, Any]) -> None:
    schema["properties"]["bid"]["description"] = "Older wording."


def _released_had_live_free_but_other_rank(schema: dict[str, Any]) -> None:
    schema["x-gie-behaviour"]["rank"]["value"] = 11


@pytest.mark.parametrize(
    ("released_was", "new_version", "expected"),
    [
        pytest.param(_released_had_other_wording, "1.0.1", None, id="patch-for-wording"),
        pytest.param(_released_lacked_a_flag, "1.1.0", None, id="minor-for-new-enum-value"),
        pytest.param(
            _released_lacked_a_flag, "1.0.1", "requires a MINOR bump", id="patch-for-new-enum-value"
        ),
        pytest.param(
            _released_had_extra_optional_field, "1.1.0", "requires a MAJOR bump", id="minor-removes-field"
        ),
        pytest.param(
            _released_had_extra_optional_field, "1.0.1", "requires a MAJOR bump", id="patch-removes-field"
        ),
        pytest.param(
            _released_had_live_free_but_other_rank, "1.4.0", "requires a MAJOR bump", id="minor-moves-rank"
        ),
        pytest.param(_released_had_extra_optional_field, "2.0.0", None, id="major-removes-field"),
        pytest.param(_released_had_live_free_but_other_rank, "2.0.0", None, id="major-moves-rank"),
    ],
)
def test_difference_to_the_released_version_must_fit_the_declared_bump(
    schema_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_was: Callable[[dict[str, Any]], None],
    new_version: str,
    expected: str | None,
) -> None:
    # The released 1.0.0 schema of the copy is replaced by a (synthetic) different one, consistently
    # locked; the code then claims to be `new_version`.
    _edit_schema(schema_copy, "Tick", released_was, relock=True)
    monkeypatch.setattr(Tick, "VERSION", new_version)
    write(schema_copy)
    problems = check(schema_copy)
    if expected is None:
        assert problems == []
    else:
        assert len(problems) == 1
        assert problems[0].startswith("Tick: schema change")
        assert expected in problems[0]
        assert f"1.0.0 -> {new_version}" in problems[0]


# =========================================================================== generated field reference
def test_field_reference_is_current(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_contract_docs.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert FIELDS_MD == render_contract_docs.OUTPUT
    assert FIELDS_MD.read_text(encoding="utf-8") == render_contract_docs.render()


def test_field_reference_check_detects_a_stale_or_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "FIELDS.md"
    monkeypatch.setattr(render_contract_docs, "OUTPUT", stale)
    assert render_contract_docs.main(["--check"]) == 1  # missing
    stale.write_text(
        render_contract_docs.render().replace("OrderEvent", "OrderEvnt", 1), encoding="utf-8", newline="\n"
    )
    assert render_contract_docs.main(["--check"]) == 1  # stale
    assert "stale" in capsys.readouterr().err
    assert render_contract_docs.main([]) == 0  # regenerate into the temporary file
    assert stale.read_bytes() == FIELDS_MD.read_bytes()
    assert render_contract_docs.main(["--check"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_field_reference_lists_every_field_and_invariant_of_the_schema(name: str) -> None:
    text = FIELDS_MD.read_text(encoding="utf-8")
    assert "GENERATED" in text.splitlines()[2]
    section = text.split(f"## {name} {VERSION}\n", 1)[1].split("\n## ", 1)[0]
    schema = _published(name)
    for field in schema["properties"]:
        if field not in ("contract", "schema_version"):
            required = "yes" if field in schema["required"] else "no"
            assert re.search(rf"^\| `{field}` \| .* \| {required} \| ", section, flags=re.MULTILINE), field
    for item in schema["x-gie-invariants"]:
        assert f"- **{item['id']}** - {item['rule']}" in section


# =========================================================================== rework 2 (change notice 12)
def _objects(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [("#", schema), *((f"#/$defs/{name}", node) for name, node in schema.get("$defs", {}).items())]


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_every_schema_object_states_min_properties_equal_to_its_declared_properties(name: str) -> None:
    """'Every schema object states minProperties = number of declared properties (every key is
    written, optional ones as null).'"""
    for schema in (_published(name), json_schema(all_contracts()[name])):
        for where, node in _objects(schema):
            assert node["type"] == "object", where
            assert node["additionalProperties"] is False, where
            assert type(node["minProperties"]) is int, where
            assert node["minProperties"] == len(node["properties"]), where
    assert _published(name)["minProperties"] == 2 + len(all_contracts()[name].FIELD_SPECS)


def _without_optional_key(name: str) -> tuple[dict[str, Any], str]:
    """A sample message of *name* with one optional key removed, and the removed path."""
    wire = to_wire(samples.BUILDERS[name]())
    optional = [spec.name for spec in all_contracts()[name].FIELD_SPECS if not spec.required]
    assert optional, name
    del wire[optional[-1]]
    return wire, optional[-1]


@pytest.mark.parametrize("name", [name for name in CONTRACT_NAMES if name != "Clock"])
def test_a_message_without_an_optional_key_is_refused_by_decoder_and_schema(name: str) -> None:
    from gie.contracts import from_wire
    from gie.core.errors import ContractValidationError

    complete = to_wire(samples.BUILDERS[name]())
    validator = jsonschema.Draft202012Validator(_published(name))
    assert validator.is_valid(complete)
    assert from_wire(complete) == samples.BUILDERS[name]()
    wire, missing = _without_optional_key(name)
    errors = list(validator.iter_errors(wire))
    assert [error.validator for error in errors] == ["minProperties"], [error.message for error in errors]
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert type(caught.value) is ContractValidationError
    assert caught.value.details["path"] == missing
    assert "explicit null" in caught.value.details["rule"]


def test_a_nested_object_without_an_optional_key_is_refused_by_decoder_and_schema() -> None:
    from gie.contracts import from_wire
    from gie.core.errors import ContractValidationError

    wire = to_wire(samples.trade_plan())
    assert "scenario" in wire["targets"][0]
    validator = jsonschema.Draft202012Validator(_published("TradePlan"))
    assert validator.is_valid(wire)
    del wire["targets"][0]["scenario"]
    errors = list(validator.iter_errors(wire))
    assert [error.validator for error in errors] == ["minProperties"]
    assert list(errors[0].absolute_path) == ["targets", 0]
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == "targets[0].scenario"


def test_the_clock_contract_without_any_key_is_refused_by_the_schema() -> None:
    """Clock has no optional field; a missing key is a missing required key (and below minProperties)."""
    wire = to_wire(samples.BUILDERS["Clock"]())
    first = all_contracts()["Clock"].FIELD_SPECS[0].name
    del wire[first]
    validators = {
        error.validator for error in jsonschema.Draft202012Validator(_published("Clock")).iter_errors(wire)
    }
    assert validators == {"required", "minProperties"}


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_behaviour_returns_a_fresh_copy_on_every_call(name: str) -> None:
    record_type = all_contracts()[name]
    pristine = copy.deepcopy(behaviour(record_type))
    first = behaviour(record_type)
    assert first == pristine
    assert first is not behaviour(record_type)
    # Damage every container of the copy that was handed out.
    first["identity"]["zz_tampered"] = "not a rule"
    first["rank"]["value"] = -1
    for value in list(first.values()):
        if isinstance(value, list):
            value.append({"zz": "tampered"})
            if value and isinstance(value[0], dict):
                value[0]["zz"] = "tampered"
    first["zz_new_key"] = True
    assert behaviour(record_type) == pristine
    assert json_schema(record_type)["x-gie-behaviour"] == pristine
    assert _published(name)["x-gie-behaviour"] == pristine


def test_a_line_end_conversion_is_reported_even_with_a_recomputed_lock_hash(schema_copy: Path) -> None:
    """The lock hashes the bytes; the published file is LF on every platform."""
    path = schema_copy / _filename("Tick")
    original = path.read_bytes()
    assert b"\r" not in original
    path.write_bytes(original.replace(b"\n", b"\r\n"))
    assert json.loads(path.read_bytes()) == json.loads(original), "the same JSON value ..."
    problems = check(schema_copy)
    assert f"{path.name}: released schema was modified (hash differs from LOCK.json)" in problems
    assert all(problem.startswith(path.name) for problem in problems)
    _edit_lock(schema_copy, lambda lock: lock["contracts"]["Tick"].__setitem__(VERSION, _sha256(path)))
    problems = check(schema_copy)
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{path.name}: committed schema differs from the code")
    # While the lock is PROPOSED, write() restores the LF bytes and the lock.
    assert sorted(write(schema_copy)) == ["LOCK.json", path.name]
    assert path.read_bytes() == original
    assert check(schema_copy) == []


def test_committed_schema_files_and_the_lock_use_lf_line_ends() -> None:
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        data = path.read_bytes()
        assert b"\r" not in data, path.name
        assert data.endswith(b"}\n"), path.name


def test_published_identity_rule_of_the_run_id_is_the_manifest_hash() -> None:
    """Change notice 2 / 10: ``run_id`` is ``identity.run_id(mode, manifest_sha256)``, the hash of every
    other field (FIELDS.md RUN-1) - no longer ``run_id(mode, config_sha256, label)``."""
    rule = _published("RunManifest")["x-gie-behaviour"]["identity"]["run_id"]
    assert rule.startswith("run_id(mode, "), rule
    assert "config_sha256" not in rule, rule
    assert "label" not in rule, rule


def test_published_position_monotonic_rules_allow_growth_while_the_entry_fills() -> None:
    """Change notice 7: 'initial_volume_lots may grow while the entry order fills, the open volume grows by
    no more than that' - a published rule 'volume_lots never increases' contradicts it."""
    rules = _published("Position")["x-gie-behaviour"]["monotonic_gie_owner"]
    assert "volume_lots never increases" not in rules, rules
