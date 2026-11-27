"""Acceptance test S00-ACC-01 "Contract round-trip tests" for the ten shared contracts.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (revision 2) section 5
(error semantics), section 7 (serialisation) and section 8 (versioning), and from the generated
field reference ``specifications/contracts/FIELDS.md``:

* the wire form is one JSON object: envelope ``contract`` + ``schema_version`` plus *all* fields,
  optional ones as explicit ``null``;
* decimals are plain strings, instants / counts are exact 64-bit integers, floats are JSON numbers
  that may appear as integer literals;
* canonical JSON bytes are what is journaled, so ``to_json(from_json(text)) == text``;
* decoding is strict: unknown field, missing field (also a missing *optional* field), wrong JSON
  type, unknown enum value, unknown contract and incompatible MAJOR version are errors, never
  repaired;
* the fingerprint is the SHA-256 of contract name, MAJOR version and fields.

All records used here are SYNTHETIC FIXTURES built with ``tests/gie/samples.py``.  They are invented
test data: not market data, not broker data and not evidence of any trading performance.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from gie.contracts import (
    Candidate,
    ClockReading,
    DataOrigin,
    Evidence,
    NamedValue,
    PriceBasis,
    PriceCondition,
    PriceLevel,
    PriceTrigger,
    Record,
    RiskDecision,
    RunManifest,
    Tick,
    TickFlag,
    TradePlan,
    fingerprint,
    from_json,
    from_wire,
    to_bytes,
    to_json,
    to_wire,
)
from gie.contracts._spec import (
    Bool,
    Dec,
    EnumOf,
    FieldSpec,
    FieldType,
    Float,
    Int,
    ListOf,
    Obj,
    Opt,
    Str,
    fld,
    record,
)
from gie.contracts.ordering import stream_name
from gie.contracts.registry import all_contracts, parse_semver
from gie.contracts.schema_gen import json_schema, load_schema
from gie.core.canonical import DECIMAL_PATTERN, canonical_dumps, strict_loads
from gie.core.errors import (
    CanonicalEncodingError,
    ContractValidationError,
    ContractVersionError,
    GieError,
    SequenceConflictError,
    UnknownContractError,
)
from gie.core.ordering import SeqOutcome, SequenceTracker
from gie.core.timeutil import MAX_TIME_NS
from tests.gie import samples

REPO_ROOT = Path(__file__).resolve().parents[3]
FIELDS_REFERENCE = REPO_ROOT / "specifications" / "contracts" / "FIELDS.md"
SHARED_CONTRACTS = REPO_ROOT / "specifications" / "SHARED_CONTRACTS.md"

#: The ten shared contracts of SHARED_CONTRACTS.md section 1.
TEN_CONTRACTS = (
    "RunManifest",
    "Tick",
    "Evidence",
    "Candidate",
    "TradePlan",
    "RiskDecision",
    "OrderEvent",
    "Position",
    "ModelVersion",
    "Clock",
)

#: Requirement that asks for each versioned contract (RunManifest is the tenth, added by section 1).
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

ENVELOPE = ("contract", "schema_version")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_RE = re.compile(DECIMAL_PATTERN)


# --------------------------------------------------------------------------- helpers
@cache
def _variants() -> dict[str, dict[str, Record]]:
    return samples.variants()


def _contract_marks(contract: str) -> list[pytest.MarkDecorator]:
    requirement = CONTRACT_REQUIREMENT.get(contract)
    return [pytest.mark.requirement(requirement)] if requirement else []


VARIANT_PARAMS = [
    pytest.param(contract, variant, id=f"{contract}-{variant}", marks=_contract_marks(contract))
    for contract, by_variant in _variants().items()
    for variant in by_variant
]
CONTRACT_PARAMS = [
    pytest.param(contract, id=contract, marks=_contract_marks(contract)) for contract in TEN_CONTRACTS
]


def _sample(contract: str, variant: str) -> Record:
    return _variants()[contract][variant]


def _first(contract: str) -> Record:
    return next(iter(_variants()[contract].values()))


def _unwrap(ftype: FieldType) -> FieldType:
    return ftype.inner if isinstance(ftype, Opt) else ftype


@dataclass(frozen=True)
class Location:
    """One field of a wire message: how to reach it and the path an error must report."""

    access: tuple[str | int, ...]
    path: str
    spec: FieldSpec


def _locations(record_type: type[Record], wire: dict[str, Any]) -> Iterator[Location]:
    """Every field of *wire*, including the fields of nested objects and of objects in lists."""

    def walk(rtype: type[Record], node: dict[str, Any], access: tuple[str | int, ...], prefix: str) -> Any:
        for spec in rtype.FIELD_SPECS:
            here = (*access, spec.name)
            path = prefix + spec.name
            yield Location(here, path, spec)
            raw = node.get(spec.name)
            inner = _unwrap(spec.ftype)
            if isinstance(inner, Obj) and isinstance(raw, dict):
                yield from walk(inner.record_type, raw, here, path + ".")
            elif isinstance(inner, ListOf) and isinstance(inner.inner, Obj) and isinstance(raw, list):
                for index, item in enumerate(raw):
                    yield from walk(inner.inner.record_type, item, (*here, index), f"{path}[{index}].")

    yield from walk(record_type, wire, (), "")


def _objects(record_type: type[Record], wire: dict[str, Any]) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Access and path of the root object and of every nested object of *wire*."""
    yield (), ""
    for location in _locations(record_type, wire):
        raw = _get(wire, location.access)
        inner = _unwrap(location.spec.ftype)
        if isinstance(inner, Obj) and isinstance(raw, dict):
            yield location.access, location.path
        elif isinstance(inner, ListOf) and isinstance(inner.inner, Obj) and isinstance(raw, list):
            for index in range(len(raw)):
                yield (*location.access, index), f"{location.path}[{index}]"


def _get(root: Any, access: tuple[str | int, ...]) -> Any:
    node = root
    for step in access:
        node = node[step]
    return node


