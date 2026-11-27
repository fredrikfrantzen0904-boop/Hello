"""Declarative field specifications for GIE contracts.

One declaration per field drives four things, so they cannot drift apart:

* validation when a record is constructed (every record that exists is valid);
* canonical encoding to the wire form;
* strict decoding from the wire form;
* the published JSON Schema.

Wire representation rules (see ``specifications/SHARED_CONTRACTS.md``):

=================  ==============================  ============================
in memory          on the wire                     notes
=================  ==============================  ============================
``str``            JSON string                     length and pattern checked
``int``            JSON integer                    signed 64-bit, exact
``Decimal``        JSON string                     plain decimal, no exponent
``float``          JSON number                     finite only
``bool``           JSON true/false
``Enum`` member    JSON string (member value)      unknown values are rejected
``tuple``          JSON array
nested record      JSON object
``None``           JSON null                       only for optional fields
=================  ==============================  ============================
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Final, TypeVar

from gie.core.canonical import DECIMAL_PATTERN, INT64_MAX, INT64_MIN, decimal_from_str, decimal_to_str
from gie.core.errors import CanonicalEncodingError, ContractValidationError
from gie.core.ids import ID_PREFIXES, id_pattern
from gie.core.timeutil import MAX_TIME_NS, TRADING_DAY_PATTERN, parse_trading_day

_META_KEY: Final[str] = "gie"


def _fail(path: str, rule: str, **details: Any) -> ContractValidationError:
    return ContractValidationError(f"{path}: {rule}", path=path, rule=rule, **details)


@dataclass(frozen=True, slots=True)
class DecodeContext:
    """Per-message decoding options."""

    #: True only when the message declares a newer MINOR version than the reader
    #: supports; unknown fields are then ignored instead of rejected.
    ignore_unknown_fields: bool = False


# =========================================================================== field types
class FieldType:
    """Base class of all field types."""

    def check(self, value: Any, path: str) -> Any:
        """Validate an in-memory value and return its normalised form."""
        raise NotImplementedError

    def encode(self, value: Any) -> Any:
        """In-memory value -> JSON-compatible value (the value has already been checked)."""
        raise NotImplementedError

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> Any:
        """JSON value -> in-memory value (full validation happens on construction)."""
        raise NotImplementedError

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        """JSON Schema fragment; nested records register themselves in *defs*."""
        raise NotImplementedError

    def describe(self) -> str:
        """Short human-readable type description for the field tables."""
        raise NotImplementedError

    @property
    def optional(self) -> bool:
        return False


class Str(FieldType):
    def __init__(
        self,
        *,
        pattern: str | None = None,
        min_len: int = 1,
        max_len: int = 128,
        choices: tuple[str, ...] | None = None,
        label: str = "string",
    ) -> None:
        self.pattern = pattern
        self._re = re.compile(pattern) if pattern else None
        self.min_len = min_len
        self.max_len = max_len
        self.choices = choices
        self.label = label

    def check(self, value: Any, path: str) -> str:
        if not isinstance(value, str):
            raise _fail(path, "must be a string", actual_type=type(value).__name__)
        if not self.min_len <= len(value) <= self.max_len:
            raise _fail(path, f"length must be {self.min_len}..{self.max_len}", length=len(value))
        if not value.isprintable():
            raise _fail(path, "must be printable text (no control characters or lone surrogates)")
        if self._re is not None and not self._re.fullmatch(value):
            raise _fail(path, f"must match {self.pattern}", value=value[:64])
        if self.choices is not None and value not in self.choices:
            raise _fail(path, f"must be one of {list(self.choices)}", value=value[:64])
        return value

    def encode(self, value: str) -> str:
        return value

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> str:
        if not isinstance(raw, str):
            raise _fail(path, "must be a JSON string", actual_type=type(raw).__name__)
        return raw

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string", "minLength": self.min_len, "maxLength": self.max_len}
        if self.pattern:
            out["pattern"] = self.pattern
        if self.choices is not None:
            out["enum"] = list(self.choices)
        return out

    def describe(self) -> str:
        if self.choices is not None:
            return "const " + " | ".join(self.choices)
        return self.label


class Id(Str):
    """Deterministic identifier with a registered prefix (see ``gie.core.ids``)."""

    def __init__(self, prefix: str) -> None:
        if prefix not in ID_PREFIXES:
            raise ValueError(f"unknown id prefix {prefix!r}")
        super().__init__(pattern=id_pattern(prefix), min_len=36, max_len=36, label=f"id<{prefix}>")
        self.prefix = prefix


class TradingDay(Str):
    """Prague trading day label ``YYYY-MM-DD``."""

    def __init__(self) -> None:
        super().__init__(pattern=TRADING_DAY_PATTERN, min_len=10, max_len=10, label="trading day")

    def check(self, value: Any, path: str) -> str:
        text = super().check(value, path)
        try:
            parse_trading_day(text)
        except ContractValidationError as exc:
            raise _fail(path, "must be a real calendar date YYYY-MM-DD", value=text) from exc
        return text


class Int(FieldType):
    def __init__(self, *, ge: int | None = None, le: int | None = None, label: str = "int") -> None:
        self.ge = INT64_MIN if ge is None else ge
        self.le = INT64_MAX if le is None else le
        self.label = label

    def check(self, value: Any, path: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, "must be an integer", actual_type=type(value).__name__)
        if not self.ge <= value <= self.le:
            raise _fail(path, f"must be within [{self.ge}, {self.le}]", value=value)
        return value

    def encode(self, value: int) -> int:
        return value

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _fail(path, "must be a JSON integer", actual_type=type(raw).__name__)
        return raw

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"type": "integer", "minimum": self.ge, "maximum": self.le}

    def describe(self) -> str:
        return self.label


class TimeNs(Int):
    """Instant: nanoseconds since the Unix epoch, UTC."""

    def __init__(self) -> None:
        super().__init__(ge=0, le=MAX_TIME_NS, label="instant (ns since epoch, UTC)")


class DurationNs(Int):
    """Non-negative duration in nanoseconds."""

    def __init__(self) -> None:
        super().__init__(ge=0, le=MAX_TIME_NS, label="duration (ns)")


class Dec(FieldType):
    """Exact decimal quantity (price, volume, money, risk fraction)."""

    def __init__(
        self,
        *,
        gt: str | None = None,
        ge: str | None = None,
        le: str | None = None,
        label: str = "decimal",
    ) -> None:
        self.gt = Decimal(gt) if gt is not None else None
        self.ge = Decimal(ge) if ge is not None else None
        self.le = Decimal(le) if le is not None else None
        self.label = label

    def check(self, value: Any, path: str) -> Decimal:
        if not isinstance(value, Decimal):
            raise _fail(path, "must be a Decimal (floats are not accepted)", actual_type=type(value).__name__)
        try:
            decimal_to_str(value)
        except CanonicalEncodingError as exc:
            raise _fail(path, "must be a finite decimal within the supported range") from exc
        if self.gt is not None and not value > self.gt:
            raise _fail(path, f"must be > {self.gt}", value=str(value))
        if self.ge is not None and not value >= self.ge:
            raise _fail(path, f"must be >= {self.ge}", value=str(value))
        if self.le is not None and not value <= self.le:
            raise _fail(path, f"must be <= {self.le}", value=str(value))
        return value

    def encode(self, value: Decimal) -> str:
        return decimal_to_str(value)

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> Decimal:
        if not isinstance(raw, str):
            raise _fail(path, "must be a decimal string", actual_type=type(raw).__name__)
        try:
            return decimal_from_str(raw)
        except CanonicalEncodingError as exc:
            raise _fail(path, "must be a plain decimal string", value=raw[:64]) from exc

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string", "pattern": DECIMAL_PATTERN, "x-gie-type": "decimal"}
        if self.gt is not None:
            out["x-gie-exclusiveMinimum"] = decimal_to_str(self.gt)
        if self.ge is not None:
            out["x-gie-minimum"] = decimal_to_str(self.ge)
        if self.le is not None:
            out["x-gie-maximum"] = decimal_to_str(self.le)
        return out

    def describe(self) -> str:
        bounds = []
        if self.gt is not None:
            bounds.append(f"> {self.gt}")
        if self.ge is not None:
            bounds.append(f">= {self.ge}")
        if self.le is not None:
            bounds.append(f"<= {self.le}")
        return self.label + (" " + ", ".join(bounds) if bounds else "")


class Float(FieldType):
    """Finite IEEE-754 double for statistical quantities (scores, R multiples)."""

    def __init__(
        self,
        *,
        ge: float | None = None,
        le: float | None = None,
        gt: float | None = None,
        lt: float | None = None,
        label: str = "float",
    ) -> None:
        self.ge, self.le, self.gt, self.lt = ge, le, gt, lt
        self.label = label

    def check(self, value: Any, path: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(path, "must be a number", actual_type=type(value).__name__)
        try:
            number = float(value)
        except OverflowError as exc:
            raise _fail(path, "must be a finite number") from exc
        if math.isnan(number) or math.isinf(number):
            raise _fail(path, "must be finite (NaN/Infinity are rejected)")
        if self.ge is not None and not number >= self.ge:
            raise _fail(path, f"must be >= {self.ge}", value=number)
        if self.le is not None and not number <= self.le:
            raise _fail(path, f"must be <= {self.le}", value=number)
        if self.gt is not None and not number > self.gt:
            raise _fail(path, f"must be > {self.gt}", value=number)
        if self.lt is not None and not number < self.lt:
            raise _fail(path, f"must be < {self.lt}", value=number)
        return number + 0.0  # normalises -0.0 to 0.0

    def encode(self, value: float) -> float:
        return value

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _fail(path, "must be a JSON number", actual_type=type(raw).__name__)
        try:
            number = float(raw)
        except OverflowError as exc:
            raise _fail(path, "must be a finite number") from exc
        if isinstance(raw, int) and number != raw:
            raise _fail(path, "integer is not exactly representable as a double", value=str(raw)[:32])
        return number

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "number"}
        if self.ge is not None:
            out["minimum"] = self.ge
        if self.le is not None:
            out["maximum"] = self.le
        if self.gt is not None:
            out["exclusiveMinimum"] = self.gt
        if self.lt is not None:
            out["exclusiveMaximum"] = self.lt
        return out

    def describe(self) -> str:
        bounds = []
        for symbol, bound in ((">=", self.ge), ("<=", self.le), (">", self.gt), ("<", self.lt)):
            if bound is not None:
                bounds.append(f"{symbol} {bound:g}")
        return self.label + (" " + ", ".join(bounds) if bounds else "")


class Bool(FieldType):
    def check(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            raise _fail(path, "must be a bool", actual_type=type(value).__name__)
        return value

    def encode(self, value: bool) -> bool:
        return value

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> bool:
        if not isinstance(raw, bool):
            raise _fail(path, "must be a JSON boolean", actual_type=type(raw).__name__)
        return raw

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"type": "boolean"}

    def describe(self) -> str:
        return "bool"


class EnumOf(FieldType):
    def __init__(self, enum_type: type[Enum]) -> None:
        self.enum_type = enum_type
        self._by_value = {member.value: member for member in enum_type}

    def check(self, value: Any, path: str) -> Enum:
        if not isinstance(value, self.enum_type):
            raise _fail(
                path,
                f"must be a {self.enum_type.__name__} member",
                actual_type=type(value).__name__,
            )
        return value

    def encode(self, value: Enum) -> Any:
        return value.value

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> Enum:
        if not isinstance(raw, str) or raw not in self._by_value:
            raise _fail(
                path,
                f"unknown {self.enum_type.__name__} value (unknown values are rejected, fail closed)",
                value=repr(raw)[:64],
            )
        return self._by_value[raw]

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"type": "string", "enum": [member.value for member in self.enum_type]}

    def describe(self) -> str:
        return "enum " + self.enum_type.__name__


class Opt(FieldType):
    """Nullable field.  ``None`` is encoded as explicit JSON ``null``."""

    def __init__(self, inner: FieldType) -> None:
        if isinstance(inner, Opt):
            raise ValueError("Opt cannot wrap Opt")
        self.inner = inner

    @property
    def optional(self) -> bool:
        return True

    def check(self, value: Any, path: str) -> Any:
        return None if value is None else self.inner.check(value, path)

    def encode(self, value: Any) -> Any:
        return None if value is None else self.inner.encode(value)

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> Any:
        return None if raw is None else self.inner.decode(raw, path, ctx)

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [self.inner.schema(defs), {"type": "null"}]}

    def describe(self) -> str:
        return self.inner.describe() + " | null"


class ListOf(FieldType):
    """Homogeneous immutable sequence, held in memory as a ``tuple``.

    ``canonical_sort`` (a key function over encoded items) makes the order part
    of the canonical form: producers may supply any order, the record stores and
    emits the sorted order.  ``unique_key`` rejects duplicates.
    """

    def __init__(
        self,
        inner: FieldType,
        *,
        min_items: int = 0,
        max_items: int = 256,
        unique_key: Callable[[Any], Any] | None = None,
        canonical_sort: Callable[[Any], Any] | None = None,
        sorted_label: str | None = None,
    ) -> None:
        self.inner = inner
        self.min_items = min_items
        self.max_items = max_items
        self.unique_key = unique_key
        self.canonical_sort = canonical_sort
        self.sorted_label = sorted_label

    def check(self, value: Any, path: str) -> tuple[Any, ...]:
        if not isinstance(value, (tuple, list)):
            raise _fail(path, "must be a tuple or list", actual_type=type(value).__name__)
        if not self.min_items <= len(value) <= self.max_items:
            raise _fail(path, f"must have {self.min_items}..{self.max_items} items", length=len(value))
        items = [self.inner.check(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if self.unique_key is not None:
            seen: set[Any] = set()
            for index, item in enumerate(items):
                key = self.unique_key(item)
                if key in seen:
                    raise _fail(f"{path}[{index}]", "duplicate item", key=repr(key)[:64])
                seen.add(key)
        if self.canonical_sort is not None:
            items.sort(key=self.canonical_sort)
        return tuple(items)

    def encode(self, value: tuple[Any, ...]) -> list[Any]:
        return [self.inner.encode(item) for item in value]

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> tuple[Any, ...]:
        if not isinstance(raw, list):
            raise _fail(path, "must be a JSON array", actual_type=type(raw).__name__)
        return tuple(self.inner.decode(item, f"{path}[{i}]", ctx) for i, item in enumerate(raw))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "array",
            "items": self.inner.schema(defs),
            "minItems": self.min_items,
            "maxItems": self.max_items,
        }
        if self.unique_key is not None:
            out["x-gie-unique"] = True
        if self.canonical_sort is not None:
            out["x-gie-canonical-order"] = self.sorted_label or "sorted"
        return out

    def describe(self) -> str:
        extra = []
        if self.min_items:
            extra.append(f"min {self.min_items}")
        if self.unique_key is not None:
            extra.append("unique")
        if self.canonical_sort is not None:
            extra.append(self.sorted_label or "sorted")
        return f"list[{self.inner.describe()}]" + (" (" + ", ".join(extra) + ")" if extra else "")


class Obj(FieldType):
    """Nested record (no envelope of its own; versioned with its parent contract)."""

    def __init__(self, record_type: type[Record]) -> None:
        self.record_type = record_type

    def check(self, value: Any, path: str) -> Record:
        if not isinstance(value, self.record_type):
            raise _fail(
                path,
                f"must be a {self.record_type.__name__}",
                actual_type=type(value).__name__,
            )
        return value

    def encode(self, value: Record) -> dict[str, Any]:
        return encode_fields(value)

    def decode(self, raw: Any, path: str, ctx: DecodeContext) -> Record:
        if not isinstance(raw, Mapping):
            raise _fail(path, "must be a JSON object", actual_type=type(raw).__name__)
        decoded: Record = decode_fields(self.record_type, raw, path, ctx, envelope=False)
        return decoded

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        name = self.record_type.__name__
        if name not in defs:
            defs[name] = {}  # placeholder guards against recursion
            defs[name] = object_schema(self.record_type, defs)
        return {"$ref": f"#/$defs/{name}"}

    def describe(self) -> str:
        return self.record_type.__name__


# =========================================================================== records
@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    ftype: FieldType
    doc: str
    unit: str | None
    required: bool


@dataclass(frozen=True, slots=True)
class Invariant:
    """A cross-field rule.  ``check`` returns True when the rule holds."""

    id: str
    text: str
    check: Callable[[Any], bool]


def inv(invariant_id: str, text: str, check: Callable[[Any], bool]) -> Invariant:
    return Invariant(invariant_id, text, check)


def fld(ftype: FieldType, doc: str, *, unit: str | None = None) -> Any:
    """Declare a contract field.  Optional (``Opt``) fields default to ``None``."""
    if not doc.strip():
        raise ValueError("every contract field needs documentation")
    metadata = {_META_KEY: (ftype, doc, unit)}
    if ftype.optional:
        return dataclasses.field(default=None, metadata=metadata)
    return dataclasses.field(metadata=metadata)


class Record:
    """Base class of every contract record (top level or nested)."""

    __slots__ = ()

    #: Set by :func:`contract` on top-level contracts only.
    CONTRACT: ClassVar[str | None] = None
    VERSION: ClassVar[str | None] = None
    #: Cross-field rules declared directly on this class.
    INVARIANTS: ClassVar[tuple[Invariant, ...]] = ()
    #: Filled by :func:`record` / :func:`contract`.
    FIELD_SPECS: ClassVar[tuple[FieldSpec, ...]] = ()
    ALL_INVARIANTS: ClassVar[tuple[Invariant, ...]] = ()

    def __post_init__(self) -> None:
        cls = type(self)
        for spec in cls.FIELD_SPECS:
            value = getattr(self, spec.name)
            normalised = spec.ftype.check(value, spec.name)
            if normalised is not value:
                object.__setattr__(self, spec.name, normalised)
        for rule in cls.ALL_INVARIANTS:
            try:
                holds = bool(rule.check(self))
            except (TypeError, ValueError, ArithmeticError) as exc:
                # A rule that cannot even be evaluated (for example a comparison with a
                # missing value) is treated as violated: fail closed, with a contract error.
                raise ContractValidationError(
                    f"{cls.__name__} invariant {rule.id} could not be evaluated: {rule.text}",
                    contract=cls.__name__,
                    invariant=rule.id,
                    rule=rule.text,
                    cause=type(exc).__name__,
                ) from exc
            if not holds:
                raise ContractValidationError(
                    f"{cls.__name__} invariant {rule.id} violated: {rule.text}",
                    contract=cls.__name__,
                    invariant=rule.id,
                    rule=rule.text,
                )


R = TypeVar("R", bound=type[Record])


def record(cls: R) -> R:
    """Finalise a (nested) record class.  Apply *above* ``@dataclass``."""
    if not hasattr(cls, "__dataclass_fields__"):
        raise TypeError(f"{cls.__name__} must be a dataclass")
    specs: list[FieldSpec] = []
    for item in dataclasses.fields(cls):  # type: ignore[arg-type]
        meta = item.metadata.get(_META_KEY)
        if meta is None:
            raise TypeError(f"{cls.__name__}.{item.name} is not declared with fld()")
        ftype, doc, unit = meta
        specs.append(FieldSpec(item.name, ftype, doc, unit, required=not ftype.optional))
    cls.FIELD_SPECS = tuple(specs)
    invariants: list[Invariant] = []
    seen: set[str] = set()
    for klass in reversed(cls.__mro__):
        for rule in klass.__dict__.get("INVARIANTS", ()):
            if rule.id in seen:
                raise TypeError(f"duplicate invariant id {rule.id} on {cls.__name__}")
            seen.add(rule.id)
            invariants.append(rule)
    cls.ALL_INVARIANTS = tuple(invariants)
    return cls


# =========================================================================== codec primitives
ENVELOPE_CONTRACT: Final[str] = "contract"
ENVELOPE_VERSION: Final[str] = "schema_version"


def encode_fields(value: Record) -> dict[str, Any]:
    return {spec.name: spec.ftype.encode(getattr(value, spec.name)) for spec in type(value).FIELD_SPECS}


def decode_fields(
    record_type: type[Record],
    raw: Mapping[str, Any],
    path: str,
    ctx: DecodeContext,
    *,
    envelope: bool,
) -> Any:
    known = {spec.name for spec in record_type.FIELD_SPECS}
    reserved = {ENVELOPE_CONTRACT, ENVELOPE_VERSION} if envelope else set()
    prefix = f"{path}." if path else ""
    for key in raw:
        if not isinstance(key, str):
            raise _fail(path or "<root>", "object keys must be strings")
        if key not in known and key not in reserved and not ctx.ignore_unknown_fields:
            raise _fail(prefix + key, "unknown field")
    kwargs: dict[str, Any] = {}
    for spec in record_type.FIELD_SPECS:
        if spec.name in raw:
            kwargs[spec.name] = spec.ftype.decode(raw[spec.name], prefix + spec.name, ctx)
        elif spec.required:
            raise _fail(prefix + spec.name, "required field is missing")
    try:
        return record_type(**kwargs)
    except ContractValidationError as exc:
        if path:
            details = dict(exc.details)
            details["parent_path"] = path
            raise ContractValidationError(exc.message, **details) from exc
        raise


def object_schema(record_type: type[Record], defs: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for spec in record_type.FIELD_SPECS:
        fragment = dict(spec.ftype.schema(defs))
        fragment["description"] = spec.doc
        if spec.unit:
            fragment["x-gie-unit"] = spec.unit
        properties[spec.name] = fragment
        if spec.required:
            required.append(spec.name)
    out: dict[str, Any] = {
        "type": "object",
        "description": (record_type.__doc__ or "").strip().split("\n\n")[0].replace("\n", " "),
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if record_type.ALL_INVARIANTS:
        out["x-gie-invariants"] = [{"id": rule.id, "rule": rule.text} for rule in record_type.ALL_INVARIANTS]
    return out
