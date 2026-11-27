"""Independent tests for ``gie.core.errors`` (requirement S00-ASG-24, "error semantics").

The expected taxonomy is the one documented in ``specifications/SHARED_CONTRACTS.md`` section 5
and listed in ``specifications/contracts/FIELDS.md`` ("Error codes").  The codes are part of the
shared contract surface ("changing or removing a code is a breaking change"), so they are pinned
here literally: a renamed or re-coded error must fail this file.

Revision 2 of the contracts (after the independent review) added ``JournalCorruptError`` and the
rule that an error can *always* be journaled: a detail that has no canonical JSON form (an integer
beyond 64 bits, a non-finite float, a value that is not a JSON scalar) is carried as text, so
``canonical_dumps(error.to_dict())`` never fails because of a detail.

All messages and details used here are synthetic fixtures; none is market data.
"""

from __future__ import annotations

import ast
import datetime as dt
import enum
import inspect
import json
import math
import re
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

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
    JournalCorruptError,
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
    JournalCorruptError: ("GIE.JOURNAL.CORRUPT", GieError),
    LiveRoutingLockedError: ("GIE.ROUTING.LIVE_LOCKED", GieError),
    ConfigError: ("GIE.CONFIG.INVALID", GieError),
}

_ALL = pytest.mark.parametrize("error_type", ALL_ERROR_TYPES, ids=lambda error_type: error_type.__name__)


# =========================================================================== the register
@pytest.mark.requirement("S00-ASG-24")
def test_all_error_types_is_exactly_the_documented_taxonomy() -> None:
    assert isinstance(ALL_ERROR_TYPES, tuple)
    assert len(ALL_ERROR_TYPES) == len(set(ALL_ERROR_TYPES)) == 18
    assert set(ALL_ERROR_TYPES) == set(DOCUMENTED)
    for error_type in ALL_ERROR_TYPES:
        assert type(error_type) is type
        assert issubclass(error_type, GieError)
    assert ALL_ERROR_TYPES[0] is GieError


@pytest.mark.requirement("S00-ASG-24")
def test_journal_corrupt_error_is_registered_with_its_documented_code() -> None:
    """Revision 2: a damaged journal has its own stable code and is not a lifecycle or sequence error."""
    assert JournalCorruptError in ALL_ERROR_TYPES
    assert JournalCorruptError.code == "GIE.JOURNAL.CORRUPT"
    assert ERROR_CODES["GIE.JOURNAL.CORRUPT"] is JournalCorruptError
    assert JournalCorruptError.__bases__ == (GieError,)
    for other in (ContractError, TimeError, SequenceError, IllegalTransitionError, ConfigError):
        assert not issubclass(JournalCorruptError, other)
        assert not issubclass(other, JournalCorruptError)
    error = JournalCorruptError("synthetic: torn last line", line=7, path="synthetic.journal")
    assert error.to_dict() == {
        "code": "GIE.JOURNAL.CORRUPT",
        "message": "synthetic: torn last line",
        "details": {"line": 7, "path": "synthetic.journal"},
    }
    assert str(error).startswith("[GIE.JOURNAL.CORRUPT] synthetic: torn last line")
    with pytest.raises(GieError) as caught:
        raise error
    assert caught.value is error


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
    standalone = (IllegalTransitionError, JournalCorruptError, LiveRoutingLockedError, ConfigError)
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


# =========================================================================== details are journalable
# Revision 2 (SHARED_CONTRACTS.md section 5, module docstring of gie.core.errors): ``details`` only
# carries JSON scalars and "a detail must never be the reason an error cannot be written":
# integers outside 64 bits, non-finite floats and values of any other type are carried as text.
INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)

PROPERTY = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _raw_canonical_text(value: Any) -> str | None:
    """Canonical JSON of ``{"value": value}``, or ``None`` when canonical JSON refuses the value."""
    try:
        return canonical_dumps({"value": value})
    except CanonicalEncodingError:
        return None