def _with(wire: dict[str, Any], access: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(wire)
    _get(changed, access[:-1])[access[-1]] = value
    return changed


def _without(wire: dict[str, Any], access: tuple[str | int, ...]) -> dict[str, Any]:
    changed = copy.deepcopy(wire)
    del _get(changed, access[:-1])[access[-1]]
    return changed


def _refused(wire: Any, error: type[GieError] = ContractValidationError) -> GieError:
    """Decode *wire* and return the error it must be refused with (exact class)."""
    with pytest.raises(error) as caught:
        from_wire(wire)
    assert type(caught.value) is error
    assert caught.value.code == error.code
    return caught.value


def _assert_same_json(parsed: Any, wire: Any, path: str = "<root>") -> None:
    """*parsed* (canonical text read back with ``strict_loads``) is the JSON value *wire*.

    The text does not say whether a number without a fraction was an integer or a double
    (``gie.core.canonical``): an integral double is read back as an integer literal - and only as
    its canonical rendering (``_spec.Float``).  Every other value must be identical, type included.
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


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"{old!r} must occur exactly once in the fixture text"
    return text.replace(old, new)


def _documented_fields(contract: str) -> dict[str, tuple[str, bool]]:
    """Field name -> (type text, required) from the generated field reference FIELDS.md."""
    lines = FIELDS_REFERENCE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if re.fullmatch(rf"## {contract} \d+\.\d+\.\d+", line))
    fields: dict[str, tuple[str, bool]] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)]
        if len(cells) >= 6 and cells[1].startswith("`"):
            fields[cells[1].strip("`")] = (cells[2].replace("\\|", "|"), cells[3] == "yes")
    assert fields, f"no field table found for {contract}"
    return fields


@cache
def _validator(contract: str) -> jsonschema.Draft202012Validator:
    schema = json_schema(all_contracts()[contract])
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


# A subclass of a contract and a look-alike class.  Both may be *defined* here only because this
# module lives under ``tests.``; neither is the registered contract class.
class SubTick(Tick):  # type: ignore[misc]
    """SYNTHETIC subclass of a shared contract (must never pass as the contract)."""


class SubPriceLevel(PriceLevel):
    """SYNTHETIC subclass of a nested value object."""


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class LookAlikeTick(Record):
    """SYNTHETIC look-alike that claims the name and version of the Tick contract."""

    symbol: str = fld(Str(), "Synthetic symbol.")
    seq: int = fld(Int(ge=0), "Synthetic sequence number.")


LookAlikeTick.CONTRACT = "Tick"
LookAlikeTick.VERSION = "1.0.0"


def _fields_of(value: Record) -> dict[str, Any]:
    return {item.name: getattr(value, item.name) for item in dataclasses.fields(value)}  # type: ignore[arg-type]


# =========================================================================== registry and samples
@pytest.mark.requirement("S00-ACC-01", *CONTRACT_REQUIREMENT.values())
@pytest.mark.acceptance("S00-ACC-01")
def test_the_ten_documented_contracts_are_registered_and_sampled() -> None:
    prose = SHARED_CONTRACTS.read_text(encoding="utf-8")
    section_1 = prose[prose.index("## 1. Scope and data flow") : prose.index("## 2. Units")]
    table = re.findall(r"^\| `(\w+)` \|", section_1, re.M)
    assert sorted(table) == sorted(TEN_CONTRACTS), "section 1 of SHARED_CONTRACTS.md lists the ten contracts"
    registered = all_contracts()
    shared = {name for name, cls in registered.items() if cls.__module__.startswith("gie.contracts.")}
    assert shared == set(TEN_CONTRACTS)
    assert set(_variants()) == set(TEN_CONTRACTS)
    assert sum(len(by_variant) for by_variant in _variants().values()) >= 45
    for name in TEN_CONTRACTS:
        cls = registered[name]
        assert name == cls.CONTRACT
        assert cls.VERSION is not None
        assert parse_semver(cls.VERSION).major == 1
        assert _variants()[name], f"{name} has no sample variant"
        assert all(type(item) is cls for item in _variants()[name].values())


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_every_sample_is_labelled_as_a_synthetic_fixture() -> None:
    checked = 0
    for by_variant in _variants().values():
        for item in by_variant.values():
            wire = to_wire(item)
            for key in ("origin", "data_origin", "training_data_origin"):
                if wire.get(key) is not None:
                    assert wire[key] == "SYNTHETIC_FIXTURE"
                    checked += 1
    assert checked >= 40


# =========================================================================== round trip
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_from_json_of_to_json_is_the_same_record(contract: str, variant: str) -> None:
    original = _sample(contract, variant)
    back = from_json(to_json(original))
    assert type(back) is all_contracts()[contract]
    assert back == original
    assert hash(back) == hash(original)
    assert _fields_of(back) == _fields_of(original)


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_to_json_of_from_json_is_the_same_canonical_text(contract: str, variant: str) -> None:
    text = to_json(_sample(contract, variant))
    assert to_json(from_json(text)) == text
    assert to_json(from_json(text.encode("utf-8"))) == text
    # Canonical: the canonical form of the parsed text is the text itself (sorted keys, no whitespace).
    assert canonical_dumps(strict_loads(text)) == text
    assert "\n" not in text
    assert ": " not in text
    assert ", " not in text


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_wire_round_trip_and_bytes(contract: str, variant: str) -> None:
    original = _sample(contract, variant)
    wire = to_wire(original)
    assert from_wire(wire) == original
    assert from_wire(wire, expect=type(original)) == original
    assert from_wire(strict_loads(to_json(original))) == original
    _assert_same_json(strict_loads(to_json(original)), wire)
    assert canonical_dumps(strict_loads(to_json(original))) == canonical_dumps(wire) == to_json(original)
    payload = to_bytes(original)
    assert isinstance(payload, bytes)
    assert payload == to_json(original).encode("utf-8")
    assert from_json(payload) == original
    # to_wire hands out a fresh structure: changing it cannot change the record.
    wire[ENVELOPE[0]] = "Changed"
    assert to_wire(original)[ENVELOPE[0]] == contract


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_envelope_and_every_documented_field_is_written(contract: str, variant: str) -> None:
    original = _sample(contract, variant)
    wire = to_wire(original)
    cls = all_contracts()[contract]
    assert wire["contract"] == contract
    assert wire["schema_version"] == cls.VERSION
    documented = _documented_fields(contract)
    assert set(wire) == set(ENVELOPE) | set(documented), (
        "envelope plus every field of FIELDS.md, nothing else"
    )
    for name, (type_text, required) in documented.items():
        assert name in wire
        assert type_text.endswith("| null") is (not required)
        if required:
            assert wire[name] is not None, f"{name} is required and can never be null"
        assert (wire[name] is None) is (getattr(original, name) is None)


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_optional_fields_are_written_as_explicit_null_at_every_level(contract: str, variant: str) -> None:
    original = _sample(contract, variant)
    cls = all_contracts()[contract]
    wire = strict_loads(to_json(original))
    for access, _path in _objects(cls, wire):
        node = _get(wire, access)
        rtype = cls if not access else _nested_type(cls, wire, access)
        expected = {spec.name for spec in rtype.FIELD_SPECS}
        if not access:
            expected |= set(ENVELOPE)
        assert set(node) == expected
        if access:
            assert "schema_version" not in node, "nested value objects carry no envelope"
    nulls = [location for location in _locations(cls, wire) if _get(wire, location.access) is None]
    for location in nulls:
        assert location.spec.ftype.optional, f"{location.path} is null but not optional"
        assert f'"{location.spec.name}":null' in to_json(original)


def _nested_type(cls: type[Record], wire: dict[str, Any], access: tuple[str | int, ...]) -> type[Record]:
    for location in _locations(cls, wire):
        inner = _unwrap(location.spec.ftype)
        if isinstance(inner, Obj) and location.access == access:
            return inner.record_type
        if isinstance(inner, ListOf) and isinstance(inner.inner, Obj) and location.access == access[:-1]:
            return inner.inner.record_type
    raise AssertionError(f"no nested object at {access}")


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_wire_values_have_the_json_type_of_their_unit(contract: str, variant: str) -> None:
    """Section 2 and 7: decimals are plain strings, instants / counts exact integers, floats numbers."""
    cls = all_contracts()[contract]
    wire = to_wire(_sample(contract, variant))

    def check(ftype: FieldType, raw: Any, path: str) -> None:
        inner = _unwrap(ftype)
        if raw is None:
            assert ftype.optional, path
        elif isinstance(inner, Dec):
            assert type(raw) is str, path
            assert DECIMAL_RE.fullmatch(raw), path
            assert "." not in raw or not raw.endswith("0"), (
                f"{path}: canonical decimals have no trailing zeros"
            )
            assert raw != "-0", path
        elif isinstance(inner, Int):
            assert type(raw) is int, path
            assert -(2**63) <= raw <= 2**63 - 1, path
        elif isinstance(inner, Float):
            assert type(raw) is float, path
            assert math.isfinite(raw), path
        elif isinstance(inner, Bool):
            assert type(raw) is bool, path
        elif isinstance(inner, EnumOf):
            assert type(raw) is str, path
            assert raw in [member.value for member in inner.enum_type], path
        elif isinstance(inner, Str):
            assert type(raw) is str, path
            assert raw.isprintable(), path
        elif isinstance(inner, ListOf):
            assert type(raw) is list, path
            for index, item in enumerate(raw):
                if not isinstance(inner.inner, Obj):
                    check(inner.inner, item, f"{path}[{index}]")
                else:
                    assert type(item) is dict, path
        elif isinstance(inner, Obj):
            assert type(raw) is dict, path
        else:  # pragma: no cover - a new field type must be added to this test
            raise AssertionError(f"{path}: unknown field type {type(inner).__name__}")

    locations = list(_locations(cls, wire))
    assert locations
    for location in locations:
        check(location.spec.ftype, _get(wire, location.access), location.path)
    # The text form is plain JSON that any JSON parser reads to the same structure.
    _assert_same_json(json.loads(to_json(_sample(contract, variant))), wire)


@pytest.mark.requirement("S00-ACC-01", "S00-DEL-02")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_wire_form_validates_against_the_generated_json_schema(contract: str, variant: str) -> None:
    cls = all_contracts()[contract]
    instance = strict_loads(to_json(_sample(contract, variant)))
    schema = json_schema(cls)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["contract"] == {"const": contract, "description": "Contract name."}
    assert list(_validator(contract).iter_errors(instance)) == []
    # The committed schema of the declared version says the same.
    assert cls.VERSION is not None
    published = load_schema(contract, cls.VERSION)
    assert published == schema
    assert list(jsonschema.Draft202012Validator(published).iter_errors(instance)) == []


@pytest.mark.requirement("S00-DEL-02", "S00-ASG-24")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_json_schema_refuses_what_the_envelope_and_field_rules_forbid(contract: str) -> None:
    """The schema is 'necessary': it must at least refuse unknown keys, wrong names and missing fields."""
    instance = strict_loads(to_json(_first(contract)))
    validator = _validator(contract)
    assert validator.is_valid(instance)
    assert not validator.is_valid({**instance, "zzz_unknown": 1})
    assert not validator.is_valid({**instance, "contract": "Other"})
    assert not validator.is_valid({**instance, "schema_version": "1.0"})
    assert not validator.is_valid({key: value for key, value in instance.items() if key != "contract"})
    assert not validator.is_valid({key: value for key, value in instance.items() if key != "schema_version"})
    for name, (_type_text, required) in _documented_fields(contract).items():
        if required:
            assert not validator.is_valid({k: v for k, v in instance.items() if k != name}), name
            assert not validator.is_valid({**instance, name: None}), name


@pytest.mark.requirement("S00-DEL-02", "S00-ASG-24")
def test_json_schema_refuses_a_message_without_an_optional_key() -> None:
    """Section 7: 'a key that is missing from a message whose version already has the field is an error'.

    The decoder refuses such a message.  The published schema of the same version is the
    machine-readable form of the contract and JSON Schema can express key presence, so a message
    of version 1.0.0 that omits a 1.0 field must not validate either (section 7 lists what the
    schemas cannot express; key presence is not on that list).
    """
    accepted_by_schema: list[str] = []
    for contract in TEN_CONTRACTS:
        instance = strict_loads(to_json(_first(contract)))
        for name, (_type_text, required) in _documented_fields(contract).items():
            if required:
                continue
            without = {key: value for key, value in instance.items() if key != name}
            _refused(without)  # the reference implementation refuses it ...
            if _validator(contract).is_valid(without):  # ... and so must the schema
                accepted_by_schema.append(f"{contract}.{name}")
    assert accepted_by_schema == []


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-16", "S00-ASG-17", "S00-ASG-18")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    "chain",
    ["order_lifecycle", "protective_stop_lifecycle", "position_lifecycle", "model_lifecycle"],
)
def test_lifecycle_chains_round_trip(chain: str) -> None:
    records = getattr(samples, chain)()
    assert len(records) >= 3
    texts = [to_json(item) for item in records]
    assert [from_json(text) for text in texts] == records
    assert [to_json(from_json(text)) for text in texts] == texts
    assert len(set(texts)) == len(texts)


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
def test_distinct_samples_have_distinct_canonical_bytes() -> None:
    texts: dict[str, str] = {}
    for contract, by_variant in _variants().items():
        for variant, item in by_variant.items():
            text = to_json(item)
            assert text not in texts, f"{contract}/{variant} encodes like {texts.get(text)}"
            texts[text] = f"{contract}/{variant}"
    assert len(texts) == len(VARIANT_PARAMS)


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_strict_but_non_canonical_json_decodes_to_the_same_record(contract: str, variant: str) -> None:
    original = _sample(contract, variant)
    canonical = to_json(original)
    parsed = json.loads(canonical)
    reordered = dict(reversed(list(parsed.items())))
    pretty = json.dumps(reordered, indent=3, ensure_ascii=True) + "\n"
    assert pretty != canonical
    assert from_json(pretty) == original
    assert to_json(from_json(pretty)) == canonical
    assert from_json(canonical + "\n") == original


# =========================================================================== units on the wire
@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20", "S00-ASG-11")
@pytest.mark.acceptance("S00-ACC-01")
def test_decimals_are_written_without_trailing_zeros_and_read_with_them() -> None:
    tick = samples.tick(bid=Decimal("2650.10"), ask=Decimal("2650.3500"), bid_volume=Decimal("3.000"))
    wire = to_wire(tick)
    assert wire["bid"] == "2650.1"
    assert wire["ask"] == "2650.35"
    assert wire["bid_volume"] == "3"
    padded = {**wire, "bid": "2650.100", "ask": "2650.350000000000", "bid_volume": "3.0"}
    decoded = from_wire(padded)
    assert decoded == tick
    assert decoded.bid == Decimal("2650.1")
    assert to_json(decoded) == to_json(tick), "trailing zeros are accepted and normalised on output"
    assert fingerprint(decoded) == fingerprint(tick)
    assert hash(decoded) == hash(tick)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-11")
@pytest.mark.parametrize(
    ("in_memory", "on_wire"),
    [
        (Decimal("0"), "0"),
        (Decimal("-0"), "0"),
        (Decimal("0.000"), "0"),
        (Decimal("1E+2"), "100"),
        (Decimal("1.50"), "1.5"),
        (Decimal("0.000000000001"), "0.000000000001"),
        (Decimal("999999999999999999.999999999999"), "999999999999999999.999999999999"),
    ],
)
def test_decimal_canonical_form(in_memory: Decimal, on_wire: str) -> None:
    tick = samples.tick(bid_volume=in_memory)
    assert to_wire(tick)["bid_volume"] == on_wire
    assert from_json(to_json(tick)) == tick
    assert from_json(to_json(tick)).bid_volume == in_memory


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20", "S00-ASG-11")
@pytest.mark.acceptance("S00-ACC-01")
def test_nanosecond_instants_survive_exactly_up_to_the_largest_instant() -> None:
    for instant in (0, 1, 2**53 + 1, MAX_TIME_NS - 1, MAX_TIME_NS):
        tick = samples.tick(event_time_ns=instant, receive_time_ns=instant, seq=instant)
        text = to_json(tick)
        assert f'"receive_time_ns":{instant},' in text, "integers are written as exact digits"
        back = from_json(text)
        assert back == tick
        assert type(back.receive_time_ns) is int
        assert back.receive_time_ns == instant
        assert back.seq == instant
    assert MAX_TIME_NS == 2**63 - 1


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20", "S00-ASG-12")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(
    ("value", "token"),
    [
        (1.0, "1"),
        (0.0, "0"),
        (-0.0, "0"),
        (-3.0, "-3"),
        (0.1, "0.1"),
        (1e-7, "1e-7"),
        (2.0**53, "9007199254740992"),
        (1e18, "1000000000000000000"),
        (1e19, "10000000000000000000"),
        (-1e19, "-10000000000000000000"),
        (2.0**63, "9223372036854776000"),
        (1e21, "1e+21"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (5e-324, "5e-324"),
    ],
)
def test_float_fields_use_ecmascript_number_tokens_and_round_trip(value: float, token: str) -> None:
    """Section 7: '1.0 is written 1; 1e19 is written 10000000000000000000'."""
    item = samples.evidence(features=(NamedValue(name="sweep_depth", value=value),))
    text = to_json(item)
    assert f'"features":[{{"name":"sweep_depth","value":{token}}}]' in text
    back = from_json(text)
    assert back == item
    assert type(back.features[0].value) is float
    assert back.features[0].value == value
    assert to_json(back) == text


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-20", "S00-ASG-12", "S00-ASG-18")
@pytest.mark.acceptance("S00-ACC-01")
def test_every_finite_double_of_a_float_field_round_trips_also_between_2_53_and_2_63() -> None:
    """A valid record must survive ``from_json(to_json(r))`` whatever finite double it carries.

    Doubles from 2**53 up to 2**63 are integers.  The canonical writer renders them with the
    ECMAScript shortest-digits rule (for example ``1234567890123456800`` for the double
    1234567890123456768), which is an integer literal inside the 64-bit range - the reader must
    accept exactly what the writer of the same version emits.
    """
    values = [
        1.2345678901234568e18,
        4.611686018427388e18,
        float(1_795_685_400_123_456_789),  # a nanosecond instant used as a feature value
        -7.3786976294838206e18 / 2,
        9007199254740993.0 * 3,
    ]
    failures: list[str] = []
    for value in values:
        item = samples.evidence(features=(NamedValue(name="sweep_depth", value=value),))
        text = to_json(item)
        token = text[text.index('"value":') :].split("}")[0]
        try:
            back = from_json(text)
        except GieError as exc:
            failures.append(f"{value!r}: written as {token} and then refused by the reader: {exc}")
            continue
        if back != item or to_json(back) != text:
            failures.append(f"{value!r}: decoded to a different record")
    assert failures == []


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24", "S00-ASG-12")
def test_integer_literals_in_float_fields_are_accepted_only_when_exact() -> None:
    text = to_json(samples.evidence())
    assert from_json(_replace_once(text, '"bullish_score":0.7', '"bullish_score":1')).bullish_score == 1.0
    assert (
        type(from_json(_replace_once(text, '"bullish_score":0.7', '"bullish_score":1')).bullish_score)
        is float
    )

    def feature(token: str) -> Any:
        return from_json(_replace_once(text, '"value":0.6', f'"value":{token}')).features[0].value

    assert feature("9007199254740992") == 2.0**53
    assert feature("-9007199254740992") == -(2.0**53)
    assert feature("10000000000000000000") == 1e19
    assert feature("9223372036854776000") == 2.0**63
    for token in ("9007199254740993", "9223372036854775807", "-9223372036854775807"):
        with pytest.raises(ContractValidationError) as caught:
            feature(token)
        assert caught.value.details["path"] == "features[0].value"
        assert "exactly representable" in str(caught.value.details["rule"])
    # Beyond 64 bits only the canonical rendering of a double exists (strict parser, section 7).
    for token in ("9223372036854775808", "10000000000000000001", "1" + "0" * 400):
        with pytest.raises(CanonicalEncodingError):
            feature(token)
    # The same rule for a JSON-compatible dict that never was text.
    wire = to_wire(samples.evidence())
    assert from_wire(_with(wire, ("features", 0, "value"), 10**19)).features[0].value == 1e19
    for unrepresentable in (2**53 + 1, 2**63 - 1, 10**400):
        error = _refused(_with(wire, ("features", 0, "value"), unrepresentable))
        assert error.details["path"] == "features[0].value"


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24", "S00-ASG-12")
@pytest.mark.parametrize(
    ("token", "double"),
    [
        ("1", 1.0),
        ("0", 0.0),
        ("9007199254740992", 2.0**53),
        ("1234567890123456800", 1.2345678901234568e18),  # exact value 1234567890123456768
        ("4611686018427388000", 4.611686018427388e18),
        ("-1234567890123456800", -1.2345678901234568e18),
        ("1000000000000000000", 1e18),
        ("9223372036854775000", 9.223372036854775e18),  # the largest double below 2**63
        ("9223372036854774784", None),  # the exact value of that double, not its rendering
        ("1234567890123456768", None),  # the exact value of a double, but not its canonical rendering
        ("1234567890123456770", None),  # the same double, other digits
        ("9007199254740993", None),  # not a double at all
        ("-1234567890123456768", None),
    ],
)
def test_a_float_field_takes_an_integer_literal_exactly_when_it_renders_a_double(
    token: str, double: float | None
) -> None:
    """Change notice 12: 'A float field accepts an integer literal exactly when it is the canonical
    rendering of a double' - from text and from a JSON-compatible dict alike."""
    text = to_json(samples.evidence())
    damaged_text = _replace_once(text, '"value":0.6', f'"value":{token}')
    wire = _with(to_wire(samples.evidence()), ("features", 0, "value"), int(token))
    if double is not None:
        assert canonical_dumps(double) == token
        for decoded in (from_json(damaged_text), from_wire(wire)):
            assert type(decoded.features[0].value) is float
            assert decoded.features[0].value == double
        assert to_json(from_json(damaged_text)) == damaged_text, "the literal is what the writer emits"
    else:
        assert canonical_dumps(float(int(token))) != token
        with pytest.raises(ContractValidationError) as caught:
            from_json(damaged_text)
        assert type(caught.value) is ContractValidationError
        assert caught.value.details["path"] == "features[0].value"
        assert "canonical rendering of a double" in str(caught.value.details["rule"])
        error = _refused(wire)
        assert error.details["path"] == "features[0].value"
        assert "canonical rendering of a double" in str(error.details["rule"])


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24", "S00-ASG-12")
def test_nan_and_infinity_are_refused_everywhere() -> None:
    text = to_json(samples.evidence())
    for token in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(CanonicalEncodingError) as caught:
            from_json(_replace_once(text, '"value":0.6', f'"value":{token}'))
        assert caught.value.code == "GIE.CONTRACT.ENCODING"
    with pytest.raises(CanonicalEncodingError):
        from_json(_replace_once(text, '"value":0.6', '"value":1e400'))
    wire = to_wire(samples.evidence())
    for bad in (float("nan"), float("inf"), float("-inf")):
        error = _refused(_with(wire, ("features", 0, "value"), bad))
        assert error.details["path"] == "features[0].value"
        assert "finite" in str(error.details["rule"])
        error = _refused(_with(wire, ("bullish_score",), bad))
        assert error.details["path"] == "bullish_score"
        with pytest.raises(ContractValidationError):
            NamedValue(name="sweep_depth", value=bad)
        with pytest.raises(ContractValidationError):
            samples.evidence(bullish_score=bad)
    for bad_decimal in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ContractValidationError) as caught_decimal:
            samples.tick(bid=bad_decimal)
        assert caught_decimal.value.details["path"] == "bid"


