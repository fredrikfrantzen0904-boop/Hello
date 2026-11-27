"""Tests of the contract framework: ``gie.contracts._spec`` and ``gie.contracts.registry``.

One declaration per field drives validation on construction, canonical encoding, strict decoding
and the published JSON Schema.  Expected behaviour is taken from the module and class docstrings
of ``gie/contracts/_spec.py`` and from ``specifications/SHARED_CONTRACTS.md`` (revision 2):

* section 5.2 - invalid input is rejected with ``ContractValidationError`` naming the field path
  and the violated rule; it is never clamped, defaulted, rounded, stripped or coerced;
* section 7 - wire representation of every in-memory type, optional fields as explicit ``null``,
  canonical order of lists whose order carries no meaning;
* section 8 - a field added by a MINOR version is optional, may be absent from an older message and
  must be present in a message of its own or a newer version; unknown fields are ignored only in a
  message of a newer MINOR;
* section 1 - a contract class cannot be defined outside ``gie.contracts``.

The record classes declared in this module (``Probe``, ``Leaf``, ``Versioned`` ...) are SYNTHETIC
FIXTURES that exist only to exercise the framework.  They are not shared contracts, they are never
registered under the name of a real contract, and none of their values is market data.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

import jsonschema
import pytest

from gie.contracts import NamedValue, Tick, from_wire, to_json, to_wire
from gie.contracts import registry as registry_module
from gie.contracts._spec import (
    FIRST_VERSION,
    PRINTABLE_PATTERN,
    Bool,
    Dec,
    DecodeContext,
    DurationNs,
    EnumOf,
    FieldSpec,
    Float,
    Id,
    Int,
    Invariant,
    ListOf,
    Obj,
    Opt,
    Record,
    Str,
    TimeNs,
    TradingDay,
    decode_fields,
    encode_fields,
    fld,
    inv,
    object_schema,
    record,
)
from gie.contracts.registry import (
    SemVer,
    all_contracts,
    contract,
    contract_version,
    get_contract,
    parse_semver,
)
from gie.core.canonical import DECIMAL_PATTERN, canonical_dumps, strict_loads
from gie.core.errors import (
    ContractValidationError,
    ContractVersionError,
    GieError,
    UnknownContractError,
)
from gie.core.timeutil import MAX_TIME_NS
from tests.gie import samples

#: Every contract of the brief rests on the framework.
CONTRACTS = (
    "S00-ASG-11",
    "S00-ASG-12",
    "S00-ASG-13",
    "S00-ASG-14",
    "S00-ASG-15",
    "S00-ASG-16",
    "S00-ASG-17",
    "S00-ASG-18",
    "S00-ASG-19",
)

RUN_ID = "run_" + "0123456789abcdef" * 2
STRICT = DecodeContext()


# --------------------------------------------------------------------------- synthetic records
class Colour(str, Enum):
    """SYNTHETIC vocabulary."""

    RED = "RED"
    GREEN = "GREEN"


class OtherColour(str, Enum):
    """SYNTHETIC look-alike vocabulary with the same values."""

    RED = "RED"
    GREEN = "GREEN"


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Leaf(Record):
    """SYNTHETIC nested value object."""

    name: str = fld(Str(pattern=r"^[a-z]{1,8}$", max_len=8, label="leaf name"), "Synthetic name.")
    weight: Decimal = fld(Dec(ge="0", le="1", label="fraction"), "Synthetic weight.", unit="fraction")
    note: str | None = fld(Opt(Str(max_len=16)), "Synthetic free text.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv("LEAF-1", "a leaf named zero weighs nothing", lambda r: r.name != "zero" or r.weight == 0),
    )


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Probe(Record):
    """SYNTHETIC record with one field of every field type.

    This second paragraph is not part of the schema description.
    """

    text: str = fld(Str(max_len=12), "Synthetic free text.")
    ident: str = fld(Id("run"), "Synthetic identifier.")
    day: str = fld(TradingDay(), "Synthetic trading day.")
    count: int = fld(Int(ge=0, le=10, label="count"), "Synthetic count.")
    at_ns: int = fld(TimeNs(), "Synthetic instant.", unit="ns since epoch, UTC")
    span_ns: int = fld(DurationNs(), "Synthetic duration.", unit="ns")
    amount: Decimal = fld(Dec(gt="0", le="1000", label="money"), "Synthetic amount.", unit="account currency")
    ratio: float = fld(Float(ge=0.0, le=1.0, label="score"), "Synthetic closed interval.")
    open_ratio: float = fld(Float(gt=0.0, lt=1.0, label="probability"), "Synthetic open interval.")
    flag: bool = fld(Bool(), "Synthetic flag.")
    colour: Colour = fld(EnumOf(Colour), "Synthetic vocabulary term.")
    maybe: int | None = fld(Opt(Int()), "Synthetic optional integer.")
    tags: tuple[str, ...] = fld(
        ListOf(
            Str(pattern=r"^[A-Z]{1,4}$", max_len=4, label="tag"),
            max_items=4,
            unique_key=lambda tag: tag,
            canonical_sort=lambda tag: tag,
            sorted_label="sorted ascending",
        ),
        "Synthetic set of tags (order carries no meaning).",
    )
    steps: tuple[int, ...] = fld(ListOf(Int(), max_items=3), "Synthetic sequence (order carries meaning).")
    leaves: tuple[Leaf, ...] = fld(
        ListOf(
            Obj(Leaf),
            min_items=1,
            max_items=3,
            unique_key=lambda leaf: leaf.name,
            canonical_sort=lambda leaf: leaf.name,
            sorted_label="sorted by name",
        ),
        "Synthetic nested objects.",
    )
    leaf: Leaf | None = fld(Opt(Obj(Leaf)), "Synthetic optional nested object.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv("PROBE-1", "count is at most the number of steps plus 9", lambda r: r.count <= len(r.steps) + 9),
        inv(
            "PROBE-2",
            "maybe is greater than count when the flag is set",
            lambda r: not r.flag or r.maybe > r.count,
        ),
    )


def probe(**overrides: Any) -> Probe:
    """A valid SYNTHETIC ``Probe``; keyword overrides replace single fields."""
    fields: dict[str, Any] = {
        "text": "hello world",
        "ident": RUN_ID,
        "day": "2026-11-26",
        "count": 3,
        "at_ns": 1_795_685_400_000_000_000,
        "span_ns": 5,
        "amount": Decimal("12.50"),
        "ratio": 0.25,
        "open_ratio": 0.5,
        "flag": False,
        "colour": Colour.GREEN,
        "maybe": None,
        "tags": ("B", "A"),
        "steps": (3, 1, 2),
        "leaves": (
            Leaf(name="b", weight=Decimal("0.5"), note=None),
            Leaf(name="a", weight=Decimal("1"), note="x"),
        ),
        "leaf": None,
    }
    fields.update(overrides)
    return Probe(**fields)


#: The wire form of ``probe()`` written by hand from the table in the ``_spec`` module docstring.
PROBE_WIRE: dict[str, Any] = {
    "text": "hello world",
    "ident": RUN_ID,
    "day": "2026-11-26",
    "count": 3,
    "at_ns": 1_795_685_400_000_000_000,
    "span_ns": 5,
    "amount": "12.5",
    "ratio": 0.25,
    "open_ratio": 0.5,
    "flag": False,
    "colour": "GREEN",
    "maybe": None,
    "tags": ["A", "B"],
    "steps": [3, 1, 2],
    "leaves": [
        {"name": "a", "weight": "1", "note": "x"},
        {"name": "b", "weight": "0.5", "note": None},
    ],
    "leaf": None,
}


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Versioned(Record):
    """SYNTHETIC record whose fields were introduced by three contract versions."""

    base: int = fld(Int(ge=0), "Synthetic field of version 1.0.")
    note: str | None = fld(Opt(Str()), "Synthetic optional field of version 1.0.")
    added: str | None = fld(Opt(Str()), "Synthetic optional field added by 1.1.", since=(1, 1))
    later: int | None = fld(Opt(Int()), "Synthetic optional field added by 1.2.", since=(1, 2))


@dataclass(frozen=True, slots=True, kw_only=True)
class Header(Record):
    """SYNTHETIC header with an inherited invariant."""

    low: int = fld(Int(), "Synthetic lower bound.")
    high: int = fld(Int(), "Synthetic upper bound.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (inv("HEAD-1", "low <= high", lambda r: r.low <= r.high),)


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Body(Header):
    """SYNTHETIC record that inherits fields and invariants from its header."""

    middle: int = fld(Int(), "Synthetic value between the bounds.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv("BODY-1", "low <= middle <= high", lambda r: r.low <= r.middle <= r.high),
    )


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Fragile(Record):
    """SYNTHETIC record whose invariants cannot be evaluated for some valid field values."""

    mode: str = fld(Str(), "Synthetic selector of the failing rule.")
    low: int = fld(Int(), "Synthetic number.")
    high: int | None = fld(Opt(Int()), "Synthetic optional number.")
    label: str | None = fld(Opt(Str()), "Synthetic optional text.")
    items: tuple[int, ...] = fld(ListOf(Int()), "Synthetic list.")

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv("FRG-1", "low is below high", lambda r: r.mode != "compare" or r.low < r.high),
        inv("FRG-2", "low divides evenly", lambda r: r.mode != "divide" or 10 % r.low == 0),
        inv("FRG-3", "label parses as a number", lambda r: r.mode != "parse" or int(r.label) >= 0),
        inv("FRG-4", "label is upper case", lambda r: r.mode != "attribute" or r.label.isupper()),
        inv("FRG-5", "the first item is positive", lambda r: r.mode != "index" or r.items[0] > 0),
        inv("FRG-6", "the mode is known", lambda r: r.mode != "lookup" or {"a": True}[r.mode]),
    )


def fragile(mode: str, **overrides: Any) -> Fragile:
    fields: dict[str, Any] = {"mode": mode, "low": 1, "high": 2, "label": "A", "items": (1,)}
    fields.update(overrides)
    return Fragile(**fields)


def _invalid(build: Any) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as caught:
        build()
    assert type(caught.value) is ContractValidationError
    assert caught.value.code == "GIE.CONTRACT.INVALID"
    return caught.value


def _decode(
    record_type: type[Record], raw: dict[str, Any], ctx: DecodeContext = STRICT, path: str = ""
) -> Any:
    return decode_fields(record_type, raw, path, ctx, envelope=False)


# =========================================================================== declaration
@pytest.mark.requirement(*CONTRACTS)
def test_record_collects_one_field_spec_per_declared_field() -> None:
    specs = Probe.FIELD_SPECS
    assert [spec.name for spec in specs] == list(PROBE_WIRE)
    assert all(isinstance(spec, FieldSpec) for spec in specs)
    by_name = {spec.name: spec for spec in specs}
    assert by_name["text"].doc == "Synthetic free text."
    assert by_name["amount"].unit == "account currency"
    assert by_name["text"].unit is None
    assert {spec.name for spec in specs if not spec.required} == {"maybe", "leaf"}
    assert all(spec.since == FIRST_VERSION == (1, 0) for spec in specs)
    assert Probe.CONTRACT is None
    assert Probe.VERSION is None
    assert [spec.since for spec in Versioned.FIELD_SPECS] == [(1, 0), (1, 0), (1, 1), (1, 2)]


@pytest.mark.requirement(*CONTRACTS)
@pytest.mark.parametrize("doc", ["", " ", "\n\t "])
def test_fld_requires_documentation(doc: str) -> None:
    with pytest.raises(ValueError, match="documentation"):
        fld(Int(), doc)
    with pytest.raises(ValueError, match="documentation"):
        fld(Opt(Int()), doc)


@pytest.mark.requirement(*CONTRACTS)
def test_a_field_added_after_the_first_version_must_be_optional() -> None:
    """Section 8: a MINOR version only adds optional fields; a new required field is a MAJOR change."""
    with pytest.raises(ValueError, match="optional"):
        fld(Int(), "Synthetic required field added later.", since=(1, 1))
    with pytest.raises(ValueError, match="optional"):
        fld(ListOf(Int()), "Synthetic required list added later.", since=(1, 3))
    assert fld(Opt(Int()), "Synthetic optional field added later.", since=(1, 1)).default is None
    assert fld(Int(), "Synthetic required field.").default is dataclasses.MISSING


@pytest.mark.requirement(*CONTRACTS)
def test_optional_fields_default_to_none_in_memory_and_are_still_written() -> None:
    value = Versioned(base=1)
    assert (value.note, value.added, value.later) == (None, None, None)
    assert encode_fields(value) == {"base": 1, "note": None, "added": None, "later": None}


@pytest.mark.requirement(*CONTRACTS)
def test_record_refuses_classes_that_are_not_declared_with_the_framework() -> None:
    class NotADataclass(Record):
        """SYNTHETIC."""

    with pytest.raises(TypeError, match="dataclass"):
        record(NotADataclass)

    @dataclass(frozen=True, slots=True, kw_only=True)
    class PlainField(Record):
        """SYNTHETIC."""

        documented: int = fld(Int(), "Synthetic.")
        undeclared: int = 0

    with pytest.raises(TypeError, match="not declared with fld"):
        record(PlainField)


@pytest.mark.requirement(*CONTRACTS)
def test_invariants_are_inherited_in_declaration_order_and_ids_are_unique() -> None:
    assert [rule.id for rule in Body.ALL_INVARIANTS] == ["HEAD-1", "BODY-1"]
    assert [spec.name for spec in Body.FIELD_SPECS] == ["low", "high", "middle"]
    assert Body(low=1, middle=2, high=3).middle == 2
    error = _invalid(lambda: Body(low=3, middle=2, high=1))
    assert error.details["invariant"] == "HEAD-1", "the inherited rule is checked first"
    error = _invalid(lambda: Body(low=1, middle=5, high=3))
    assert error.details["invariant"] == "BODY-1"

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Clash(Header):
        """SYNTHETIC record that reuses an invariant id of its base class."""

        INVARIANTS: ClassVar[tuple[Invariant, ...]] = (inv("HEAD-1", "duplicate id", lambda r: True),)

    with pytest.raises(TypeError, match="duplicate invariant id HEAD-1"):
        record(Clash)


@pytest.mark.requirement(*CONTRACTS)
def test_field_type_constructors_refuse_nonsense() -> None:
    with pytest.raises(ValueError, match="Opt cannot wrap Opt"):
        Opt(Opt(Int()))
    with pytest.raises(ValueError, match="unknown id prefix"):
        Id("zzz")
    with pytest.raises(ValueError, match="unknown id prefix"):
        Id("")
    assert Opt(Int()).optional is True
    assert Int().optional is False
    assert ListOf(Opt(Int())).optional is False


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-20")
@pytest.mark.parametrize(
    ("ftype", "text"),
    [
        (Str(label="label"), "label"),
        (Str(choices=("XAUUSD",)), "const XAUUSD"),
        (Id("mev"), "id<mev>"),
        (TradingDay(), "trading day"),
        (Int(ge=0, label="sequence number"), "sequence number"),
        (TimeNs(), "instant (ns since epoch, UTC)"),
        (DurationNs(), "duration (ns)"),
        (Dec(gt="0", label="price"), "price > 0"),
        (Dec(ge="0", le="1", label="fraction"), "fraction >= 0, <= 1"),
        (Dec(label="money"), "money"),
        (Float(gt=0.0, lt=1.0, label="probability"), "probability > 0, < 1"),
        (Float(ge=0.0, le=1.0, label="score"), "score >= 0, <= 1"),
        (Float(label="R multiple"), "R multiple"),
        (Bool(), "bool"),
        (EnumOf(Colour), "enum Colour"),
        (Opt(Dec(gt="0", label="price")), "price > 0 | null"),
        (ListOf(Obj(Leaf)), "list[Leaf]"),
        (
            ListOf(
                Id("mev"), min_items=1, unique_key=str, canonical_sort=str, sorted_label="sorted ascending"
            ),
            "list[id<mev>] (min 1, unique, sorted ascending)",
        ),
        (ListOf(Str(label="tag"), unique_key=str, canonical_sort=str), "list[tag] (unique, sorted)"),
    ],
)
def test_describe_matches_the_wording_of_the_field_reference(ftype: Any, text: str) -> None:
    """FIELDS.md is generated from ``describe()``; the wordings below are the ones it publishes."""
    assert ftype.describe() == text


# =========================================================================== construction
@pytest.mark.requirement(*CONTRACTS)
def test_a_valid_record_is_constructed_and_normalised() -> None:
    value = probe(tags=["B", "A", "C"], steps=[3, 1, 2], ratio=1, leaves=[Leaf(name="z", weight=Decimal(0))])
    assert value.tags == ("A", "B", "C"), "lists become tuples in canonical order"
    assert type(value.tags) is tuple
    assert value.steps == (3, 1, 2), "a list whose order carries meaning keeps it"
    assert type(value.steps) is tuple
    assert type(value.leaves) is tuple
    assert value.ratio == 1.0
    assert type(value.ratio) is float, "an integer given for a float field is stored as a float"
    assert value.maybe is None
    assert [leaf.name for leaf in probe().leaves] == ["a", "b"], "sorted by name"
    assert probe(leaf=Leaf(name="x", weight=Decimal("0.1"))).leaf == Leaf(
        name="x", weight=Decimal("0.1"), note=None
    )


BAD_VALUES: list[tuple[str, Any, str]] = [
    # --- Str
    ("text", 5, "text"),
    ("text", None, "text"),
    ("text", b"hello", "text"),
    ("text", "", "text"),
    ("text", "x" * 13, "text"),
    ("text", "line\nbreak", "text"),
    ("text", "tab\there", "text"),
    ("text", "nul" + chr(0), "text"),
    ("text", "del" + chr(0x7F), "text"),
    ("text", "c1" + chr(0x85), "text"),
    ("text", "esc" + chr(0x1B), "text"),
    ("text", "sep" + chr(0x2028), "text"),
    ("text", "lone" + chr(0xD800), "text"),
    ("text", ["hello"], "text"),
    # --- Id
    ("ident", "run_123", "ident"),
    ("ident", "mev_" + "0" * 32, "ident"),
    ("ident", "run_" + "G" * 32, "ident"),
    ("ident", "run_" + "A" * 32, "ident"),
    ("ident", "RUN_" + "0" * 32, "ident"),
    ("ident", "run_" + "0" * 31, "ident"),
    ("ident", "run_" + "0" * 33, "ident"),
    ("ident", "run_" + "0" * 32 + "\n", "ident"),
    ("ident", 5, "ident"),
    # --- TradingDay
    ("day", "2026-02-30", "day"),
    ("day", "2026-02-29", "day"),
    ("day", "2026-13-01", "day"),
    ("day", "2026-00-10", "day"),
    ("day", "2026-1-01", "day"),
    ("day", "26-11-26", "day"),
    ("day", "2026/11/26", "day"),
    ("day", "2026-11-26T00:00", "day"),
    ("day", 20261126, "day"),
    # --- Int
    ("count", True, "count"),
    ("count", False, "count"),
    ("count", 1.0, "count"),
    ("count", "1", "count"),
    ("count", Decimal(1), "count"),
    ("count", None, "count"),
    ("count", -1, "count"),
    ("count", 11, "count"),
    ("maybe", 2**63, "maybe"),
    ("maybe", -(2**63) - 1, "maybe"),
    ("maybe", True, "maybe"),
    ("maybe", 1.5, "maybe"),
    # --- TimeNs / DurationNs
    ("at_ns", -1, "at_ns"),
    ("at_ns", MAX_TIME_NS + 1, "at_ns"),
    ("at_ns", 1.0e18, "at_ns"),
    ("at_ns", True, "at_ns"),
    ("span_ns", -1, "span_ns"),
    ("span_ns", MAX_TIME_NS + 1, "span_ns"),
    ("span_ns", 0.5, "span_ns"),
    # --- Dec
    ("amount", 12.5, "amount"),
    ("amount", 12, "amount"),
    ("amount", "12.5", "amount"),
    ("amount", True, "amount"),
    ("amount", None, "amount"),
    ("amount", Decimal(0), "amount"),
    ("amount", Decimal("-0"), "amount"),
    ("amount", Decimal("-1"), "amount"),
    ("amount", Decimal("1000.000000000001"), "amount"),
    ("amount", Decimal("NaN"), "amount"),
    ("amount", Decimal("sNaN"), "amount"),
    ("amount", Decimal("Infinity"), "amount"),
    ("amount", Decimal("-Infinity"), "amount"),
    ("amount", Decimal("0.0000000000001"), "amount"),
    ("amount", Decimal("1E-13"), "amount"),
    # --- Float
    ("ratio", True, "ratio"),
    ("ratio", "0.5", "ratio"),
    ("ratio", Decimal("0.5"), "ratio"),
    ("ratio", None, "ratio"),
    ("ratio", float("nan"), "ratio"),
    ("ratio", float("inf"), "ratio"),
    ("ratio", float("-inf"), "ratio"),
    ("ratio", -0.000001, "ratio"),
    ("ratio", 1.000001, "ratio"),
    ("ratio", 2, "ratio"),
    ("ratio", 10**400, "ratio"),
    ("open_ratio", 0.0, "open_ratio"),
    ("open_ratio", 1.0, "open_ratio"),
    ("open_ratio", 0, "open_ratio"),
    ("open_ratio", 1, "open_ratio"),
    ("open_ratio", -0.0, "open_ratio"),
    # --- Bool
    ("flag", 0, "flag"),
    ("flag", 1, "flag"),
    ("flag", "false", "flag"),
    ("flag", None, "flag"),
    # --- EnumOf
    ("colour", "GREEN", "colour"),
    ("colour", OtherColour.GREEN, "colour"),
    ("colour", 1, "colour"),
    ("colour", None, "colour"),
    # --- ListOf
    ("tags", {"A", "B"}, "tags"),
    ("tags", "AB", "tags"),
    ("tags", None, "tags"),
    ("tags", iter(["A"]), "tags"),
    ("tags", {"A": 1}, "tags"),
    ("tags", ("A", "B", "C", "D", "E"), "tags"),
    ("tags", ("A", "b"), "tags[1]"),
    ("tags", ("A", 5), "tags[1]"),
    ("tags", ("A", "B", "A"), "tags[2]"),
    ("steps", (1, True), "steps[1]"),
    ("steps", (1, 2, 3, 4), "steps"),
    ("steps", (1, 2.0), "steps[1]"),
    ("leaves", (), "leaves"),
    ("leaves", ({"name": "a", "weight": "1", "note": None},), "leaves[0]"),
    ("leaves", (None,), "leaves[0]"),
    ("leaves", (Leaf(name="a", weight=Decimal(1)), Leaf(name="a", weight=Decimal(0))), "leaves[1]"),
    # --- Obj
    ("leaf", {"name": "a", "weight": "1", "note": None}, "leaf"),
    ("leaf", "a", "leaf"),
    ("leaf", NamedValue(name="aa", value=1.0), "leaf"),
]


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    ("field", "bad", "path"),
    BAD_VALUES,
    ids=[f"{field}-{index}" for index, (field, _bad, _path) in enumerate(BAD_VALUES)],
)
def test_construction_validates_every_field(field: str, bad: Any, path: str) -> None:
    """Section 5.2: rejected with the field path and the violated rule - never repaired."""
    assert probe() == probe(), "the unmodified fixture is valid"
    error = _invalid(lambda: probe(**{field: bad}))
    assert error.details["path"] == path
    assert isinstance(error.details["rule"], str)
    assert error.details["rule"]
    assert "invariant" not in error.details, "a field rule fails before any cross-field rule is evaluated"


GOOD_VALUES: list[tuple[str, Any]] = [
    ("text", "x"),
    ("text", "x" * 12),
    ("text", "a b"),
    ("text", 'quote"back\\'),
    ("text", "Z" + chr(0xFC) + "rich"),
    ("text", chr(0x1F600)),
    ("ident", "run_" + "f" * 32),
    ("day", "2024-02-29"),
    ("day", "1970-01-01"),
    ("count", 0),
    ("count", 10),
    ("maybe", 2**63 - 1),
    ("maybe", -(2**63)),
    ("at_ns", 0),
    ("at_ns", MAX_TIME_NS),
    ("span_ns", 0),
    ("span_ns", MAX_TIME_NS),
    ("amount", Decimal("0.000000000001")),
    ("amount", Decimal(1000)),
    ("amount", Decimal("1000.000")),
    ("amount", Decimal("1E+3")),
    ("ratio", 0.0),
    ("ratio", 1.0),
    ("ratio", 0),
    ("ratio", 1),
    ("ratio", -0.0),
    ("open_ratio", 5e-324),
    ("open_ratio", 0.9999999999999999),
    ("flag", False),
    ("colour", Colour.RED),
    ("tags", ()),
    ("tags", ["D", "C", "B", "A"]),
    ("steps", [2, 2, 2]),
    ("leaf", Leaf(name="zero", weight=Decimal(0))),
]


@pytest.mark.requirement(*CONTRACTS)
@pytest.mark.parametrize(
    ("field", "good"), GOOD_VALUES, ids=[f"{f}-{i}" for i, (f, _g) in enumerate(GOOD_VALUES)]
)
def test_boundary_values_are_accepted_and_round_trip_through_the_field_codec(field: str, good: Any) -> None:
    value = probe(**{field: good})
    stored = getattr(value, field)
    assert stored == (tuple(sorted(good)) if field == "tags" else tuple(good) if field == "steps" else good)
    wire = encode_fields(value)
    assert strict_loads(canonical_dumps(wire)) == wire, "the encoded form is canonical JSON data"
    assert _decode(Probe, wire) == value
    assert encode_fields(_decode(Probe, strict_loads(canonical_dumps(wire)))) == wire


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-20")
def test_negative_zero_is_normalised_for_floats_and_written_as_zero_for_decimals() -> None:
    value = probe(ratio=-0.0)
    assert math.copysign(1.0, value.ratio) == 1.0
    assert encode_fields(value)["ratio"] == 0.0
    assert canonical_dumps(encode_fields(value)["ratio"]) == "0"
    leaf = Leaf(name="zero", weight=Decimal("-0"))
    assert encode_fields(leaf)["weight"] == "0"
    assert leaf == Leaf(name="zero", weight=Decimal(0))
    assert hash(leaf) == hash(Leaf(name="zero", weight=Decimal("0.00")))


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-20")
def test_integers_for_float_fields_must_be_exactly_representable() -> None:
    assert NamedValue(name="aa", value=2**53).value == 2.0**53
    assert NamedValue(name="aa", value=10**19).value == 1e19
    assert type(NamedValue(name="aa", value=10**19).value) is float
    for unrepresentable in (2**53 + 1, 2**63 - 1, 10**19 + 1):
        error = _invalid(lambda bad=unrepresentable: NamedValue(name="aa", value=bad))
        assert error.details["path"] == "value"
        assert "exactly representable" in str(error.details["rule"])
    error = _invalid(lambda: NamedValue(name="aa", value=10**400))
    assert error.details["path"] == "value"


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-20")
def test_string_length_counts_characters_and_choices_are_closed() -> None:
    assert probe(text=chr(0x1F600) * 12).text == chr(0x1F600) * 12, "12 characters, 48 UTF-8 bytes"
    _invalid(lambda: probe(text=chr(0x1F600) * 13))
    choice = Str(choices=("XAUUSD",), max_len=24)
    assert choice.check("XAUUSD", "symbol") == "XAUUSD"
    for bad in ("XAGUSD", "xauusd", "XAUUSD ", ""):
        with pytest.raises(ContractValidationError) as caught:
            choice.check(bad, "symbol")
        assert caught.value.details["path"] == "symbol"
    assert choice.schema({})["enum"] == ["XAUUSD"]


@pytest.mark.requirement(*CONTRACTS)
def test_enum_members_are_compared_by_identity() -> None:
    ftype = EnumOf(Colour)
    assert ftype.check(Colour.RED, "colour") is Colour.RED
    assert Colour.RED == "RED" == OtherColour.RED, "the look-alikes compare equal as strings"
    for impostor in ("RED", OtherColour.RED):
        with pytest.raises(ContractValidationError) as caught:
            ftype.check(impostor, "colour")
        assert caught.value.details["path"] == "colour"
        assert "Colour member" in str(caught.value.details["rule"])
    assert ftype.decode("RED", "colour", STRICT) is Colour.RED
    for raw in ("red", "BLUE", "", " RED", 1, None, True, ["RED"], Colour.RED):
        with pytest.raises(ContractValidationError) as caught:
            ftype.decode(raw, "colour", STRICT)
        assert caught.value.details["path"] == "colour"
        assert "unknown Colour value" in str(caught.value.details["rule"])
    assert ftype.encode(Colour.GREEN) == "GREEN"
    assert type(ftype.encode(Colour.GREEN)) is str


# =========================================================================== invariants
@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_a_violated_invariant_names_the_contract_the_invariant_and_the_rule() -> None:
    error = _invalid(lambda: probe(count=10, steps=()))
    assert error.details == {
        "contract": "Probe",
        "invariant": "PROBE-1",
        "rule": "count is at most the number of steps plus 9",
    }
    assert "PROBE-1" in error.message
    assert error.to_dict()["code"] == "GIE.CONTRACT.INVALID"
    error = _invalid(lambda: Leaf(name="zero", weight=Decimal("0.5")))
    assert error.details["invariant"] == "LEAF-1"
    assert error.details["contract"] == "Leaf"
    # dataclasses.replace builds a new record and therefore validates again.
    error = _invalid(lambda: dataclasses.replace(probe(), count=10, steps=()))
    assert error.details["invariant"] == "PROBE-1"
    error = _invalid(lambda: dataclasses.replace(probe(), amount=Decimal(-1)))
    assert error.details["path"] == "amount"


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    ("mode", "overrides", "invariant"),
    [
        ("compare", {"high": None}, "FRG-1"),  # TypeError: int < None
        ("divide", {"low": 0}, "FRG-2"),  # ZeroDivisionError
        ("parse", {"label": "abc"}, "FRG-3"),  # ValueError
        ("parse", {"label": None}, "FRG-3"),  # TypeError
    ],
)
def test_an_invariant_that_cannot_be_evaluated_is_a_contract_error(
    mode: str, overrides: dict[str, Any], invariant: str
) -> None:
    """Fail closed: a rule that cannot be evaluated counts as violated, never as a raw exception."""
    assert fragile("none", **overrides) is not None, "the field values themselves are valid"
    error = _invalid(lambda: fragile(mode, **overrides))
    assert error.details["invariant"] == invariant
    assert error.details["contract"] == "Fragile"
    assert error.details["rule"]
    assert "could not be evaluated" in error.message
    assert error.__cause__ is not None
    wire = encode_fields(fragile("none", **overrides))
    with pytest.raises(ContractValidationError) as caught:
        _decode(Fragile, {**wire, "mode": mode})
    assert caught.value.details["invariant"] == invariant


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    ("mode", "overrides", "invariant"),
    [
        ("attribute", {"label": None}, "FRG-4"),  # AttributeError: None has no method isupper
        ("index", {"items": ()}, "FRG-5"),  # IndexError: the list is empty
        ("lookup", {}, "FRG-6"),  # KeyError
    ],
)
def test_an_invariant_that_fails_on_a_missing_value_is_a_contract_error_too(
    mode: str, overrides: dict[str, Any], invariant: str
) -> None:
    """``Record.__post_init__``: 'A rule that cannot even be evaluated (for example a comparison with a
    missing value) is treated as violated: fail closed, with a contract error.'

    Reading an attribute of an absent optional value, or the first element of an empty list, is
    the same situation as comparing with a missing value and must not surface as a raw
    AttributeError / IndexError / KeyError (section 5: all errors derive from GieError).
    """
    assert fragile("none", **overrides) is not None, "the field values themselves are valid"
    with pytest.raises(GieError) as caught:
        fragile(mode, **overrides)
    assert type(caught.value) is ContractValidationError
    assert caught.value.details["invariant"] == invariant


# =========================================================================== immutability, equality
@pytest.mark.requirement(*CONTRACTS)
def test_records_are_frozen() -> None:
    for value in (probe(), samples.tick(), NamedValue(name="aa", value=1.0)):
        name = type(value).FIELD_SPECS[0].name
        before = getattr(value, name)
        with pytest.raises(AttributeError):
            setattr(value, name, before)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, name, "changed")
        with pytest.raises(AttributeError):
            delattr(value, name)
        with pytest.raises((AttributeError, TypeError)):
            value.not_a_field = 1  # type: ignore[attr-defined]
        assert getattr(value, name) == before
        assert not hasattr(value, "__dict__"), "slots: nothing can be attached to a record"
    with pytest.raises(TypeError):
        probe().tags[0] = "Z"  # type: ignore[index]


@pytest.mark.requirement(*CONTRACTS)
def test_records_are_equal_by_value_and_hashable() -> None:
    left, right = probe(), probe()
    assert left is not right
    assert left == right
    assert hash(left) == hash(right)
    assert len({left, right, probe(count=4)}) == 2
    assert {left: "found"}[right] == "found"
    assert probe(tags=("B", "A")) == probe(tags=["A", "B"]), "canonical order makes input order irrelevant"
    assert probe(amount=Decimal("12.5")) == probe(amount=Decimal("12.500"))
    assert hash(probe(amount=Decimal("12.5"))) == hash(probe(amount=Decimal("12.500")))
    assert probe(ratio=1) == probe(ratio=1.0)
    assert hash(probe(ratio=1)) == hash(probe(ratio=1.0))
    assert probe(steps=(1, 2)) != probe(steps=(2, 1)), "meaningful order is part of the value"
    assert probe(maybe=None) != probe(maybe=0), "null is not zero"
    assert left != encode_fields(left)
    assert Leaf(name="a", weight=Decimal(1)) != NamedValue(name="aa", value=1.0)
    rebuilt = samples.variants()
    for name, by_variant in samples.variants().items():
        for variant, item in by_variant.items():
            assert isinstance(hash(item), int), f"{name}/{variant} is hashable"
            assert item is not rebuilt[name][variant]
            assert item == rebuilt[name][variant]
            assert hash(item) == hash(rebuilt[name][variant])


# =========================================================================== encode / decode
@pytest.mark.requirement(*CONTRACTS, "S00-ASG-20")
def test_encode_fields_produces_the_documented_wire_representation() -> None:
    wire = encode_fields(probe())
    assert wire == PROBE_WIRE
    assert list(wire) == list(PROBE_WIRE)
    assert type(wire["amount"]) is str
    assert type(wire["colour"]) is str
    assert type(wire["tags"]) is list
    assert type(wire["leaves"][0]) is dict
    assert canonical_dumps(wire) == (
        '{"amount":"12.5","at_ns":1795685400000000000,"colour":"GREEN","count":3,"day":"2026-11-26",'
        '"flag":false,"ident":"' + RUN_ID + '","leaf":null,'
        '"leaves":[{"name":"a","note":"x","weight":"1"},{"name":"b","note":null,"weight":"0.5"}],'
        '"maybe":null,"open_ratio":0.5,"ratio":0.25,"span_ns":5,"steps":[3,1,2],"tags":["A","B"],'
        '"text":"hello world"}'
    )


@pytest.mark.requirement(*CONTRACTS)
def test_decode_fields_inverts_encode_fields() -> None:
    assert _decode(Probe, PROBE_WIRE) == probe()
    assert encode_fields(_decode(Probe, PROBE_WIRE)) == PROBE_WIRE
    assert _decode(Probe, strict_loads(canonical_dumps(PROBE_WIRE))) == probe()
    full = probe(maybe=7, flag=True, leaf=Leaf(name="x", weight=Decimal("0.25"), note="n"))
    assert _decode(Probe, encode_fields(full)) == full
    # Canonical order: any order of a set-like list decodes to the same record; trailing zeros too.
    shuffled = copy.deepcopy(PROBE_WIRE)
    shuffled["tags"] = ["B", "A"]
    shuffled["leaves"] = list(reversed(shuffled["leaves"]))
    shuffled["amount"] = "12.500000000000"
    shuffled["ratio"] = 0.25
    assert _decode(Probe, shuffled) == probe()
    assert encode_fields(_decode(Probe, shuffled)) == PROBE_WIRE
    # Integer literals are valid tokens for float fields.
    assert _decode(Probe, {**PROBE_WIRE, "ratio": 1}).ratio == 1.0
    assert type(_decode(Probe, {**PROBE_WIRE, "ratio": 1}).ratio) is float


BAD_WIRE: list[tuple[tuple[str | int, ...], Any, str]] = [
    (("text",), 5, "text"),
    (("text",), None, "text"),
    (("text",), ["hello"], "text"),
    (("text",), "x" * 13, "text"),
    (("text",), "line\nbreak", "text"),
    (("ident",), "run_1", "ident"),
    (("day",), "2026-02-30", "day"),
    (("day",), 20261126, "day"),
    (("count",), True, "count"),
    (("count",), 3.0, "count"),
    (("count",), "3", "count"),
    (("count",), None, "count"),
    (("count",), 11, "count"),
    (("at_ns",), 1.5, "at_ns"),
    (("at_ns",), -1, "at_ns"),
    (("at_ns",), 2**63, "at_ns"),
    (("span_ns",), "5", "span_ns"),
    (("amount",), 12.5, "amount"),
    (("amount",), 12, "amount"),
    (("amount",), True, "amount"),
    (("amount",), "1e1", "amount"),
    (("amount",), "+12.5", "amount"),
    (("amount",), "012.5", "amount"),
    (("amount",), "12.", "amount"),
    (("amount",), " 12.5", "amount"),
    (("amount",), "12.5\n", "amount"),
    (("amount",), "0", "amount"),
    (("amount",), "1000.01", "amount"),
    (("ratio",), True, "ratio"),
    (("ratio",), "0.25", "ratio"),
    (("ratio",), 2, "ratio"),
    (("ratio",), float("nan"), "ratio"),
    (("ratio",), float("inf"), "ratio"),
    (("open_ratio",), 0, "open_ratio"),
    (("open_ratio",), 1, "open_ratio"),
    (("flag",), 0, "flag"),
    (("flag",), "false", "flag"),
    (("flag",), None, "flag"),
    (("colour",), "BLUE", "colour"),
    (("colour",), "green", "colour"),
    (("colour",), 1, "colour"),
    (("maybe",), "1", "maybe"),
    (("maybe",), False, "maybe"),
    (("tags",), "AB", "tags"),
    (("tags",), {"A": 1}, "tags"),
    (("tags",), None, "tags"),
    (("tags",), ["A", "A"], "tags[1]"),
    (("tags",), ["A", 1], "tags[1]"),
    (("tags",), ["A", "B", "C", "D", "E"], "tags"),
    (("steps",), [1, "2"], "steps[1]"),
    (("steps",), [1, 2.0], "steps[1]"),
    (("leaves",), [], "leaves"),
    (("leaves",), {"name": "a"}, "leaves"),
    (("leaves", 0), "a", "leaves[0]"),
    (("leaves", 0), None, "leaves[0]"),
    (("leaves", 1, "weight"), 0.5, "leaves[1].weight"),
    (("leaves", 1, "weight"), "1.5", "leaves[1].weight"),
    (("leaves", 0, "name"), "TOO LONG NAME", "leaves[0].name"),
    (("leaves", 0, "note"), 5, "leaves[0].note"),
    (("leaf",), [], "leaf"),
    (("leaf",), "a", "leaf"),
    (("leaf",), {"name": "a", "weight": 1, "note": None}, "leaf.weight"),
    (("leaf",), {"name": "a", "weight": "2", "note": None}, "leaf.weight"),
    (("leaf",), {"name": "a", "weight": "1"}, "leaf.note"),
    (("leaf",), {"name": "a", "note": None}, "leaf.weight"),
    (("leaf",), {"name": "a", "weight": "1", "note": None, "extra": 1}, "leaf.extra"),
    (("leaf",), {"name": "zero", "weight": "1", "note": None}, "leaf"),
]


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    ("access", "bad", "path"),
    BAD_WIRE,
    ids=[f"{path}-{index}" for index, (_access, _bad, path) in enumerate(BAD_WIRE)],
)
def test_decoding_is_strict_for_every_field_kind(access: tuple[str | int, ...], bad: Any, path: str) -> None:
    wire = copy.deepcopy(PROBE_WIRE)
    node: Any = wire
    for step in access[:-1]:
        node = node[step]
    node[access[-1]] = bad
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, wire)
    assert caught.value.details["path"] == path
    assert caught.value.details.get("rule") or caught.value.details.get("invariant")
    # Decoding under an outer path (a nested object of some parent) reports the full path.
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, wire, path="outer[2]")
    assert caught.value.details["path"] == f"outer[2].{path}"


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_decode_fields_refuses_unknown_missing_and_misplaced_keys() -> None:
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, {**PROBE_WIRE, "surprise": None})
    assert caught.value.details == {"path": "surprise", "rule": "unknown field"}
    for name in PROBE_WIRE:
        with pytest.raises(ContractValidationError) as caught:
            _decode(Probe, {key: value for key, value in PROBE_WIRE.items() if key != name})
        assert caught.value.details["path"] == name
        assert "missing" in str(caught.value.details["rule"])
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, {**PROBE_WIRE, 5: None})  # type: ignore[dict-item]
    assert caught.value.details["rule"] == "object keys must be strings"
    # The envelope keys are reserved for top-level records only.
    for key in ("contract", "schema_version"):
        with pytest.raises(ContractValidationError) as caught:
            _decode(Probe, {**PROBE_WIRE, key: "x"})
        assert caught.value.details == {"path": key, "rule": "unknown field"}
        assert decode_fields(Probe, {**PROBE_WIRE, key: "x"}, "", STRICT, envelope=True) == probe()
    # A violated invariant of a record decoded under a path names that path.
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, {**PROBE_WIRE, "count": 10, "steps": []}, path="outer")
    assert caught.value.details["path"] == "outer"
    assert caught.value.details["invariant"] == "PROBE-1"


# =========================================================================== versions of fields
@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_a_field_may_be_absent_only_from_a_message_older_than_the_field() -> None:
    """``fld``: 'a message of that version or newer must contain the key, an older message may omit it'."""
    v10, v11, v12 = (DecodeContext(message_version=(1, minor)) for minor in (0, 1, 2))
    base = {"base": 1, "note": None}
    assert _decode(Versioned, base, v10) == Versioned(base=1)
    assert _decode(Versioned, {**base, "added": "x"}, v10) == Versioned(base=1, added="x")
    assert _decode(Versioned, {**base, "added": "x", "later": 5}, v10) == Versioned(
        base=1, added="x", later=5
    )

    with pytest.raises(ContractValidationError) as caught:
        _decode(Versioned, base, v11)
    assert caught.value.details["path"] == "added"
    assert "explicit null" in str(caught.value.details["rule"])
    assert _decode(Versioned, {**base, "added": None}, v11) == Versioned(base=1)
    assert _decode(Versioned, {**base, "added": "x"}, v11).added == "x"

    with pytest.raises(ContractValidationError) as caught:
        _decode(Versioned, {**base, "added": None}, v12)
    assert caught.value.details["path"] == "later"
    with pytest.raises(ContractValidationError) as caught:
        _decode(Versioned, {**base, "later": None}, v12)
    assert caught.value.details["path"] == "added"
    assert _decode(Versioned, {**base, "added": None, "later": None}, v12) == Versioned(base=1)

    # A field of the first version is never optional on the wire, whatever the message version.
    for ctx in (v10, v11, v12, DecodeContext(message_version=(1, 99)), STRICT):
        with pytest.raises(ContractValidationError) as caught:
            _decode(Versioned, {"base": 1, "added": None, "later": None}, ctx)
        assert caught.value.details["path"] == "note"
        with pytest.raises(ContractValidationError) as caught:
            _decode(Versioned, {"note": None, "added": None, "later": None}, ctx)
        assert caught.value.details["path"] == "base"
        assert caught.value.details["rule"] == "required field is missing"

    # Without a declared version nothing may be missing (the strict default).
    assert STRICT.message_version is None
    assert STRICT.ignore_unknown_fields is False
    with pytest.raises(ContractValidationError) as caught:
        _decode(Versioned, base, STRICT)
    assert caught.value.details["path"] == "added"


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_unknown_fields_are_ignored_only_when_the_context_says_so() -> None:
    wire = {"base": 1, "note": None, "added": None, "later": None, "from_the_future": [1, {"a": None}]}
    for version in (None, (1, 0), (1, 2)):
        with pytest.raises(ContractValidationError) as caught:
            _decode(Versioned, wire, DecodeContext(message_version=version))
        assert caught.value.details == {"path": "from_the_future", "rule": "unknown field"}
    tolerant = DecodeContext(ignore_unknown_fields=True, message_version=(1, 3))
    assert _decode(Versioned, wire, tolerant) == Versioned(base=1)
    # Tolerance covers unknown keys only, at every level.
    nested = {**PROBE_WIRE, "leaf": {"name": "a", "weight": "1", "note": None, "new": 1}, "new": 2}
    assert _decode(Probe, nested, tolerant) == probe(leaf=Leaf(name="a", weight=Decimal(1)))
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, {**nested, "colour": "BLUE"}, tolerant)
    assert caught.value.details["path"] == "colour"
    with pytest.raises(ContractValidationError) as caught:
        _decode(Probe, {key: value for key, value in nested.items() if key != "maybe"}, tolerant)
    assert caught.value.details["path"] == "maybe"


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Record]]:
    """A private copy of the contract registry, so a synthetic contract never leaks into other tests."""
    private = dict(registry_module._CONTRACTS)
    monkeypatch.setattr(registry_module, "_CONTRACTS", private)
    return private


def _register_synthetic(name: str, version: str) -> type[Record]:
    @contract(name, version)
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Synthetic(Record):
        """SYNTHETIC top-level contract (registered in an isolated registry copy only)."""

        base: int = fld(Int(ge=0), "Synthetic field of version 1.0.")
        note: str | None = fld(Opt(Str()), "Synthetic optional field of version 1.0.")
        added: str | None = fld(Opt(Str()), "Synthetic optional field added by 1.1.", since=(1, 1))

    return Synthetic


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_reader_rules_for_minor_versions_through_the_codec(
    isolated_registry: dict[str, type[Record]],
) -> None:
    """Section 8 reader rules, exercised with a synthetic contract whose reader is at 1.1.0."""
    synthetic = _register_synthetic("ZzSyntheticVersioned", "1.1.0")
    assert isolated_registry["ZzSyntheticVersioned"] is synthetic
    assert (synthetic.CONTRACT, synthetic.VERSION) == ("ZzSyntheticVersioned", "1.1.0")
    value = synthetic(base=1, note="n", added="a")  # type: ignore[call-arg]
    wire = to_wire(value)
    assert wire == {
        "contract": "ZzSyntheticVersioned",
        "schema_version": "1.1.0",
        "base": 1,
        "note": "n",
        "added": "a",
    }
    assert from_wire(wire) == value
    assert to_json(value) == (
        '{"added":"a","base":1,"contract":"ZzSyntheticVersioned","note":"n","schema_version":"1.1.0"}'
    )

    def message(version: str, **fields: Any) -> dict[str, Any]:
        return {"contract": "ZzSyntheticVersioned", "schema_version": version, **fields}

    # An older MINOR (1.0.x) did not have the field yet: it may be absent, and then reads as null.
    old = from_wire(message("1.0.0", base=1, note=None))
    assert old == synthetic(base=1)  # type: ignore[call-arg]
    assert to_wire(old)["added"] is None, "re-encoded by this reader with the field as explicit null"
    assert to_wire(old)["schema_version"] == "1.1.0"
    assert from_wire(message("1.0.7", base=1, note=None)) == old, "PATCH never affects decoding"
    # ... but it is strict in every other respect.
    with pytest.raises(ContractValidationError) as caught:
        from_wire(message("1.0.0", base=1))
    assert caught.value.details["path"] == "note"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(message("1.0.0", base=1, note=None, unknown=1))
    assert caught.value.details["rule"] == "unknown field"

    # A message of the version that has the field must carry it.
    for version in ("1.1.0", "1.1.9"):
        with pytest.raises(ContractValidationError) as caught:
            from_wire(message(version, base=1, note=None))
        assert caught.value.details["path"] == "added"
        assert from_wire(message(version, base=1, note=None, added=None)) == old
        with pytest.raises(ContractValidationError) as caught:
            from_wire(message(version, base=1, note=None, added=None, unknown=1))
        assert caught.value.details["rule"] == "unknown field"

    # A newer MINOR than the reader: unknown fields are ignored, known ones are still mandatory.
    assert from_wire(message("1.2.0", base=1, note=None, added=None, unknown={"x": 1})) == old
    assert from_wire(message("1.9.3", base=1, note="n", added="a", a=1, b=2)) == value
    with pytest.raises(ContractValidationError) as caught:
        from_wire(message("1.2.0", base=1, note=None, unknown=1))
    assert caught.value.details["path"] == "added"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(message("1.2.0", base=-1, note=None, added=None))
    assert caught.value.details["path"] == "base"

    # Another MAJOR is refused, older or newer.
    for version in ("0.9.0", "2.0.0"):
        with pytest.raises(ContractVersionError) as version_error:
            from_wire(message(version, base=1, note=None, added=None))
        assert version_error.value.details["reader_version"] == "1.1.0"


@pytest.mark.requirement(*CONTRACTS)
def test_registering_a_contract_twice_is_refused(isolated_registry: dict[str, type[Record]]) -> None:
    first = _register_synthetic("ZzSyntheticOnce", "1.0.0")
    with pytest.raises(TypeError, match="already registered"):
        _register_synthetic("ZzSyntheticOnce", "1.0.0")
    with pytest.raises(TypeError, match="already registered"):
        _register_synthetic("ZzSyntheticOnce", "2.0.0")
    assert get_contract("ZzSyntheticOnce") is first
    assert isolated_registry["ZzSyntheticOnce"] is first
    assert contract_version(first) == SemVer(1, 0, 0)
    assert all_contracts()["Tick"] is Tick, "the real contracts are untouched"


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize("version", ["1.0", "1", "v1.0.0", "1.0.0-rc1", "01.0.0", "", "1.0.0 "])
def test_a_contract_needs_a_semantic_version(version: str) -> None:
    before = dict(all_contracts())
    with pytest.raises(ContractVersionError):
        contract("ZzSyntheticNeverRegistered", version)
    assert all_contracts() == before


# =========================================================================== registry
@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
def test_registry_lookup() -> None:
    contracts = all_contracts()
    assert get_contract("Tick") is Tick
    assert contracts["Tick"] is Tick
    for name, cls in contracts.items():
        assert get_contract(name) is cls
        assert name == cls.CONTRACT
        assert contract_version(cls) == parse_semver(cls.VERSION)
        assert cls.FIELD_SPECS, f"{name} declares fields"
    # The returned mapping is a copy: it cannot be used to replace or remove a contract.
    contracts.pop("Tick")
    contracts["Evil"] = Probe
    assert get_contract("Tick") is Tick
    assert "Evil" not in all_contracts()
    for unknown in ("Evil", "tick", "", "Probe", "NamedValue", None, 1, Tick, ["Tick"]):
        with pytest.raises(UnknownContractError) as caught:
            get_contract(unknown)
        assert caught.value.code == "GIE.CONTRACT.UNKNOWN"
    for not_top_level in (Probe, Leaf, NamedValue):
        with pytest.raises(UnknownContractError):
            contract_version(not_top_level)
        with pytest.raises(UnknownContractError):
            to_wire(not_top_level.__new__(not_top_level))


@pytest.mark.requirement(*CONTRACTS)
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.0.0", SemVer(1, 0, 0)),
        ("0.0.0", SemVer(0, 0, 0)),
        ("10.20.30", SemVer(10, 20, 30)),
        ("999999999.999999999.999999999", SemVer(999999999, 999999999, 999999999)),
    ],
)
def test_parse_semver_accepts_major_minor_patch(text: str, expected: SemVer) -> None:
    parsed = parse_semver(text)
    assert parsed == expected
    assert str(parsed) == text
    assert (parsed.major, parsed.minor, parsed.patch) == dataclasses.astuple(expected)


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        "",
        "1",
        "1.0",
        "1.0.0.0",
        "v1.0.0",
        "1.0.0-alpha",
        "1.0.0+build",
        "01.0.0",
        "1.01.0",
        "1.0.01",
        "-1.0.0",
        "1.0.0\n",
        " 1.0.0",
        "1.0.0 ",
        "1..0",
        "a.b.c",
        "1.0.1234567890",
        None,
        1,
        1.0,
        True,
        (1, 0, 0),
        b"1.0.0",
    ],
)
def test_parse_semver_refuses_everything_else(text: Any) -> None:
    with pytest.raises(ContractVersionError) as caught:
        parse_semver(text)
    assert caught.value.code == "GIE.CONTRACT.VERSION"


@pytest.mark.requirement(*CONTRACTS)
def test_semver_orders_numerically() -> None:
    assert parse_semver("1.10.0") > parse_semver("1.9.9")
    assert parse_semver("2.0.0") > parse_semver("1.999.999")
    assert parse_semver("1.0.10") > parse_semver("1.0.9")
    assert sorted(map(parse_semver, ["1.2.0", "1.10.0", "1.1.5"])) == [
        SemVer(1, 1, 5),
        SemVer(1, 2, 0),
        SemVer(1, 10, 0),
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parse_semver("1.0.0").major = 2  # type: ignore[misc]


# =========================================================================== where records may live
def _define(module: str, base: type = Record) -> type:
    return type("Redefined", (base,), {"__module__": module, "__doc__": "SYNTHETIC."})


@pytest.mark.requirement(*CONTRACTS, "S00-ASG-24")
@pytest.mark.parametrize(
    "module",
    [
        "gie.execution.orders",
        "gie.market_data.ticks",
        "gie.core.clock",
        "gie.contracts",  # the package itself is not a contract module
        "gie.contractsx.fake",
        "gie.contracts_private",
        "gie",
        "__main__",
        "builtins",
        "tests_helper.fake",
        "mytests.fake",
        "other.tests.fake",
        "",
    ],
)
def test_record_classes_cannot_be_defined_outside_the_contracts_package(module: str) -> None:
    """Section 1 (COM-06): 'the framework refuses a contract class defined outside gie.contracts'.

    What can be verified from a ``tests.*`` module: the check runs when the class is *created*
    and looks at the module the class claims (``__module__``); a new record, a subclass of a
    shared contract and a subclass of a nested value object are all refused.  What cannot be
    verified here is the behaviour inside a real subsystem package, and a class that *claims* a
    ``gie.contracts.*`` module name is not distinguished from one that is really defined there.
    """
    for base in (Record, Tick, NamedValue, Probe):
        with pytest.raises(TypeError, match=r"may only be defined in gie\.contracts"):
            _define(module, base)


@pytest.mark.requirement(*CONTRACTS)
def test_test_modules_may_define_synthetic_records_but_they_never_pass_as_contracts() -> None:
    assert __name__.startswith("tests."), "this module is importable as tests.gie.contracts.*"
    assert Probe.__module__ == __name__
    local = _define("tests.gie.contracts.synthetic", Tick)
    assert issubclass(local, Tick)
    assert all_contracts()["Tick"] is Tick
    assert local not in all_contracts().values()
    with pytest.raises(UnknownContractError):
        to_wire(probe())
    with pytest.raises(UnknownContractError):
        to_wire(Leaf(name="a", weight=Decimal(1)))
    # A subclass or a look-alike is not accepted where the nested record is expected.
    sub_leaf = type("SubLeaf", (Leaf,), {"__module__": __name__, "__doc__": "SYNTHETIC."})
    impostor = sub_leaf(name="a", weight=Decimal(1))
    assert isinstance(impostor, Leaf)
    for field, value, path in (("leaf", impostor, "leaf"), ("leaves", (impostor,), "leaves[0]")):
        error = _invalid(lambda field=field, value=value: probe(**{field: value}))
        assert error.details["path"] == path
        assert error.details["actual_type"] == "SubLeaf"


# =========================================================================== JSON Schema fragments
@pytest.mark.requirement(*CONTRACTS, "S00-DEL-02")
def test_object_schema_is_generated_from_the_field_declarations() -> None:
    defs: dict[str, Any] = {}
    schema = object_schema(Probe, defs)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["description"] == "SYNTHETIC record with one field of every field type."
    assert list(schema["properties"]) == list(PROBE_WIRE)
    assert schema["required"] == [name for name in PROBE_WIRE if name not in ("maybe", "leaf")]
    assert schema["x-gie-invariants"] == [
        {"id": "PROBE-1", "rule": "count is at most the number of steps plus 9"},
        {"id": "PROBE-2", "rule": "maybe is greater than count when the flag is set"},
    ]
    assert "x-gie-since" not in schema, "only published when a field was added after 1.0"
    properties = schema["properties"]
    assert properties["text"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 12,
        "pattern": PRINTABLE_PATTERN,
        "description": "Synthetic free text.",
    }
    assert properties["ident"]["pattern"] == "^run_[0-9a-f]{32}$"
    assert (properties["ident"]["minLength"], properties["ident"]["maxLength"]) == (36, 36)
    assert properties["day"]["minLength"] == properties["day"]["maxLength"] == 10
    assert properties["count"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "description": "Synthetic count.",
    }
    assert properties["at_ns"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 2**63 - 1,
        "description": "Synthetic instant.",
        "x-gie-unit": "ns since epoch, UTC",
    }
    assert properties["span_ns"]["minimum"] == 0
    assert properties["amount"] == {
        "type": "string",
        "pattern": DECIMAL_PATTERN,
        "x-gie-type": "decimal",
        "x-gie-exclusiveMinimum": "0",
        "x-gie-maximum": "1000",
        "description": "Synthetic amount.",
        "x-gie-unit": "account currency",
    }
    assert properties["ratio"] == {
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "description": "Synthetic closed interval.",
    }
    assert properties["open_ratio"] == {
        "type": "number",
        "exclusiveMinimum": 0.0,
        "exclusiveMaximum": 1.0,
        "description": "Synthetic open interval.",
    }
    assert properties["flag"] == {"type": "boolean", "description": "Synthetic flag."}
    assert properties["colour"] == {
        "type": "string",
        "enum": ["RED", "GREEN"],
        "description": "Synthetic vocabulary term.",
    }
    assert properties["maybe"]["anyOf"] == [
        {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1},
        {"type": "null"},
    ]
    assert properties["tags"]["x-gie-canonical-order"] == "sorted ascending"
    assert properties["tags"]["x-gie-unique"] is True
    assert properties["tags"]["uniqueItems"] is True
    assert (properties["tags"]["minItems"], properties["tags"]["maxItems"]) == (0, 4)
    assert "x-gie-canonical-order" not in properties["steps"]
    assert "x-gie-unique" not in properties["steps"]
    assert properties["leaves"]["items"] == {"$ref": "#/$defs/Leaf"}
    assert (properties["leaves"]["minItems"], properties["leaves"]["maxItems"]) == (1, 3)
    assert properties["leaves"]["x-gie-canonical-order"] == "sorted by name"
    assert properties["leaves"]["x-gie-unique"] is True
    assert properties["leaf"]["anyOf"] == [{"$ref": "#/$defs/Leaf"}, {"type": "null"}]
    assert set(defs) == {"Leaf"}
    assert defs["Leaf"]["required"] == ["name", "weight"]
    assert defs["Leaf"]["additionalProperties"] is False
    assert defs["Leaf"]["x-gie-invariants"] == [{"id": "LEAF-1", "rule": "a leaf named zero weighs nothing"}]
    assert defs["Leaf"]["properties"]["weight"]["x-gie-minimum"] == "0"
    assert defs["Leaf"]["properties"]["weight"]["x-gie-maximum"] == "1"
    assert defs["Leaf"]["properties"]["weight"]["x-gie-unit"] == "fraction"

    versioned = object_schema(Versioned, {})
    assert versioned["x-gie-since"] == {"base": "1.0", "note": "1.0", "added": "1.1", "later": "1.2"}
    assert versioned["required"] == ["base"]
    assert "x-gie-invariants" not in versioned


@pytest.mark.requirement(*CONTRACTS, "S00-DEL-02")
def test_the_generated_schema_fragment_is_valid_json_schema_and_accepts_the_wire_form() -> None:
    defs: dict[str, Any] = {}
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **object_schema(Probe, defs),
        "$defs": defs,
    }
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(PROBE_WIRE)) == []
    full = encode_fields(probe(maybe=7, leaf=Leaf(name="x", weight=Decimal("0.25"), note="n"), tags=()))
    assert list(validator.iter_errors(full)) == []
    refused = [
        {**PROBE_WIRE, "surprise": 1},
        {key: value for key, value in PROBE_WIRE.items() if key != "count"},
        {**PROBE_WIRE, "count": 11},
        {**PROBE_WIRE, "count": "3"},
        {**PROBE_WIRE, "amount": 12.5},
        {**PROBE_WIRE, "amount": "1e3"},
        {**PROBE_WIRE, "amount": "+1"},
        {**PROBE_WIRE, "amount": "01"},
        {**PROBE_WIRE, "ratio": 1.5},
        {**PROBE_WIRE, "open_ratio": 1},
        {**PROBE_WIRE, "flag": 0},
        {**PROBE_WIRE, "colour": "BLUE"},
        {**PROBE_WIRE, "text": "line\nbreak"},
        {**PROBE_WIRE, "text": ""},
        {**PROBE_WIRE, "ident": "run_1"},
        {**PROBE_WIRE, "day": "2026-13-01"},
        {**PROBE_WIRE, "tags": ["A", "A"]},
        {**PROBE_WIRE, "tags": ["A", "B", "C", "D", "E"]},
        {**PROBE_WIRE, "leaves": []},
        {**PROBE_WIRE, "leaves": [{"name": "a", "weight": "1", "note": None, "extra": 1}]},
        {**PROBE_WIRE, "leaf": {"name": "a"}},
        {**PROBE_WIRE, "maybe": "1"},
    ]
    for instance in refused:
        assert not validator.is_valid(instance), instance
        with pytest.raises(ContractValidationError):
            _decode(Probe, instance)


@pytest.mark.requirement(*CONTRACTS, "S00-DEL-02")
def test_the_published_printable_pattern_never_refuses_text_the_framework_accepts() -> None:
    """The schema is necessary, not sufficient: it may accept more than the code, never less."""
    import re

    pattern = re.compile(PRINTABLE_PATTERN)
    free_text = Str(max_len=64)
    for codepoint in [*range(0x250), 0x2028, 0x2029, 0xFEFF, 0xFFFD, 0x1F600, 0xE000, 0x10FFFF]:
        text = "a" + chr(codepoint) + "b"
        try:
            free_text.check(text, "text")
        except ContractValidationError:
            continue
        assert pattern.fullmatch(text), f"U+{codepoint:04X} is accepted by the code but not by the schema"
    for control in [*range(0x20), *range(0x7F, 0xA0)]:
        assert not pattern.fullmatch(chr(control))
        with pytest.raises(ContractValidationError):
            free_text.check(chr(control), "text")
