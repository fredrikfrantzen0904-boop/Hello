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
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from gie.contracts.registry import SemVer, parse_semver

_IGNORED_KEYS = frozenset({"$id", "title"})


class Change(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


def declared_change(old_version: str, new_version: str) -> Change:
    """Change level that two version numbers declare.  A lower new version is an error."""
    old, new = parse_semver(old_version), parse_semver(new_version)
    if new < old:
        raise ValueError(f"version went backwards: {old} -> {new}")
    if new.major != old.major:
        return Change.MAJOR
    if new.minor != old.minor:
        return Change.MINOR
    if new.patch != old.patch:
        return Change.PATCH
    return Change.NONE


def _invariant_map(items: Any) -> dict[str, str]:
    return {item["id"]: item["rule"] for item in (items or [])}


def _diff(old: Any, new: Any, path: str, reasons: list[str]) -> Change:
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
            if key not in old or key not in new:
                if key == "x-gie-invariants":
                    reasons.append(f"{here}: invariants added or removed")
                elif key == "$defs":
                    if key in old:
                        reasons.append(f"{here}: definitions removed")
                        level = max(level, Change.MAJOR)
                    continue
                else:
                    reasons.append(f"{here}: keyword added or removed")
                level = max(level, Change.MAJOR)
                continue
            if key == "enum":
                old_values, new_values = list(old[key]), list(new[key])
                if path.endswith(("/properties/mode", "/properties/venue")) and old_values != new_values:
                    # The run-mode and venue vocabularies are the routing lock: never additive.
                    reasons.append(f"{here}: routing-lock vocabulary changed {old_values} -> {new_values}")
                    level = max(level, Change.MAJOR)
                elif set(old_values) == set(new_values):
                    level = max(level, Change.PATCH)
                elif set(old_values) < set(new_values):
                    reasons.append(f"{here}: enum values added {sorted(set(new_values) - set(old_values))}")
                    level = max(level, Change.MINOR)
                else:
                    reasons.append(f"{here}: enum values removed {sorted(set(old_values) - set(new_values))}")
                    level = max(level, Change.MAJOR)
            elif key == "properties":
                level = max(level, _diff_properties(old, new, here, reasons))
            elif key == "required":
                # Handled together with "properties" where the field set is known.
                if "properties" not in old:
                    reasons.append(f"{here}: required list changed")
                    level = max(level, Change.MAJOR)
            elif key == "$defs":
                for name in sorted(set(old[key]) - set(new[key])):
                    reasons.append(f"{here}/{name}: definition removed")
                    level = max(level, Change.MAJOR)
                for name in sorted(set(old[key]) & set(new[key])):
                    level = max(level, _diff(old[key][name], new[key][name], f"{here}/{name}", reasons))
            elif key == "x-gie-invariants":
                if _invariant_map(old[key]) != _invariant_map(new[key]):
                    reasons.append(f"{here}: invariants changed")
                    level = max(level, Change.MAJOR)
            else:
                level = max(level, _diff(old[key], new[key], here, reasons))
        return level
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        level = Change.NONE
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            level = max(level, _diff(old_item, new_item, f"{path}[{index}]", reasons))
        return level
    reasons.append(f"{path}: {old!r} -> {new!r}")
    return Change.MAJOR


def _diff_properties(old: dict[str, Any], new: dict[str, Any], path: str, reasons: list[str]) -> Change:
    level = Change.NONE
    old_props, new_props = old["properties"], new["properties"]
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))
    for name in sorted(set(old_props) - set(new_props)):
        reasons.append(f"{path}/{name}: field removed")
        level = max(level, Change.MAJOR)
    for name in sorted(set(new_props) - set(old_props)):
        if name in new_required:
            reasons.append(f"{path}/{name}: new required field")
            level = max(level, Change.MAJOR)
        else:
            reasons.append(f"{path}/{name}: new optional field")
            level = max(level, Change.MINOR)
    for name in sorted(set(old_props) & set(new_props)):
        if (name in old_required) != (name in new_required):
            reasons.append(f"{path}/{name}: optionality changed")
            level = max(level, Change.MAJOR)
        level = max(level, _diff(old_props[name], new_props[name], f"{path}/{name}", reasons))
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