# =========================================================================== strict decoding
def _wrong_json_values(spec: FieldSpec) -> list[Any]:
    """JSON values of the wrong type (or wrong vocabulary) for a field, by field kind."""
    inner = _unwrap(spec.ftype)
    if isinstance(inner, Int):
        return [True, False, 1.0, 0.5, "1", [1], {"value": 1}]
    if isinstance(inner, Dec):
        return [
            1,
            1.5,
            True,
            ["1"],
            {"value": "1"},
            "1e3",
            "1E3",
            "+1",
            "01",
            "1.",
            ".5",
            " 1",
            "1 ",
            "1\n",
            "",
        ]
    if isinstance(inner, Float):
        return [True, False, "0.5", [0.5], {"value": 0.5}]
    if isinstance(inner, Bool):
        return [0, 1, "true", "false", [], 1.0]
    if isinstance(inner, EnumOf):
        member = next(iter(inner.enum_type)).value
        return ["NO_SUCH_MEMBER", "", member.lower(), f" {member}", 1, True, [member], {"value": member}]
    if isinstance(inner, ListOf):
        return [{}, "x", 1, True, 1.5]
    if isinstance(inner, Obj):
        return [[], "x", 1, True, 1.5]
    if isinstance(inner, Str):
        return [1, 1.5, True, ["x"], {"value": "x"}]
    raise AssertionError(f"unknown field type {type(inner).__name__}")  # pragma: no cover


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_wrong_json_types_are_refused_for_every_field_with_its_full_path(contract: str) -> None:
    cls = all_contracts()[contract]
    refused = 0
    kinds: set[str] = set()
    for item in _variants()[contract].values():
        wire = to_wire(item)
        for location in _locations(cls, wire):
            kinds.add(type(_unwrap(location.spec.ftype)).__name__)
            wrong = _wrong_json_values(location.spec)
            if not location.spec.ftype.optional:
                wrong = [*wrong, None]
            for bad in wrong:
                error = _refused(_with(wire, location.access, bad))
                assert error.details.get("path") == location.path, f"{location.path} = {bad!r}: {error}"
                assert error.details.get("rule"), f"{location.path} = {bad!r}: the violated rule is named"
                refused += 1
    assert refused > 100
    assert kinds, "the walk visited the fields of the contract"


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_unknown_field_is_refused_in_a_same_version_message_at_every_level(contract: str) -> None:
    cls = all_contracts()[contract]
    objects = 0
    for item in _variants()[contract].values():
        wire = to_wire(item)
        for access, path in _objects(cls, wire):
            error = _refused(_with(wire, (*access, "zzz_unknown"), None))
            assert error.details["path"] == (f"{path}.zzz_unknown" if path else "zzz_unknown")
            assert error.details["rule"] == "unknown field"
            objects += 1
    assert objects >= len(_variants()[contract])


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_missing_field_is_refused_required_or_optional_at_every_level(contract: str) -> None:
    """Writers always write null, so a missing optional key is corruption, not 'null' (section 7)."""
    cls = all_contracts()[contract]
    required = optional = 0
    for item in _variants()[contract].values():
        wire = to_wire(item)
        for location in _locations(cls, wire):
            error = _refused(_without(wire, location.access))
            assert error.details["path"] == location.path
            assert "missing" in str(error.details["rule"])
            if location.spec.ftype.optional:
                assert "explicit null" in str(error.details["rule"])
                optional += 1
            else:
                required += 1
    assert required > 0
    if any(not required_flag for _text, required_flag in _documented_fields(contract).values()):
        assert optional > 0


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
def test_json_number_tokens_do_not_decide_the_type_the_field_does() -> None:
    text = to_json(samples.tick())
    for token in ("0.0", "1e0", "-0.0", "0.5", '"0"', "true", "null", "[0]"):
        with pytest.raises(ContractValidationError) as caught:
            from_json(_replace_once(text, '"seq":0,', f'"seq":{token},'))
        assert caught.value.details["path"] == "seq"
    for token in ("2650.1", "2650", "true", "null", '["2650.1"]'):
        with pytest.raises(ContractValidationError) as caught:
            from_json(_replace_once(text, '"bid":"2650.1"', f'"bid":{token}'))
        assert caught.value.details["path"] == "bid"
    for token in ("5", "5.5", "true", "null", '["XAUUSD"]'):
        with pytest.raises(ContractValidationError) as caught:
            from_json(_replace_once(text, '"symbol":"XAUUSD"', f'"symbol":{token}'))
        assert caught.value.details["path"] == "symbol"
    # An integer beyond 64 bits is never an instant: the strict parser refuses the literal ...
    for token in ("9223372036854775808", "-9223372036854775809", "10000000000000000001"):
        with pytest.raises(CanonicalEncodingError) as beyond:
            from_json(_replace_once(text, '"seq":0,', f'"seq":{token},'))
        assert beyond.value.code == "GIE.CONTRACT.ENCODING"
    # ... unless it is the canonical rendering of a double (1e19), and a double is not an integer.
    with pytest.raises(ContractValidationError) as caught:
        from_json(_replace_once(text, '"seq":0,', '"seq":10000000000000000000,'))
    assert caught.value.details["path"] == "seq"
    # The largest instant itself is read exactly.
    largest = from_json(_replace_once(text, '"seq":0,', f'"seq":{MAX_TIME_NS},'))
    assert largest.seq == MAX_TIME_NS


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-20", "S00-ASG-11")
@pytest.mark.parametrize(
    "text",
    [
        "1e3",
        "1E3",
        "1e-3",
        "+1",
        "01",
        "00",
        "-01",
        "1.",
        ".5",
        "-.5",
        " 1",
        "1 ",
        "1\n",
        "\t1",
        "",
        "-",
        "--1",
        "1.2.3",
        "1,5",
        "1_000",
        "0x10",
        "NaN",
        "Infinity",
        "-Infinity",
        "\u0661",  # ARABIC-INDIC DIGIT ONE: a Unicode digit, not an ASCII digit
        "\uff11",  # FULLWIDTH DIGIT ONE
        "1234567890123456789",  # 19 integer digits
        "0.1234567890123",  # 13 fractional digits
    ],
)
def test_non_canonical_decimal_strings_are_refused(text: str) -> None:
    """Section 7: plain notation only - no exponent, no '+', no leading zeros, 18 + 12 digits."""
    wire = to_wire(samples.tick())
    for field in ("bid", "ask", "bid_volume"):
        error = _refused({**wire, field: text})
        assert error.details["path"] == field
        assert error.details["rule"] == "must be a plain decimal string"
    # ... also when the message arrives as JSON text.
    message = _replace_once(to_json(samples.tick()), '"bid":"2650.1"', f'"bid":{json.dumps(text)}')
    with pytest.raises(ContractValidationError) as caught:
        from_json(message)
    assert caught.value.details["path"] == "bid"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-20", "S00-ASG-11")
