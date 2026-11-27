"""Canonical JSON used for hashing, identifiers, golden files and journals.

The canonical form is RFC 8785 (JSON Canonicalization Scheme, "JCS") with one
documented deviation:

* **Integers are written as exact decimal digits**, whatever their size.  GIE
  timestamps are 64-bit nanosecond counts (about 1.8e18), which do not fit into
  an IEEE-754 double.  Every reader of GIE JSON therefore MUST parse JSON
  integers as 64-bit (or arbitrary precision) integers.  JavaScript consumers
  must use a BigInt-aware parser.

Everything else follows JCS:

* object keys sorted by UTF-16 code units, no insignificant whitespace;
* strings escaped exactly like ECMAScript ``JSON.stringify``;
* non-integer numbers written with the ECMAScript ``Number::toString`` algorithm
  (shortest digits that round-trip), so ``1.0`` is written ``1`` and ``1e-7``
  is written ``1e-7``;
* NaN and infinities are rejected.

Consequence for readers: the JSON text does not say whether a number without a
fraction was an integer or a double.  :func:`strict_loads` returns an ``int`` for
every integer literal inside the signed 64-bit range and a ``float`` for a
larger literal that is the canonical rendering of a double - the digits this
writer produces for it, for example ``1e20`` written as
``100000000000000000000``.  A larger literal with any other digits is refused,
also when its value happens to be exactly representable; the contract field
type decides how a number is used.

Decimal quantities (prices, volumes, money) never appear as JSON numbers; they
are carried as canonical decimal *strings* (see :func:`decimal_to_str`).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from typing import Any, Final

from gie.core.errors import CanonicalEncodingError

INT64_MIN: Final[int] = -(2**63)
INT64_MAX: Final[int] = 2**63 - 1

MAX_DECIMAL_INTEGER_DIGITS: Final[int] = 18
MAX_DECIMAL_FRACTION_DIGITS: Final[int] = 12

#: Wire grammar for decimal strings: plain notation, optional leading minus, no exponent,
#: no leading zeros, at most 18 integer and 12 fractional digits.
DECIMAL_PATTERN: Final[str] = r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?$"
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?")

#: Arithmetic context for contract decimals.  64 significant digits cover every sum, difference
#: and product of wire decimals (at most 30 digits each) exactly, and a result that would need
#: rounding raises ``decimal.Inexact`` instead of being rounded silently.  Python's default
#: context (28 digits, silent rounding) must not be used for prices, volumes, money or risk
#: fractions.
EXACT: Final[Context] = Context(prec=64, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])

#: Maximum nesting depth of canonical JSON.  GIE records are at most four levels deep; the limit
#: turns cyclic or absurdly nested input into a clean error instead of a RecursionError.
MAX_DEPTH: Final[int] = 64

#: Longest integer literal accepted by :func:`strict_loads` (guards against pathological input).
MAX_INTEGER_LITERAL_CHARS: Final[int] = 400

JsonValue = Any  # None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


# --------------------------------------------------------------------------- decimals
def decimal_to_str(value: Decimal) -> str:
    """Canonical wire form of a finite :class:`~decimal.Decimal`.

    Plain notation, trailing fractional zeros removed, ``-0`` written as ``0``.
    Values that need more than 18 integer or 12 fractional digits are refused;
    nothing is ever rounded.
    """
    if not isinstance(value, Decimal):
        raise CanonicalEncodingError("decimal_to_str expects a Decimal", actual_type=type(value).__name__)
    if not value.is_finite():
        raise CanonicalEncodingError("non-finite Decimal cannot be encoded", value=str(value))
    if value == 0:
        return "0"
    # Decide from the digit tuple, before any formatting, whether the value fits.  A value such
    # as Decimal("1E+1000000") is then refused immediately instead of being expanded.
    _sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # true for every finite Decimal
    trailing_zeros = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zeros += 1
    least_significant_exponent = exponent + trailing_zeros
    if (
        value.adjusted() >= MAX_DECIMAL_INTEGER_DIGITS
        or least_significant_exponent < -MAX_DECIMAL_FRACTION_DIGITS
    ):
        raise CanonicalEncodingError(
            "Decimal outside the supported range (18 integer / 12 fractional digits)",
            exponent=value.adjusted(),
        )
    # normalize() rounds to the precision of its context; the default context (28 digits) would
    # silently drop digits of a 30-digit value, so the exact context is passed explicitly.
    text = format(value.normalize(EXACT), "f")
    if not _DECIMAL_RE.fullmatch(text):  # pragma: no cover - guarded by the range check above
        raise CanonicalEncodingError("Decimal cannot be written in canonical form", value=text[:64])
    return text


def decimal_from_str(text: str) -> Decimal:
    """Parse a wire decimal string.  Accepts trailing zeros; rejects exponents, NaN, ``+``."""
    if not isinstance(text, str) or not _DECIMAL_RE.fullmatch(text):
        raise CanonicalEncodingError("not a valid decimal string", value=repr(text)[:64])
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - the grammar already guards this
        raise CanonicalEncodingError("not a valid decimal string", value=text) from exc


# --------------------------------------------------------------------------- numbers
def _es_number(value: float) -> str:
    """ECMAScript ``Number::toString`` for a finite double (RFC 8785 section 3.2.2.3)."""
    if math.isnan(value) or math.isinf(value):
        raise CanonicalEncodingError("NaN/Infinity cannot be encoded as JSON", value=repr(value))
    if value == 0:
        return "0"  # covers -0.0
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = int_part + frac_part
    point = len(int_part) + exponent  # value = 0.<digits> * 10**point
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    e = point - 1
    exp_text = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return sign + digits + "e" + exp_text
    return sign + digits[0] + "." + digits[1:] + "e" + exp_text


#: Fractional digits of every fraction field (risk fractions, daily result, profit floor).
FRACTION_DIGITS: Final[int] = 12
_FRACTION_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-FRACTION_DIGITS)
_DIVISION: Final[Context] = Context(prec=64, traps=[InvalidOperation, DivisionByZero, Overflow])


def fraction_of(amount: Decimal, base: Decimal, *, round_up: bool) -> Decimal:
    """``amount / base`` as a fraction with :data:`FRACTION_DIGITS` digits, rounded in a stated direction.

    An amount divided by an arbitrary equity is rarely an exact decimal, so the
    direction of rounding is part of the contract:

    * ``round_up=False`` rounds towards minus infinity - use it for a result such
      as the daily P&L, so that the fraction never looks better than the amounts;
    * ``round_up=True`` rounds towards plus infinity - use it for a floor or a
      risk, so that the fraction never looks smaller than it is.

    Decisions at a boundary (for example "at exactly the floor no new risk") are
    taken on the exact amounts; a fraction is a derived value for reporting.
    """
    for name, value in (("amount", amount), ("base", base)):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise CanonicalEncodingError(f"{name} must be a finite Decimal")
    if base <= 0:
        raise CanonicalEncodingError("base must be greater than zero")
    quotient = _DIVISION.divide(amount, base)
    return quotient.quantize(
        _FRACTION_QUANTUM, rounding=ROUND_CEILING if round_up else ROUND_FLOOR, context=_DIVISION
    )


def _utf16_key(key: object) -> bytes:
    if not isinstance(key, str):
        raise CanonicalEncodingError("object keys must be strings", actual_type=type(key).__name__)
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("object key is not valid Unicode (lone surrogate)") from exc


def _encode_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("string is not valid Unicode (lone surrogate)") from exc
    return json.dumps(value, ensure_ascii=False)


def _encode(value: JsonValue, out: list[str], depth: int) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise CanonicalEncodingError("integer outside the signed 64-bit range", value=str(value)[:64])
        out.append(str(int(value)))
    elif isinstance(value, float):
        out.append(_es_number(value))
    elif isinstance(value, str):
        out.append(_encode_string(value))
    elif isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            raise CanonicalEncodingError("value is nested too deeply or is cyclic", max_depth=MAX_DEPTH)
        out.append("{")
        first = True
        for key in sorted(value, key=_utf16_key):
            if not first:
                out.append(",")
            first = False
            out.append(_encode_string(key))
            out.append(":")
            _encode(value[key], out, depth + 1)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            raise CanonicalEncodingError("value is nested too deeply or is cyclic", max_depth=MAX_DEPTH)
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out, depth + 1)
        out.append("]")
    else:
        # Includes bytes, bytearray, memoryview, set, Decimal and every other type: only JSON
        # data (None, bool, int, float, str, list/tuple, mapping) has a canonical form.
        raise CanonicalEncodingError(
            "type cannot be encoded as canonical JSON", actual_type=type(value).__name__
        )


def canonical_dumps(value: JsonValue) -> str:
    """Serialise *value* (JSON-compatible Python data) to canonical JSON text."""
    out: list[str] = []
    _encode(value, out, 0)
    return "".join(out)


def canonical_bytes(value: JsonValue) -> bytes:
    """UTF-8 bytes of :func:`canonical_dumps`."""
    return canonical_dumps(value).encode("utf-8")


def content_hash(value: JsonValue) -> str:
    """Lower-case hex SHA-256 of the canonical bytes of *value*."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# --------------------------------------------------------------------------- parsing
