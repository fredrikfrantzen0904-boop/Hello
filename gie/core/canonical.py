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

Decimal quantities (prices, volumes, money) never appear as JSON numbers; they
are carried as canonical decimal *strings* (see :func:`decimal_to_str`).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow
from typing import Any, Final

from gie.core.errors import CanonicalEncodingError

INT64_MIN: Final[int] = -(2**63)
INT64_MAX: Final[int] = 2**63 - 1

#: Wire grammar for decimal strings: plain notation, optional leading minus, no exponent,
#: no leading zeros, at most 18 integer and 12 fractional digits.
DECIMAL_PATTERN: Final[str] = r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,12})?$"
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(DECIMAL_PATTERN)

#: Arithmetic context for contract decimals.  64 significant digits cover every sum, difference
#: and product of wire decimals (at most 30 digits each) exactly, and a result that would need
#: rounding raises ``decimal.Inexact`` instead of being rounded silently.  Python's default
#: context (28 digits, silent rounding) must not be used for prices, volumes, money or risk
#: fractions.
EXACT: Final[Context] = Context(prec=64, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])

JsonValue = Any  # None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


# --------------------------------------------------------------------------- decimals
def decimal_to_str(value: Decimal) -> str:
    """Canonical wire form of a finite :class:`~decimal.Decimal`.

    Plain notation, trailing fractional zeros removed, ``-0`` written as ``0``.
    """
    if not isinstance(value, Decimal):
        raise CanonicalEncodingError("decimal_to_str expects a Decimal", actual_type=type(value).__name__)
    if not value.is_finite():
        raise CanonicalEncodingError("non-finite Decimal cannot be encoded", value=str(value))
    if value == 0:
        return "0"
    # normalize() rounds to the precision of its context; the default context (28 digits) would
    # silently drop digits of a 30-digit value, so the exact context is passed explicitly.
    text = format(value.normalize(EXACT), "f")
    if not _DECIMAL_RE.match(text):
        raise CanonicalEncodingError(
            "Decimal outside the supported range (18 integer / 12 fractional digits)", value=text
        )
    return text


def decimal_from_str(text: str) -> Decimal:
    """Parse a wire decimal string.  Accepts trailing zeros; rejects exponents, NaN, ``+``."""
    if not isinstance(text, str) or not _DECIMAL_RE.match(text):
        raise CanonicalEncodingError("not a valid decimal string", value=repr(text)[:64])
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guards this
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


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def _encode(value: JsonValue, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise CanonicalEncodingError("integer outside the signed 64-bit range", value=str(value))
        out.append(str(int(value)))
    elif isinstance(value, float):
        out.append(_es_number(value))
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalEncodingError("string is not valid Unicode (lone surrogate)") from exc
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, Mapping):
        out.append("{")
        first = True
        for key in sorted(value, key=_utf16_key_checked):
            if not first:
                out.append(",")
            first = False
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _encode(item, out)
        out.append("]")
    else:
        raise CanonicalEncodingError(
            "type cannot be encoded as canonical JSON", actual_type=type(value).__name__
        )


def _utf16_key_checked(key: object) -> bytes:
    if not isinstance(key, str):
        raise CanonicalEncodingError("object keys must be strings", actual_type=type(key).__name__)
    try:
        return _utf16_key(key)
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("object key is not valid Unicode") from exc


def canonical_dumps(value: JsonValue) -> str:
    """Serialise *value* (JSON-compatible Python data) to canonical JSON text."""
    out: list[str] = []
    _encode(value, out)
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
    return number


def _parse_int(text: str) -> int:
    number = int(text)
    if not INT64_MIN <= number <= INT64_MAX:
        raise CanonicalEncodingError("integer outside the signed 64-bit range", value=text[:64])
    return number


def strict_loads(text: str | bytes) -> JsonValue:
    """Parse JSON strictly: exact integers, no NaN/Infinity, no duplicate keys, UTF-8 only."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalEncodingError("JSON bytes are not valid UTF-8") from exc
    if not isinstance(text, str):
        raise CanonicalEncodingError("JSON input must be str or bytes", actual_type=type(text).__name__)
    try:
        return json.loads(
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