def test_decimal_bounds_are_enforced_when_decoding() -> None:
    wire = to_wire(samples.tick())
    for text in ("0", "0.0", "-0", "-0.01", "-2650.1"):
        error = _refused({**wire, "bid": text})
        assert error.details["path"] == "bid"
        assert error.details["rule"] == "must be > 0"
    assert from_wire({**wire, "bid_volume": "0"}).bid_volume == 0
    assert from_wire({**wire, "bid_volume": "-0"}).bid_volume == 0
    assert to_wire(from_wire({**wire, "bid_volume": "-0.0"}))["bid_volume"] == "0"
    error = _refused({**wire, "bid_volume": "-0.000000000001"})
    assert error.details["path"] == "bid_volume"
    assert error.details["rule"] == "must be >= 0"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11", "S00-ASG-12")
def test_unknown_enum_values_are_refused_fail_closed() -> None:
    wire = to_wire(samples.tick())
    for bad in ("LIVE", "synthetic_fixture", "SYNTHETIC_FIXTURE ", "", "BROKER_FEED\n"):
        error = _refused({**wire, "origin": bad})
        assert error.details["path"] == "origin"
        assert "unknown DataOrigin value" in str(error.details["rule"])
    error = _refused({**wire, "flags": ["RECEIVE_TIME_MODELLED", "NO_SUCH_FLAG"]})
    assert error.details["path"] == "flags[1]"
    evidence_wire = to_wire(samples.evidence())
    error = _refused(_with(evidence_wire, ("invalidation", "basis"), "LAST"))
    assert error.details["path"] == "invalidation.basis"
    # No live run mode and no real venue exist in the vocabulary.
    for bad_mode in ("LIVE", "REAL", "PRODUCTION", "DEMO"):
        error = _refused({**evidence_wire, "mode": bad_mode})
        assert error.details["path"] == "mode"
    order_wire = to_wire(samples.order_event())
    for bad_venue in ("LIVE", "MT5", "BROKER", "DEMO"):
        error = _refused({**order_wire, "venue": bad_venue})
        assert error.details["path"] == "venue"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-12", "S00-ASG-13", "S00-ASG-14", "S00-ASG-18")
