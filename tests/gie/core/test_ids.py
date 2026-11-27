"""Independent tests for ``gie.core.ids`` (deterministic identifiers, requirement S00-ASG-22).

Expected identifiers are recomputed here from the documented definition only
(``specifications/SHARED_CONTRACTS.md`` section 4 and the module docstring):

    <prefix>_<first 32 hex characters of SHA-256(canonical JSON of [prefix, [parts...]])>

where a decimal part is encoded as ``{"d": "<canonical decimal string>"}``.  The canonical JSON
payloads below are written by hand and hashed with ``hashlib``; the pinned literals were
additionally computed outside Python with ``sha256sum``.

Every value used here is a synthetic fixture invented for the test; none is market data.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from gie.core.errors import ContractError, ContractValidationError, GieError
from gie.core.ids import ID_HEX_LENGTH, ID_PATTERN, ID_PREFIXES, id_pattern, is_valid_id, make_id, require_id

PROPERTY = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Prefixes listed in specifications/contracts/FIELDS.md, "Identifier prefixes".
DOCUMENTED_PREFIXES = (
    "run",
    "mev",
    "evd",
    "cnd",
    "grp",
    "pln",
    "rsk",
    "rsv",
    "ord",
    "brk",
    "oev",
    "pos",
    "mdl",
)

_HEX = "0123456789abcdef0123456789abcdef"  # 32 lower-case hex characters (synthetic fixture)


def _reference_id(prefix: str, canonical_payload: str) -> str:
    """Identifier computed from a hand-written canonical JSON payload."""
    return prefix + "_" + hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()[:32]


# =========================================================================== form
@pytest.mark.requirement("S00-ASG-22")
def test_documented_prefixes_are_registered() -> None:
    assert set(DOCUMENTED_PREFIXES) <= set(ID_PREFIXES)
    for prefix, meaning in ID_PREFIXES.items():
        assert re.fullmatch(r"[a-z]{3}", prefix), prefix
        assert isinstance(meaning, str)
        assert meaning.strip()


@pytest.mark.requirement("S00-ASG-22")
def test_hex_length_is_128_bits() -> None:
    assert ID_HEX_LENGTH == 32


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_identifier_form(prefix: str) -> None:
    identifier = make_id(prefix, "synthetic", 1)
    assert isinstance(identifier, str)
    assert len(identifier) == 36
    assert re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", identifier)
    assert re.fullmatch(ID_PATTERN, identifier)
    assert re.fullmatch(id_pattern(prefix), identifier)
    assert is_valid_id(identifier)
    assert is_valid_id(identifier, prefix)
    assert require_id(identifier, prefix) == identifier


# =========================================================================== pinned values
@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    ("prefix", "parts", "canonical_payload", "pinned"),
    [
        (
            "run",
            ("replay-2026-11-27",),
            '["run",["replay-2026-11-27"]]',
            "run_f1818bf6f3e9c3e80850cb68ebf878bc",
        ),
        (
            "mev",
            ("SWEEP", "XAUUSD", "M5", 1764230400000000000, [Decimal("2650.50"), Decimal("2648")], None),
            '["mev",["SWEEP","XAUUSD","M5",1764230400000000000,[{"d":"2650.5"},{"d":"2648"}],null]]',
            "mev_62b73b7dc998a995218ed075f9234c29",
        ),
        (
            "evd",
            ("zlato-\u20ac",),
            '["evd",["zlato-\u20ac"]]',
            "evd_373f3c461d95041d9391881c2be90775",
        ),
    ],
    ids=["run", "mev", "evd-non-ascii"],
)
def test_pinned_identifiers(prefix: str, parts: tuple[Any, ...], canonical_payload: str, pinned: str) -> None:
    assert _reference_id(prefix, canonical_payload) == pinned  # the fixture itself is consistent
    assert make_id(prefix, *parts) == pinned


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    ("parts", "canonical_parts"),
    [
        (("a",), '["a"]'),
        (("a", "b"), '["a","b"]'),
        ((("a", "b"),), '[["a","b"]]'),
        ((["a", ["b", []]],), '[["a",["b",[]]]]'),
        ((1,), "[1]"),
        (("1",), '["1"]'),
        ((Decimal("1"),), '[{"d":"1"}]'),
        ((Decimal("1.0"),), '[{"d":"1"}]'),
        ((Decimal("-0.0175"),), '[{"d":"-0.0175"}]'),
        ((Decimal("0"),), '[{"d":"0"}]'),
        ((Decimal("-0.00"),), '[{"d":"0"}]'),
        ((None,), "[null]"),
        (("",), '[""]'),
        ((0,), "[0]"),
        ((-1,), "[-1]"),
        ((2**63 - 1, -(2**63)), "[9223372036854775807,-9223372036854775808]"),
        ((9007199254740993,), "[9007199254740993]"),
        (('quote " backslash \\ newline \n',), '["quote \\" backslash \\\\ newline \\n"]'),
        (("\U0001f600",), '["\U0001f600"]'),
        ((None, [None, [1, "x", Decimal("2.50")]]), '[null,[null,[1,"x",{"d":"2.5"}]]]'),
    ],
)
def test_identifier_is_the_hash_of_the_documented_canonical_payload(
    parts: tuple[Any, ...], canonical_parts: str
) -> None:
    for prefix in ("evd", "ord"):
        assert make_id(prefix, *parts) == _reference_id(prefix, f'["{prefix}",{canonical_parts}]')


# =========================================================================== determinism
@pytest.mark.requirement("S00-ASG-22")
def test_same_parts_give_the_same_identifier() -> None:
    parts = ("SWEEP", "XAUUSD", "M5", 1764230400000000000, [Decimal("2650.5"), Decimal("2648")])
    first = make_id("mev", *parts)
    assert all(make_id("mev", *parts) == first for _ in range(10))
    # equal values built independently (new objects) give the same identifier
    rebuilt = ("SW" + "EEP", "XAUUSD", "M5", 1764230400 * 10**9, [Decimal("2650.5"), Decimal("2648")])
    assert make_id("mev", *rebuilt) == first


@pytest.mark.requirement("S00-ASG-22")
def test_lists_and_tuples_are_the_same_identity() -> None:
    assert make_id("evd", ["a", ["b", 1]]) == make_id("evd", ("a", ("b", 1)))
    assert make_id("evd", ["a", ("b", 1)]) == make_id("evd", ("a", ["b", 1]))


@pytest.mark.requirement("S00-ASG-22")
def test_equal_decimals_give_the_same_identifier() -> None:
    forms = [Decimal("1"), Decimal("1.0"), Decimal("1.000000000000"), Decimal("1E+0"), Decimal("10E-1")]
    assert len({make_id("pln", form) for form in forms}) == 1
    assert make_id("pln", Decimal("2650.50")) == make_id("pln", Decimal("2650.5"))
    assert make_id("pln", Decimal("-0")) == make_id("pln", Decimal("0")) == make_id("pln", Decimal("0.00"))
    assert make_id("pln", Decimal("1E+2")) == make_id("pln", Decimal("100"))
    assert make_id("pln", [Decimal("1.0")]) == make_id("pln", [Decimal("1")])


# =========================================================================== sensitivity
@pytest.mark.requirement("S00-ASG-22")
def test_every_part_matters() -> None:
    base = ("SWEEP", "XAUUSD", "M5", 1764230400000000000, Decimal("2650.5"), None)
    changed = ("SWEEQ", "XAGUSD", "M15", 1764230400000000001, Decimal("2650.51"), "x")
    identifiers = {make_id("mev", *base)}
    for index, replacement in enumerate(changed):
        variant = (*base[:index], replacement, *base[index + 1 :])
        identifiers.add(make_id("mev", *variant))
    assert len(identifiers) == len(base) + 1


@pytest.mark.requirement("S00-ASG-22")
def test_part_order_matters() -> None:
    identifiers = {make_id("evd", *permutation) for permutation in itertools.permutations(("a", "b", "c", 1))}
    assert len(identifiers) == 24


@pytest.mark.requirement("S00-ASG-22")
def test_nesting_and_part_boundaries_matter() -> None:
    variants: list[tuple[Any, ...]] = [
        ("a", "b"),
        (("a", "b"),),
        ((("a", "b"),),),
        (("a",), "b"),
        ("a", ("b",)),
        (("a",), ("b",)),
        ("ab",),
        ("a,b",),
        ('["a","b"]',),
        ("a", "b", None),
        ("a", "b", ""),
        ("a", "b", []),
        ("a", "b", [None]),
        ("a",),
        ("b", "a"),
        ("", "ab"),
        ("ab", ""),
    ]
    identifiers = [make_id("evd", *variant) for variant in variants]
    assert len(set(identifiers)) == len(variants)


@pytest.mark.requirement("S00-ASG-22")
def test_part_types_matter() -> None:
    variants: list[Any] = [
        "1",
        1,
        Decimal("1"),
        ["1"],
        [1],
        [Decimal("1")],
        None,
        "None",
        "null",
        "",
        0,
        "0",
        Decimal("0"),
        [],
        [None],
        [[]],
        "d",
        ["d", "1"],
        '{"d":"1"}',
        "A",
        "a",
        " a",
        "a ",
        -1,
        "-1",
        Decimal("-1"),
        "1.0",
    ]
    identifiers = [make_id("evd", variant) for variant in variants]
    assert len(set(identifiers)) == len(variants)


@pytest.mark.requirement("S00-ASG-22")
def test_prefix_is_part_of_the_hashed_identity() -> None:
    identifiers = [make_id(prefix, "same", 1) for prefix in DOCUMENTED_PREFIXES]
    assert len(set(identifiers)) == len(DOCUMENTED_PREFIXES)
    hex_parts = [identifier.split("_", 1)[1] for identifier in identifiers]
    assert len(set(hex_parts)) == len(DOCUMENTED_PREFIXES)


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-20")
def test_distinct_decimals_at_the_full_documented_precision_get_distinct_identifiers() -> None:
    """Decimals may carry 18 integer and 12 fractional digits; different values are different ids."""
    first = Decimal("123456789012345678.123456789012")
    second = Decimal("123456789012345678.123456789")
    assert first != second
    assert make_id("pln", first) == _reference_id("pln", '["pln",[{"d":"123456789012345678.123456789012"}]]')
    assert make_id("pln", second) == _reference_id("pln", '["pln",[{"d":"123456789012345678.123456789"}]]')
    assert make_id("pln", first) != make_id("pln", second)


# =========================================================================== rejections
@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "parts",
    [
        (1.0,),
        (0.5,),
        (2650.5,),
        (float("nan"),),
        (float("inf"),),
        ("ok", 1.5),
        ([1.0],),
        (("a", [("b", 2.5)]),),
        ("ok", ["x", ["y", 0.1]]),
    ],
    ids=repr,
)
def test_float_parts_are_rejected(parts: tuple[Any, ...]) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        make_id("evd", *parts)
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize("parts", [(True,), (False,), ("ok", True), ([False],), (("a", [True]),)], ids=repr)
def test_bool_parts_are_rejected(parts: tuple[Any, ...]) -> None:
    with pytest.raises(ContractValidationError):
        make_id("evd", *parts)


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "part",
    [{"d": "1"}, {"a", "b"}, b"bytes", bytearray(b"ab"), object(), 1 + 2j, frozenset({1})],
    ids=lambda part: type(part).__name__,
)
def test_unsupported_part_types_are_rejected(part: Any) -> None:
    with pytest.raises(ContractValidationError):
        make_id("evd", part)
    with pytest.raises(ContractValidationError):
        make_id("evd", "ok", [part])


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "part",
    [
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1000000000000000000"),  # 19 integer digits
        Decimal("0.0000000000001"),  # 13 fractional digits
        2**63,
        -(2**63) - 1,
        chr(0xD800),  # lone surrogate: not valid Unicode
    ],
    ids=repr,
)
def test_parts_without_a_canonical_form_are_rejected_with_a_contract_error(part: Any) -> None:
    with pytest.raises(ContractError) as excinfo:
        make_id("evd", part)
    assert isinstance(excinfo.value, GieError)
    with pytest.raises(ContractError):
        make_id("evd", "ok", [part])


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize("prefix", ["xyz", "", "EVD", "Evd", "evd_", "ev", "evdx", "tick", " evd", "id"])
def test_unknown_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(ContractValidationError):
        make_id(prefix, "part")
    with pytest.raises(ContractValidationError):
        id_pattern(prefix)
    with pytest.raises(ContractValidationError):
        require_id(f"evd_{_HEX}", prefix)


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_an_identifier_needs_at_least_one_part(prefix: str) -> None:
    with pytest.raises(ContractValidationError):
        make_id(prefix)


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
def test_rejection_names_the_offending_part() -> None:
    with pytest.raises(ContractValidationError) as first:
        make_id("evd", 1.5, "ok")
    with pytest.raises(ContractValidationError) as second:
        make_id("evd", "ok", 1.5)
    assert first.value.details["path"] != second.value.details["path"]


# =========================================================================== is_valid_id / require_id
@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_is_valid_id_accepts_well_formed_identifiers(prefix: str) -> None:
    identifier = f"{prefix}_{_HEX}"
    assert is_valid_id(identifier) is True
    assert is_valid_id(identifier, prefix) is True
    other = "run" if prefix != "run" else "evd"
    assert is_valid_id(identifier, other) is False


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [
        "",
        "evd",
        "evd_",
        _HEX,
        f"evd{_HEX}",
        f"evd-{_HEX}",
        f"evd__{_HEX}",
        f"evd_{_HEX[:31]}",  # 31 hex characters
        f"evd_{_HEX}0",  # 33 hex characters
        f"evd_{_HEX.upper()}",  # upper-case hex
        f"evd_{_HEX[:31]}A",
        f"evd_{_HEX[:31]}g",  # not a hex digit
        f"EVD_{_HEX}",
        f"Evd_{_HEX}",
        f"ev_{_HEX}",
        f"evdx_{_HEX}",
        f"xyz_{_HEX}",  # well formed but unregistered prefix
        f"abc_{_HEX}",
        f" evd_{_HEX}",
        f"evd_{_HEX} ",
        f"evd_{_HEX}\t",
        f"\nevd_{_HEX}",
        f"evd_{_HEX[:16]} {_HEX[:15]}",
        f"evd_{_HEX[:31]}\uff11",  # full-width digit
        f"evd_{_HEX}_{_HEX}",
        None,
        123,
        1.5,
        True,
        b"evd_0123456789abcdef0123456789abcdef",
        ["evd_0123456789abcdef0123456789abcdef"],
        ("evd", _HEX),
    ],
    ids=repr,
)
def test_malformed_identifiers_are_not_valid(value: Any) -> None:
    assert is_valid_id(value) is False
    assert is_valid_id(value, "evd") is False
    with pytest.raises(ContractValidationError) as excinfo:
        require_id(value, "evd")
    assert excinfo.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
def test_is_valid_id_rejects_a_trailing_newline() -> None:
    """``<prefix>_<32 hex>`` followed by a line feed is 37 characters and is not an identifier."""
    assert is_valid_id(f"evd_{_HEX}\n") is False
    assert is_valid_id(f"evd_{_HEX}\n", "evd") is False


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
def test_require_id_rejects_a_trailing_newline() -> None:
    with pytest.raises(ContractValidationError):
        require_id(f"evd_{_HEX}\n", "evd")


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
def test_require_id_returns_the_identifier_or_raises_with_context() -> None:
    identifier = make_id("ord", "run-1", "origin", "ENTRY", 0)
    assert require_id(identifier, "ord") is identifier
    assert require_id(identifier, "ord", path="order.client_order_id") == identifier

    with pytest.raises(ContractValidationError) as wrong_prefix:
        require_id(identifier, "pln")
    assert wrong_prefix.value.details["path"] == "id"
    assert wrong_prefix.value.details["expected_prefix"] == "pln"

    with pytest.raises(ContractValidationError) as custom_path:
        require_id(identifier, "pln", path="plan.plan_id")
    assert custom_path.value.details["path"] == "plan.plan_id"
    # the journal form of the error is JSON data
    assert json.loads(json.dumps(custom_path.value.to_dict()))["code"] == "GIE.CONTRACT.INVALID"


@pytest.mark.requirement("S00-ASG-22")
def test_is_valid_id_with_an_unknown_expected_prefix_is_false() -> None:
    assert is_valid_id(f"evd_{_HEX}", "xyz") is False
    assert is_valid_id(f"xyz_{_HEX}", "xyz") is False


# =========================================================================== id_pattern
@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("prefix", DOCUMENTED_PREFIXES)
def test_id_pattern_matches_only_identifiers_of_its_prefix(prefix: str) -> None:
    pattern = id_pattern(prefix)
    assert isinstance(pattern, str)
    assert re.fullmatch(pattern, f"{prefix}_{_HEX}")
    assert re.fullmatch(pattern, make_id(prefix, "synthetic"))
    for other in DOCUMENTED_PREFIXES:
        if other != prefix:
            assert re.fullmatch(pattern, f"{other}_{_HEX}") is None
    for malformed in (
        f"{prefix}_{_HEX[:31]}",
        f"{prefix}_{_HEX}0",
        f"{prefix}_{_HEX.upper()}",
        f"x{prefix}_{_HEX}",
    ):
        assert re.fullmatch(pattern, malformed) is None


# =========================================================================== properties
_ID_LEAVES = st.one_of(
    st.none(),
    st.text(max_size=6),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
)
_ID_PARTS = st.recursive(_ID_LEAVES, lambda children: st.lists(children, max_size=3), max_leaves=6)
_ID_PART_LISTS = st.lists(_ID_PARTS, min_size=1, max_size=4)


def _as_tuples(part: Any) -> Any:
    return tuple(_as_tuples(item) for item in part) if isinstance(part, list) else part


@pytest.mark.requirement("S00-ASG-22")
@PROPERTY
@given(prefix=st.sampled_from(DOCUMENTED_PREFIXES), parts=_ID_PART_LISTS)
def test_identifier_matches_the_reference_computation(prefix: str, parts: list[Any]) -> None:
    # For arrays of strings, integers and null, compact JSON without ASCII escaping is the
    # canonical form (there are no object keys to sort and no floats).
    payload = json.dumps([prefix, parts], ensure_ascii=False, separators=(",", ":"))
    identifier = make_id(prefix, *parts)
    assert identifier == _reference_id(prefix, payload)
    assert make_id(prefix, *parts) == identifier
    assert make_id(prefix, *(_as_tuples(part) for part in parts)) == identifier
    assert is_valid_id(identifier, prefix)
    assert re.fullmatch(id_pattern(prefix), identifier)


@pytest.mark.requirement("S00-ASG-22")
@PROPERTY
@given(first=_ID_PART_LISTS, second=_ID_PART_LISTS)
def test_different_parts_give_different_identifiers(first: list[Any], second: list[Any]) -> None:
    assume(first != second)
    assert make_id("evd", *first) != make_id("evd", *second)


_SCALE = 10**12
_TYPICAL_UNITS = 10**24 - 1  # 12 integer digits and 12 fractional digits


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-20")
@PROPERTY
@given(units=st.integers(min_value=-_TYPICAL_UNITS, max_value=_TYPICAL_UNITS))
def test_decimal_parts_are_hashed_in_canonical_decimal_form(units: int) -> None:
    digits = tuple(int(character) for character in str(abs(units)))
    value = Decimal((1 if units < 0 else 0, digits, -12))  # exactly units / 10**12
    whole, fraction = divmod(abs(units), _SCALE)
    fraction_text = f"{fraction:012d}".rstrip("0")
    text = ("-" if units < 0 else "") + str(whole) + ("." + fraction_text if fraction_text else "")
    assert make_id("pln", value) == _reference_id("pln", f'["pln",[{{"d":"{text}"}}]]')