def _assert_journalable(error: GieError) -> dict[str, Any]:
    """The journal form is canonical GIE JSON and survives strict parsing unchanged."""
    journal_form = error.to_dict()
    assert set(journal_form) == {"code", "message", "details"}
    details = journal_form["details"]
    assert isinstance(details, dict)
    for key, value in details.items():
        assert type(key) is str
        assert value is None or isinstance(value, (str, int, float, bool)), (key, type(value))
        if isinstance(value, float):
            assert math.isfinite(value)
        if isinstance(value, int) and not isinstance(value, bool):
            assert INT64_MIN <= value <= INT64_MAX
    text = canonical_dumps(journal_form)
    restored = strict_loads(text)
    assert canonical_dumps(restored) == text  # the journal line is a fixed point
    assert restored["code"] == journal_form["code"]
    assert restored["message"] == journal_form["message"]
    assert set(restored["details"]) == set(details)
    for key, value in details.items():
        if isinstance(value, float):
            # canonical JSON writes an integral double without a fraction; the reader then
            # returns an int and "the field type decides" (gie.core.canonical module docstring)
            assert float(restored["details"][key]) == value
        else:
            assert restored["details"][key] == value
            assert type(restored["details"][key]) is type(value)
    json.dumps(journal_form, allow_nan=False)  # also plain JSON without NaN/Infinity tokens
    return details


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("value", [0, 1, -1, 2**53 + 1, 1795771815123456789, INT64_MAX, INT64_MIN])
def test_integers_within_64_bits_stay_exact_integers(value: int) -> None:
    error = StaleDataError("synthetic", value=value)
    assert error.details["value"] == value
    assert type(error.details["value"]) is int
    details = _assert_journalable(error)
    assert details == {"value": value}
    restored = strict_loads(canonical_dumps(error.to_dict()))["details"]["value"]
    assert restored == value
    assert type(restored) is int


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [INT64_MAX + 1, INT64_MIN - 1, 2**64, -(2**64), 2**70, 10**30, -(10**30)],
    ids=["int64max+1", "int64min-1", "2**64", "-2**64", "2**70", "1e30", "-1e30"],
)
def test_integers_beyond_64_bits_are_carried_as_their_decimal_text(value: int) -> None:
    """Canonical JSON refuses such an integer, so the detail is text - never a rounded number."""
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps({"value": value})  # positive control: the raw value is not journalable
    error = ContractValidationError("synthetic", value=value, path="tick.receive_time_ns")
    detail = error.details["value"]
    assert type(detail) is str
    assert detail == str(value)
    assert int(detail) == value  # nothing was rounded through a double
    assert error.details["path"] == "tick.receive_time_ns"
    assert _assert_journalable(error) == {"value": str(value), "path": "tick.receive_time_ns"}


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("value", [10**100, -(10**100), 2**4000, -(2**4000)])
def test_very_large_integers_are_carried_as_a_prefix_of_their_digits(value: int) -> None:
    error = GieError("synthetic", value=value)
    detail = error.details["value"]
    assert type(detail) is str
    assert len(detail) >= 20
    assert str(value).startswith(detail)
    _assert_journalable(error)


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("value", [10**5000, -(1 << 20_000)], ids=["1e5000", "-2**20000"])
def test_an_integer_too_large_to_print_is_still_a_journalable_detail(value: int) -> None:
    """An integer of any size is "carried as text"; constructing the error must not itself fail.

    CPython refuses to convert integers of more than 4300 digits to text (``ValueError``), so
    this is the case in which a detail *is* the reason an error cannot be written.
    """
    error = ContractValidationError("synthetic", value=value)
    assert type(error.details["value"]) is str
    _assert_journalable(error)
    assert "GIE.CONTRACT.INVALID" in str(error)


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    ("value", "marker"),
    [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")],
    ids=["nan", "inf", "-inf"],
)
def test_non_finite_floats_are_carried_as_text(value: float, marker: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps({"value": value})  # positive control
    error = GieError("synthetic", value=value)
    detail = error.details["value"]
    assert type(detail) is str
    assert marker in detail.lower()
    assert detail.startswith("-") == (value < 0)
    _assert_journalable(error)
    # NaN in details must not make two readings of the same error differ
    assert error.to_dict() == error.to_dict()


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("value", [0.0, -0.0, 0.25, -1.5, 5e-324, 1.7976931348623157e308, 1e21, 1e-7])
def test_finite_floats_stay_floats(value: float) -> None:
    error = GieError("synthetic", value=value)
    assert type(error.details["value"]) is float
    assert error.details["value"] == value
    assert math.copysign(1.0, error.details["value"]) == math.copysign(1.0, value)
    _assert_journalable(error)


@pytest.mark.requirement("S00-ASG-24")
def test_booleans_none_and_text_are_carried_unchanged() -> None:
    error = GieError("synthetic", yes=True, no=False, missing=None, text="zlato € \U0001f600", empty="")
    assert error.details["yes"] is True
    assert error.details["no"] is False
    assert error.details["missing"] is None
    assert error.details["text"] == "zlato € \U0001f600"
    assert error.details["empty"] == ""
    restored = strict_loads(canonical_dumps(error.to_dict()))["details"]
    assert restored["yes"] is True
    assert restored["no"] is False
    assert restored["missing"] is None
    assert restored["text"] == "zlato € \U0001f600"
    _assert_journalable(error)


_NON_SCALARS: list[tuple[str, Any, tuple[str, ...]]] = [
    ("list", [1, 2, 3], ("1", "2", "3")),
    ("tuple", ("XAUUSD", 5), ("XAUUSD", "5")),
    ("dict", {"bid": 1}, ("bid", "1")),
    ("set", {7}, ("7",)),
    ("frozenset", frozenset({"a"}), ("a",)),
    ("bytes", b"synthetic", ("synthetic",)),
    ("bytearray", bytearray(b"ab"), ("ab",)),
    ("decimal", Decimal("2650.50"), ("2650.50",)),
    ("complex", 1 + 2j, ("1", "2")),
    ("date", dt.date(2026, 11, 27), ("2026", "11", "27")),
    ("path", PurePosixPath("synthetic/journal.jsonl"), ("synthetic/journal.jsonl",)),
    ("exception", ValueError("synthetic cause"), ("synthetic cause",)),
    ("type", dict, ("dict",)),
    ("range", range(3), ("3",)),
    ("nested", {"levels": [Decimal("1"), float("nan"), 2**70]}, ("levels", "nan")),
]


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    ("value", "fragments"), [(case[1], case[2]) for case in _NON_SCALARS], ids=[c[0] for c in _NON_SCALARS]
)
def test_values_that_are_not_json_scalars_are_carried_as_text(value: Any, fragments: tuple[str, ...]) -> None:
    # positive control: as a raw value this is either refused by canonical JSON or would be
    # written as a JSON container, which is not a scalar
    raw = _raw_canonical_text(value)
    assert raw is None or raw.startswith(('{"value":[', '{"value":{'))
    error = IllegalTransitionError("synthetic", value=value, order_seq=4)
    detail = error.details["value"]
    assert type(detail) is str
    assert detail.strip()
    for fragment in fragments:
        assert fragment in detail
    assert error.details["order_seq"] == 4
    _assert_journalable(error)