@pytest.mark.parametrize(
    ("builder", "access", "bad", "path"),
    [
        ("evidence", ("levels", 0, "price"), 2648.4, "levels[0].price"),
        ("evidence", ("levels", 0, "price"), "1e3", "levels[0].price"),
        ("evidence", ("levels", 0, "identity"), 1, "levels[0].identity"),
        ("evidence", ("levels", 0, "role"), "swept level", "levels[0].role"),
        ("evidence", ("levels", 0), "SWEPT_LEVEL", "levels[0]"),
        ("evidence", ("features", 0, "name"), "BAD NAME", "features[0].name"),
        ("evidence", ("features", 0, "value"), "0.6", "features[0].value"),
        ("evidence", ("invalidation",), [1], "invalidation"),
        ("evidence", ("invalidation", "trigger"), "TOUCH", "invalidation.trigger"),
        ("evidence", ("invalidation", "price"), 2647.9, "invalidation.price"),
        ("candidate", ("destinations", 0, "price"), "-1", "destinations[0].price"),
        ("candidate", ("destinations", 0, "probability_frac"), 1.5, "destinations[0].probability_frac"),
        ("candidate", ("destinations", 0, "horizon_ns"), -1, "destinations[0].horizon_ns"),
        ("candidate", ("net_r_quantiles", 2, "q_frac"), 1.0, "net_r_quantiles[2].q_frac"),
        ("candidate", ("net_r_quantiles", 1, "q_frac"), 0, "net_r_quantiles[1].q_frac"),
        ("candidate", ("contributions", 1, "weight_r"), "0.05", "contributions[1].weight_r"),
        ("candidate", ("contributions", 0, "evidence_id"), "evd_123", "contributions[0].evidence_id"),
        ("candidate", ("market_event_ids", 0), "mev_XYZ", "market_event_ids[0]"),
        ("trade_plan", ("targets", 1, "close_frac"), "1.5", "targets[1].close_frac"),
        ("trade_plan", ("targets", 0, "price"), "0", "targets[0].price"),
        ("trade_plan", ("targets", 0, "label"), "First", "targets[0].label"),
        ("trade_plan", ("invalidations", 0, "basis"), "LAST", "invalidations[0].basis"),
        ("model_version", ("contract_versions", 1, "version"), "1.0", "contract_versions[1].version"),
        ("model_version", ("metrics", 0, "value"), True, "metrics[0].value"),
    ],
)
def test_nested_errors_report_the_full_path(
    builder: str, access: tuple[str | int, ...], bad: Any, path: str
) -> None:
    wire = to_wire(getattr(samples, builder)())
    assert _get(wire, access) is not None
    error = _refused(_with(wire, access, bad))
    assert error.details["path"] == path
    assert error.details["rule"]
    assert str(access[-1]) in error.message or path in error.message


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-12", "S00-ASG-13")
def test_nested_invariant_violations_name_the_object_and_the_invariant() -> None:
    wire = to_wire(samples.evidence())
    error = _refused(_with(wire, ("invalidation", "timeframe"), "M5"))
    assert error.details["path"] == "invalidation"
    assert error.details["invariant"] == "COND-1"
    assert error.details["contract"] == "PriceCondition"
    candidate_wire = to_wire(samples.candidate())
    error = _refused(_with(candidate_wire, ("destinations", 0, "horizon_ns"), None))
    assert error.details["path"] == "destinations[0]"
    assert error.details["invariant"] == "DEST-1"
    error = _refused(_with(candidate_wire, ("contributions", 0, "feature"), "session_london"))
    assert error.details["path"] == "contributions[0]"
    assert error.details["invariant"] == "CONTRIB-1"
    # Envelope keys are reserved at the root only: a nested value object has no envelope.
    error = _refused(_with(wire, ("invalidation", "contract"), "PriceCondition"))
    assert error.details["path"] == "invalidation.contract"
    assert error.details["rule"] == "unknown field"
    error = _refused(_with(wire, ("levels", 0, "schema_version"), "1.0.0"))
    assert error.details["path"] == "levels[0].schema_version"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11", "S00-ASG-12", "S00-ASG-15", "S00-ASG-19")
