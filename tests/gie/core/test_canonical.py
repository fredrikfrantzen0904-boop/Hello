"""Independent tests for ``gie.core.canonical`` (canonical JSON, strict parsing, decimal strings).

The expected values come from the standards the module cites, not from its code:

* RFC 8785 (JSON Canonicalization Scheme): the Appendix B number vectors, the sorting example
  of section 3.2.3, the string rules of section 3.2.2.2 and the I-JSON input constraints of
  section 3.1;
* ECMA-262 ``Number::toString`` / ``JSON.stringify`` for the remaining number and string vectors
  (each literal was cross-checked once against an independent ECMAScript engine when this file
  was written; the tests themselves need no such engine);
* RFC 8259 for what is and is not JSON;
* ``specifications/SHARED_CONTRACTS.md`` sections 2, 5 and 7 for the decimal grammar, the exact
  64-bit integers and the strict-parsing rules.  Revision 2 added to the latter: "no number that
  overflows or underflows a double" - a non-zero literal such as ``1e-400`` must not silently
  become zero.

Every value used here is a synthetic fixture invented for the test; none is market data.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import math
import re
import struct
from collections import deque
from collections.abc import Callable
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    getcontext,
    localcontext,
)
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from gie.core import canonical as canonical_module
from gie.core.canonical import (
    DECIMAL_PATTERN,
    EXACT,
    INT64_MAX,
    INT64_MIN,
    canonical_bytes,
    canonical_dumps,
    content_hash,
    decimal_from_str,
    decimal_to_str,
    strict_loads,
)
from gie.core.errors import CanonicalEncodingError, ContractError, GieError

# Deterministic property testing: fixed derivation of examples, no example database, no
# wall-clock dependent deadline or health check.
PROPERTY = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _double(bits_hex: str) -> float:
    """The IEEE-754 double with the given big-endian bit pattern."""
    value: float = struct.unpack(">d", bytes.fromhex(bits_hex))[0]
    return value


# =========================================================================== numbers
#: RFC 8785 Appendix B: IEEE-754 bit pattern -> required JSON text.
RFC8785_APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]

#: RFC 8785 Appendix B: bit patterns that must be refused (NaN, Infinity, -Infinity).
RFC8785_APPENDIX_B_INVALID = ["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"]

#: ECMA-262 Number::toString results (float -> text).
ES_NUMBER_VECTORS = [
    (0.0, "0"),
    (-0.0, "0"),
    (1.0, "1"),
    (-1.0, "-1"),
    (100.0, "100"),
    (4.5, "4.5"),
    (-4.5, "-4.5"),
    (25.0, "25"),
    (0.5, "0.5"),
    (0.1, "0.1"),
    (0.1 + 0.2, "0.30000000000000004"),
    (0.1 + 0.7, "0.7999999999999999"),
    (1 / 3, "0.3333333333333333"),
    (100 / 3, "33.333333333333336"),
    (4.35, "4.35"),
    (123.456, "123.456"),
    (123456789.0, "123456789"),
    (333333333.3333333, "333333333.3333333"),
    (1424953923781206.2, "1424953923781206.2"),
    (1e6, "1000000"),
    (1.5e15, "1500000000000000"),
    (9007199254740992.0, "9007199254740992"),
    (1e19, "10000000000000000000"),
    (9223372036854775808.0, "9223372036854776000"),
    (1e20, "100000000000000000000"),
    (1.2345678901234568e20, "123456789012345680000"),
    (1e21, "1e+21"),
    (1e23, "1e+23"),
    (1e300, "1e+300"),
    (1.5e300, "1.5e+300"),
    (1.7976931348623157e308, "1.7976931348623157e+308"),
    (0.000001, "0.000001"),
    (0.00001234, "0.00001234"),
    (2.5e-5, "0.000025"),
    (1e-7, "1e-7"),
    (-1e-7, "-1e-7"),
    (1.5e-7, "1.5e-7"),
    (1.234e-7, "1.234e-7"),
    (123e-20, "1.23e-18"),
    (2.2250738585072014e-308, "2.2250738585072014e-308"),
    (1e-323, "1e-323"),
    (5e-324, "5e-324"),
]


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@pytest.mark.parametrize(("bits_hex", "expected"), RFC8785_APPENDIX_B)
def test_rfc8785_appendix_b_number_vectors(bits_hex: str, expected: str) -> None:
    assert canonical_dumps(_double(bits_hex)) == expected


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("bits_hex", RFC8785_APPENDIX_B_INVALID)
def test_rfc8785_appendix_b_invalid_numbers_are_refused(bits_hex: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps(_double(bits_hex))


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@pytest.mark.parametrize(("value", "expected"), ES_NUMBER_VECTORS)
def test_floats_use_ecmascript_number_formatting(value: float, expected: str) -> None:
    assert canonical_dumps(value) == expected


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
def test_floats_inside_containers_use_the_same_formatting() -> None:
    assert canonical_dumps([1.0, 2.5, -0.0, 1e21, 1e-7]) == "[1,2.5,0,1e+21,1e-7]"
    assert canonical_dumps({"score": 0.25, "r": -1.5}) == '{"r":-1.5,"score":0.25}'


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_nan_and_infinity_are_rejected_everywhere(value: float) -> None:
    for candidate in (value, [value], {"k": value}, {"k": [1, {"deep": value}]}):
        with pytest.raises(CanonicalEncodingError) as excinfo:
            canonical_dumps(candidate)
        assert excinfo.value.code == "GIE.CONTRACT.ENCODING"
        with pytest.raises(CanonicalEncodingError):
            canonical_bytes(candidate)
        with pytest.raises(CanonicalEncodingError):
            content_hash(candidate)


_ES_NUMBER_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?(e[+-][1-9][0-9]*)?")


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@PROPERTY
@given(st.floats(allow_nan=False, allow_infinity=False))
def test_float_text_is_the_shortest_round_tripping_ecmascript_form(value: float) -> None:
    text = canonical_dumps(value)
    if value == 0:
        assert text == "0"  # covers -0.0
        return
    assert _ES_NUMBER_RE.fullmatch(text), text
    assert text.startswith("-") == (value < 0)
    assert float(text) == value  # exact round trip
    # ECMA-262: exponent notation exactly when the value is >= 1e21 or < 1e-6 in magnitude
    assert ("e" in text) == (abs(value) >= 1e21 or abs(value) < 1e-6)
    mantissa = text.lstrip("-").split("e")[0]
    digits = mantissa.replace(".", "").strip("0")
    assert 1 <= len(digits) <= 17
    if len(digits) > 1:
        # necessary condition for "shortest": the correctly rounded decimal with one digit
        # fewer must not identify the same double
        assert float(f"{value:.{len(digits) - 2}e}") != value


# =========================================================================== integers
@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
def test_int64_constants() -> None:
    assert INT64_MAX == 9223372036854775807
    assert INT64_MIN == -9223372036854775808


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (7, "7"),
        (-1, "-1"),
        (9007199254740993, "9007199254740993"),  # 2**53 + 1: not representable as a double
        (1795771815123456789, "1795771815123456789"),  # a nanosecond instant in 2026
        (2**63 - 1, "9223372036854775807"),
        (-(2**63), "-9223372036854775808"),
    ],
)
def test_integers_are_written_as_exact_digits(value: int, expected: str) -> None:
    assert canonical_dumps(value) == expected
    assert canonical_dumps([value]) == f"[{expected}]"
    assert canonical_dumps({"t": value}) == f'{{"t":{expected}}}'


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 2**64, 10**30, -(10**30)])
def test_integers_beyond_64_bits_are_rejected(value: int) -> None:
    for candidate in (value, [value], {"t": value}, {"a": [{"b": value}]}):
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps(candidate)


@pytest.mark.requirement("S00-ASG-20")
def test_booleans_are_not_integers() -> None:
    assert canonical_dumps(True) == "true"
    assert canonical_dumps(False) == "false"
    assert canonical_dumps(1) == "1"
    assert canonical_dumps(0) == "0"
    assert canonical_dumps(None) == "null"
    assert canonical_dumps([True, 1, False, 0, None, 1.0]) == "[true,1,false,0,null,1]"
    assert canonical_dumps({"a": True, "b": 1, "c": False, "d": 0}) == '{"a":true,"b":1,"c":false,"d":0}'
    assert content_hash(True) != content_hash(1)
    assert content_hash(False) != content_hash(0)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
@PROPERTY
@given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_every_int64_round_trips_exactly(value: int) -> None:
    text = canonical_dumps(value)
    assert text == str(value)
    parsed = strict_loads(text)
    assert type(parsed) is int
    assert parsed == value


# =========================================================================== strings
_TWO_CHARACTER_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r"}


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("code_point", range(0x20))
def test_each_control_character_is_escaped_as_json_stringify_does(code_point: int) -> None:
    expected = _TWO_CHARACTER_ESCAPES.get(code_point, f"\\u{code_point:04x}")
    assert canonical_dumps(chr(code_point)) == f'"{expected}"'
    assert canonical_dumps({chr(code_point): 1}) == f'{{"{expected}":1}}'


@pytest.mark.requirement("S00-ASG-22")
def test_all_control_characters_in_one_string() -> None:
    value = "".join(chr(code_point) for code_point in range(0x20))
    expected = (
        r'"\u0000\u0001\u0002\u0003\u0004\u0005\u0006\u0007\b\t\n\u000b\f\r\u000e\u000f'
        r'\u0010\u0011\u0012\u0013\u0014\u0015\u0016\u0017\u0018\u0019\u001a\u001b\u001c\u001d\u001e\u001f"'
    )
    assert canonical_dumps(value) == expected


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", '""'),
        ("XAUUSD", '"XAUUSD"'),
        ('"', '"\\""'),
        ("\\", '"\\\\"'),
        ('say "hi" \\ bye', '"say \\"hi\\" \\\\ bye"'),
        ("/", '"/"'),  # the solidus is not escaped
        ("a\tb\nc", '"a\\tb\\nc"'),
        ("<script>&'", '"<script>&\'"'),  # no HTML-safe escaping
        ("\x7f", '"\x7f"'),  # DEL is not a JSON control character
        ("\u0080", '"\u0080"'),
        ("\u00e9", '"\u00e9"'),
        ("\u20ac", '"\u20ac"'),
        ("\u2028\u2029", '"\u2028\u2029"'),
        ("\ufb33", '"\ufb33"'),
        ("\U0001f600", '"\U0001f600"'),
    ],
)
def test_string_escaping_and_literal_non_ascii(value: str, expected: str) -> None:
    assert canonical_dumps(value) == expected
    assert canonical_bytes(value) == expected.encode("utf-8")


@pytest.mark.requirement("S00-ASG-22")
def test_canonical_bytes_are_utf8() -> None:
    assert canonical_bytes("\u20ac") == b'"\xe2\x82\xac"'
    assert canonical_bytes("\U0001f600") == b'"\xf0\x9f\x98\x80"'
    assert canonical_bytes({"\u00e9": ["\u20ac"]}) == b'{"\xc3\xa9":["\xe2\x82\xac"]}'
    value = {"k": ["\u20ac", 1, None]}
    assert isinstance(canonical_bytes(value), bytes)
    assert canonical_bytes(value) == canonical_dumps(value).encode("utf-8")


@pytest.mark.requirement("S00-ASG-22")
def test_strings_are_not_unicode_normalised() -> None:
    composed = "\u00e9"
    decomposed = "e\u0301"
    assert canonical_dumps(composed) != canonical_dumps(decomposed)
    # two different keys stay two members; "e" (0x65) sorts before U+00E9
    assert canonical_dumps({composed: 1, decomposed: 2}) == '{"e\u0301":2,"\u00e9":1}'


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [chr(0xD800), chr(0xDFFF), "a" + chr(0xDC00) + "b", chr(0xD83D), chr(0xDE00) + chr(0xD83D)],
    ids=["high", "low", "embedded-low", "unpaired-high", "reversed-pair"],
)
def test_lone_surrogates_are_rejected(value: str) -> None:
    for candidate in (value, [value], {"k": value}, {value: 1}, {"k": [{value: None}]}):
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps(candidate)


# =========================================================================== objects and arrays
@pytest.mark.requirement("S00-ASG-22")
def test_rfc8785_section_3_2_3_sorting_example() -> None:
    value = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "\ufb33": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "\u00f6": "Latin Small Letter O With Diaeresis",
    }
    expected = (
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
        '"\u00f6":"Latin Small Letter O With Diaeresis","\u20ac":"Euro Sign",'
        '"\U0001f600":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}'
    )
    assert canonical_dumps(value) == expected


@pytest.mark.requirement("S00-ASG-22")
def test_astral_key_sorts_before_a_bmp_key_above_the_surrogate_range() -> None:
    astral = "\U0001f600"  # UTF-16 code units D83D DE00
    private_use = "\ue000"  # UTF-16 code unit E000
    # Code-point order (and UTF-8 byte order) is the opposite of UTF-16 code-unit order here,
    # so an implementation that sorts by code points produces different bytes.
    assert private_use < astral
    assert private_use.encode("utf-8") < astral.encode("utf-8")
    expected = '{"\U0001f600":2,"\ue000":1}'
    assert canonical_dumps({private_use: 1, astral: 2}) == expected
    assert canonical_dumps({astral: 2, private_use: 1}) == expected


@pytest.mark.requirement("S00-ASG-22")
def test_key_order_for_mixed_bmp_and_astral_keys() -> None:
    keys = ["\ue000", "\U0001f600", "\ufb33", "z", "\U00010000"]
    text = canonical_dumps({key: index for index, key in enumerate(keys)})
    assert text == '{"z":3,"\U00010000":4,"\U0001f600":1,"\ue000":0,"\ufb33":2}'


@pytest.mark.requirement("S00-ASG-22")
def test_key_order_is_plain_code_unit_order_not_natural_or_case_insensitive() -> None:
    keys = ["b", "a", "B", "aa", "", "10", "9", "1", "a b", "a\x00", "A", "_", "\u00e9"]
    expected_order = ["", "1", "10", "9", "A", "B", "_", "a", "a\x00", "a b", "aa", "b", "\u00e9"]
    text = canonical_dumps(dict.fromkeys(keys, 0))
    assert json.loads(text, object_pairs_hook=lambda pairs: [key for key, _ in pairs]) == expected_order


def _utf16_code_units(text: str) -> list[int]:
    """UTF-16 code units of *text*, computed arithmetically (independent of any codec)."""
    units: list[int] = []
    for character in text:
        code_point = ord(character)
        if code_point < 0x10000:
            units.append(code_point)
        else:
            offset = code_point - 0x10000
            units.extend((0xD800 + (offset >> 10), 0xDC00 + (offset & 0x3FF)))
    return units


_KEY_ALPHABET = st.one_of(
    st.sampled_from(["a", "z", "\x00", "\u00e9", "\ud7ff", "\ue000", "\ufb33", "\U00010000", "\U0001f600"]),
    st.characters(exclude_categories=["Cs"]),
)


@pytest.mark.requirement("S00-ASG-22")
@PROPERTY
@given(st.dictionaries(st.text(alphabet=_KEY_ALPHABET, max_size=5), st.integers(0, 9), max_size=8))
def test_keys_are_always_in_utf16_code_unit_order(mapping: dict[str, int]) -> None:
    text = canonical_dumps(mapping)
    written_order = json.loads(text, object_pairs_hook=lambda pairs: [key for key, _ in pairs])
    assert written_order == sorted(mapping, key=_utf16_code_units)


@pytest.mark.requirement("S00-ASG-22")
def test_nested_structures_have_sorted_keys_and_no_whitespace() -> None:
    value = {"b": [3, 1, 2, {"d": None, "c": []}], "a": {}, "c": {"z": {"y": 1, "x": 2}}}
    text = canonical_dumps(value)
    assert text == '{"a":{},"b":[3,1,2,{"c":[],"d":null}],"c":{"z":{"x":2,"y":1}}}'
    assert re.search(r"\s", text) is None


@pytest.mark.requirement("S00-ASG-22")
def test_empty_containers_and_tuples() -> None:
    assert canonical_dumps({}) == "{}"
    assert canonical_dumps([]) == "[]"
    assert canonical_dumps(()) == "[]"
    assert canonical_dumps((1, "a", None)) == canonical_dumps([1, "a", None]) == '[1,"a",null]'
    assert canonical_dumps([[], {}, [[]]]) == "[[],{},[[]]]"


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
@pytest.mark.parametrize(
    "key",
    [1, 1.5, None, True, (1, 2), b"k", Decimal("1")],
    ids=["int", "float", "none", "bool", "tuple", "bytes", "decimal"],
)
def test_non_string_keys_are_rejected(key: Any) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps({key: "v"})
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps([{"outer": {key: "v"}}])
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps({"a": 1, key: 2})


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [
        b"bytes",
        bytearray(b"ab"),
        {1, 2},
        frozenset({1}),
        Decimal("2650.5"),
        1 + 2j,
        object(),
        dt.date(2026, 11, 27),
        dt.datetime(2026, 11, 27, tzinfo=dt.UTC),
        len,
        range(3),
        deque([1, 2]),
        memoryview(b"ab"),
    ],
    ids=lambda value: type(value).__name__,
)
def test_unsupported_types_are_rejected(value: Any) -> None:
    for candidate in (value, [value], {"k": value}, {"k": [1, {"deep": value}]}):
        with pytest.raises(CanonicalEncodingError) as excinfo:
            canonical_dumps(candidate)
        assert isinstance(excinfo.value, ContractError)
        assert isinstance(excinfo.value, GieError)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
def test_memoryview_is_rejected_like_other_binary_buffers() -> None:
    """A binary buffer is not JSON data; it must not be coerced silently into an array of ints."""
    with pytest.raises(CanonicalEncodingError):
        canonical_dumps(memoryview(b"ab"))


def _raised_type(function: Callable[..., Any], *arguments: Any) -> type[Exception] | None:
    """Type of the exception raised by ``function(*arguments)``, or ``None`` if it returns.

    Used instead of ``pytest.raises`` where the wrong exception would be a ``RecursionError``:
    reporting its thousand-frame traceback is slow and adds nothing to the verdict.
    """
    try:
        function(*arguments)
    except Exception as exc:
        return type(exc)
    return None


@pytest.mark.requirement("S00-ASG-24")
def test_cyclic_structure_is_refused_with_a_gie_error() -> None:
    """A value that cannot be represented must raise ``CanonicalEncodingError``, not RecursionError."""
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    assert _raised_type(canonical_dumps, cyclic) is CanonicalEncodingError
    cyclic_object: dict[str, Any] = {}
    cyclic_object["self"] = cyclic_object
    assert _raised_type(canonical_dumps, cyclic_object) is CanonicalEncodingError


def _nested(levels: int, kind: str, leaf: Any = None) -> Any:
    """*levels* containers inside each other: arrays, objects, or both alternating."""
    value: Any = leaf
    for level in range(levels):
        as_object = kind == "objects" or (kind == "mixed" and level % 2 == 0)
        if as_object:
            value = {} if value is None and level == 0 else {"k": value}
        else:
            value = [] if value is None and level == 0 else [value]
    return value


@pytest.mark.requirement("S00-ASG-22", "S00-ASG-24")
def test_max_depth_is_64() -> None:
    assert canonical_module.MAX_DEPTH == 64


@pytest.mark.requirement("S00-ASG-22")
def test_nesting_up_to_the_maximum_depth_is_encoded() -> None:
    assert canonical_dumps(_nested(64, "arrays")) == "[" * 64 + "]" * 64
    assert canonical_dumps(_nested(64, "arrays", leaf=7)) == "[" * 64 + "7" + "]" * 64
    assert canonical_dumps(_nested(64, "objects", leaf="x")) == '{"k":' * 64 + '"x"' + "}" * 64
    mixed = canonical_dumps(_nested(64, "mixed", leaf=None))
    assert mixed.count("[") + mixed.count("{") == 64
    assert strict_loads(mixed) == _nested(64, "mixed", leaf=None)
    # what the parser accepts at this depth can be written back unchanged
    text = "[" * 64 + "7" + "]" * 64
    assert canonical_dumps(strict_loads(text)) == text


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("kind", ["arrays", "objects", "mixed"])
@pytest.mark.parametrize("levels", [65, 66, 200, 3000])
def test_nesting_beyond_the_maximum_depth_is_refused_with_a_gie_error(kind: str, levels: int) -> None:
    """Too deep for canonical JSON: a ``CanonicalEncodingError``, never a ``RecursionError``."""
    value = _nested(levels, kind, leaf=1)
    assert _raised_type(canonical_dumps, value) is CanonicalEncodingError
    assert _raised_type(canonical_bytes, value) is CanonicalEncodingError
    assert _raised_type(content_hash, value) is CanonicalEncodingError


# =========================================================================== determinism and hashing
@pytest.mark.requirement("S00-ASG-22")
def test_output_does_not_depend_on_insertion_order() -> None:
    items = [("symbol", "XAUUSD"), ("seq", 7), ("bid", "2650.5"), ("flags", ["B", "A"])]
    expected = '{"bid":"2650.5","flags":["B","A"],"seq":7,"symbol":"XAUUSD"}'
    hashes = set()
    for permutation in itertools.permutations(items):
        value = dict(permutation)
        assert canonical_dumps(value) == expected
        assert canonical_bytes(value) == expected.encode("utf-8")
        hashes.add(content_hash(value))
    assert len(hashes) == 1


@pytest.mark.requirement("S00-ASG-22")
def test_repeated_calls_are_identical_and_do_not_mutate_the_input() -> None:
    value = {"b": [1, 2.5, {"y": None, "x": True}], "a": "\u20ac"}
    snapshot = json.dumps(value, sort_keys=False)
    first = canonical_bytes(value)
    assert all(canonical_bytes(value) == first for _ in range(5))
    assert json.dumps(value, sort_keys=False) == snapshot
    assert list(value) == ["b", "a"]


@pytest.mark.requirement("S00-ASG-22")
def test_content_hash_is_the_sha256_of_a_hand_written_canonical_string() -> None:
    value = {
        "symbol": "XAUUSD",
        "bid": "2650.5",
        "seq": 7,
        "time_ns": 1764230400123456789,
        "score": 0.25,
        "flags": [],
        "note": None,
        "ok": True,
    }
    canonical = (
        '{"bid":"2650.5","flags":[],"note":null,"ok":true,"score":0.25,"seq":7,'
        '"symbol":"XAUUSD","time_ns":1764230400123456789}'
    )
    assert canonical_dumps(value) == canonical
    assert canonical_bytes(value) == canonical.encode("utf-8")
    assert content_hash(value) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # pinned: computed outside Python with `printf '%s' '<canonical>' | sha256sum`
    assert content_hash(value) == "f709b9201ab89485b5fde20528483b57d90742c2457c32df54def0f8540c41fe"


@pytest.mark.requirement("S00-ASG-22")
def test_content_hash_of_trivial_values_is_pinned() -> None:
    # well-known SHA-256 digests of the two-byte text {} and the four-byte text null
    assert content_hash({}) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert content_hash(None) == "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"


@pytest.mark.requirement("S00-ASG-22")
def test_content_hash_form_and_sensitivity() -> None:
    base = {"a": 1, "b": [1, 2]}
    digest = content_hash(base)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert content_hash({"b": [1, 2], "a": 1}) == digest
    different = [
        {"a": 2, "b": [1, 2]},
        {"a": 1, "b": [2, 1]},
        {"a": 1, "b": [1, 2], "c": None},
        {"a": "1", "b": [1, 2]},
        {"a": True, "b": [1, 2]},
        {"A": 1, "b": [1, 2]},
        [base],
    ]
    digests = {content_hash(value) for value in different}
    assert len(digests) == len(different)
    assert digest not in digests


# =========================================================================== strict parsing
@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
def test_strict_loads_parses_json_types_exactly() -> None:
    parsed = strict_loads('{"a":[1,2.5,"x",null,true,false,{"n":-3}],"t":1795771815123456789}')
    assert parsed == {"a": [1, 2.5, "x", None, True, False, {"n": -3}], "t": 1795771815123456789}
    assert type(parsed["a"][0]) is int
    assert type(parsed["a"][1]) is float
    assert parsed["a"][4] is True
    assert parsed["a"][5] is False
    assert type(parsed["t"]) is int


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("-0", 0),
        ("9007199254740993", 9007199254740993),
        ("1795771815123456789", 1795771815123456789),
        ("9223372036854775807", 2**63 - 1),
        ("-9223372036854775808", -(2**63)),
    ],
)
def test_strict_loads_keeps_integers_exact(text: str, expected: int) -> None:
    parsed = strict_loads(text)
    assert type(parsed) is int
    assert parsed == expected
    nested = strict_loads(f'{{"t":[{text}]}}')
    assert type(nested["t"][0]) is int
    assert nested["t"][0] == expected


@pytest.mark.requirement("S00-ASG-20")
def test_strict_loads_distinguishes_number_tokens_by_their_syntax() -> None:
    assert type(strict_loads("1")) is int
    assert type(strict_loads("1.0")) is float
    assert type(strict_loads("1e2")) is float
    assert strict_loads("1e2") == 100.0
    assert strict_loads("1.7976931348623157e308") == 1.7976931348623157e308
    assert strict_loads("5e-324") == 5e-324
    assert strict_loads("true") is True
    assert strict_loads("null") is None


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        '{"a":1,"a":2}',
        '{"a":1,"a":1}',
        '{"a":1,"b":2,"a":3}',
        '{"outer":{"k":1,"k":2}}',
        '[{"k":1,"k":2}]',
        '{"a":1,"\\u0061":2}',  # the same key written with an escape
        '{"":1,"":2}',
    ],
)
def test_strict_loads_rejects_duplicate_keys(text: str) -> None:
    with pytest.raises(CanonicalEncodingError) as excinfo:
        strict_loads(text)
    assert excinfo.value.code == "GIE.CONTRACT.ENCODING"
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text.encode("utf-8"))


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    ["NaN", "Infinity", "-Infinity", "[NaN]", '{"x":Infinity}', '{"x":[1,-Infinity]}', "nan", "inf", "-inf"],
)
def test_strict_loads_rejects_nan_and_infinity_tokens(text: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-21", "S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        "9223372036854775808",  # 2**63: one beyond the largest instant
        "-9223372036854775809",
        "18446744073709551616",  # 2**64
        "9223372036854775809",
        "100000000000000000001",
        "1000000000000000000000",  # 1e21 written as digits (its canonical form is 1e+21)
        "123456789012345678901234567890",
        "-123456789012345678901234567890",
        "1" + "0" * 400,
        "1" + "0" * 5000,
        "[9223372036854775808]",
        '{"time_ns":9223372036854775808}',
    ],
    ids=[
        "max+1",
        "min-1",
        "2**64",
        "max+2",
        "1e20+1",
        "1e21-as-digits",
        "30-digits",
        "-30-digits",
        "401-digits",
        "5001-digits",
        "in-array",
        "in-object",
    ],
)
def test_strict_loads_rejects_integers_beyond_64_bits(text: str) -> None:
    """No integer beyond 64 bits exists in GIE JSON.

    The only longer digit strings a reader may meet are canonical renderings of integral
    doubles (see the next test); none of these literals is one, so each would have to be
    rounded and must be refused instead.
    """
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10000000000000000000", 1e19),
        ("-10000000000000000000", -1e19),
        ("100000000000000000000", 1e20),
        ("9223372036854776000", 9223372036854775808.0),  # the double 2**63
        ("-9223372036854776000", -9223372036854775808.0),  # the double -2**63, not the int INT64_MIN
        ("12345678901234567000", 1.2345678901234567e19),
        ("295147905179352830000", 295147905179352830000.0),  # RFC 8785 Appendix B (2**68)
        ("999999999999999900000", 9.999999999999999e20),
    ],
)
def test_strict_loads_reads_canonical_integral_doubles_beyond_int64_as_floats(
    text: str, expected: float
) -> None:
    """RFC 8785 writes an integral double below 1e21 as plain digits; the reader must take it back."""
    assert canonical_dumps(expected) == text  # the literal is the canonical form of that double
    parsed = strict_loads(text)
    assert type(parsed) is float
    assert parsed == expected
    assert canonical_dumps(parsed) == text


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("text", ["1e999", "-1e999", "1E400", "1.8e308", "[1e999]", '{"x":-1e999}', "1e309"])
def test_strict_loads_rejects_floats_that_overflow_a_double(text: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text)


# --------------------------------------------------------------------------- underflow
#: Non-zero literals whose nearest double is zero.  The smallest positive double is
#: 4.94...e-324; anything below half of it rounds to zero.
_UNDERFLOWING_LITERALS = [
    "1e-400",
    "-1e-400",
    "1E-400",
    "1.0e-400",
    "1e-324",
    "2e-324",
    "-2e-324",
    "2.4703282292062327e-324",  # just below half of the smallest positive double
    "10e-401",
    "0.1e-399",
    "123456789e-340",
    "1e-9999",
    "1e-9999999999999999999999",
    "0." + "0" * 400 + "1",  # no exponent at all
    "0." + "0" * 323 + "2",
    "-0." + "0" * 500 + "7",
    "9" * 300 + "e-700",
    "0.000001e-320",
]


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("text", _UNDERFLOWING_LITERALS, ids=lambda text: text[:24])
def test_strict_loads_rejects_non_zero_literals_that_underflow_to_zero(text: str) -> None:
    """Section 7: "no number that overflows or underflows a double"."""
    # positive control: an ordinary parser silently returns zero for this non-zero literal
    assert json.loads(text) == 0.0
    assert any(digit in "123456789" for digit in re.split("[eE]", text)[0])
    documents = (text, f"[{text}]", f'{{"x":{text}}}', f'{{"a":[0.5,{{"b":{text}}}],"c":1}}', f" {text} ")
    for document in documents:
        with pytest.raises(CanonicalEncodingError) as excinfo:
            strict_loads(document)
        error = excinfo.value
        assert error.code == "GIE.CONTRACT.ENCODING"
        assert isinstance(error, ContractError)
        assert "underflow" in error.message
        with pytest.raises(CanonicalEncodingError):
            strict_loads(document.encode("utf-8"))


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
def test_the_underflow_refusal_can_be_journaled_also_for_a_very_long_literal() -> None:
    literal = "0." + "0" * 5000 + "1"
    with pytest.raises(CanonicalEncodingError) as excinfo:
        strict_loads(literal)
    error = excinfo.value
    detail = error.details["value"]
    assert isinstance(detail, str)
    assert 0 < len(detail) <= 64  # the refusal does not carry kilobytes of input
    assert literal.startswith(detail)
    journal_form = error.to_dict()
    assert strict_loads(canonical_dumps(journal_form)) == journal_form


@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize(
    "text",
    [
        "0.0",
        "-0.0",
        "0e0",
        "0E0",
        "0e-400",
        "-0e-400",
        "0.0e-999",
        "0.000",
        "0.0000e-5",
        "0e999",
        "0.0E+999",
        "0e-9999999999999999999999",
        "0." + "0" * 600,
    ],
    ids=lambda text: text[:24],
)
def test_strict_loads_still_accepts_every_spelling_of_zero(text: str) -> None:
    """Zero does not underflow: the rule is about non-zero literals only."""
    parsed = strict_loads(text)
    assert type(parsed) is float
    assert parsed == 0.0
    assert math.copysign(1.0, parsed) == (-1.0 if text.startswith("-") else 1.0)
    assert strict_loads(f'{{"x":[{text}]}}') == {"x": [0.0]}
    assert canonical_dumps(parsed) == "0"


@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize(
    ("text", "bits_hex"),
    [
        ("5e-324", "0000000000000001"),  # the smallest positive double (RFC 8785 Appendix B)
        ("-5e-324", "8000000000000001"),
        ("4.9406564584124654e-324", "0000000000000001"),  # the same double with 17 digits
        ("1e-323", "0000000000000002"),
        ("0." + "0" * 322 + "1", "0000000000000002"),  # 1e-323 without an exponent
        ("2.225073858507201e-308", "000fffffffffffff"),  # the largest subnormal double
        ("2.2250738585072014e-308", "0010000000000000"),  # the smallest normal double
        ("1e-307", "0031fa182c40c60d"),
    ],
    ids=["min", "-min", "min-17-digits", "2*min", "2*min-plain", "max-subnormal", "min-normal", "1e-307"],
)
def test_strict_loads_still_reads_the_smallest_doubles(text: str, bits_hex: str) -> None:
    """Subnormal numbers are doubles; only a literal that becomes zero is refused."""
    expected = _double(bits_hex)
    assert expected != 0.0
    parsed = strict_loads(text)
    assert type(parsed) is float
    assert struct.pack(">d", parsed) == bytes.fromhex(bits_hex)
    assert strict_loads(canonical_dumps(parsed)) == expected


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@PROPERTY
@example(digit=1, exponent=324)
@example(digit=2, exponent=324)
@example(digit=9, exponent=325)
@example(digit=1, exponent=400)
@given(digit=st.integers(min_value=1, max_value=9), exponent=st.integers(min_value=325, max_value=10**6))
def test_any_non_zero_literal_below_the_smallest_double_is_refused(digit: int, exponent: int) -> None:
    for text in (f"{digit}e-{exponent}", f"-{digit}E-{exponent}", f"0.{digit}e-{exponent}"):
        with pytest.raises(CanonicalEncodingError) as excinfo:
            strict_loads(text)
        assert "underflow" in excinfo.value.message
    assert strict_loads(f"0e-{exponent}") == 0.0  # the same exponent on a zero is harmless


@pytest.mark.requirement("S00-ASG-20")
@PROPERTY
@example(mantissa=1, negative=False)
@example(mantissa=1, negative=True)
@example(mantissa=2**52 - 1, negative=False)
@given(mantissa=st.integers(min_value=1, max_value=2**52 - 1), negative=st.booleans())
def test_every_subnormal_double_survives_canonical_text(mantissa: int, negative: bool) -> None:
    """The writer never produces a literal that the strict reader refuses as an underflow."""
    bits = mantissa | (1 << 63 if negative else 0)
    value: float = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
    assert value != 0.0
    text = canonical_dumps(value)
    parsed = strict_loads(text)
    assert type(parsed) is float
    assert struct.pack(">d", parsed) == bits.to_bytes(8, "big")


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "data",
    [
        b'"\xff"',
        b'{"a":"\xc3\x28"}',
        b'"\xc0\x80"',  # overlong encoding of U+0000
        b'"\xed\xa0\x80"',  # UTF-8 encoded surrogate (CESU-8 style)
        b'"\xe2\x82"',  # truncated sequence
        b'"\xf4\x90\x80\x80"',  # beyond U+10FFFF
        '{"a":"\u00e9"}'.encode("latin-1"),
        '{"a":1}'.encode("utf-16"),
        '{"a":1}'.encode("utf-16-le"),
        '{"a":1}'.encode("utf-32"),
    ],
    ids=[
        "ff",
        "bad-continuation",
        "overlong",
        "surrogate",
        "truncated",
        "too-large",
        "latin-1",
        "utf-16-bom",
        "utf-16-le",
        "utf-32",
    ],
)
def test_strict_loads_rejects_bytes_that_are_not_utf8_json(data: bytes) -> None:
    with pytest.raises(CanonicalEncodingError):
        strict_loads(data)


@pytest.mark.requirement("S00-ASG-22")
def test_strict_loads_accepts_utf8_bytes() -> None:
    text = '{"k":"\u20ac\U0001f600","n":[1,2.5,null]}'
    assert strict_loads(text.encode("utf-8")) == {"k": "\u20ac\U0001f600", "n": [1, 2.5, None]}
    assert strict_loads(text.encode("utf-8")) == strict_loads(text)


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "{",
        "}",
        "[",
        "]",
        "[1,]",
        "[,1]",
        "[1 2]",
        "[1,2",
        '{"a":1,}',
        '{"a" 1}',
        '{"a":}',
        "{a:1}",
        "{'a':1}",
        "{1:2}",
        '{"a":1}{"b":2}',
        "1 2",
        "01",
        "-01",
        "1.",
        ".5",
        "+1",
        "-",
        "--1",
        "1e",
        "1e+",
        "0x10",
        "1_000",
        "tru",
        "True",
        "None",
        "nul",
        "undefined",
        '"abc',
        "'abc'",
        '"a\tb"',  # raw control character inside a string
        '"a\nb"',
        '"\\x41"',
        '"\\u12"',
        '"\\"',
        "// comment\n1",
        "/* c */ 1",
        "1 // trailing",
        "\u00a01",  # no-break space is not JSON whitespace
    ],
)
def test_strict_loads_rejects_malformed_json(text: str) -> None:
    with pytest.raises(CanonicalEncodingError) as excinfo:
        strict_loads(text)
    assert excinfo.value.code == "GIE.CONTRACT.ENCODING"
    assert isinstance(excinfo.value, GieError)


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize("value", [None, 1, 1.5, ["{}"], {"a": 1}, object()], ids=lambda v: type(v).__name__)
def test_strict_loads_rejects_input_that_is_not_text_or_bytes(value: Any) -> None:
    with pytest.raises(CanonicalEncodingError):
        strict_loads(value)


@pytest.mark.requirement("S00-ASG-22")
def test_strict_loads_accepts_insignificant_whitespace_and_escapes() -> None:
    assert strict_loads(' { "a" : [ 1 , 2 ] ,\r\n\t"b" : null } \n') == {"a": [1, 2], "b": None}
    assert strict_loads('"\\u00e9\\ud83d\\ude00\\/\\n\\"\\\\"') == '\u00e9\U0001f600/\n"\\'


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    ["[" * 100_000 + "]" * 100_000, '{"a":' * 100_000 + "1" + "}" * 100_000, "[" * 100_000],
    ids=["arrays", "objects", "unterminated"],
)
def test_strict_loads_refuses_extreme_nesting_with_a_gie_error(text: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text)


@pytest.mark.requirement("S00-ASG-22")
def test_strict_loads_accepts_moderate_nesting() -> None:
    depth = 64
    parsed = strict_loads("[" * depth + "7" + "]" * depth)
    for _ in range(depth):
        assert type(parsed) is list
        assert len(parsed) == 1
        parsed = parsed[0]
    assert parsed == 7


@pytest.mark.requirement("S00-ASG-24")
@pytest.mark.parametrize(
    "text",
    [
        '"\\ud800"',
        '"a\\udc00b"',
        '{"\\ud83d":1}',
        '"' + chr(0xD800) + '"',  # the same defect with the surrogate given directly in a Python str
    ],
    ids=["high-escape", "embedded-low-escape", "key-escape", "raw-str"],
)
def test_strict_loads_rejects_lone_surrogates(text: str) -> None:
    """RFC 8785 section 3.1 requires I-JSON input: strings must be valid Unicode.

    A lone surrogate cannot be written back by ``canonical_dumps`` (nor encoded as UTF-8), so a
    strict reader must refuse it instead of creating a string that can never be journaled.
    """
    with pytest.raises(CanonicalEncodingError):
        strict_loads(text)


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(
    "canonical",
    [
        "null",
        "true",
        "0",
        "-9223372036854775808",
        "0.25",
        "1e+21",
        "1e-7",
        '""',
        '"\\u0000\\n\\"\\\\"',
        "[]",
        "{}",
        '{"a":{},"b":[3,1,2,{"c":[],"d":null}],"c":{"z":{"x":2,"y":1}}}',
        '{"\\r":"Carriage Return","1":"One","\u0080":"Control","\u20ac":"Euro Sign",'
        '"\U0001f600":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}',
    ],
)
def test_canonical_text_is_a_fixed_point_of_parse_then_dump(canonical: str) -> None:
    assert canonical_dumps(strict_loads(canonical)) == canonical
    assert canonical_bytes(strict_loads(canonical.encode("utf-8"))) == canonical.encode("utf-8")


@pytest.mark.requirement("S00-ASG-22")
def test_non_canonical_text_is_normalised_by_parse_then_dump() -> None:
    text = '{ "b" : 1.0, "a" : [ 1e0, 10E-1, 100e-2 ], "c": "\\u20ac\\/" }'
    assert canonical_dumps(strict_loads(text)) == '{"a":[1,1,1],"b":1,"c":"\u20ac/"}'


# =========================================================================== round-trip properties
def _same_json(original: Any, parsed: Any) -> bool:
    """Type-aware equality: bool is not a number; floats compare numerically as doubles.

    The canonical form writes an integral float without a fraction, so it is read back as an
    ``int``; converting that ``int`` to a double must give the original value.
    """
    if original is None or isinstance(original, bool):
        return parsed is original
    if isinstance(original, int):
        return type(parsed) is int and parsed == original
    if isinstance(original, float):
        return type(parsed) in (int, float) and float(parsed) == original
    if isinstance(original, str):
        return type(parsed) is str and parsed == original
    if isinstance(original, list):
        return (
            type(parsed) is list
            and len(parsed) == len(original)
            and all(_same_json(a, b) for a, b in zip(original, parsed, strict=True))
        )
    if isinstance(original, dict):
        return (
            type(parsed) is dict
            and parsed.keys() == original.keys()
            and all(_same_json(item, parsed[key]) for key, item in original.items())
        )
    raise AssertionError(f"unexpected fixture type {type(original).__name__}")


def _json_values(floats: st.SearchStrategy[float]) -> st.SearchStrategy[Any]:
    leaves = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63 - 1),
        floats,
        st.text(alphabet=_KEY_ALPHABET, max_size=8),
    )
    return st.recursive(
        leaves,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(alphabet=_KEY_ALPHABET, max_size=4), children, max_size=4),
        ),
        max_leaves=12,
    )


_FINITE_FLOATS = st.floats(allow_nan=False, allow_infinity=False)


def _assert_round_trip(value: Any) -> None:
    text = canonical_dumps(value)
    parsed = strict_loads(text)
    assert _same_json(value, parsed)
    # idempotent through a parse, for text and for bytes
    assert canonical_dumps(parsed) == text
    assert canonical_bytes(strict_loads(text.encode("utf-8"))) == text.encode("utf-8")
    assert content_hash(parsed) == content_hash(value)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@PROPERTY
@example(1e20)
@example(9223372036854775808.0)
@example(-9223372036854775808.0)
@example({"metric": [9.3e18, -1e19, 1.2345678901234567e19, 9.999999999999999e20, 1e21]})
@example([2**63 - 1, -(2**63), 9007199254740993, 9007199254740993.0, 0.1, -0.0, 5e-324])
@given(_json_values(_FINITE_FLOATS))
def test_round_trip_property_for_all_json_values(value: Any) -> None:
    """``strict_loads(canonical_dumps(x)) == x`` for every JSON value with finite floats."""
    _assert_round_trip(value)


#: Integral doubles from 2**63 up to (not including) 1e21: RFC 8785 writes them as digit strings
#: longer than any 64-bit integer.  Random floats almost never land here, so the window gets
#: its own strategy.
_LARGE_INTEGRAL_DOUBLES = st.floats(min_value=2.0**63, max_value=1e21, exclude_max=True).flatmap(
    lambda value: st.sampled_from([value, -value])
)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@PROPERTY
@example(9223372036854775808.0)
@example(1e19)
@example(1e20)
@example(-1.2345678901234567e19)
@example(9.999999999999999e20)
@given(_LARGE_INTEGRAL_DOUBLES)
def test_integral_doubles_beyond_int64_round_trip(value: float) -> None:
    """Whatever ``canonical_dumps`` writes for a finite double, ``strict_loads`` must read back."""
    text = canonical_dumps(value)
    assert re.fullmatch(r"-?[1-9][0-9]{18,20}", text), text  # plain digits, no exponent
    parsed = strict_loads(text)
    assert type(parsed) is float
    assert struct.pack(">d", parsed) == struct.pack(">d", value)
    assert canonical_dumps(parsed) == text
    assert strict_loads(canonical_dumps({"x": [value]})) == {"x": [value]}


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@PROPERTY
@example(0.0)
@example(-0.0)
@example(5e-324)
@example(1.7976931348623157e308)
@example(9007199254740993.0)
@example(9223372036854774784.0)
@given(_FINITE_FLOATS)
def test_float_round_trip_is_bit_exact_up_to_the_sign_of_zero(value: float) -> None:
    parsed = float(strict_loads(canonical_dumps(value)))
    if value == 0:
        assert parsed == 0  # -0.0 is written as 0 (RFC 8785)
    else:
        assert struct.pack(">d", parsed) == struct.pack(">d", value)


# =========================================================================== decimals: writer
@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("2650.5"), "2650.5"),
        (Decimal("2650.50"), "2650.5"),
        (Decimal("2650.500000000000"), "2650.5"),
        (Decimal("100"), "100"),
        (Decimal("100.00"), "100"),
        (Decimal("1E+2"), "100"),
        (Decimal("1.5E+3"), "1500"),
        (Decimal("12.5E-1"), "1.25"),
        (Decimal("0.1"), "0.1"),
        (Decimal("-1.50"), "-1.5"),
        (Decimal("-0.0175"), "-0.0175"),
        (Decimal("0.05"), "0.05"),
        (Decimal("0"), "0"),
        (Decimal("-0"), "0"),
        (Decimal("0.00"), "0"),
        (Decimal("-0.00"), "0"),
        (Decimal("0E-10"), "0"),
        (Decimal("0E+10"), "0"),
        (Decimal("-0E-20"), "0"),
        (Decimal("1"), "1"),
        (Decimal("-1"), "-1"),
        (Decimal("10"), "10"),
        (Decimal("1.0000000000000"), "1"),  # 13 fractional digits, all zero: the value is 1
    ],
    ids=str,
)
def test_decimal_to_str_canonical_forms(value: Decimal, expected: str) -> None:
    assert decimal_to_str(value) == expected


@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("999999999999999999"), "999999999999999999"),  # 18 integer digits
        (Decimal("-999999999999999999"), "-999999999999999999"),
        (Decimal("100000000000000000"), "100000000000000000"),
        (Decimal("1E+17"), "100000000000000000"),
        (Decimal("0.000000000001"), "0.000000000001"),  # 12 fractional digits
        (Decimal("1E-12"), "0.000000000001"),
        (Decimal("-0.000000000001"), "-0.000000000001"),
        (Decimal("0.999999999999"), "0.999999999999"),
        (Decimal("2650.123456789012"), "2650.123456789012"),
        (Decimal("999999999999999999.5"), "999999999999999999.5"),
        (Decimal("1.000000000001"), "1.000000000001"),
    ],
    ids=str,
)
def test_decimal_to_str_accepts_18_integer_and_12_fractional_digits(value: Decimal, expected: str) -> None:
    assert decimal_to_str(value) == expected


@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize(
    "text",
    [
        "999999999999999999.999999999999",
        "-123456789012345678.123456789012",
        "12345678901234567.123456789012",  # 29 significant digits
    ],
)
def test_decimal_to_str_is_exact_at_the_full_documented_precision(text: str) -> None:
    """18 integer and 12 fractional digits together (up to 30 significant digits) are in range."""
    assert decimal_to_str(Decimal(text)) == text


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
def test_decimal_to_str_does_not_depend_on_the_ambient_decimal_context() -> None:
    """Canonical text must be a function of the value alone, not of thread-local context."""
    value = Decimal("2650.123456")
    assert decimal_to_str(value) == "2650.123456"
    with localcontext() as context:
        context.prec = 5
        assert decimal_to_str(value) == "2650.123456"
    with localcontext() as context:
        context.prec = 1
        assert decimal_to_str(Decimal("-0.0175")) == "-0.0175"


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [
        Decimal("1000000000000000000"),  # 19 integer digits
        Decimal("-1000000000000000000"),
        Decimal("1E+18"),
        Decimal("9999999999999999999.5"),
        Decimal("1E+30"),
        Decimal("0.0000000000001"),  # 13 fractional digits
        Decimal("-0.0000000000001"),
        Decimal("1E-13"),
        Decimal("1.0000000000001"),
        Decimal("2650.1234567890123"),
        Decimal("1E-30"),
    ],
    ids=str,
)
def test_decimal_to_str_rejects_19_integer_or_13_fractional_digits(value: Decimal) -> None:
    with pytest.raises(CanonicalEncodingError) as excinfo:
        decimal_to_str(value)
    assert excinfo.value.code == "GIE.CONTRACT.ENCODING"


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "text", ["1E+999999", "1E+1000000", "-1E+2000000", "1E-1000030", "1E-1000100", "-1E-2000000"]
)
def test_decimal_to_str_rejects_extreme_exponents_with_a_gie_error(text: str) -> None:
    """Far out-of-range magnitudes are rejected like any other out-of-range value.

    They must neither leak a ``decimal`` arithmetic exception nor be turned into ``"0"``.
    """
    with pytest.raises(CanonicalEncodingError):
        decimal_to_str(Decimal(text))


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("text", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "NaN123"])
def test_decimal_to_str_rejects_non_finite_decimals(text: str) -> None:
    with pytest.raises(CanonicalEncodingError):
        decimal_to_str(Decimal(text))


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "value", [1, 1.5, "1.5", None, True, b"1", [Decimal("1")]], ids=lambda v: type(v).__name__
)
def test_decimal_to_str_rejects_non_decimals(value: Any) -> None:
    with pytest.raises(CanonicalEncodingError):
        decimal_to_str(value)


# =========================================================================== decimals: reader
_VALID_DECIMAL_STRINGS = [
    "0",
    "1",
    "-1",
    "10",
    "2650.5",
    "2650.50",
    "2650.500000000000",
    "0.5",
    "-0.0175",
    "0.000000000001",
    "999999999999999999",
    "-999999999999999999",
    "999999999999999999.999999999999",
    "100.000000000000",
    "0.0",
    "-0",
    "-0.0",
]

_INVALID_DECIMAL_STRINGS = [
    "",
    " ",
    "1e5",
    "1E5",
    "1E+2",
    "1e-2",
    "+1",
    "+0.5",
    "01",
    "00",
    "-01",
    "007.5",
    "00.5",
    " 1",
    "1 ",
    " 1 ",
    "\t1",
    "1\t",
    "\n1",
    "1\r",
    "1 .5",
    ".5",
    "-.5",
    "5.",
    "-5.",
    ".",
    "-",
    "--1",
    "1-",
    "1..2",
    "1.2.3",
    "1,5",
    "1_000",
    "0x10",
    "NaN",
    "nan",
    "sNaN",
    "Infinity",
    "-Infinity",
    "inf",
    "abc",
    "1.5abc",
    "\uff11\uff12",  # full-width digits
    "\u0661\u0662",  # Arabic-Indic digits
    "1.0000000000000",  # 13 fractional digits
    "0.0000000000001",
    "1000000000000000000",  # 19 integer digits
    "-1000000000000000000",
    "1234567890123456789.5",
]


@pytest.mark.requirement("S00-ASG-20")
@pytest.mark.parametrize("text", _VALID_DECIMAL_STRINGS)
def test_decimal_from_str_accepts_the_wire_grammar(text: str) -> None:
    parsed = decimal_from_str(text)
    assert type(parsed) is Decimal
    assert parsed == Decimal(text)
    assert parsed.is_finite()
    assert re.fullmatch(DECIMAL_PATTERN, text)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("text", _INVALID_DECIMAL_STRINGS, ids=repr)
def test_decimal_from_str_rejects_everything_outside_the_wire_grammar(text: str) -> None:
    with pytest.raises(CanonicalEncodingError) as excinfo:
        decimal_from_str(text)
    assert excinfo.value.code == "GIE.CONTRACT.ENCODING"
    assert re.fullmatch(DECIMAL_PATTERN, text) is None


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize("text", ["2650.5\n", "0\n"], ids=repr)
def test_decimal_from_str_rejects_a_trailing_newline(text: str) -> None:
    """Whitespace is not part of the grammar; ``"2650.5\\n"`` must not be repaired to 2650.5."""
    assert re.fullmatch(DECIMAL_PATTERN, text) is None
    with pytest.raises(CanonicalEncodingError):
        decimal_from_str(text)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-24")
@pytest.mark.parametrize(
    "value",
    [1, 1.5, None, True, Decimal("1"), b"1", ["1"]],
    ids=lambda v: type(v).__name__,
)
def test_decimal_from_str_rejects_non_strings(value: Any) -> None:
    with pytest.raises(CanonicalEncodingError):
        decimal_from_str(value)


@pytest.mark.requirement("S00-ASG-20")
def test_decimal_reader_accepts_trailing_zeros_and_the_writer_removes_them() -> None:
    assert decimal_from_str("2650.50") == Decimal("2650.5")
    assert decimal_to_str(decimal_from_str("2650.50")) == "2650.5"
    assert decimal_to_str(decimal_from_str("100.000000000000")) == "100"
    assert decimal_to_str(decimal_from_str("-0.0")) == "0"
    # exact, unlike binary floats
    assert EXACT.add(decimal_from_str("0.1"), decimal_from_str("0.2")) == decimal_from_str("0.3")


# =========================================================================== decimals: properties
_SCALE = 10**12


def _decimal_from_units(units: int) -> Decimal:
    """Exact Decimal equal to ``units / 10**12`` (built from digits, no context arithmetic)."""
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((1 if units < 0 else 0, digits, -12))


def _expected_decimal_text(units: int) -> str:
    """Canonical text of ``units / 10**12`` derived with integer arithmetic only."""
    whole, fraction = divmod(abs(units), _SCALE)
    fraction_text = f"{fraction:012d}".rstrip("0")
    text = str(whole) + ("." + fraction_text if fraction_text else "")
    return "-" + text if units < 0 else text


_CANONICAL_DECIMAL_RE = re.compile(r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{0,11}[1-9])?")
_MAX_UNITS = 10**30 - 1  # 18 integer digits and 12 fractional digits
_TYPICAL_UNITS = 10**24 - 1  # 12 integer digits and 12 fractional digits


def _assert_decimal_round_trip(units: int) -> None:
    value = _decimal_from_units(units)
    text = decimal_to_str(value)
    assert text == _expected_decimal_text(units)
    assert _CANONICAL_DECIMAL_RE.fullmatch(text)
    assert text != "-0"
    parsed = decimal_from_str(text)
    assert parsed == value
    assert decimal_to_str(parsed) == text


@pytest.mark.requirement("S00-ASG-20")
@PROPERTY
@example(units=_MAX_UNITS)
@example(units=-_MAX_UNITS)
@example(units=123456789012345678_123456789012)
@given(units=st.integers(min_value=-_MAX_UNITS, max_value=_MAX_UNITS))
def test_decimal_round_trip_property_for_all_in_range_decimals(units: int) -> None:
    """``decimal_from_str(decimal_to_str(d)) == d`` for every decimal inside the documented range."""
    _assert_decimal_round_trip(units)


@pytest.mark.requirement("S00-ASG-20")
@PROPERTY
@example(units=0)
@example(units=-1)
@example(units=2650_500000000000)
@example(units=_TYPICAL_UNITS)
@given(units=st.integers(min_value=-_TYPICAL_UNITS, max_value=_TYPICAL_UNITS))
def test_decimal_round_trip_property_for_typical_magnitudes(units: int) -> None:
    """The same property concentrated on realistic magnitudes (12 integer, 12 fractional digits).

    Prices, volumes and fractions live here; the full-range property above spends most of its
    examples on 29- and 30-digit values.
    """
    _assert_decimal_round_trip(units)


@pytest.mark.requirement("S00-ASG-20", "S00-ASG-22")
@PROPERTY
@given(
    units=st.integers(min_value=-_TYPICAL_UNITS, max_value=_TYPICAL_UNITS),
    extra_zeros=st.integers(min_value=0, max_value=6),
)
def test_equal_decimals_have_one_canonical_text(units: int, extra_zeros: int) -> None:
    value = _decimal_from_units(units)
    digits = tuple(int(character) for character in str(abs(units))) + (0,) * extra_zeros
    padded = Decimal((1 if units < 0 else 0, digits, -12 - extra_zeros))
    assert padded == value
    assert decimal_to_str(padded) == decimal_to_str(value)


# =========================================================================== decimals: EXACT context
def _exact_decimal(integer: int, exponent: int) -> Decimal:
    """Exact Decimal equal to ``integer * 10**exponent`` (built from digits, no context arithmetic)."""
    digits = tuple(int(character) for character in str(abs(integer)))
    return Decimal((1 if integer < 0 else 0, digits, exponent))


@pytest.mark.requirement("S00-ASG-20")
def test_decimal_range_constants() -> None:
    assert canonical_module.MAX_DECIMAL_INTEGER_DIGITS == 18
    assert canonical_module.MAX_DECIMAL_FRACTION_DIGITS == 12
    assert decimal_to_str(Decimal("9" * 18 + "." + "9" * 12)) == "9" * 18 + "." + "9" * 12


@pytest.mark.requirement("S00-ASG-20")
def test_exact_context_has_64_digits_and_traps_rounding() -> None:
    assert EXACT.prec == 64
    for signal in (Inexact, InvalidOperation, DivisionByZero, Overflow):
        assert EXACT.traps[signal], signal.__name__


@pytest.mark.requirement("S00-ASG-20")
def test_exact_context_raises_instead_of_rounding() -> None:
    assert EXACT.divide(Decimal("5301"), Decimal(2)) == Decimal("2650.5")  # exact results are fine
    with pytest.raises(Inexact):
        EXACT.divide(Decimal(1), Decimal(3))
    with pytest.raises(Inexact):
        EXACT.add(Decimal("1E+64"), Decimal(1))  # 65 significant digits
    with pytest.raises(DivisionByZero):
        EXACT.divide(Decimal(1), Decimal(0))
    with pytest.raises(InvalidOperation):
        EXACT.divide(Decimal(0), Decimal(0))


@pytest.mark.requirement("S00-ASG-20")
def test_exact_context_keeps_what_the_default_context_would_round() -> None:
    """The motivating case: 30-digit wire decimals do not fit the 28 digits of the default context."""
    first = Decimal("999999999999999999.999999999999")
    second = Decimal("0.000000000001")
    assert EXACT.add(first, second) == Decimal("1000000000000000000")
    assert EXACT.subtract(first, second) == Decimal("999999999999999999.999999999998")
    assert EXACT.multiply(first, first) == Decimal(
        "999999999999999999999999999998000000.000000000000000000000001"
    )
    with localcontext(Context(prec=28, traps=[])):  # Python's default precision, rounding silently
        assert first + Decimal(0) != first  # plain arithmetic rounds the 30-digit value


@pytest.mark.requirement("S00-ASG-20")
def test_using_the_exact_context_does_not_change_the_ambient_context() -> None:
    before = (getcontext().prec, getcontext().rounding, dict(getcontext().traps))
    EXACT.add(Decimal("1.5"), Decimal("2.5"))
    with pytest.raises(Inexact):
        EXACT.divide(Decimal(2), Decimal(3))
    assert (getcontext().prec, getcontext().rounding, dict(getcontext().traps)) == before
    assert EXACT.prec == 64


@pytest.mark.requirement("S00-ASG-20")
@PROPERTY
@example(first=_MAX_UNITS, second=_MAX_UNITS)
@example(first=-_MAX_UNITS, second=_MAX_UNITS)
@example(first=_MAX_UNITS, second=1)
@given(
    first=st.integers(min_value=-_MAX_UNITS, max_value=_MAX_UNITS),
    second=st.integers(min_value=-_MAX_UNITS, max_value=_MAX_UNITS),
)
def test_exact_context_sums_differences_and_products_of_wire_decimals_are_exact(
    first: int, second: int
) -> None:
    """Oracle: integer arithmetic on the scaled values (units of 10**-12)."""
    a = _decimal_from_units(first)
    b = _decimal_from_units(second)
    assert EXACT.add(a, b) == _exact_decimal(first + second, -12)
    assert EXACT.subtract(a, b) == _exact_decimal(first - second, -12)
    assert EXACT.multiply(a, b) == _exact_decimal(first * second, -24)
    assert EXACT.compare(a, b) == Decimal((first > second) - (first < second))