@pytest.mark.requirement("S00-ASG-24")
def test_a_very_large_non_scalar_detail_is_truncated_text_not_megabytes() -> None:
    value = list(range(100_000))
    error = GieError("synthetic", value=value)
    detail = error.details["value"]
    assert type(detail) is str
    assert 0 < len(detail) < len(repr(value))
    assert repr(value).startswith(detail)
    _assert_journalable(error)


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    ["lone high \ud800 surrogate", "\udfff", "reversed pair \udc00\ud800", "ok \U0001f600 then \udcff"],
    ids=["high", "low", "reversed-pair", "astral-then-lone"],
)
def test_text_that_is_not_valid_unicode_is_still_a_journalable_detail(text: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps({"value": text})  # positive control
    error = CanonicalEncodingError("synthetic", value=text)
    detail = error.details["value"]
    assert type(detail) is str
    detail.encode("utf-8")  # valid Unicode now
    assert "\U0001f600" in detail or "\U0001f600" not in text  # valid characters are kept
    _assert_journalable(error)


@pytest.mark.requirement("S00-ASG-24")
def test_an_error_whose_message_is_not_valid_unicode_is_still_journalable() -> None:
    """Errors are data and are never swallowed: the journal form must exist for every error.

    ``gie.core.freshness.require_fresh(what=...)`` and ``require_not_expired(what=...)`` build
    their message from caller text, so a message can contain the same unencodable characters that
    a detail can.
    """
    error = StaleDataError("synthetic \ud800 is stale", what="synthetic \ud800")
    journal_form = error.to_dict()
    assert journal_form["code"] == "GIE.TIME.STALE"
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


@pytest.mark.requirement("S00-ASG-24")
def test_an_error_whose_detail_name_is_not_valid_unicode_is_still_journalable() -> None:
    """The names of the details are part of the journal form as much as their values."""
    error = ContractValidationError("synthetic", **{"field \udc00": 1})
    journal_form = error.to_dict()
    assert journal_form["code"] == "GIE.CONTRACT.INVALID"
    assert len(journal_form["details"]) == 1
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


class _ReprRaises:
    """Synthetic value whose text form cannot be built (for example a half-initialised object)."""

    def __repr__(self) -> str:
        raise RuntimeError("synthetic: repr failed")


class _ReprIsNotUnicode:
    """Synthetic value whose text form contains a lone surrogate."""

    def __repr__(self) -> str:
        return "<synthetic \udfff>"


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "value_type", [_ReprRaises, _ReprIsNotUnicode], ids=["repr-raises", "repr-not-unicode"]
)
def test_a_value_without_a_usable_text_form_is_still_a_journalable_detail(value_type: type) -> None:
    """A value "of any other type" is carried as text, also when its own text form is unusable.

    The error being raised is the information that must survive; a detail must never replace it
    with another exception (here ``RuntimeError``) or make the journal line unwritable.
    """
    error = OutOfOrderError("synthetic", offender=value_type(), seq=5)
    assert type(error.details["offender"]) is str
    assert error.details["seq"] == 5
    _assert_journalable(error)
    assert "GIE.SEQ.OUT_OF_ORDER" in str(error)