def test_cross_field_invariants_are_checked_when_decoding() -> None:
    """Section 9.1: a violating record cannot be constructed *or decoded*."""
    tick_wire = to_wire(samples.tick())
    error = _refused({**tick_wire, "bid": "2650.40"})
    assert error.details["invariant"] == "TICK-1"
    assert error.details["contract"] == "Tick"
    error = _refused({**tick_wire, "event_time_ns": tick_wire["receive_time_ns"] + 1})
    assert error.details["invariant"] == "TICK-2"
    evidence_wire = to_wire(samples.evidence())
    error = _refused({**evidence_wire, "as_of_time_ns": evidence_wire["decision_time_ns"] + 1})
    assert error.details["invariant"] == "CAUSAL-1"
    risk_wire = to_wire(samples.risk_decision())
    error = _refused({**risk_wire, "daily_state": "EMERGENCY_HALT"})
    assert error.details["invariant"] == "RSK-5"
    clock_wire = to_wire(samples.clock_reading())
    error = _refused({**clock_wire, "trading_day": "2026-11-27"})
    assert error.details["invariant"] == "CLK-1"
    error = _refused({**clock_wire, "trading_day": "2026-02-30"})
    assert error.details["path"] == "trading_day"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11", "S00-ASG-13")
def test_list_rules_are_enforced_when_decoding() -> None:
    wire = to_wire(samples.tick())
    error = _refused({**wire, "flags": ["RECEIVE_TIME_MODELLED", "RECEIVE_TIME_MODELLED"]})
    assert error.details["path"] == "flags[1]"
    assert error.details["rule"] == "duplicate item"
    for not_a_list in (None, "RECEIVE_TIME_MODELLED", {"0": "RECEIVE_TIME_MODELLED"}, 1):
        error = _refused({**wire, "flags": not_a_list})
        assert error.details["path"] == "flags"
    candidate_wire = to_wire(samples.candidate())
    error = _refused({**candidate_wire, "market_event_ids": []})
    assert error.details["path"] == "market_event_ids"
    assert "1..32" in str(error.details["rule"])
    seventeen = [flag.value for flag in TickFlag] * 2
    assert len(seventeen) > 16
    error = _refused({**wire, "flags": seventeen[:17]})
    assert error.details["path"] in {"flags", "flags[9]"}


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-11", "S00-ASG-13", "S00-ASG-14")
@pytest.mark.acceptance("S00-ACC-01")
def test_lists_without_meaning_in_their_order_are_normalised_to_the_canonical_order() -> None:
    crossed = samples.tick(
        bid=Decimal("2650.40"),
        ask=Decimal("2650.30"),
        flags=(TickFlag.RECEIVE_TIME_MODELLED, TickFlag.OUTLIER, TickFlag.CROSSED),
    )
    wire = to_wire(crossed)
    assert wire["flags"] == ["CROSSED", "OUTLIER", "RECEIVE_TIME_MODELLED"], "sorted by value"
    shuffled = {**wire, "flags": ["RECEIVE_TIME_MODELLED", "CROSSED", "OUTLIER"]}
    assert from_wire(shuffled) == crossed
    assert to_json(from_wire(shuffled)) == to_json(crossed)
    assert fingerprint(from_wire(shuffled)) == fingerprint(crossed)

    plan_wire = to_wire(samples.trade_plan())
    assert [target["price"] for target in plan_wire["targets"]] == ["2652.8", "2655"], "sorted by price"
    reversed_targets = {**plan_wire, "targets": list(reversed(plan_wire["targets"]))}
    assert from_wire(reversed_targets) == samples.trade_plan()
    assert to_json(from_wire(reversed_targets)) == to_json(samples.trade_plan())

    candidate_wire = to_wire(samples.candidate())
    assert len(candidate_wire["contributions"]) == 2
    reversed_contributions = {
        **candidate_wire,
        "contributions": list(reversed(candidate_wire["contributions"])),
    }
    assert to_json(from_wire(reversed_contributions)) == to_json(samples.candidate())


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-13", "S00-ASG-14")
@pytest.mark.acceptance("S00-ACC-01")
def test_lists_whose_order_carries_meaning_keep_their_order() -> None:
    first = PriceCondition(
        price=Decimal("2648.00"), trigger=PriceTrigger.AT_OR_BELOW, basis=PriceBasis.BID, timeframe=None
    )
    second = PriceCondition(
        price=Decimal("2647.00"), trigger=PriceTrigger.BELOW, basis=PriceBasis.MID, timeframe=None
    )
    forward = samples.trade_plan(invalidations=(first, second))
    backward = samples.trade_plan(invalidations=(second, first))
    assert forward != backward
    assert to_json(forward) != to_json(backward)
    assert fingerprint(forward) != fingerprint(backward)
    assert from_json(to_json(forward)).invalidations == (first, second)
    assert from_json(to_json(backward)).invalidations == (second, first)
    schema = json_schema(TradePlan)
    assert "x-gie-canonical-order" not in schema["properties"]["invalidations"]
    assert schema["properties"]["targets"]["x-gie-canonical-order"] == "sorted by price"
    # Quantiles are ascending by rule (CND-7), not by silent re-sorting: a wrong order is refused.
    candidate_wire = to_wire(samples.candidate())
    error = _refused({**candidate_wire, "net_r_quantiles": list(reversed(candidate_wire["net_r_quantiles"]))})
    assert error.details["invariant"] == "CND-7"


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("root", [None, True, 0, 1.5, "Tick", [], [{"contract": "Tick"}], b"{}"])
def test_non_object_root_is_refused(root: Any) -> None:
    error = _refused(root)
    assert error.details["path"] == "<root>"
    if not isinstance(root, bytes):
        with pytest.raises(ContractValidationError) as caught:
            from_json(json.dumps(root))
        assert caught.value.details["path"] == "<root>"


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        '{"contract":"Tick",}',
        "{'contract':'Tick'}",
        '{"contract":"Tick"} {"contract":"Tick"}',
        '{"contract":"Tick","contract":"Tick"}',
        b"\xff\xfe{}",
        b'{"contract":"Ti\xffck"}',
    ],
)
def test_text_that_is_not_strict_json_is_refused(text: str | bytes) -> None:
    with pytest.raises(CanonicalEncodingError) as caught:
        from_json(text)
    assert caught.value.code == "GIE.CONTRACT.ENCODING"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
