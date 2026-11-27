"""Contract evolution rules, checked mechanically on the published JSON Schemas.

==========  ==================================================================
change      required version bump
==========  ==================================================================
PATCH       documentation only (``description`` text)
MINOR       new *optional* field; new enum value
MAJOR       anything else: removed or renamed field, new required field,
            changed type, bound, pattern, unit, optionality, invariant or
            behaviour (x-gie-behaviour: transitions, identity rules, immutable
            fields, stream, rank); any change of the run-mode or venue
            vocabulary (the routing lock)
==========  ==================================================================

A new enum value is MINOR although older readers refuse it (unknown enum values
fail closed).  Producers must therefore not emit a new value before every
consumer of that stream has been upgraded; this is an integration step owned by
the coordinator.

Invariant rule texts are frozen with the contract.  Rewording one counts as a
MAJOR change because the tool cannot prove the meaning is unchanged;
clarifications belong in the prose specification.

Three keywords are *derived* from the set of fields and follow it:

* ``x-gie-since`` (introduction version per field) appears, or grows, when a
  MINOR version adds an optional field.  Only a changed or removed entry of a
  field that exists in both schemas is a (MAJOR) change.
* ``minProperties`` equals the number of declared properties ("every key is
  written") and grows with a new field.  It is a (MAJOR) change only when one of
  the two schemas does not state that rule.
* ``required`` is compared field by field together with ``properties``.

The run-mode and venue vocabularies are the routing lock.  A property named
``mode`` or ``venue`` - at any depth, existing or new - is never additive: any
difference of its vocabulary, and a new property of that name that does not
carry exactly the vocabulary of :data:`ROUTING_VOCABULARY`, is MAJOR.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Final

from gie.contracts.registry import SemVer, parse_semver
from gie.core.errors import ContractVersionError

_IGNORED_KEYS = frozenset({"$id", "title"})

#: The routing-lock vocabulary: property name -> the only enum such a property may carry.
ROUTING_VOCABULARY: Final[dict[str, list[str]]] = {"mode": ["REPLAY", "PAPER"], "venue": ["SIMULATED"]}

_SINCE: Final[str] = "x-gie-since"
#: Introduction version of every field of a schema that publishes no ``x-gie-since``.
_FIRST_VERSION: Final[str] = "1.0"
_NULL_SCHEMA: Final[dict[str, str]] = {"type": "null"}


class Change(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


def declared_change(old_version: str, new_version: str) -> Change:
    """Change level that two version numbers declare.  A lower new version is an error."""
    old, new = parse_semver(old_version), parse_semver(new_version)
    if new < old:
        raise ContractVersionError(
            f"version went backwards: {old} -> {new}", old_version=str(old), new_version=str(new)
        )
    if new.major != old.major:
        return Change.MAJOR
    if new.minor != old.minor:
        return Change.MINOR
    if new.patch != old.patch:
        return Change.PATCH
    return Change.NONE


def _invariant_map(items: Any) -> dict[str, str]:
    return {item["id"]: item["rule"] for item in (items or [])}


# --------------------------------------------------------------------------- routing vocabulary
def _routing_enum(fragment: Any) -> Any:
    """The enum of a routing property (of its non-null branch when it is nullable), else ``None``.

    Only the two shapes the generator emits are recognised - ``{"enum": [...]}`` and
    ``{"anyOf": [{"enum": [...]}, {"type": "null"}]}``.  Everything else (a reference, an open
    string, several alternatives) has no recognisable vocabulary and therefore never passes as
    the routing vocabulary.
    """
    if not isinstance(fragment, dict):
        return None
    alternatives = fragment.get("anyOf")
    if alternatives is None:
        return fragment.get("enum")
    if not isinstance(alternatives, list):
        return None
    values = [item for item in alternatives if item != _NULL_SCHEMA]
    if len(values) != 1 or not isinstance(values[0], dict) or "anyOf" in values[0]:
        return None
    return values[0].get("enum")


def _new_routing_property(name: str, fragment: Any, here: str, reasons: list[str]) -> Change:
    """A *new* property named like a routing field must carry exactly the routing vocabulary."""
    expected = ROUTING_VOCABULARY.get(name)
    if expected is None or _routing_enum(fragment) == expected:
        return Change.NONE
    reasons.append(
        f"{here}: new field with a routing-lock vocabulary: {name} must be exactly {expected} "
        f"(found {_routing_enum(fragment)!r})"
    )
    return Change.MAJOR


def _scan_added(fragment: Any, path: str, reasons: list[str]) -> Change:
    """Routing properties anywhere inside an added part of a schema (new field, new definition)."""
    level = Change.NONE
    if isinstance(fragment, dict):
        for key in sorted(fragment, key=str):
            value = fragment[key]
            here = f"{path}/{key}"
            if key == "properties" and isinstance(value, dict):
                for name in sorted(value, key=str):
                    level = max(level, _new_routing_property(name, value[name], f"{here}/{name}", reasons))
                    level = max(level, _scan_added(value[name], f"{here}/{name}", reasons))
            else:
                level = max(level, _scan_added(value, here, reasons))
    elif isinstance(fragment, list):
        for index, item in enumerate(fragment):
            level = max(level, _scan_added(item, f"{path}[{index}]", reasons))
    return level


# --------------------------------------------------------------------------- derived keywords
def _states_every_key(schema: dict[str, Any]) -> bool:
    """Whether an object schema demands every declared key (``minProperties`` = number of properties)."""
    properties, minimum = schema.get("properties"), schema.get("minProperties")
    return isinstance(properties, dict) and type(minimum) is int and minimum == len(properties)


def _diff_since(old: dict[str, Any], new: dict[str, Any], here: str, reasons: list[str]) -> Change:
    """Introduction versions (``x-gie-since``) of the fields that exist in both schemas.

    The generator publishes the table only once a field was added after 1.0; without it every
    field is a 1.0 field.  The entry of a *new* field is not judged here: whether that field may
    be added is decided by the field comparison (optional = MINOR, required = MAJOR).
    """
    old_since, new_since = old.get(_SINCE), new.get(_SINCE)
    old_fields, new_fields = old.get("properties"), new.get("properties")
    if (
        not isinstance(old_fields, dict)
        or not isinstance(new_fields, dict)
        or not (old_since is None or isinstance(old_since, dict))
        or not (new_since is None or isinstance(new_since, dict))
    ):
        # Not the shape the generator publishes: nothing can be attributed to a field.
        if old_since == new_since:
            return Change.NONE
        reasons.append(f"{here}: introduction versions changed")
        return Change.MAJOR
    level = Change.NONE
    for name in sorted(set(old_fields) & set(new_fields), key=str):
        before = _FIRST_VERSION if old_since is None else old_since.get(name)
        after = _FIRST_VERSION if new_since is None else new_since.get(name)
        if before != after:
            reasons.append(
                f"{here}/{name}: introduction version of an existing field changed {before!r} -> {after!r}"
            )
            level = Change.MAJOR
    return level


def _diff_enum(old_values: Any, new_values: Any, here: str, reasons: list[str], routing: bool) -> Change:
    if old_values == new_values:
        return Change.NONE
    if routing:
        # The run-mode and venue vocabularies are the routing lock: never additive.
        reasons.append(f"{here}: routing-lock vocabulary changed {old_values} -> {new_values}")
        return Change.MAJOR
    try:
        old_set, new_set = set(old_values), set(new_values)
    except TypeError:  # not lists of plain values: nothing can be said about "added" or "removed"
        reasons.append(f"{here}: {old_values!r} -> {new_values!r}")
        return Change.MAJOR
    if old_set == new_set:
        return Change.PATCH
    if old_set < new_set:
        reasons.append(f"{here}: enum values added {sorted(new_set - old_set, key=str)}")
        return Change.MINOR
    reasons.append(f"{here}: enum values removed {sorted(old_set - new_set, key=str)}")
    return Change.MAJOR


# --------------------------------------------------------------------------- comparison
def _diff(old: Any, new: Any, path: str, reasons: list[str], routing: bool = False) -> Change:
    """Compare two schema fragments.  *routing* is set inside a property named ``mode`` / ``venue``."""
    if old == new:
        return Change.NONE
    if isinstance(old, dict) and isinstance(new, dict):
        level = Change.NONE
        for key in sorted(set(old) | set(new)):
            if key in _IGNORED_KEYS:
                continue
            here = f"{path}/{key}"
            if key == "description":
                if old.get(key) != new.get(key):
                    level = max(level, Change.PATCH)
                continue
            if key == _SINCE:
                level = max(level, _diff_since(old, new, here, reasons))
                continue
            if key not in old or key not in new:
                if key == "x-gie-invariants":
                    reasons.append(f"{here}: invariants added or removed")
                elif key == "$defs":
                    if key in old:
                        reasons.append(f"{here}: definitions removed")
                        level = max(level, Change.MAJOR)
                    else:
                        level = max(level, _scan_added(new[key], here, reasons))
                    continue
                elif routing and key in ("enum", "const"):
                    reasons.append(f"{here}: routing-lock vocabulary changed (keyword added or removed)")
                else:
                    reasons.append(f"{here}: keyword added or removed")
                level = max(level, Change.MAJOR)
                continue
            if key == "enum":
                level = max(level, _diff_enum(old[key], new[key], here, reasons, routing))
            elif key == "properties":
                level = max(level, _diff_properties(old, new, here, reasons, routing))
            elif key == "required":
                # Handled together with "properties" where the field set is known.
                if "properties" not in old:
                    reasons.append(f"{here}: required list changed")
                    level = max(level, Change.MAJOR)
            elif key == "minProperties":
                # Derived from the field set while both schemas demand every declared key.
                if old[key] != new[key] and not (_states_every_key(old) and _states_every_key(new)):
                    reasons.append(f"{here}: key-presence rule changed {old[key]!r} -> {new[key]!r}")
                    level = max(level, Change.MAJOR)
            elif key == "$defs":
                for name in sorted(set(old[key]) - set(new[key])):
                    reasons.append(f"{here}/{name}: definition removed")
                    level = max(level, Change.MAJOR)
                for name in sorted(set(new[key]) - set(old[key])):
                    level = max(level, _scan_added(new[key][name], f"{here}/{name}", reasons))
                for name in sorted(set(old[key]) & set(new[key])):
                    level = max(level, _diff(old[key][name], new[key][name], f"{here}/{name}", reasons))
            elif key == "x-gie-invariants":
                if _invariant_map(old[key]) != _invariant_map(new[key]):
                    reasons.append(f"{here}: invariants changed")
                    level = max(level, Change.MAJOR)
            else:
                level = max(level, _diff(old[key], new[key], here, reasons, routing))
        return level
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        level = Change.NONE
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            level = max(level, _diff(old_item, new_item, f"{path}[{index}]", reasons, routing))
        return level
    if routing:
        reasons.append(f"{path}: routing-lock vocabulary or its schema changed {old!r} -> {new!r}")
    else:
        reasons.append(f"{path}: {old!r} -> {new!r}")
    return Change.MAJOR


def _diff_properties(
    old: dict[str, Any], new: dict[str, Any], path: str, reasons: list[str], routing: bool = False
) -> Change:
    level = Change.NONE
    old_props, new_props = old["properties"], new["properties"]
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))
    for name in sorted(set(old_props) - set(new_props)):
        reasons.append(f"{path}/{name}: field removed")
        level = max(level, Change.MAJOR)
    for name in sorted(set(new_props) - set(old_props)):
        here = f"{path}/{name}"
        if name in new_required:
            reasons.append(f"{here}: new required field")
            level = max(level, Change.MAJOR)
        else:
            reasons.append(f"{here}: new optional field")
            level = max(level, Change.MINOR)
        # A new field must not bring a routing vocabulary other than the locked one with it.
        level = max(level, _new_routing_property(name, new_props[name], here, reasons))
        level = max(level, _scan_added(new_props[name], here, reasons))
    for name in sorted(set(old_props) & set(new_props)):
        here = f"{path}/{name}"
        if (name in old_required) != (name in new_required):
            reasons.append(f"{here}: optionality changed")
            level = max(level, Change.MAJOR)
        inside_routing = routing or name in ROUTING_VOCABULARY
        reported = len(reasons)
        level = max(level, _diff(old_props[name], new_props[name], here, reasons, inside_routing))
        if name in ROUTING_VOCABULARY:
            # The vocabulary can also change with the shape of the field (enum dropped, field made
            # an open string, a reference put in its place): say so even when no enum was compared.
            before, after = _routing_enum(old_props[name]), _routing_enum(new_props[name])
            said = any("routing-lock vocabulary" in reason for reason in reasons[reported:])
            if before != after and not said:
                reasons.append(f"{here}: routing-lock vocabulary changed {before!r} -> {after!r}")
                level = max(level, Change.MAJOR)
    return level


def classify_schema_change(old: dict[str, Any], new: dict[str, Any]) -> tuple[Change, list[str]]:
    """Smallest version bump that the difference between two schemas requires, with reasons."""
    reasons: list[str] = []
    level = _diff(old, new, "#", reasons)
    return level, reasons


def check_evolution(
    old_schema: dict[str, Any], old_version: str, new_schema: dict[str, Any], new_version: str
) -> list[str]:
    """Problems (empty when fine) with evolving *old* into *new* under the declared versions."""
    declared = declared_change(old_version, new_version)
    required, reasons = classify_schema_change(old_schema, new_schema)
    problems: list[str] = []
    if required > declared:
        problems.append(
            f"schema change requires a {required.name} bump but {old_version} -> {new_version} "
            f"declares {declared.name}: " + "; ".join(reasons)
        )
    return problems


def can_read(reader_version: str, message_version: str) -> bool:
    """Whether a reader built for *reader_version* accepts a message of *message_version*."""
    reader: SemVer = parse_semver(reader_version)
    message: SemVer = parse_semver(message_version)
    return reader.major == message.major
