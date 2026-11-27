"""Acceptance test S00-ACC-02 - contract compatibility (reader rules and evolution rules).

SYNTHETIC FIXTURE DATA ONLY.  Every record is built with the labelled builders of
``tests/gie/samples.py`` (``DataOrigin.SYNTHETIC_FIXTURE``); the messages edited by hand below are
derived from those records.  Nothing here is market data, broker data or performance evidence.

Expected behaviour is taken from ``specifications/SHARED_CONTRACTS.md`` (revision 2) section 8:

* reader rules of ``gie.contracts.codec.from_wire`` (other MAJOR refused, same MAJOR and a MINOR
  not newer than the reader strict, newer MINOR tolerant for unknown fields only, PATCH irrelevant);
* the change table (documentation = PATCH, additive = MINOR, anything else = MAJOR, any change of
  the run-mode / venue vocabulary = MAJOR) as implemented by ``gie.contracts.compat``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from gie.contracts import (
    OrderEvent,
    Record,
    Tick,
    all_contracts,
    fingerprint,
    from_json,
    from_wire,
    to_json,
    to_wire,
)
from gie.contracts._spec import (
    DecodeContext,
    EnumOf,
    ListOf,
    Opt,
    Str,
    decode_fields,
    fld,
    object_schema,
    record,
)
from gie.contracts.compat import (
    Change,
    can_read,
    check_evolution,
    classify_schema_change,
    declared_change,
)
from gie.contracts.registry import contract
from gie.contracts.schema_gen import json_schema
from gie.core.errors import (
    ContractValidationError,
    ContractVersionError,
    GieError,
    UnknownContractError,
)
from tests.gie import samples

pytestmark = [pytest.mark.acceptance("S00-ACC-02"), pytest.mark.requirement("S00-ACC-02")]

CONTRACT_NAMES = sorted(samples.BUILDERS)
READER_VERSION = "1.0.0"
Mutation = Callable[[dict[str, Any]], None]


# =========================================================================== helpers
def _sample(name: str) -> Record:
    return samples.BUILDERS[name]()


def _wire(name: str, **edits: Any) -> dict[str, Any]:
    wire = to_wire(_sample(name))
    wire.update(edits)
    return wire


def _schema(name: str) -> dict[str, Any]:
    return copy.deepcopy(json_schema(all_contracts()[name]))


def _classify(name: str, mutate: Mutation) -> tuple[Change, list[str]]:
    old, new = _schema(name), _schema(name)
    mutate(new)
    result = classify_schema_change(old, new)
    assert old == _schema(name), "classify_schema_change must not modify the old schema"
    return result


def _first_field(name: str, *, required: bool) -> str | None:
    for spec in all_contracts()[name].FIELD_SPECS:
        if spec.required is required:
            return spec.name
    return None


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class _FixtureV10(Record):
    """Synthetic fixture record (never registered): the shape of a contract at version 1.0."""

    base: str = fld(Str(), "Field that exists since version 1.0.")


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class _FixtureV11(Record):
    """Synthetic fixture record (never registered): the shape of a contract at version 1.0."""

    base: str = fld(Str(), "Field that exists since version 1.0.")
    added: str | None = fld(Opt(Str()), "Optional field added by MINOR version 1.1.", since=(1, 1))


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class _FixtureV12(Record):
    """Synthetic fixture record (never registered): the shape of a contract at version 1.0."""

    base: str = fld(Str(), "Field that exists since version 1.0.")
    added: str | None = fld(Opt(Str()), "Optional field added by MINOR version 1.1.", since=(1, 1))
    later: str | None = fld(Opt(Str()), "Optional field added by MINOR version 1.2.", since=(1, 2))


# =========================================================================== reader rules
def test_samples_cover_every_registered_contract() -> None:
    assert set(CONTRACT_NAMES) == set(all_contracts())
    assert len(CONTRACT_NAMES) == 10
    assert {record_type.VERSION for record_type in all_contracts().values()} == {READER_VERSION}


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_same_version_message_decodes_to_the_same_record(name: str) -> None:
    original = _sample(name)
    wire = to_wire(original)
    assert wire["contract"] == name
    assert wire["schema_version"] == READER_VERSION
    decoded = from_wire(copy.deepcopy(wire))
    assert decoded == original
    assert type(decoded) is type(original)
    assert to_wire(decoded) == wire


@pytest.mark.parametrize("version", ["0.9.0", "2.0.0", "0.0.0", "0.1.0", "3.1.4", "999999999.0.0"])
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_other_major_version_is_refused(name: str, version: str) -> None:
    with pytest.raises(ContractVersionError) as caught:
        from_wire(_wire(name, schema_version=version))
    assert caught.value.code == "GIE.CONTRACT.VERSION"
    assert isinstance(caught.value, GieError)


@pytest.mark.parametrize("version", ["0.9.0", "2.0.0"])
def test_other_major_is_refused_whatever_the_fields_look_like(version: str) -> None:
    # A reader must not try to interpret the fields of a MAJOR it does not know.
    message = {"contract": "Tick", "schema_version": version, "completely": "different", "shape": [1, 2]}
    with pytest.raises(ContractVersionError):
        from_wire(message)


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_same_version_unknown_field_is_refused(name: str) -> None:
    with pytest.raises(ContractValidationError) as caught:
        from_wire(_wire(name, zz_unknown_field=None))
    assert caught.value.details["path"] == "zz_unknown_field"
    assert "unknown field" in str(caught.value.details["rule"])


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_same_version_missing_required_field_is_refused(name: str) -> None:
    field = _first_field(name, required=True)
    assert field is not None
    wire = _wire(name)
    del wire[field]
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == field


@pytest.mark.parametrize("version", ["1.0.0", "1.0.3", "1.1.0"])
@pytest.mark.parametrize("name", [n for n in CONTRACT_NAMES if _first_field(n, required=False)])
def test_missing_optional_field_is_refused_when_the_message_version_has_it(name: str, version: str) -> None:
    # Section 7: optional fields are always written as explicit null; a key missing from a message
    # whose version already has the field is an error - also when the MINOR is newer.
    field = _first_field(name, required=False)
    assert field is not None
    wire = _wire(name, schema_version=version)
    del wire[field]
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == field


def test_most_contracts_have_optional_fields_so_the_previous_test_is_not_empty() -> None:
    assert sum(1 for name in CONTRACT_NAMES if _first_field(name, required=False)) >= 8


@pytest.mark.parametrize("version", ["1.1.0", "1.7.3", "1.999999999.0"])
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_newer_minor_ignores_unknown_fields(name: str, version: str) -> None:
    original = _sample(name)
    wire = _wire(
        name,
        schema_version=version,
        zz_added_scalar="value of a field this reader does not know",
        zz_added_null=None,
        zz_added_object={"nested": [1, 2, {"deep": True}]},
    )
    decoded = from_wire(wire)
    assert decoded == original
    again = to_wire(decoded)
    assert not [key for key in again if key.startswith("zz_")]
    assert again["schema_version"] == READER_VERSION


@pytest.mark.parametrize(
    ("name", "list_field"),
    [
        ("Evidence", "levels"),
        ("Evidence", "features"),
        ("TradePlan", "targets"),
        ("Candidate", "destinations"),
        ("ModelVersion", "contract_versions"),
    ],
)
def test_newer_minor_ignores_unknown_fields_of_nested_objects(name: str, list_field: str) -> None:
    original = _sample(name)
    wire = _wire(name)
    assert wire[list_field], "the sample must carry a nested object"
    wire[list_field][0]["zz_added_inside"] = "x"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(copy.deepcopy(wire))  # strict at the reader's own version
    assert caught.value.details["path"] == f"{list_field}[0].zz_added_inside"
    wire["schema_version"] = "1.2.0"
    assert from_wire(wire) == original


def _enum_field_cases() -> list[Any]:
    cases = []
    for name in CONTRACT_NAMES:
        for spec in all_contracts()[name].FIELD_SPECS:
            ftype = spec.ftype.inner if isinstance(spec.ftype, Opt) else spec.ftype
            if isinstance(ftype, EnumOf):
                cases.append(
                    pytest.param(name, spec.name, "ZZ_NOT_KNOWN", spec.name, id=f"{name}.{spec.name}")
                )
            elif isinstance(ftype, ListOf) and isinstance(ftype.inner, EnumOf):
                cases.append(
                    pytest.param(
                        name, spec.name, ["ZZ_NOT_KNOWN"], f"{spec.name}[0]", id=f"{name}.{spec.name}"
                    )
                )
    return cases


ENUM_FIELD_CASES = _enum_field_cases()


def test_enum_field_cases_cover_the_vocabularies_of_all_contracts() -> None:
    ids = {case.id for case in ENUM_FIELD_CASES}
    assert {
        "Tick.origin",
        "Tick.flags",
        "OrderEvent.mode",
        "OrderEvent.venue",
        "OrderEvent.event_type",
        "OrderEvent.order_state",
        "RunManifest.mode",
        "Clock.clock_mode",
        "ModelVersion.status",
        "Candidate.status",
        "TradePlan.status",
        "RiskDecision.outcome",
        "Position.owner",
        "Evidence.state",
    } <= ids
    assert {case.id.split(".")[0] for case in ENUM_FIELD_CASES} == set(CONTRACT_NAMES)


@pytest.mark.parametrize("version", ["1.0.0", "1.0.4", "1.1.0", "1.25.0"])
@pytest.mark.parametrize(("name", "field", "value", "path"), ENUM_FIELD_CASES)
def test_unknown_enum_value_is_refused_also_from_a_newer_minor(
    name: str, field: str, value: Any, path: str, version: str
) -> None:
    wire = _wire(name, schema_version=version)
    wire[field] = value
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == path
    assert "unknown" in str(caught.value.details["rule"])


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.999.0"])
@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("OrderEvent", "mode", "LIVE"),
        ("OrderEvent", "venue", "LIVE"),
        ("OrderEvent", "venue", "MT5"),
        ("OrderEvent", "venue", "simulated"),
        ("RunManifest", "mode", "LIVE"),
        ("RunManifest", "mode", "DEMO"),
        ("Clock", "mode", "LIVE"),
        ("Position", "mode", "REAL"),
        ("RiskDecision", "mode", "live"),
        ("TradePlan", "mode", "LIVE"),
    ],
)
def test_no_version_number_makes_a_live_mode_or_real_venue_decodable(
    name: str, field: str, value: str, version: str
) -> None:
    wire = _wire(name, schema_version=version, zz_live_routing=True)
    wire[field] = value
    with pytest.raises(GieError) as caught:
        from_wire(wire)
    assert isinstance(caught.value, ContractValidationError)
    assert caught.value.details["path"] in (field, "zz_live_routing")


def test_newer_minor_unknown_enum_value_inside_a_nested_object_is_refused() -> None:
    wire = _wire("Evidence", schema_version="1.1.0")
    assert wire["invalidation"] is not None
    wire["invalidation"]["trigger"] = "TOUCHED"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == "invalidation.trigger"


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_newer_minor_still_requires_the_known_required_fields(name: str) -> None:
    field = _first_field(name, required=True)
    assert field is not None
    wire = _wire(name, schema_version="1.1.0", zz_added=1)
    del wire[field]
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == field


def test_newer_minor_still_validates_fields_and_invariants() -> None:
    wire = _wire("Tick", schema_version="1.1.0", zz_added=1)
    wire["bid"] = "-1"
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["path"] == "bid"

    wire = _wire("Evidence", schema_version="1.1.0", zz_added=1)
    wire["as_of_time_ns"] = wire["decision_time_ns"] + 1
    with pytest.raises(ContractValidationError) as caught:
        from_wire(wire)
    assert caught.value.details["invariant"] == "CAUSAL-1"


@pytest.mark.parametrize("version", ["1.0.1", "1.0.17", "1.0.999999999"])
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_patch_difference_never_affects_decoding(name: str, version: str) -> None:
    assert from_wire(_wire(name, schema_version=version)) == _sample(name)
    # ... and it does not make the reader tolerant either.
    with pytest.raises(ContractValidationError) as caught:
        from_wire(_wire(name, schema_version=version, zz_unknown_field=1))
    assert caught.value.details["path"] == "zz_unknown_field"


MALFORMED_VERSIONS = [
    "",
    "1",
    "1.0",
    "1.0.0.0",
    "v1.0.0",
    "V1.0.0",
    "1.0.0\n",
    "\n1.0.0",
    " 1.0.0",
    "1.0.0 ",
    "1.0.0\x00",
    "01.0.0",
    "1.00.0",
    "1.0.00",
    "1..0",
    "1.0.",
    ".0.0",
    "1.0.x",
    "1.x.0",
    "-1.0.0",
    "1.-1.0",
    "+1.0.0",
    "1.0.0-rc1",
    "1.0.0+build",
    "1,0,0",
    "1.0.9999999999",
    "1.9999999999.0",
    "9999999999.0.0",
    "1" + "0" * 400 + ".0.0",
    "1.0." + "9" * 5000,
    chr(0x661) + "." + chr(0x660) + "." + chr(0x660),  # Arabic-Indic digits
    chr(0xFF11) + ".0.0",  # full-width digit one
    "1.0.0e0",
    "0x1.0.0",
    1,
    100,
    1.0,
    None,
    True,
    False,
    ["1.0.0"],
    {"major": 1, "minor": 0, "patch": 0},
    b"1.0.0",
]


@pytest.mark.parametrize("name", ["Tick", "OrderEvent", "RunManifest"])
@pytest.mark.parametrize("version", MALFORMED_VERSIONS, ids=[repr(v)[:24] for v in MALFORMED_VERSIONS])
def test_malformed_schema_version_is_a_contract_version_error(name: str, version: Any) -> None:
    # pytest.raises would let a ValueError / TypeError / AttributeError escape and fail the test.
    with pytest.raises(ContractVersionError) as caught:
        from_wire(_wire(name, schema_version=version))
    assert caught.value.code == "GIE.CONTRACT.VERSION"
    assert caught.value.to_dict()["code"] == "GIE.CONTRACT.VERSION"


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_missing_schema_version_is_a_contract_version_error(name: str) -> None:
    wire = _wire(name)
    del wire["schema_version"]
    with pytest.raises(ContractVersionError, match="schema_version is missing"):
        from_wire(wire)


@pytest.mark.parametrize("contract_name", ["Tock", "tick", "TICK", "Tick ", "", None, 7, ["Tick"], "Record"])
def test_unknown_contract_name_is_refused(contract_name: Any) -> None:
    with pytest.raises(UnknownContractError) as caught:
        from_wire(_wire("Tick", contract=contract_name))
    assert caught.value.code == "GIE.CONTRACT.UNKNOWN"


def test_missing_contract_name_is_refused() -> None:
    wire = _wire("Tick")
    del wire["contract"]
    with pytest.raises(UnknownContractError):
        from_wire(wire)


def test_expect_pins_the_contract_type() -> None:
    assert from_wire(_wire("Tick"), expect=Tick) == samples.tick()
    with pytest.raises(ContractValidationError, match="unexpected contract"):
        from_wire(_wire("Tick"), expect=OrderEvent)


@pytest.mark.parametrize("message", [None, 1, "Tick", ["contract", "Tick"], b"{}"])
def test_non_object_message_is_refused(message: Any) -> None:
    with pytest.raises(ContractValidationError):
        from_wire(message)


def test_reader_rules_apply_to_json_text_and_bytes() -> None:
    original = samples.order_event()
    text = to_json(original)
    newer = text.replace('"schema_version":"1.0.0"', '"schema_version":"1.3.0","zz_added":{"a":[1]}')
    assert newer != text
    assert from_json(newer) == original
    assert from_json(newer.encode("utf-8")) == original
    with pytest.raises(ContractVersionError):
        from_json(text.replace('"schema_version":"1.0.0"', '"schema_version":"2.0.0"'))
    with pytest.raises(ContractVersionError):
        from_json(text.replace('"schema_version":"1.0.0"', '"schema_version":"1.0"'))
    with pytest.raises(ContractValidationError):
        from_json(text.replace('"schema_version":"1.0.0"', '"schema_version":"1.0.0","zz_added":1'))


@pytest.mark.parametrize(
    ("reader", "message", "tolerant"),
    [
        ("1.2.0", "1.0.0", False),
        ("1.2.0", "1.1.0", False),
        ("1.2.0", "1.1.99", False),
        ("1.2.0", "1.2.0", False),
        ("1.2.0", "1.2.7", False),
        ("1.2.5", "1.2.9", False),
        ("1.2.0", "1.3.0", True),
        ("1.2.9", "1.3.0", True),
        ("1.2.0", "1.10.0", True),  # numeric, not lexicographic, comparison
        ("1.10.0", "1.9.0", False),
        ("1.10.0", "1.11.0", True),
    ],
)
def test_unknown_fields_are_tolerated_only_from_a_newer_minor_than_the_reader(
    monkeypatch: pytest.MonkeyPatch, reader: str, message: str, tolerant: bool
) -> None:
    original = samples.tick()
    wire = to_wire(original)
    monkeypatch.setattr(Tick, "VERSION", reader)  # the reader's version, restored after the test
    wire["schema_version"] = message
    assert from_wire(dict(wire)) == original
    wire["zz_unknown_field"] = 1
    if tolerant:
        assert from_wire(wire) == original
    else:
        with pytest.raises(ContractValidationError) as caught:
            from_wire(wire)
        assert caught.value.details["path"] == "zz_unknown_field"


@pytest.mark.parametrize(("reader", "message"), [("1.2.0", "2.0.0"), ("1.2.0", "0.2.0"), ("2.0.0", "1.0.0")])
def test_major_mismatch_is_refused_in_both_directions(
    monkeypatch: pytest.MonkeyPatch, reader: str, message: str
) -> None:
    wire = to_wire(samples.tick())
    monkeypatch.setattr(Tick, "VERSION", reader)
    wire["schema_version"] = message
    with pytest.raises(ContractVersionError):
        from_wire(wire)


def test_fingerprint_ignores_minor_and_patch_but_not_major(monkeypatch: pytest.MonkeyPatch) -> None:
    # Section 7: MINOR and PATCH are excluded from the fingerprint so that compatible versions agree.
    original = samples.tick()
    base = fingerprint(original)
    assert len(base) == 64
    assert set(base) <= set("0123456789abcdef")
    for version in ("1.0.9", "1.4.0", "1.4.2"):
        monkeypatch.setattr(Tick, "VERSION", version)
        assert fingerprint(original) == base
    monkeypatch.setattr(Tick, "VERSION", "2.0.0")
    assert fingerprint(original) != base


# --------------------------------------------------------------------------- fields added by a MINOR
@pytest.mark.parametrize(
    ("message_version", "raw", "expected_added"),
    [
        ((1, 0), {"base": "x"}, None),  # written before the field existed
        ((1, 0), {"base": "x", "added": "y"}, "y"),
        ((1, 1), {"base": "x", "added": None}, None),
        ((1, 1), {"base": "x", "added": "y"}, "y"),
        ((1, 4), {"base": "x", "added": "y"}, "y"),
    ],
)
def test_field_added_by_a_minor_may_be_absent_only_from_older_messages(
    message_version: tuple[int, int], raw: dict[str, Any], expected_added: str | None
) -> None:
    ctx = DecodeContext(ignore_unknown_fields=False, message_version=message_version)
    decoded = decode_fields(_FixtureV11, raw, "", ctx, envelope=False)
    assert decoded == _FixtureV11(base="x", added=expected_added)


@pytest.mark.parametrize("message_version", [(1, 1), (1, 2), (1, 30), None])
def test_field_added_by_a_minor_must_be_present_from_that_version_on(
    message_version: tuple[int, int] | None,
) -> None:
    ctx = DecodeContext(ignore_unknown_fields=False, message_version=message_version)
    with pytest.raises(ContractValidationError) as caught:
        decode_fields(_FixtureV11, {"base": "x"}, "", ctx, envelope=False)
    assert caught.value.details["path"] == "added"


def test_a_field_added_after_the_first_version_must_be_optional() -> None:
    # "a newer minor may only have added optional fields" is enforced where the field is declared.
    with pytest.raises(ValueError, match="optional"):
        fld(Str(), "Required field that claims to be added by 1.1.", since=(1, 1))


# =========================================================================== classify_schema_change
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_identical_schemas_need_no_bump(name: str) -> None:
    assert _classify(name, lambda schema: None) == (Change.NONE, [])


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_id_and_title_of_a_new_version_are_not_a_change(name: str) -> None:
    def mutate(schema: dict[str, Any]) -> None:
        schema["$id"] = f"urn:gie:contract:{name}:1.4.0"
        schema["title"] = f"{name} 1.4.0"

    assert _classify(name, mutate) == (Change.NONE, [])


def _reword_first_property(schema: dict[str, Any]) -> None:
    field = next(key for key in schema["properties"] if key not in ("contract", "schema_version"))
    schema["properties"][field]["description"] += "  (clarified wording)"


def _reword_nested(schema: dict[str, Any]) -> None:
    definition = schema["$defs"]["PriceLevel"]
    definition["description"] = "Reworded."
    definition["properties"]["role"]["description"] = "Reworded."


PATCH_CASES = [
    pytest.param(name, _reword_first_property, id=f"{name}-field-description") for name in CONTRACT_NAMES
] + [
    pytest.param("Tick", lambda s: s.__setitem__("description", "Reworded."), id="contract-description"),
    pytest.param("Tick", lambda s: s["properties"]["bid"].pop("description"), id="description-removed"),
    pytest.param("Evidence", _reword_nested, id="nested-object-descriptions"),
    pytest.param(
        "OrderEvent",
        lambda s: s["properties"]["schema_version"].__setitem__("description", "Reworded."),
        id="envelope-description",
    ),
]


@pytest.mark.parametrize(("name", "mutate"), PATCH_CASES)
def test_description_only_change_is_patch(name: str, mutate: Mutation) -> None:
    level, _reasons = _classify(name, mutate)
    assert level is Change.PATCH


def _add_optional(target: dict[str, Any], field: str = "zz_added") -> None:
    target["properties"][field] = {
        "anyOf": [{"type": "string", "minLength": 1, "maxLength": 64}, {"type": "null"}],
        "description": "Synthetic optional field added by a MINOR version.",
    }


def _add_optional_with_new_definition(schema: dict[str, Any]) -> None:
    schema.setdefault("$defs", {})["ZzAdded"] = {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
        "additionalProperties": False,
    }
    schema["properties"]["zz_added"] = {
        "anyOf": [{"$ref": "#/$defs/ZzAdded"}, {"type": "null"}],
        "description": "Synthetic optional nested object added by a MINOR version.",
    }


MINOR_CASES = [
    pytest.param(name, _add_optional, "zz_added", id=f"{name}-new-optional-field") for name in CONTRACT_NAMES
] + [
    pytest.param(
        "Evidence", lambda s: _add_optional(s["$defs"]["PriceLevel"]), "zz_added", id="nested-optional"
    ),
    pytest.param("Tick", _add_optional_with_new_definition, "zz_added", id="optional-nested-object"),
    pytest.param("Tick", lambda s: s["properties"]["origin"]["enum"].append("ZZ_NEW"), "ZZ_NEW", id="enum"),
    pytest.param(
        "Tick",
        lambda s: s["properties"]["flags"]["items"]["enum"].append("ZZ_NEW"),
        "ZZ_NEW",
        id="enum-of-list-items",
    ),
    pytest.param(
        "OrderEvent", lambda s: s["properties"]["side"]["enum"].append("ZZ_NEW"), "ZZ_NEW", id="side"
    ),
    pytest.param(
        "Clock",
        lambda s: s["properties"]["clock_mode"]["enum"].append("ZZ_NEW"),
        "ZZ_NEW",
        id="clock_mode-is-not-the-run-mode",
    ),
    pytest.param(
        "Evidence",
        lambda s: s["$defs"]["PriceCondition"]["properties"]["trigger"]["enum"].append("ZZ_NEW"),
        "ZZ_NEW",
        id="enum-in-nested-object",
    ),
    pytest.param(
        "RiskDecision",
        lambda s: s["properties"]["daily_state"]["enum"].insert(0, "ZZ_NEW"),
        "ZZ_NEW",
        id="enum-value-inserted-first",
    ),
]


@pytest.mark.parametrize(("name", "mutate", "mentioned"), MINOR_CASES)
def test_additive_change_is_minor(name: str, mutate: Mutation, mentioned: str) -> None:
    level, reasons = _classify(name, mutate)
    assert level is Change.MINOR
    assert any(mentioned in reason for reason in reasons), reasons


def _remove_field(field: str) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        del schema["properties"][field]
        if field in schema["required"]:
            schema["required"].remove(field)

    return mutate


def _rename_field(old: str, new: str) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        schema["properties"][new] = schema["properties"].pop(old)
        schema["required"] = [new if item == old else item for item in schema["required"]]

    return mutate


def _add_required(schema: dict[str, Any]) -> None:
    schema["properties"]["zz_required"] = {"type": "integer", "description": "Synthetic required field."}
    schema["required"].append("zz_required")


def _set(path: list[Any], value: Any) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        node: Any = schema
        for key in path[:-1]:
            node = node[key]
        assert node[path[-1]] != value, "the mutation must change the schema"
        node[path[-1]] = value

    return mutate


def _add(path: list[Any], value: Any) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        node: Any = schema
        for key in path[:-1]:
            node = node[key]
        assert path[-1] not in node, "the keyword must be new"
        node[path[-1]] = value

    return mutate


def _drop(path: list[Any]) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        node: Any = schema
        for key in path[:-1]:
            node = node[key]
        if isinstance(node, list):
            node.pop(path[-1])
        else:
            del node[path[-1]]

    return mutate


def _append(path: list[Any], value: Any) -> Mutation:
    def mutate(schema: dict[str, Any]) -> None:
        node: Any = schema
        for key in path:
            node = node[key]
        assert isinstance(node, list)
        node.append(value)

    return mutate


_P = "properties"
_B = "x-gie-behaviour"
_I = "x-gie-invariants"

MAJOR_CASES = [
    # ---- fields
    pytest.param("Tick", _remove_field("bid"), "#/properties/bid", id="required-field-removed"),
    pytest.param("Tick", _remove_field("bid_volume"), "#/properties/bid_volume", id="optional-field-removed"),
    pytest.param("Tick", _rename_field("bid", "best_bid"), "#/properties/bid", id="field-renamed"),
    pytest.param("Tick", _add_required, "#/properties/zz_required", id="new-required-field"),
    pytest.param(
        "Evidence",
        lambda s: _add_required(s["$defs"]["PriceLevel"]),
        "PriceLevel/properties/zz_required",
        id="new-required-field-in-nested-object",
    ),
    pytest.param(
        "Evidence",
        lambda s: _remove_field("identity")(s["$defs"]["PriceLevel"]),
        "PriceLevel/properties/identity",
        id="field-removed-from-nested-object",
    ),
    # ---- type
    pytest.param("Tick", _set([_P, "seq", "type"], "string"), "#/properties/seq/type", id="type-changed"),
    pytest.param(
        "Tick",
        _set([_P, "bid_volume", "anyOf", 0, "type"], "number"),
        "#/properties/bid_volume",
        id="type-of-optional-field-changed",
    ),
    pytest.param(
        "Tick", _set([_P, "flags", "items", "type"], "integer"), "#/properties/flags", id="item-type-changed"
    ),
    pytest.param(
        "Tick", _drop([_P, "bid", "x-gie-type"]), "#/properties/bid/x-gie-type", id="decimal-marker-removed"
    ),
    pytest.param(
        "Evidence",
        _set([_P, "levels", "items", "$ref"], "#/$defs/NamedValue"),
        "#/properties/levels",
        id="nested-type-changed",
    ),
    # ---- pattern
    pytest.param("Tick", _set([_P, "bid", "pattern"], "^.*$"), "#/properties/bid/pattern", id="pattern"),
    pytest.param(
        "OrderEvent",
        _set([_P, "client_order_id", "pattern"], "^ord_[0-9a-f]{40}$"),
        "#/properties/client_order_id/pattern",
        id="id-pattern",
    ),
    pytest.param(
        "Tick",
        _set([_P, "schema_version", "pattern"], "^.+$"),
        "#/properties/schema_version/pattern",
        id="version-pattern",
    ),
    # ---- bounds
    pytest.param("Tick", _set([_P, "seq", "minimum"], 1), "#/properties/seq/minimum", id="minimum"),
    pytest.param("Tick", _set([_P, "seq", "maximum"], 2**31), "#/properties/seq/maximum", id="maximum"),
    pytest.param("Tick", _drop([_P, "seq", "minimum"]), "#/properties/seq/minimum", id="minimum-removed"),
    pytest.param(
        "Tick",
        _set([_P, "bid", "x-gie-exclusiveMinimum"], "0.01"),
        "#/properties/bid/x-gie-exclusiveMinimum",
        id="decimal-exclusive-minimum",
    ),
    pytest.param(
        "Tick",
        _drop([_P, "bid", "x-gie-exclusiveMinimum"]),
        "#/properties/bid/x-gie-exclusiveMinimum",
        id="decimal-bound-removed",
    ),
    pytest.param(
        "Tick",
        _add([_P, "ask", "x-gie-maximum"], "100000"),
        "#/properties/ask/x-gie-maximum",
        id="decimal-bound-added",
    ),
    pytest.param(
        "Tick",
        _set([_P, "bid_volume", "anyOf", 0, "x-gie-minimum"], "1"),
        "#/properties/bid_volume",
        id="decimal-minimum-of-optional-field",
    ),
    pytest.param(
        "RiskDecision",
        _set([_P, "risk_frac", "x-gie-maximum"], "2"),
        "#/properties/risk_frac/x-gie-maximum",
        id="decimal-maximum",
    ),
    pytest.param(
        "RiskDecision",
        _set([_P, "risk_frac", "x-gie-minimum"], "-1"),
        "#/properties/risk_frac/x-gie-minimum",
        id="decimal-minimum",
    ),
    pytest.param(
        "Candidate",
        _set([_P, "win_probability_frac", "anyOf", 0, "maximum"], 2.0),
        "#/properties/win_probability_frac",
        id="float-maximum",
    ),
    pytest.param(
        "Tick", _set([_P, "source", "maxLength"], 128), "#/properties/source/maxLength", id="maxLength"
    ),
    pytest.param(
        "Tick", _set([_P, "source", "minLength"], 0), "#/properties/source/minLength", id="minLength"
    ),
    pytest.param("Tick", _set([_P, "flags", "maxItems"], 64), "#/properties/flags/maxItems", id="maxItems"),
    pytest.param("Tick", _set([_P, "flags", "minItems"], 1), "#/properties/flags/minItems", id="minItems"),
    pytest.param("Tick", _drop([_P, "flags", "uniqueItems"]), "#/properties/flags/uniqueItems", id="unique"),
    pytest.param(
        "Tick",
        _set([_P, "flags", "x-gie-canonical-order"], "as supplied"),
        "#/properties/flags/x-gie-canonical-order",
        id="canonical-order",
    ),
    # ---- unit
    pytest.param("Tick", _set([_P, "bid", "x-gie-unit"], "EUR"), "#/properties/bid/x-gie-unit", id="unit"),
    pytest.param(
        "Tick",
        _set([_P, "event_time_ns", "x-gie-unit"], "ms since epoch, UTC"),
        "#/properties/event_time_ns/x-gie-unit",
        id="time-unit",
    ),
    pytest.param(
        "Tick",
        _drop([_P, "receive_time_ns", "x-gie-unit"]),
        "#/properties/receive_time_ns/x-gie-unit",
        id="unit-removed",
    ),
    pytest.param(
        "Tick", _add([_P, "seq", "x-gie-unit"], "lots"), "#/properties/seq/x-gie-unit", id="unit-added"
    ),
    # ---- optionality
    pytest.param(
        "Tick", lambda s: s["required"].remove("bid"), "#/properties/bid", id="required-to-optional"
    ),
    pytest.param(
        "Tick",
        lambda s: s["required"].append("bid_volume"),
        "#/properties/bid_volume",
        id="optional-to-required",
    ),
    pytest.param(
        "Tick", _drop([_P, "bid_volume", "anyOf", 1]), "#/properties/bid_volume", id="null-no-longer-allowed"
    ),
    pytest.param(
        "Tick",
        lambda s: s["properties"].__setitem__(
            "bid", {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Best bid."}
        ),
        "#/properties/bid",
        id="field-became-nullable",
    ),
    pytest.param(
        "Evidence",
        lambda s: s["$defs"]["PriceLevel"]["required"].remove("identity"),
        "PriceLevel/properties/identity",
        id="nested-required-to-optional",
    ),
    # ---- invariants
    pytest.param("Tick", _set([_I, 0, "rule"], "reworded rule"), "x-gie-invariants", id="invariant-reworded"),
    pytest.param("Tick", _drop([_I, 0]), "x-gie-invariants", id="invariant-removed"),
    pytest.param(
        "Tick",
        _append([_I], {"id": "TICK-99", "rule": "synthetic additional rule"}),
        "x-gie-invariants",
        id="invariant-added",
    ),
    pytest.param("Tick", _drop([_I]), "x-gie-invariants", id="all-invariants-removed"),
    pytest.param("Tick", _set([_I, 0, "id"], "TICK-77"), "x-gie-invariants", id="invariant-renumbered"),
    pytest.param("OrderEvent", _drop([_I, 4]), "x-gie-invariants", id="OEV-5-removed"),
    pytest.param("RiskDecision", _drop([_I, -1]), "x-gie-invariants", id="last-RSK-rule-removed"),
    # ---- behaviour
    pytest.param(
        "OrderEvent",
        _append([_B, "transitions", 0, "to"], "WORKING"),
        "x-gie-behaviour/transitions",
        id="transition-row-widened",
    ),
    pytest.param(
        "OrderEvent",
        _drop([_B, "transitions", 5]),
        "x-gie-behaviour/transitions",
        id="transition-row-removed",
    ),
    pytest.param(
        "OrderEvent",
        _append([_B, "transitions"], {"event": "ACCEPTED", "from": "FILLED", "to": ["WORKING"]}),
        "x-gie-behaviour/transitions",
        id="transition-out-of-a-terminal-state-added",
    ),
    pytest.param(
        "OrderEvent",
        _set([_B, "transitions", 3, "event"], "ACCEPTED"),
        "x-gie-behaviour/transitions",
        id="transition-event-changed",
    ),
    pytest.param(
        "OrderEvent",
        _set([_B, "transitions", 0, "from"], "WORKING"),
        "x-gie-behaviour/transitions",
        id="transition-origin-changed",
    ),
    pytest.param(
        "OrderEvent",
        _drop([_B, "immutable_fields", 0]),
        "x-gie-behaviour/immutable_fields",
        id="immutable-removed",
    ),
    pytest.param(
        "OrderEvent",
        _append([_B, "replaceable_fields"], "side"),
        "x-gie-behaviour/replaceable_fields",
        id="replaceable-field-added",
    ),
    pytest.param(
        "OrderEvent",
        _drop([_B, "terminal_states", 0]),
        "x-gie-behaviour/terminal_states",
        id="terminal-state",
    ),
    pytest.param(
        "OrderEvent", _drop([_B, "request_events", 0]), "x-gie-behaviour/request_events", id="request-events"
    ),
    pytest.param(
        "OrderEvent",
        _set([_B, "rank_of_requests", "value"], 15),
        "x-gie-behaviour/rank_of_requests",
        id="rank-of-requests",
    ),
    pytest.param("OrderEvent", _set([_B, "rank", "value"], 21), "x-gie-behaviour/rank", id="rank-value"),
    pytest.param("Tick", _set([_B, "rank", "name"], "OTHER"), "x-gie-behaviour/rank", id="rank-name"),
    pytest.param("Tick", _set([_B, "stream"], "tick/<symbol>"), "x-gie-behaviour/stream", id="stream"),
    pytest.param(
        "Tick",
        _set([_B, "ordering_time"], "event_time_ns"),
        "x-gie-behaviour/ordering_time",
        id="ordering-time-moved-to-source-time",
    ),
    pytest.param(
        "Evidence",
        _set([_B, "identity", "evidence_id"], "evidence_id(engine, market_event_id)"),
        "x-gie-behaviour/identity",
        id="identity-rule-changed",
    ),
    pytest.param(
        "OrderEvent",
        _drop([_B, "identity", "client_order_id"]),
        "x-gie-behaviour/identity",
        id="identity-rule-removed",
    ),
    pytest.param(
        "ModelVersion",
        _append([_B, "transitions", 4, "to"], "SHADOW"),
        "x-gie-behaviour/transitions",
        id="model-transition-widened",
    ),
    pytest.param(
        "ModelVersion",
        _drop([_B, "immutable_fields", 0]),
        "x-gie-behaviour/immutable_fields",
        id="model-immutable-field-removed",
    ),
    pytest.param(
        "TradePlan",
        _append([_B, "transitions", 2, "to"], "ARMED"),
        "x-gie-behaviour/transitions",
        id="plan-final-status-reopened",
    ),
    pytest.param(
        "TradePlan", _drop([_B, "monotonic"]), "x-gie-behaviour/monotonic", id="plan-monotonic-rule"
    ),
    pytest.param(
        "Candidate",
        _append([_B, "transitions", 2, "to"], "AVAILABLE"),
        "x-gie-behaviour/transitions",
        id="candidate-final-status-reopened",
    ),
    pytest.param(
        "Position",
        _drop([_B, "monotonic_gie_owner", 1]),
        "x-gie-behaviour/monotonic_gie_owner",
        id="position-stop-rule-removed",
    ),
    pytest.param(
        "Position",
        _drop([_B, "immutable_fields_gie_owner", 2]),
        "x-gie-behaviour/immutable_fields_gie_owner",
        id="position-initial-stop-mutable",
    ),
    pytest.param(
        "Evidence", _drop([_B, "final_states", 0]), "x-gie-behaviour/final_states", id="final-states"
    ),
    pytest.param("Tick", _drop([_B]), "x-gie-behaviour", id="behaviour-removed"),
    pytest.param(
        "Tick",
        _add([_B, "transitions"], [{"from": None, "to": ["X"]}]),
        "x-gie-behaviour/transitions",
        id="behaviour-keyword-added",
    ),
    # ---- definitions, vocabulary, structure
    pytest.param("Evidence", _drop(["$defs", "PriceLevel"]), "$defs/PriceLevel", id="definition-removed"),
    pytest.param("Evidence", _drop(["$defs"]), "$defs", id="all-definitions-removed"),
    pytest.param(
        "Tick", _drop([_P, "origin", "enum", 0]), "#/properties/origin/enum", id="enum-value-removed"
    ),
    pytest.param(
        "Tick",
        _set([_P, "flags", "items", "enum", 0], "ZZ_RENAMED"),
        "#/properties/flags",
        id="enum-value-renamed",
    ),
    pytest.param("Tick", _drop([_P, "origin", "enum"]), "#/properties/origin/enum", id="enum-opened"),
    pytest.param(
        "Tick",
        _add([_P, "symbol", "enum"], ["XAUUSD"]),
        "#/properties/symbol/enum",
        id="enum-restriction-added",
    ),
    pytest.param("Tick", _set(["additionalProperties"], True), "additionalProperties", id="open-object"),
    pytest.param(
        "Tick", _set([_P, "contract", "const"], "Tock"), "#/properties/contract", id="contract-renamed"
    ),
    pytest.param("Tick", _set(["type"], "array"), "#/type", id="not-an-object"),
]


@pytest.mark.parametrize(("name", "mutate", "mentioned"), MAJOR_CASES)
def test_any_other_change_is_major_and_gives_a_reason(name: str, mutate: Mutation, mentioned: str) -> None:
    level, reasons = _classify(name, mutate)
    assert level is Change.MAJOR
    assert reasons, "a MAJOR classification must say why"
    assert any(mentioned in reason for reason in reasons), reasons


@pytest.mark.parametrize(("name", "mutate", "mentioned"), MAJOR_CASES)
def test_major_change_is_not_releasable_as_a_minor_or_patch(
    name: str, mutate: Mutation, mentioned: str
) -> None:
    old, new = _schema(name), _schema(name)
    mutate(new)
    for declared in ("1.0.0", "1.0.1", "1.1.0", "1.99.99"):
        problems = check_evolution(old, "1.0.0", new, declared)
        assert len(problems) == 1, declared
        assert "MAJOR" in problems[0]
        assert mentioned in problems[0]
    assert check_evolution(old, "1.0.0", new, "2.0.0") == []


def test_the_highest_required_level_wins() -> None:
    def patch_and_minor(schema: dict[str, Any]) -> None:
        _reword_first_property(schema)
        _add_optional(schema)

    def minor_and_major(schema: dict[str, Any]) -> None:
        _add_optional(schema)
        _remove_field("bid")(schema)

    assert _classify("Tick", patch_and_minor)[0] is Change.MINOR
    level, reasons = _classify("Tick", minor_and_major)
    assert level is Change.MAJOR
    assert len(reasons) >= 2


@pytest.mark.parametrize(("name", "mutate", "mentioned"), MINOR_CASES)
def test_undoing_an_additive_change_is_major(name: str, mutate: Mutation, mentioned: str) -> None:
    older, newer = _schema(name), _schema(name)
    mutate(newer)
    level, reasons = classify_schema_change(newer, older)  # the additive change is taken away again
    assert level is Change.MAJOR
    assert reasons


# --------------------------------------------------------------------------- routing-lock vocabulary
MODE_CONTRACTS = [
    name for name in CONTRACT_NAMES if "mode" in json_schema(all_contracts()[name])["properties"]
]


def test_contracts_that_carry_the_run_mode() -> None:
    assert set(MODE_CONTRACTS) == set(CONTRACT_NAMES) - {"Tick", "ModelVersion"}
    assert [n for n in CONTRACT_NAMES if "venue" in json_schema(all_contracts()[n])["properties"]] == [
        "OrderEvent"
    ]


VOCABULARY_EDITS = [
    pytest.param(lambda values: values.append("LIVE"), id="LIVE-added"),
    pytest.param(lambda values: values.insert(0, "LIVE"), id="LIVE-inserted-first"),
    pytest.param(lambda values: values.append("DEMO"), id="DEMO-added"),
    pytest.param(lambda values: values.pop(), id="value-removed"),
    pytest.param(lambda values: values.__setitem__(0, "LIVE"), id="value-replaced"),
    pytest.param(lambda values: values.extend(["REPLAY"]), id="value-duplicated"),
]


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("edit", VOCABULARY_EDITS)
@pytest.mark.parametrize("name", MODE_CONTRACTS)
def test_any_change_of_the_run_mode_vocabulary_is_major(name: str, edit: Callable[[list[str]], Any]) -> None:
    level, reasons = _classify(name, lambda schema: edit(schema["properties"]["mode"]["enum"]))
    assert level is Change.MAJOR
    assert any("routing-lock vocabulary" in reason and "mode" in reason for reason in reasons), reasons


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("edit", VOCABULARY_EDITS)
def test_any_change_of_the_venue_vocabulary_is_major(edit: Callable[[list[str]], Any]) -> None:
    level, reasons = _classify("OrderEvent", lambda schema: edit(schema["properties"]["venue"]["enum"]))
    assert level is Change.MAJOR
    assert any("routing-lock vocabulary" in reason and "venue" in reason for reason in reasons), reasons


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("field", ["mode", "venue"])
def test_reordering_the_routing_vocabulary_is_major_although_the_set_is_unchanged(field: str) -> None:
    def mutate(schema: dict[str, Any]) -> None:
        if field == "venue":
            schema["properties"]["venue"]["enum"] = ["SIMULATED", "SIMULATED"]
        else:
            schema["properties"]["mode"]["enum"].reverse()

    level, reasons = _classify("OrderEvent", mutate)
    assert level is Change.MAJOR
    assert any("routing-lock vocabulary" in reason for reason in reasons), reasons


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("declared", ["1.0.0", "1.0.1", "1.1.0", "1.50.0"])
@pytest.mark.parametrize("field", ["mode", "venue"])
def test_adding_live_cannot_be_released_below_a_major_bump(field: str, declared: str) -> None:
    old, new = _schema("OrderEvent"), _schema("OrderEvent")
    new["properties"][field]["enum"].append("LIVE")
    problems = check_evolution(old, "1.0.0", new, declared)
    assert len(problems) == 1
    assert "requires a MAJOR bump" in problems[0]
    assert "routing-lock vocabulary" in problems[0]
    assert "LIVE" in problems[0]


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize("field", ["mode", "venue"])
def test_routing_vocabulary_of_a_nested_object_is_protected_too(field: str) -> None:
    def nested(values: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"leg": {"$ref": "#/$defs/Leg"}},
            "required": ["leg"],
            "$defs": {
                "Leg": {
                    "type": "object",
                    "properties": {field: {"type": "string", "enum": values}},
                    "required": [field],
                }
            },
        }

    genuine = ["REPLAY", "PAPER"] if field == "mode" else ["SIMULATED"]
    level, reasons = classify_schema_change(nested(genuine), nested([*genuine, "LIVE"]))
    assert level is Change.MAJOR
    assert any("routing-lock vocabulary" in reason for reason in reasons), reasons


@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize(
    ("name", "field", "values"),
    [
        pytest.param("TradePlan", "venue", ["SIMULATED", "LIVE"], id="TradePlan-venue-with-LIVE"),
        pytest.param("Position", "venue", ["LIVE"], id="Position-venue-LIVE-only"),
        pytest.param("Tick", "mode", ["REPLAY", "PAPER", "LIVE"], id="Tick-mode-with-LIVE"),
    ],
)
def test_introducing_a_wider_routing_vocabulary_through_a_new_field_is_major(
    name: str, field: str, values: list[str]
) -> None:
    # Section 8: "**any** change of the run-mode or venue vocabulary" is MAJOR.  A contract that had no
    # venue (or mode) field and gains an *optional* one whose vocabulary contains a live value changes
    # that vocabulary just as much as appending the value to an existing field does.
    def mutate(schema: dict[str, Any]) -> None:
        assert field not in schema["properties"]
        schema["properties"][field] = {
            "anyOf": [{"type": "string", "enum": values}, {"type": "null"}],
            "description": "Synthetic optional field carrying a routing vocabulary.",
        }

    level, reasons = _classify(name, mutate)
    assert level is Change.MAJOR, reasons


# --------------------------------------------------------------------------- generator output
@pytest.mark.parametrize(
    ("old_type", "old_version", "new_type", "new_version"),
    [
        pytest.param(_FixtureV10, "1.0.0", _FixtureV11, "1.1.0", id="first-optional-field-added-by-1.1"),
        pytest.param(_FixtureV11, "1.1.0", _FixtureV12, "1.2.0", id="second-optional-field-added-by-1.2"),
    ],
)
def test_optional_field_declared_with_since_is_a_minor_change_of_the_generated_schema(
    old_type: type[Record], old_version: str, new_type: type[Record], new_version: str
) -> None:
    # The documented way to add a field is fld(Opt(...), since=(1, n)); the generator then publishes
    # the introduction versions under "x-gie-since".  Section 8: a new optional field is MINOR.
    old, new = object_schema(old_type, {}), object_schema(new_type, {})
    added = set(new["properties"]) - set(old["properties"])
    assert len(added) == 1
    assert not added & set(new["required"])
    level, reasons = classify_schema_change(old, new)
    assert level is Change.MINOR, reasons
    assert check_evolution(old, old_version, new, new_version) == []


def test_fixture_schemas_differ_only_by_the_added_optional_fields() -> None:
    # Guards the test above against passing or failing for the wrong reason.
    old, new = object_schema(_FixtureV10, {}), object_schema(_FixtureV11, {})
    assert old["description"] == new["description"]
    assert old["required"] == new["required"] == ["base"]
    assert set(new["properties"]) - set(old["properties"]) == {"added"}
    assert new["properties"]["base"] == old["properties"]["base"]
    assert new["properties"]["added"]["anyOf"][1] == {"type": "null"}
    without_since = {key: value for key, value in new.items() if key != "x-gie-since"}
    assert classify_schema_change(old, without_since)[0] is Change.MINOR


# =========================================================================== version helpers
@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("1.0.0", "1.0.0", Change.NONE),
        ("0.0.0", "0.0.0", Change.NONE),
        ("1.0.0", "1.0.1", Change.PATCH),
        ("1.0.9", "1.0.10", Change.PATCH),
        ("1.2.3", "1.2.30", Change.PATCH),
        ("1.0.0", "1.1.0", Change.MINOR),
        ("1.0.7", "1.1.0", Change.MINOR),
        ("1.9.0", "1.10.0", Change.MINOR),
        ("1.1.5", "1.2.9", Change.MINOR),
        ("1.0.0", "2.0.0", Change.MAJOR),
        ("0.9.9", "1.0.0", Change.MAJOR),
        ("1.7.3", "2.0.0", Change.MAJOR),
        ("9.0.0", "10.0.0", Change.MAJOR),
        ("1.5.5", "3.0.1", Change.MAJOR),
    ],
)
def test_declared_change(old: str, new: str, expected: Change) -> None:
    assert declared_change(old, new) is expected


def test_change_levels_are_ordered() -> None:
    assert Change.NONE < Change.PATCH < Change.MINOR < Change.MAJOR


@pytest.mark.parametrize(
    ("old", "new"),
    [("1.0.1", "1.0.0"), ("1.1.0", "1.0.9"), ("2.0.0", "1.9.9"), ("1.10.0", "1.9.0"), ("10.0.0", "9.0.0")],
)
def test_declared_change_refuses_a_version_going_backwards(old: str, new: str) -> None:
    # The docstring promises "an error"; the class is not specified (see the report), the text is.
    with pytest.raises((ValueError, GieError), match="backwards"):
        declared_change(old, new)
    with pytest.raises((ValueError, GieError), match="backwards"):
        check_evolution(_schema("Tick"), old, _schema("Tick"), new)


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "01.0.0", "1.0.0\n", "", "1.0.9999999999", None, 1])
def test_version_helpers_refuse_malformed_versions(bad: Any) -> None:
    with pytest.raises(ContractVersionError):
        declared_change(bad, "1.0.0")
    with pytest.raises(ContractVersionError):
        declared_change("1.0.0", bad)
    with pytest.raises(ContractVersionError):
        can_read(bad, "1.0.0")
    with pytest.raises(ContractVersionError):
        can_read("1.0.0", bad)


_LEVEL_MUTATIONS: dict[Change, Mutation] = {
    Change.NONE: lambda schema: None,
    Change.PATCH: _reword_first_property,
    Change.MINOR: _add_optional,
    Change.MAJOR: _remove_field("bid"),
}
_DECLARED_VERSIONS = {
    Change.NONE: "1.0.0",
    Change.PATCH: "1.0.1",
    Change.MINOR: "1.1.0",
    Change.MAJOR: "2.0.0",
}


@pytest.mark.parametrize("declared", list(Change), ids=lambda level: f"declared-{level.name}")
@pytest.mark.parametrize("required", list(Change), ids=lambda level: f"required-{level.name}")
def test_check_evolution_reports_a_change_larger_than_the_declared_bump(
    required: Change, declared: Change
) -> None:
    old, new = _schema("Tick"), _schema("Tick")
    _LEVEL_MUTATIONS[required](new)
    assert classify_schema_change(old, new)[0] is required
    new_version = _DECLARED_VERSIONS[declared]
    problems = check_evolution(old, "1.0.0", new, new_version)
    if required > declared:
        assert len(problems) == 1
        assert f"requires a {required.name} bump" in problems[0]
        assert f"1.0.0 -> {new_version}" in problems[0]
        assert f"declares {declared.name}" in problems[0]
    else:
        assert problems == []


@pytest.mark.parametrize(
    ("reader", "message", "expected"),
    [
        ("1.0.0", "1.0.0", True),
        ("1.0.0", "1.0.9", True),
        ("1.0.0", "1.1.0", True),
        ("1.0.0", "1.999.3", True),
        ("1.4.0", "1.0.0", True),
        ("1.0.0", "2.0.0", False),
        ("1.0.0", "0.9.0", False),
        ("2.0.0", "1.9.9", False),
        ("10.0.0", "1.0.0", False),
        ("0.1.0", "0.2.0", True),
    ],
)
def test_can_read_depends_on_the_major_version_only(reader: str, message: str, expected: bool) -> None:
    assert can_read(reader, message) is expected


@pytest.mark.parametrize("version", ["0.9.0", "1.0.0", "1.0.7", "1.1.0", "1.99.0", "2.0.0", "11.0.0"])
@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_can_read_agrees_with_the_codec(name: str, version: str) -> None:
    wire = _wire(name, schema_version=version)
    if can_read(READER_VERSION, version):
        assert from_wire(wire) == _sample(name)
    else:
        with pytest.raises(ContractVersionError):
            from_wire(wire)


# =========================================================================== COM-06
@pytest.mark.requirement("COM-06")
@pytest.mark.parametrize(
    "module", ["gie.strategy.rogue", "gie.execution.sim", "gie.contractsx.tick", "__main__"]
)
def test_contract_records_cannot_be_defined_outside_the_shared_contracts(module: str) -> None:
    with pytest.raises(TypeError, match=r"may only be defined in gie\.contracts"):
        type("RogueTick", (Tick,), {"__module__": module, "__slots__": ()})
    with pytest.raises(TypeError, match=r"may only be defined in gie\.contracts"):
        type("RogueRecord", (Record,), {"__module__": module, "__slots__": ()})


@pytest.mark.requirement("COM-06")
def test_a_registered_contract_name_cannot_be_registered_again() -> None:
    before = all_contracts()
    with pytest.raises(TypeError, match="already registered"):
        contract("Tick", "1.0.0")(_FixtureV10)
    assert all_contracts() == before
    assert all_contracts()["Tick"] is Tick
    assert _FixtureV10.CONTRACT is None


@pytest.mark.requirement("COM-06")
def test_a_subclass_does_not_pass_as_the_contract() -> None:
    class LocalTick(Tick):  # only possible inside tests; subsystems are refused (see above)
        __slots__ = ()

    genuine = samples.tick()
    local = LocalTick(**{spec.name: getattr(genuine, spec.name) for spec in Tick.FIELD_SPECS})
    with pytest.raises(UnknownContractError):
        to_wire(local)
    assert to_wire(genuine)["contract"] == "Tick"