def test_duplicate_keys_are_refused_even_when_the_values_agree() -> None:
    text = to_json(samples.tick())
    with pytest.raises(CanonicalEncodingError):
        from_json(text[:-1] + ',"seq":0}')
    with pytest.raises(CanonicalEncodingError):
        from_json(text[:-1] + ',"bid_volume":null}')


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "name",
    [
        "Nope",
        "",
        "tick",
        "TICK",
        "Tick ",
        "ClockReading",  # the class of the contract named "Clock"
        "NamedValue",  # nested value objects are not contracts
        "PriceLevel",
        "Record",
        None,
        1,
        True,
        ["Tick"],
        {"name": "Tick"},
    ],
)
def test_unknown_contract_is_refused(name: Any) -> None:
    wire = {**to_wire(samples.tick()), "contract": name}
    error = _refused(wire, UnknownContractError)
    assert error.code == "GIE.CONTRACT.UNKNOWN"
    without = {key: value for key, value in wire.items() if key != "contract"}
    _refused(without, UnknownContractError)
    if isinstance(name, str):
        with pytest.raises(UnknownContractError):
            from_json(canonical_dumps(wire))


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11", "S00-ASG-12", "S00-ASG-19")
def test_expect_pins_the_contract_type() -> None:
    tick = samples.tick()
    assert from_wire(to_wire(tick), expect=Tick) == tick
    assert from_json(to_json(tick), expect=Tick) == tick
    assert from_json(to_json(samples.clock_reading()), expect=ClockReading) == samples.clock_reading()
    for other in (Evidence, Candidate, RiskDecision, ClockReading, NamedValue, SubTick, LookAlikeTick):
        with pytest.raises(ContractValidationError) as caught:
            from_wire(to_wire(tick), expect=other)
        assert caught.value.details["path"] == "contract"
        assert caught.value.details["actual"] == "Tick"
        with pytest.raises(ContractValidationError):
            from_json(to_json(tick), expect=other)
        with pytest.raises(ContractValidationError):
            from_json(to_bytes(tick), expect=other)


# =========================================================================== versions (section 8)
@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
@pytest.mark.parametrize(
    "version",
    ["2.0.0", "0.9.0", "0.0.0", "10.0.0", "999999999.0.0"],
)
def test_a_different_major_version_is_refused(version: str) -> None:
    error = _refused({**to_wire(samples.tick()), "schema_version": version}, ContractVersionError)
    assert error.code == "GIE.CONTRACT.VERSION"
    assert error.details["message_version"] == version
    assert error.details["contract"] == "Tick"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
@pytest.mark.parametrize(
    "version",
    [
        None,
        1,
        1.0,
        True,
        ["1.0.0"],
        "",
        "1",
        "1.0",
        "1.0.0.0",
        "v1.0.0",
        "1.0.0-rc1",
        "1.0.0+build",
        "01.0.0",
        "1.00.0",
        "1.0.00",
        " 1.0.0",
        "1.0.0 ",
        "1.0.0\n",
        "1.-1.0",
        "1.x.0",
        "1.0.1234567890",
        "\u0661.0.0",  # ARABIC-INDIC DIGIT ONE
    ],
)
def test_a_malformed_schema_version_is_refused(version: Any) -> None:
    error = _refused({**to_wire(samples.tick()), "schema_version": version}, ContractVersionError)
    assert error.code == "GIE.CONTRACT.VERSION"


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_a_message_without_schema_version_is_refused(contract: str) -> None:
    wire = to_wire(_first(contract))
    del wire["schema_version"]
    error = _refused(wire, ContractVersionError)
    assert error.details["contract"] == contract


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-24")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_patch_never_affects_decoding(contract: str) -> None:
    original = _first(contract)
    wire = to_wire(original)
    current = parse_semver(wire["schema_version"])
    for patch in (0, 1, 7, 999_999_999):
        variant = {**wire, "schema_version": f"{current.major}.{current.minor}.{patch}"}
        decoded = from_wire(variant)
        assert decoded == original
        assert to_json(decoded) == to_json(original), "the reader re-encodes with its own version"
        # ... and stays strict: a different PATCH does not relax anything.
        _refused({**variant, "zzz_unknown": None})


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-12")
def test_a_newer_minor_version_is_read_tolerantly_but_still_fails_closed() -> None:
    original = samples.evidence()
    wire = to_wire(original)
    current = parse_semver(wire["schema_version"])
    newer = f"{current.major}.{current.minor + 1}.0"
    tolerant = {**wire, "schema_version": newer, "added_by_a_newer_minor": {"anything": [1, 2.5, None]}}
    tolerant["invalidation"] = {**wire["invalidation"], "also_new": "x"}
    tolerant["levels"] = [{**wire["levels"][0], "new_level_field": None}]
    assert from_wire(tolerant) == original
    assert to_json(from_wire(tolerant)) == to_json(original)
    # The same unknown fields are an error in a message of the reader's own MINOR.
    error = _refused({**tolerant, "schema_version": wire["schema_version"]})
    assert error.details["rule"] == "unknown field"
    # Unknown enum values are still refused, also nested.
    error = _refused({**tolerant, "state": "A_NEWER_STATE"})
    assert error.details["path"] == "state"
    error = _refused(_with(tolerant, ("invalidation", "basis"), "A_NEWER_BASIS"))
    assert error.details["path"] == "invalidation.basis"
    # A known field may not be missing, wrongly typed or invalid.
    error = _refused({key: value for key, value in tolerant.items() if key != "missing_reason"})
    assert error.details["path"] == "missing_reason"
    error = _refused({key: value for key, value in tolerant.items() if key != "engine"})
    assert error.details["path"] == "engine"
    error = _refused({**tolerant, "seq": "0"})
    assert error.details["path"] == "seq"
    error = _refused({**tolerant, "as_of_time_ns": tolerant["decision_time_ns"] + 1})
    assert error.details["invariant"] == "CAUSAL-1"
    # The envelope is never optional.
    _refused({**tolerant, "contract": "A_NEWER_CONTRACT"}, UnknownContractError)


# =========================================================================== only registered classes
@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
def test_a_subclass_instance_of_a_contract_cannot_be_encoded() -> None:
    genuine = samples.tick()
    impostor = SubTick(**_fields_of(genuine))
    assert isinstance(impostor, Tick)
    assert impostor != genuine, "records of different classes are never equal"
    for encode in (to_wire, to_json, to_bytes, fingerprint):
        with pytest.raises(UnknownContractError) as caught:
            encode(impostor)
        assert caught.value.code == "GIE.CONTRACT.UNKNOWN"
        assert caught.value.details["contract"] == "SubTick"
    assert to_wire(genuine)["contract"] == "Tick"


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-11")
def test_a_look_alike_class_cannot_be_encoded() -> None:
    impostor = LookAlikeTick(symbol="XAUUSD", seq=0)
    assert LookAlikeTick.CONTRACT == "Tick"
    for encode in (to_wire, to_json, to_bytes, fingerprint):
        with pytest.raises(UnknownContractError) as caught:
            encode(impostor)
        assert caught.value.details["contract"] == "LookAlikeTick"
    assert all_contracts()["Tick"] is Tick


@pytest.mark.requirement("S00-ASG-24", "S00-ASG-12")
def test_nested_value_objects_have_no_envelope_and_subclasses_are_refused_inside_records() -> None:
    level = PriceLevel(role="SWEPT_LEVEL", price=Decimal("2648.40"), identity=True)
    for encode in (to_wire, to_json, to_bytes, fingerprint):
        with pytest.raises(UnknownContractError):
            encode(level)
    impostor = SubPriceLevel(role="SWEPT_LEVEL", price=Decimal("2648.40"), identity=True)
    with pytest.raises(ContractValidationError) as caught:
        samples.evidence(levels=(impostor,))
    assert caught.value.details["path"] == "levels[0]"
    assert caught.value.details["actual_type"] == "SubPriceLevel"
    not_a_record = {"role": "SWEPT_LEVEL", "price": "2648.40", "identity": True}
    with pytest.raises(ContractValidationError) as caught:
        dataclasses.replace(samples.evidence(), levels=(level, not_a_record))
    assert caught.value.details["path"] == "levels[1]"


@pytest.mark.requirement("S00-ASG-24")
def test_encoding_something_that_is_not_a_record_is_refused_with_a_contract_error() -> None:
    """Section 5: all errors derive from GieError; only exact registered classes are accepted anywhere."""
    wire = to_wire(samples.tick())
    raw: list[str] = []
    for not_a_record in (None, wire, to_json(samples.tick()), 5, Tick, [samples.tick()]):
        for encode in (to_wire, to_json, to_bytes, fingerprint):
            try:
                encode(not_a_record)  # type: ignore[arg-type]
            except GieError:
                continue
            except Exception as exc:
                raw.append(f"{encode.__name__}({type(not_a_record).__name__}): {type(exc).__name__}")
            else:
                raw.append(f"{encode.__name__}({type(not_a_record).__name__}): accepted")
    assert raw == []