def _reject_constant(name: str) -> float:
    raise CanonicalEncodingError("NaN/Infinity are not valid GIE JSON", value=name)


def _no_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in result:
            raise CanonicalEncodingError("duplicate object key", key=key)
        result[key] = item
    return result


def _parse_float(text: str) -> float:
    number = float(text)
    if math.isinf(number):
        raise CanonicalEncodingError("number overflows a double", value=text[:64])
    if number == 0 and any(digit in "123456789" for digit in text.lower().partition("e")[0]):
        # A non-zero literal such as 1e-400 must not silently become zero.
        raise CanonicalEncodingError("number underflows a double", value=text[:64])
    return number


def _parse_int(text: str) -> int | float:
    """Parse a JSON integer literal.

    Within 64 bits it is an exact ``int``.  Beyond that only doubles exist in GIE
    JSON: the canonical form writes an integral double such as ``1e20`` as plain
    digits (``100000000000000000000``).  A larger literal is therefore accepted
    only if it is exactly the canonical rendering of a double; any other large
    integer (for example ``2**63``) would be silently rounded and is refused.
    """
    if len(text) > MAX_INTEGER_LITERAL_CHARS:
        raise CanonicalEncodingError("integer literal is too long", length=len(text))
    number = int(text)
    if INT64_MIN <= number <= INT64_MAX:
        return number
    try:
        as_double = float(number)
    except OverflowError:
        as_double = math.inf
    if math.isinf(as_double) or _es_number(as_double) != text:
        raise CanonicalEncodingError(
            "integer outside the signed 64-bit range (and not the canonical form of a double)",
            value=text[:64],
        )
    return as_double


def _check_text(value: JsonValue) -> None:
    """Refuse strings (values and keys) that are not valid Unicode, e.g. lone surrogates."""
    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalEncodingError("string is not valid Unicode (lone surrogate)") from exc
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def strict_loads(text: str | bytes) -> JsonValue:
    """Parse JSON strictly: exact integers, no NaN/Infinity, no duplicate keys, valid UTF-8 only."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalEncodingError("JSON bytes are not valid UTF-8") from exc
    if not isinstance(text, str):
        raise CanonicalEncodingError("JSON input must be str or bytes", actual_type=type(text).__name__)
    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
            object_pairs_hook=_no_duplicate_keys,
        )
    except CanonicalEncodingError:
        raise
    except (ValueError, RecursionError) as exc:
        raise CanonicalEncodingError("malformed JSON", reason=str(exc)[:200]) from exc
    _check_text(parsed)
    return parsed