@pytest.mark.requirement("S00-ASG-24")
def test_subclasses_of_the_scalar_types_are_journaled_as_their_scalar() -> None:
    """Enum members that are ``int`` or ``str`` (for example ``ClockMode``) are ordinary scalars."""

    class _Level(enum.IntEnum):
        HIGH = 7

    class _Mode(str, enum.Enum):
        REPLAY = "REPLAY"

    error = GieError("synthetic", level=_Level.HIGH, mode=_Mode.REPLAY)
    assert error.details["level"] == 7
    assert error.details["mode"] == "REPLAY"
    journal_form = error.to_dict()
    assert strict_loads(canonical_dumps(journal_form)) == {
        "code": "GIE.ERROR",
        "message": "synthetic",
        "details": {"level": 7, "mode": "REPLAY"},
    }


@pytest.mark.requirement("S00-ASG-24")
def test_a_very_long_text_detail_is_carried_as_a_prefix_of_itself() -> None:
    """Details are "(truncated) text": never megabytes in one journal line, never altered text."""
    text = "synthetic " * 100_000
    error = GieError("synthetic", value=text)
    detail = error.details["value"]
    assert type(detail) is str
    assert 0 < len(detail) <= len(text)
    assert text.startswith(detail)
    assert len(canonical_dumps(error.to_dict())) < len(text)
    _assert_journalable(error)
    # an ordinary message-sized text is carried unchanged
    assert GieError("synthetic", value="x" * 200).details["value"] == "x" * 200


