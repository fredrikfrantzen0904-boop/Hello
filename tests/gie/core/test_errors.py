"""Independent tests for ``gie.core.errors`` (requirement S00-ASG-24, "error semantics").

The expected taxonomy is the one documented in ``specifications/SHARED_CONTRACTS.md`` section 5
and listed in ``specifications/contracts/FIELDS.md`` ("Error codes").  The codes are part of the
shared contract surface ("changing or removing a code is a breaking change"), so they are pinned
here literally: a renamed or re-coded error must fail this file.

All messages and details used here are synthetic fixtures.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from gie.core import errors as errors_module
from gie.core.canonical import canonical_dumps, strict_loads
from gie.core.errors import (
    ALL_ERROR_TYPES,
    ERROR_CODES,
    CanonicalEncodingError,
    ClockRegressionError,
    ConfigError,
    ContractError,
    ContractValidationError,
    ContractVersionError,
    FutureDataError,
    GieError,
    IllegalTransitionError,
    LiveRoutingLockedError,
    OutOfOrderError,
    SequenceConflictError,
    SequenceError,
    SequenceGapError,
    StaleDataError,
    TimeError,
    UnknownContractError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIELDS_REFERENCE = REPO_ROOT / "specifications" / "contracts" / "FIELDS.md"

CODE_PATTERN = re.compile(r"^GIE(\.[A-Z_]+)+$")

#: class -> (pinned stable code, documented direct base class)
DOCUMENTED: dict[type[GieError], tuple[str, type[BaseException]]] = {
    GieError: ("GIE.ERROR", Exception),
    ContractError: ("GIE.CONTRACT.ERROR", GieError),
    ContractValidationError: ("GIE.CONTRACT.INVALID", ContractError),
    UnknownContractError: ("GIE.CONTRACT.UNKNOWN", ContractError),
    ContractVersionError: ("GIE.CONTRACT.VERSION", ContractError),
    CanonicalEncodingError: ("GIE.CONTRACT.ENCODING", ContractError),
    TimeError: ("GIE.TIME.ERROR", GieError),
    StaleDataError: ("GIE.TIME.STALE", TimeError),
    FutureDataError: ("GIE.TIME.FUTURE", TimeError),
    ClockRegressionError: ("GIE.TIME.CLOCK_REGRESSION", TimeError),
    SequenceError: ("GIE.SEQ.ERROR", GieError),
    OutOfOrderError: ("GIE.SEQ.OUT_OF_ORDER", SequenceError),
    SequenceConflictError: ("GIE.SEQ.CONFLICT", SequenceError),
    SequenceGapError: ("GIE.SEQ.GAP", SequenceError),
    IllegalTransitionError: ("GIE.STATE.ILLEGAL_TRANSITION", GieError),
    LiveRoutingLockedError: ("GIE.ROUTING.LIVE_LOCKED", GieError),
    ConfigError: ("GIE.CONFIG.INVALID", GieError),
}

_ALL = pytest.mark.parametrize("error_type", ALL_ERROR_TYPES, ids=lambda error_type: error_type.__name__)


# =========================================================================== the register
@pytest.mark.requirement("S00-ASG-24")
def test_all_error_types_is_exactly_the_documented_taxonomy() -> None:
    assert isinstance(ALL_ERROR_TYPES, tuple)
    assert len(ALL_ERROR_TYPES) == len(set(ALL_ERROR_TYPES)) == 17
    assert set(ALL_ERROR_TYPES) == set(DOCUMENTED)


@pytest.mark.requirement("S00-ASG-24")
def test_every_error_class_of_the_module_is_registered() -> None:
    """No GieError subclass may exist in the module without being listed (and thus coded)."""
    defined = {
        member
        for _, member in inspect.getmembers(errors_module, inspect.isclass)
        if issubclass(member, GieError) and member.__module__ == errors_module.__name__
    }
    assert defined == set(ALL_ERROR_TYPES)


@pytest.mark.requirement("S00-ASG-24")
def test_no_gie_error_subclass_is_defined_outside_the_register() -> None:
    """Every error class of the ``gie`` package is defined in ``gie.core.errors``.

    A subclass defined elsewhere would carry a code that is not in ``ERROR_CODES`` and not in the
    published list.  The sources are scanned statically so that the result does not depend on
    which modules happen to be imported.
    """
    known = {error_type.__name__ for error_type in ALL_ERROR_TYPES}
    errors_file = Path(inspect.getfile(errors_module)).resolve()
    sources = sorted(path for path in (REPO_ROOT / "gie").rglob("*.py") if path.resolve() != errors_file)
    assert sources, "the gie package sources were not found"
    outside: set[str] = set()
    changed = True
    while changed:  # fixpoint: also catches subclasses of subclasses
        changed = False
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                base_names = {
                    base.id if isinstance(base, ast.Name) else base.attr
                    for base in node.bases
                    if isinstance(base, (ast.Name, ast.Attribute))
                }
                label = f"{path.relative_to(REPO_ROOT)}::{node.name}"
                if base_names & (known | {name.split("::")[1] for name in outside}) and label not in outside:
                    outside.add(label)
                    changed = True
    assert outside == set()


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_code_is_pinned_well_formed_and_defined_on_the_class_itself(error_type: type[GieError]) -> None:
    expected_code, _ = DOCUMENTED[error_type]
    assert error_type.code == expected_code
    assert isinstance(error_type.code, str)
    assert CODE_PATTERN.fullmatch(error_type.code)
    assert "code" in vars(error_type), "a class must not silently inherit its parent's code"
    assert error_type("synthetic").code == expected_code


@pytest.mark.requirement("S00-ASG-24")
def test_codes_are_unique() -> None:
    codes = [error_type.code for error_type in ALL_ERROR_TYPES]
    assert len(codes) == len(set(codes))


@pytest.mark.requirement("S00-ASG-24")
def test_error_codes_maps_every_code_back_to_its_class() -> None:
    assert len(ERROR_CODES) == len(ALL_ERROR_TYPES)
    assert set(ERROR_CODES) == {code for code, _ in DOCUMENTED.values()}
    for error_type in ALL_ERROR_TYPES:
        assert ERROR_CODES[error_type.code] is error_type
    for code, error_type in ERROR_CODES.items():
        assert error_type.code == code
        assert DOCUMENTED[error_type][0] == code


@pytest.mark.requirement("S00-ASG-24")
def test_codes_match_the_published_field_reference() -> None:
    """``FIELDS.md`` is the published list of codes; it must agree with the code, both ways."""
    text = FIELDS_REFERENCE.read_text(encoding="utf-8")
    section = text.split("## Error codes", 1)[1]
    published = dict(re.findall(r"^\| `(GIE\.[A-Z_.]+)` \| `(\w+)` \|", section, flags=re.MULTILINE))
    assert published == {code: error_type.__name__ for error_type, (code, _) in DOCUMENTED.items()}
    assert published == {code: error_type.__name__ for code, error_type in ERROR_CODES.items()}


# =========================================================================== hierarchy
@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_documented_base_class(error_type: type[GieError]) -> None:
    _, expected_base = DOCUMENTED[error_type]
    assert error_type.__bases__ == (expected_base,)
    assert issubclass(error_type, GieError)
    assert issubclass(error_type, Exception)


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_code_family_follows_the_hierarchy(error_type: type[GieError]) -> None:
    families = {ContractError: "GIE.CONTRACT.", TimeError: "GIE.TIME.", SequenceError: "GIE.SEQ."}
    for family_root, prefix in families.items():
        assert issubclass(error_type, family_root) == error_type.code.startswith(prefix)


@pytest.mark.requirement("S00-ASG-24")
def test_families_are_disjoint() -> None:
    families = (ContractError, TimeError, SequenceError)
    standalone = (IllegalTransitionError, LiveRoutingLockedError, ConfigError)
    for error_type in ALL_ERROR_TYPES:
        memberships = [family for family in families if issubclass(error_type, family)]
        assert len(memberships) <= 1
    for error_type in standalone:
        assert not any(issubclass(error_type, family) for family in families)
        assert not any(issubclass(error_type, other) for other in standalone if other is not error_type)
    # stale and future data are different verdicts
    assert not issubclass(StaleDataError, FutureDataError)
    assert not issubclass(FutureDataError, StaleDataError)


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_gie_errors_are_not_builtin_error_kinds(error_type: type[GieError]) -> None:
    """A broad ``except ValueError`` / ``except KeyError`` elsewhere must never swallow a GieError."""
    builtin_kinds = (
        ValueError,
        TypeError,
        LookupError,
        ArithmeticError,
        OSError,
        RuntimeError,
        AssertionError,
    )
    assert not issubclass(error_type, builtin_kinds)


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_every_error_can_be_caught_as_gie_error_and_as_its_family(error_type: type[GieError]) -> None:
    with pytest.raises(GieError) as caught_as_base:
        raise error_type("synthetic failure", attempt=3)
    assert type(caught_as_base.value) is error_type
    assert caught_as_base.value.details == {"attempt": 3}

    _, base = DOCUMENTED[error_type]
    with pytest.raises(base) as caught_as_parent:
        raise error_type("synthetic failure")
    assert type(caught_as_parent.value) is error_type


# =========================================================================== instances
@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_message_and_details_are_carried(error_type: type[GieError]) -> None:
    error = error_type(
        "synthetic message", path="plan.stop_price", count=3, ratio=0.5, ok=False, missing=None
    )
    assert error.message == "synthetic message"
    assert error.args == ("synthetic message",)
    assert error.details == {
        "path": "plan.stop_price",
        "count": 3,
        "ratio": 0.5,
        "ok": False,
        "missing": None,
    }
    assert error.details["ok"] is False
    assert type(error.details["count"]) is int

    plain = error_type("synthetic message")
    assert plain.details == {}
    assert isinstance(plain.details, dict)


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_to_dict_is_the_json_serialisable_journal_form(error_type: type[GieError]) -> None:
    error = error_type(
        "synthetic message \u20ac",
        path="tick.bid",
        time_ns=1795771815123456789,
        ratio=0.25,
        ok=True,
        missing=None,
    )
    journal_form = error.to_dict()
    assert set(journal_form) == {"code", "message", "details"}
    assert journal_form["code"] == DOCUMENTED[error_type][0]
    assert journal_form["message"] == "synthetic message \u20ac"
    assert journal_form["details"] == {
        "path": "tick.bid",
        "time_ns": 1795771815123456789,
        "ratio": 0.25,
        "ok": True,
        "missing": None,
    }
    # plain JSON
    assert json.loads(json.dumps(journal_form)) == journal_form
    # canonical GIE JSON (exact 64-bit integers, strict parsing)
    restored = strict_loads(canonical_dumps(journal_form))
    assert restored == journal_form
    assert restored["details"]["time_ns"] == 1795771815123456789
    assert restored["details"]["ok"] is True
    # the code in the journal form identifies the class again
    assert ERROR_CODES[restored["code"]] is error_type


@pytest.mark.requirement("S00-ASG-24")
def test_to_dict_without_details() -> None:
    assert GieError("synthetic").to_dict() == {"code": "GIE.ERROR", "message": "synthetic", "details": {}}
    assert ConfigError("synthetic").to_dict() == {
        "code": "GIE.CONFIG.INVALID",
        "message": "synthetic",
        "details": {},
    }


@pytest.mark.requirement("S00-ASG-24")
def test_to_dict_returns_an_independent_copy() -> None:
    error = StaleDataError("synthetic", age_ns=5)
    first = error.to_dict()
    assert isinstance(first["details"], dict)
    first["details"]["age_ns"] = 999
    first["details"]["injected"] = True
    first["code"] = "GIE.TAMPERED"
    assert error.details == {"age_ns": 5}
    assert error.to_dict() == {"code": "GIE.TIME.STALE", "message": "synthetic", "details": {"age_ns": 5}}


@pytest.mark.requirement("S00-ASG-24")
def test_details_are_copied_from_the_caller() -> None:
    supplied = {"path": "a.b", "count": 1}
    error = ContractValidationError("synthetic", **supplied)
    supplied["count"] = 2
    supplied["extra"] = 3
    assert error.details == {"path": "a.b", "count": 1}


@pytest.mark.requirement("S00-ASG-24")
def test_a_message_is_required() -> None:
    with pytest.raises(TypeError):
        GieError()  # type: ignore[call-arg]


# =========================================================================== text form
@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_str_contains_code_and_message(error_type: type[GieError]) -> None:
    code = DOCUMENTED[error_type][0]
    plain = str(error_type("synthetic message"))
    assert code in plain
    assert "synthetic message" in plain

    detailed = str(error_type("synthetic message", path="tick.bid", count=31337))
    assert code in detailed
    assert "synthetic message" in detailed
    for fragment in ("path", "tick.bid", "count", "31337"):
        assert fragment in detailed


@pytest.mark.requirement("S00-ASG-24")
def test_str_does_not_depend_on_the_order_details_were_given() -> None:
    first = str(StaleDataError("synthetic", what="tick", age_ns=5, max_age_ns=3))
    second = str(StaleDataError("synthetic", max_age_ns=3, age_ns=5, what="tick"))
    assert first == second


@pytest.mark.requirement("S00-ASG-24")
def test_instances_of_different_classes_never_share_a_code() -> None:
    instances = [error_type("synthetic") for error_type in ALL_ERROR_TYPES]
    assert len({instance.code for instance in instances}) == len(instances)
    assert len({instance.to_dict()["code"] for instance in instances}) == len(instances)
    assert len({str(instance) for instance in instances}) == len(instances)