# =========================================================================== fingerprint
@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize(("contract", "variant"), VARIANT_PARAMS)
def test_fingerprint_is_the_documented_hash_and_is_stable(contract: str, variant: str) -> None:
    """Section 7: SHA-256 of contract name, MAJOR version and fields; MINOR and PATCH are excluded."""
    original = _sample(contract, variant)
    digest = fingerprint(original)
    assert SHA256_HEX.fullmatch(digest)
    assert fingerprint(original) == digest
    assert fingerprint(from_json(to_json(original))) == digest
    assert fingerprint(samples.variants()[contract][variant]) == digest, "a rebuilt equal record"

    def by_hand(wire: dict[str, Any]) -> str:
        fields = {key: value for key, value in wire.items() if key not in ENVELOPE}
        major = int(wire["schema_version"].split(".")[0])
        content = {"contract": wire["contract"], "major": major, "fields": fields}
        return hashlib.sha256(canonical_dumps(content).encode("utf-8")).hexdigest()

    wire = strict_loads(to_json(original))
    assert by_hand(wire) == digest
    current = parse_semver(wire["schema_version"])
    for minor, patch in ((current.minor, current.patch + 1), (current.minor + 1, 0), (current.minor + 7, 3)):
        compatible = {**wire, "schema_version": f"{current.major}.{minor}.{patch}"}
        assert by_hand(compatible) == digest, "MINOR and PATCH do not enter the fingerprint"
        assert fingerprint(from_wire(compatible)) == digest
    assert by_hand({**wire, "schema_version": f"{current.major + 1}.0.0"}) != digest, "MAJOR does"
    assert by_hand({**wire, "contract": "Other"}) != digest, "the contract name does"
    assert digest != hashlib.sha256(to_bytes(original)).hexdigest(), "it is not the hash of the full envelope"


def _replace_field(original: Record, name: str, value: Any) -> Record:
    """*original* with one field changed.  A RunManifest derives its ``run_id`` from every other
    field (RUN-1), so a changed manifest is built with ``RunManifest.create`` (change notice 2)."""
    if isinstance(original, RunManifest) and name != "run_id":
        fields = {spec.name: getattr(original, spec.name) for spec in RunManifest.FIELD_SPECS}
        del fields["run_id"]
        fields[name] = value
        return RunManifest.create(**fields)
    return dataclasses.replace(original, **{name: value})  # type: ignore[type-var]


def _single_field_changes(original: Record) -> Iterator[tuple[str, Record]]:
    """Valid records that differ from *original* in exactly one field."""
    for spec in type(original).FIELD_SPECS:
        value = getattr(original, spec.name)
        inner = _unwrap(spec.ftype)
        candidates: list[Any] = []
        if spec.ftype.optional and value is not None:
            candidates.append(None)
        if isinstance(inner, Int):
            base = value if isinstance(value, int) else 0
            candidates += [base + 1, base - 1, base + 1000]
        elif isinstance(inner, Dec):
            base_decimal = value if isinstance(value, Decimal) else Decimal(1)
            candidates += [base_decimal + Decimal("0.01"), base_decimal - Decimal("0.01"), base_decimal * 2]
        elif isinstance(inner, Float):
            base_float = value if isinstance(value, float) else 0.5
            candidates += [base_float / 2 + 0.01, base_float + 0.125]
        elif isinstance(inner, EnumOf):
            if inner.enum_type is DataOrigin:
                continue  # fixtures stay labelled SYNTHETIC_FIXTURE; the origin is never relabelled here
            candidates += [member for member in inner.enum_type if member is not value]
        elif isinstance(inner, Bool):
            candidates.append(not value)
        elif isinstance(inner, ListOf):
            if value:
                candidates.append(tuple(value)[:-1])
        elif isinstance(inner, Str) and isinstance(value, str):
            candidates += [value + "x", value + "_x", value + "X", value[:-1]]
        for candidate in candidates:
            try:
                changed = _replace_field(original, spec.name, candidate)
            except ContractValidationError:
                continue
            if changed != original:
                yield spec.name, changed
                break


@pytest.mark.requirement("S00-ACC-01")
@pytest.mark.acceptance("S00-ACC-01")
@pytest.mark.parametrize("contract", CONTRACT_PARAMS)
def test_fingerprint_differs_when_any_field_differs(contract: str) -> None:
    covered: set[str] = set()
    for original in _variants()[contract].values():
        digest = fingerprint(original)
        for name, changed in _single_field_changes(original):
            assert fingerprint(changed) != digest, f"{contract}.{name} does not enter the fingerprint"
            assert to_json(changed) != to_json(original)
            assert from_json(to_json(changed)) == changed
            covered.add(name)
    assert len(covered) >= 5, f"only {sorted(covered)} could be varied"


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-12", "S00-ASG-13", "S00-ASG-14", "S00-ASG-15")
@pytest.mark.acceptance("S00-ACC-01")
def test_fingerprint_differs_for_identity_bound_fields_too() -> None:
    """Fields that are verified against an identifier are varied through the labelled builders."""
    pairs = [
        (samples.run_manifest(), samples.run_manifest(label="fixture-other")),
        (samples.run_manifest(), samples.run_manifest(config_sha256="1" * 64)),
        (samples.run_manifest(), samples.run_manifest(data_sources=("fixture", "fixture-b"))),
        (samples.evidence(), samples.evidence(anchor_time_ns=samples.T0 - 600 * 10**9)),
        (samples.evidence(), samples.evidence(kind="fair_value_gap")),
        (samples.candidate(), samples.candidate(setup_type="sweep_reversal_long_b")),
        (samples.trade_plan(), samples.trade_plan(variant=3)),
        (samples.risk_decision(), samples.risk_decision(attempt=1)),
        (samples.risk_decision(), samples.risk_decision(reason_codes=("FIXTURE_NOTE",))),
        (samples.order_event(), samples.order_event(attached_take_profit_price=Decimal("2655.00"))),
        (samples.model_version(), samples.model_version(semver="0.1.1")),
        (samples.model_version(), samples.model_version(status_reasons=("FIXTURE_NOTE",))),
        (samples.clock_reading(), samples.clock_reading(wall_time_ns=samples.T0 + 86_400 * 10**9)),
    ]
    for left, right in pairs:
        assert left != right
        assert fingerprint(left) != fingerprint(right)
    everything = [item for by_variant in _variants().values() for item in by_variant.values()]
    assert len({fingerprint(item) for item in everything}) == len(everything)


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-14", "S00-ASG-15")
@pytest.mark.acceptance("S00-ACC-01")
def test_fingerprint_binds_a_risk_decision_to_the_plan_content() -> None:
    plan = samples.trade_plan()
    decision = samples.risk_decision(plan)
    assert decision.plan_fingerprint == fingerprint(plan)
    assert decision.plan_fingerprint == fingerprint(from_json(to_json(plan)))
    assert fingerprint(samples.trade_plan(stop_price=Decimal("2647.70"))) != decision.plan_fingerprint


@pytest.mark.requirement("S00-ACC-01", "S00-ASG-11")
@pytest.mark.acceptance("S00-ACC-01")
def test_fingerprint_recognises_idempotent_duplicates_and_conflicts() -> None:
    """Section 6.1: same position and identical content is a duplicate, different content a conflict."""
    first = samples.tick()
    stream = stream_name(first)
    tracker = SequenceTracker(allow_gaps=True)
    assert tracker.observe(stream, first.seq, fingerprint(first)).outcome is SeqOutcome.ACCEPTED
    wire = to_wire(first)
    resent = [
        samples.tick(),
        from_json(to_json(first)),
        from_wire({**wire, "schema_version": "1.0.9"}),
        from_wire({**wire, "bid": "2650.100", "flags": list(reversed(wire["flags"]))}),
        samples.tick(bid=Decimal("2650.1000")),
    ]
    for again in resent:
        assert tracker.observe(stream, again.seq, fingerprint(again)).outcome is SeqOutcome.DUPLICATE
    different = samples.tick(bid=Decimal("2650.11"))
    with pytest.raises(SequenceConflictError):
        tracker.observe(stream, different.seq, fingerprint(different))
    following = samples.tick(seq=1)
    assert tracker.observe(stream, following.seq, fingerprint(following)).outcome is SeqOutcome.ACCEPTED