@pytest.mark.requirement("S00-ASG-24")
@_ALL
def test_every_error_type_is_journalable_with_details_of_every_kind(error_type: type[GieError]) -> None:
    error = error_type(
        "synthetic message",
        beyond_int64=2**63,
        below_int64=-(2**63) - 1,
        not_a_number=float("nan"),
        infinite=float("-inf"),
        container=["a", {"b": Decimal("1.5")}],
        binary=b"\x00\xff",
        broken_text="\ud83d",
        exact=INT64_MAX,
        ratio=0.25,
        flag=True,
        missing=None,
    )
    details = _assert_journalable(error)
    assert set(details) == {
        "beyond_int64",
        "below_int64",
        "not_a_number",
        "infinite",
        "container",
        "binary",
        "broken_text",
        "exact",
        "ratio",
        "flag",
        "missing",
    }
    assert details["beyond_int64"] == "9223372036854775808"
    assert details["below_int64"] == "-9223372036854775809"
    assert details["exact"] == INT64_MAX
    assert details["ratio"] == 0.25
    assert details["flag"] is True
    assert details["missing"] is None
    restored = strict_loads(canonical_dumps(error.to_dict()))
    assert ERROR_CODES[restored["code"]] is error_type
    # the text form never fails either
    rendered = str(error)
    assert error_type.code in rendered
    assert "synthetic message" in rendered


@pytest.mark.requirement("S00-ASG-24")
def test_the_instance_details_and_the_journal_form_agree() -> None:
    """What is journaled is what ``details`` holds: sanitising happens once, at construction."""
    error = OutOfOrderError("synthetic", seq=2**80, ratio=float("inf"), parts=(1, 2))
    assert error.to_dict()["details"] == error.details
    assert all(type(value) is str for value in error.details.values())
    assert error.to_dict()["details"] is not error.details


#: Detail names are keyword arguments; ``message`` is the positional parameter of the constructor.
_DETAIL_NAMES = st.from_regex(r"[a-z_]{1,8}", fullmatch=True).filter(lambda name: name != "message")
_DETAIL_VALUES = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**200), max_value=2**200),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(alphabet=st.characters(), max_size=40),  # includes lone surrogates
        st.binary(max_size=8),
        st.decimals(allow_nan=True, allow_infinity=True, places=4),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=3), children, max_size=3),
        st.tuples(children, children),
    ),
    max_leaves=6,
)


@pytest.mark.requirement("S00-ASG-24")
@PROPERTY
@example(details={"a": 2**63, "b": -(2**63) - 1, "c": float("nan"), "d": "\ud800", "e": [float("inf")]})
@example(details={"a": INT64_MAX, "b": INT64_MIN, "c": -0.0, "d": 1e308, "e": 5e-324})
@given(details=st.dictionaries(_DETAIL_NAMES, _DETAIL_VALUES, max_size=5))
def test_any_details_can_be_journaled(details: dict[str, Any]) -> None:
    error = GieError("synthetic", **details)
    assert set(error.details) == set(details)
    journaled = _assert_journalable(error)
    for key, value in details.items():
        carried = journaled[key]
        if value is None or isinstance(value, bool):
            assert carried is value
        elif isinstance(value, int):
            if INT64_MIN <= value <= INT64_MAX:
                assert type(carried) is int
                assert carried == value
            else:
                assert type(carried) is str
                assert str(value).startswith(carried)
        elif isinstance(value, float):
            if math.isfinite(value):
                assert type(carried) is float
                assert carried == value
            else:
                assert type(carried) is str
        else:
            assert type(carried) is str
    assert isinstance(str(error), str)
