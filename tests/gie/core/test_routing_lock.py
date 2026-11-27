"""Independent tests for ``gie.core.routing_lock`` - acceptance test S00-ACC-04 "Paper-only routing lock".

Requirements: G08-03 (live lock), G08-10 (never enable real execution during this project
phase), M-AUTH-13 (paper/replay only), M-AUTH-19 (keep real order routing disabled), OWN-14
(live execution remains disabled) and M-DIR-07 / COM-11 (the same rule under other ids).  Every
test of this module verifies that no order can leave the process and carries these markers.

The expected behaviour is taken from the module, function and class docstrings of
``gie/core/routing_lock.py``, from ``specifications/SHARED_CONTRACTS.md`` sections 9.1 / 9.3 and
from ``docs/ARCHITECTURE.md`` section 6, not from the implementation:

1. vocabulary - ``RunMode`` only contains REPLAY and PAPER, ``ExecutionVenue`` only contains
   SIMULATED; the genuine members are compared by identity, and the shared contracts cannot
   construct or decode a live order;
2. constant - ``LIVE_ROUTING_PERMITTED`` is the literal ``False`` and is not read from the
   environment, a file, a command line or any setting;
3. guard - ``RoutingGuard`` is final and immutable and only admits instances of adapter *classes*
   that were registered from an allow-listed module, declare the simulated venue, have no
   instance ``__dict__`` and still have exactly the method functions they were registered with;
4. self-check - ``assert_live_routing_locked`` detects a changed constant, alias members, members
   injected into the enum internals and replaced enums, and everything fails closed.

SYNTHETIC FIXTURE: the adapter classes built in this file are inert fakes.  They execute
nothing, connect to nothing and carry no credentials.  Every test works on a private copy of
the adapter registry, so no registration leaks into other tests.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from enum import Enum
from pathlib import Path
from types import FunctionType, ModuleType
from typing import Any

import pytest

from gie import contracts
from gie.contracts.interfaces import ExecutionAdapter
from gie.core import routing_lock
from gie.core.errors import ContractValidationError, GieError, LiveRoutingLockedError
from gie.core.routing_lock import (
    ADAPTER_METHODS,
    ALLOWED_ADAPTER_MODULE_PREFIXES,
    ExecutionVenue,
    RoutingGuard,
    RunMode,
    assert_live_routing_locked,
    is_run_mode,
    is_simulated_venue,
    parse_run_mode,
    register_simulated_adapter,
    registered_adapter_types,
)
from tests.gie import samples

pytestmark = [
    pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-19", "OWN-14", "M-DIR-07", "COM-11"),
    pytest.mark.acceptance("S00-ACC-04"),
]

_MODULE_PATH = Path(routing_lock.__file__).resolve()
_REPO_ROOT = _MODULE_PATH.parents[2]

#: The six methods of an execution adapter (``gie.contracts.interfaces.ExecutionAdapter``).
_DOCUMENTED_ADAPTER_METHODS = ("submit", "cancel", "replace", "on_tick", "poll", "open_positions")

#: Words somebody might try in order to obtain real routing.  None of them is a run mode.
_LIVE_WORDS = ["LIVE", "live", "Live", "REAL", "PROD", "PRODUCTION", "DEMO", "TRADING", "MT5", "SIMULATED"]

#: Settings somebody might try in the environment.  None of them may have any effect.
_HOSTILE_ENVIRONMENT = {
    "GIE_LIVE": "1",
    "GIE_ENABLE_LIVE": "true",
    "LIVE_ROUTING_PERMITTED": "1",
    "GIE_MODE": "LIVE",
    "GIE_RUN_MODE": "LIVE",
    "GIE_LIVE_ROUTING_PERMITTED": "True",
    "GIE_EXECUTION_VENUE": "LIVE",
    "GIE_ROUTING_LOCK": "off",
    "GIE_UNLOCK": "1",
    "RUN_MODE": "LIVE",
    "LIVE": "1",
}

_MISSING: Any = object()


class _LookalikeVenue(str, Enum):
    """SYNTHETIC FIXTURE: equal to ``ExecutionVenue.SIMULATED`` as a string, but not that member."""

    SIMULATED = "SIMULATED"


class _LookalikeMode(str, Enum):
    """SYNTHETIC FIXTURE: a perfect copy of ``RunMode`` - same names, same values, other objects."""

    REPLAY = "REPLAY"
    PAPER = "PAPER"


class _PaperString(str):
    """SYNTHETIC FIXTURE: a ``str`` subclass; its instances equal the plain string they wrap."""

    __slots__ = ()


class _EqualToEverything:
    """SYNTHETIC FIXTURE: an object whose ``==`` answers yes to everything."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


class _RunModeWithLive(str, Enum):
    """SYNTHETIC FIXTURE: a tampered run-mode vocabulary with an additional member."""

    REPLAY = "REPLAY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class _RunModeWithLiveValue(str, Enum):
    """SYNTHETIC FIXTURE: a tampered run-mode vocabulary whose PAPER member means LIVE on the wire."""

    REPLAY = "REPLAY"
    PAPER = "LIVE"


class _VenueWithLive(str, Enum):
    """SYNTHETIC FIXTURE: a tampered venue vocabulary with a real venue."""

    SIMULATED = "SIMULATED"
    LIVE = "LIVE"


class _VenueWithBroker(str, Enum):
    """SYNTHETIC FIXTURE: a tampered venue vocabulary with a broker terminal."""

    SIMULATED = "SIMULATED"
    MT5_DEMO = "MT5_DEMO"


class _VenueReplaced(str, Enum):
    """SYNTHETIC FIXTURE: a tampered venue vocabulary without the simulated venue."""

    LIVE = "LIVE"


# Built with the functional API: these vocabularies keep the genuine wire values and smuggle in
# an additional *name* as an alias of an existing member.
_RunModeWithLiveAlias = Enum(
    "_RunModeWithLiveAlias", {"REPLAY": "REPLAY", "PAPER": "PAPER", "LIVE": "PAPER"}, type=str
)
_VenueWithLiveAlias = Enum("_VenueWithLiveAlias", {"SIMULATED": "SIMULATED", "LIVE": "SIMULATED"}, type=str)
# The genuine wire values under other member names: PAPER is called LIVE, SIMULATED is called LIVE.
_RunModeWithRenamedMember = Enum("_RunModeWithRenamedMember", {"REPLAY": "REPLAY", "LIVE": "PAPER"}, type=str)
_VenueWithRenamedMember = Enum("_VenueWithRenamedMember", {"LIVE": "SIMULATED"}, type=str)


@pytest.fixture(autouse=True)
def _isolated_adapter_registry() -> Iterator[None]:
    """Run every test on a private copy of the adapter registry and prove nothing leaked."""
    original = routing_lock._SIMULATED_ADAPTER_TYPES
    original_snapshots = routing_lock._FUNCTION_SNAPSHOTS
    assert isinstance(original, dict)
    assert isinstance(original_snapshots, dict)
    snapshot = list(original.items())
    function_snapshots = dict(original_snapshots)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routing_lock, "_SIMULATED_ADAPTER_TYPES", dict(original))
        patch.setattr(routing_lock, "_FUNCTION_SNAPSHOTS", dict(original_snapshots))
        yield
    assert routing_lock._SIMULATED_ADAPTER_TYPES is original
    assert routing_lock._FUNCTION_SNAPSHOTS is original_snapshots
    assert [(id(key), value) for key, value in original.items()] == [
        (id(key), value) for key, value in snapshot
    ]
    assert original_snapshots == function_snapshots
    # Nothing a test does to the enums may survive it either.
    assert_live_routing_locked()


def _inert_method(name: str) -> FunctionType:
    """SYNTHETIC FIXTURE: a new plain function that does nothing but report how it was called."""

    def method(self: object, *arguments: object) -> tuple[str, int]:
        return (name, len(arguments))

    method.__name__ = name
    return method


def _adapter_namespace(lock: ModuleType = routing_lock, **overrides: Any) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "__doc__": "SYNTHETIC FIXTURE adapter: inert, executes nothing.",
        "__slots__": (),
        "venue": lock.ExecutionVenue.SIMULATED,
        "sends_real_orders": False,
    }
    namespace.update({name: _inert_method(name) for name in _DOCUMENTED_ADAPTER_METHODS})
    namespace.update(overrides)
    return {key: value for key, value in namespace.items() if value is not _MISSING}


def _adapter_class(
    name: str = "PaperSimulator",
    *,
    bases: tuple[type, ...] = (),
    lock: ModuleType = routing_lock,
    **overrides: Any,
) -> type:
    """SYNTHETIC FIXTURE: a new inert adapter class; by default it satisfies every registration rule.

    The class is created here, so its ``__module__`` is this test module (``tests.``...).
    """
    return type(name, bases, _adapter_namespace(lock, **overrides))


def _registered_adapter_class(name: str = "PaperSimulator", **overrides: Any) -> type:
    return register_simulated_adapter(_adapter_class(name, **overrides))


def _own_functions(adapter_type: type) -> dict[str, Any]:
    return {name: getattr(adapter_type, name) for name in _DOCUMENTED_ADAPTER_METHODS}


def _assert_admitted(guard: RoutingGuard, adapter: object) -> dict[str, FunctionType]:
    assert guard.authorize(adapter) is None  # type: ignore[func-returns-value]
    return guard.authorized_methods(adapter)


def _assert_refused(guard: RoutingGuard, adapter: object) -> LiveRoutingLockedError:
    """Both entry points refuse: ``authorize`` delegates to ``authorized_methods``."""
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(adapter)
    with pytest.raises(LiveRoutingLockedError) as caught:
        guard.authorized_methods(adapter)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    return caught.value


def _assert_not_registrable(adapter_type: Any) -> None:
    before = registered_adapter_types()
    with pytest.raises(LiveRoutingLockedError) as caught:
        register_simulated_adapter(adapter_type)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    assert registered_adapter_types() == before


def _assert_fails_closed(guard: RoutingGuard, adapter: object) -> None:
    """With a broken static layer nothing is admitted: no new guard, no adapter, no live word."""
    with pytest.raises(LiveRoutingLockedError):
        assert_live_routing_locked()
    for mode in ("PAPER", "REPLAY", "LIVE", RunMode.PAPER, RunMode.REPLAY):
        with pytest.raises(LiveRoutingLockedError):
            RoutingGuard(mode)
    _assert_refused(guard, adapter)


def _forged_member(vocabulary: Any, word: str) -> Any:
    """SYNTHETIC FIXTURE: an instance of the genuine enum *class* that the enum never defined."""
    forged = str.__new__(vocabulary, word)
    forged._name_ = word
    forged._value_ = word
    return forged


def _case_variants(word: str) -> list[str]:
    """Every upper/lower-case spelling of *word*."""
    return ["".join(letters) for letters in itertools.product(*((c.lower(), c.upper()) for c in word))]


def _assert_everything_is_locked(lock: ModuleType = routing_lock) -> None:
    """The observable behaviour of an intact lock, used as the "nothing changed" yardstick."""
    assert lock.LIVE_ROUTING_PERMITTED is False
    assert {member.name: member.value for member in lock.RunMode} == {"REPLAY": "REPLAY", "PAPER": "PAPER"}
    assert {member.name: member.value for member in lock.ExecutionVenue} == {"SIMULATED": "SIMULATED"}
    lock.assert_live_routing_locked()
    for word in _LIVE_WORDS:
        with pytest.raises(LiveRoutingLockedError):
            lock.parse_run_mode(word)
        with pytest.raises(LiveRoutingLockedError):
            lock.RoutingGuard(word)
    guard = lock.RoutingGuard("PAPER")
    assert guard.mode is lock.RunMode.PAPER
    unregistered = _adapter_class("Unregistered", lock=lock)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(unregistered())
    claims_live = _adapter_class("ClaimsLive", lock=lock, venue="LIVE", sends_real_orders=True)
    with pytest.raises(LiveRoutingLockedError):
        lock.register_simulated_adapter(claims_live)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(claims_live())
    assert claims_live not in lock.registered_adapter_types()
    # Not a blanket refusal: a simulated adapter is still admitted.
    simulator = lock.register_simulated_adapter(_adapter_class("Simulator", lock=lock))
    assert guard.authorize(simulator()) is None
    assert sorted(guard.authorized_methods(simulator())) == sorted(_DOCUMENTED_ADAPTER_METHODS)


# =========================================================================== the fixtures themselves


def test_the_helper_adapters_satisfy_the_documented_registration_rules() -> None:
    # Guards every "must be refused" test below against being vacuous: the unmodified helper class
    # is admitted, so a refusal is caused by the one thing a test changes.
    assert __name__.startswith("tests.")
    assert ALLOWED_ADAPTER_MODULE_PREFIXES == ("gie.execution.", "gie.backtesting.", "tests.")
    assert ADAPTER_METHODS == _DOCUMENTED_ADAPTER_METHODS
    adapter_type = _adapter_class()
    assert adapter_type.__module__ == __name__
    assert adapter_type.venue is ExecutionVenue.SIMULATED  # type: ignore[attr-defined]
    assert adapter_type.sends_real_orders is False  # type: ignore[attr-defined]
    assert all(type(vars(adapter_type)[name]) is FunctionType for name in ADAPTER_METHODS)
    adapter = adapter_type()
    assert not hasattr(adapter, "__dict__")
    assert register_simulated_adapter(adapter_type) is adapter_type
    assert _assert_admitted(RoutingGuard("PAPER"), adapter) == _own_functions(adapter_type)


def test_the_adapter_methods_are_the_methods_of_the_execution_adapter_interface() -> None:
    declared = {name for name, value in vars(ExecutionAdapter).items() if isinstance(value, FunctionType)}
    declared = {name for name in declared if not name.startswith("_")}
    assert declared == set(ADAPTER_METHODS)
    assert len(set(ADAPTER_METHODS)) == len(ADAPTER_METHODS) == 6


# =========================================================================== layer 1: vocabulary


def test_run_mode_has_exactly_replay_and_paper() -> None:
    assert {member.name: member.value for member in RunMode} == {"REPLAY": "REPLAY", "PAPER": "PAPER"}
    assert set(RunMode.__members__) == {"REPLAY", "PAPER"}  # __members__ would also reveal aliases
    assert len(RunMode) == 2


def test_execution_venue_has_exactly_simulated() -> None:
    assert {member.name: member.value for member in ExecutionVenue} == {"SIMULATED": "SIMULATED"}
    assert set(ExecutionVenue.__members__) == {"SIMULATED"}
    assert len(ExecutionVenue) == 1


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
@pytest.mark.parametrize("word", ["LIVE", "live", "REAL", "PROD", "DEMO", "MT5", "BROKER", "TERMINAL"])
def test_no_live_word_resolves_to_a_member(vocabulary: type[Enum], word: str) -> None:
    with pytest.raises(ValueError, match=word):
        vocabulary(word)
    assert word not in vocabulary.__members__
    assert not hasattr(vocabulary, word)


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_the_vocabularies_cannot_be_extended_by_subclassing(vocabulary: type[Enum]) -> None:
    def subclass_with_a_live_member() -> type[Enum]:
        class Extended(vocabulary):  # type: ignore[valid-type,misc]
            LIVE = "LIVE"

        return Extended

    with pytest.raises(TypeError):
        subclass_with_a_live_member()


def test_the_shared_contracts_use_the_lock_vocabulary() -> None:
    assert contracts.RunMode is RunMode
    assert contracts.ExecutionVenue is ExecutionVenue
    assert routing_lock.RunMode is RunMode
    assert routing_lock.ExecutionVenue is ExecutionVenue


def test_is_run_mode_accepts_exactly_the_two_genuine_members() -> None:
    assert is_run_mode(RunMode.REPLAY) is True
    assert is_run_mode(RunMode.PAPER) is True
    assert [member for member in RunMode if is_run_mode(member)] == [RunMode.REPLAY, RunMode.PAPER]


@pytest.mark.parametrize(
    "value",
    [
        "PAPER",  # the plain string is equal to the member, but it is not the member
        "REPLAY",
        "LIVE",
        _PaperString("PAPER"),
        _LookalikeMode.PAPER,
        _LookalikeMode.REPLAY,
        _RunModeWithLive.PAPER,
        _RunModeWithLive.LIVE,
        _EqualToEverything(),
        ExecutionVenue.SIMULATED,
        RunMode,
        None,
        True,
        1,
        ["PAPER"],
    ],
)
def test_is_run_mode_compares_by_identity_not_equality(value: object) -> None:
    assert is_run_mode(value) is False


def test_is_simulated_venue_accepts_exactly_the_genuine_member() -> None:
    assert is_simulated_venue(ExecutionVenue.SIMULATED) is True
    assert [member for member in ExecutionVenue if is_simulated_venue(member)] == [ExecutionVenue.SIMULATED]


@pytest.mark.parametrize(
    "value",
    [
        "SIMULATED",
        "LIVE",
        _PaperString("SIMULATED"),
        _LookalikeVenue.SIMULATED,
        _VenueWithLive.SIMULATED,
        _VenueWithLive.LIVE,
        _EqualToEverything(),
        RunMode.PAPER,
        ExecutionVenue,
        None,
        True,
        1,
        ["SIMULATED"],
    ],
)
def test_is_simulated_venue_compares_by_identity_not_equality(value: object) -> None:
    assert is_simulated_venue(value) is False


def test_the_lookalikes_really_are_equal_but_not_identical() -> None:
    # Guards the identity tests against being vacuous: each lookalike passes an equality check,
    # so only an identity check on the genuine member keeps it out.
    venue: Any = _LookalikeVenue.SIMULATED
    assert venue == ExecutionVenue.SIMULATED
    assert venue is not ExecutionVenue.SIMULATED
    mode: Any = _LookalikeMode.PAPER
    assert mode == RunMode.PAPER
    assert mode is not RunMode.PAPER
    assert _PaperString("PAPER") == "PAPER"
    assert type(_PaperString("PAPER")) is not str


@pytest.mark.parametrize(
    ("vocabulary", "word"),
    [(RunMode, "LIVE"), (RunMode, "PAPER"), (RunMode, "REPLAY"), (ExecutionVenue, "LIVE")],
)
def test_a_forged_instance_of_the_genuine_enum_class_is_not_a_member(vocabulary: Any, word: str) -> None:
    # An object of the right class and with the right value that the enum never defined - what a
    # member injected into the enum later would look like.
    forged = _forged_member(vocabulary, word)
    assert isinstance(forged, vocabulary)
    assert forged == word
    assert forged.value == word
    assert is_run_mode(forged) is False
    assert is_simulated_venue(forged) is False
    with pytest.raises(LiveRoutingLockedError):
        parse_run_mode(forged)
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard(forged)
    _assert_not_registrable(_adapter_class(venue=forged))


def test_a_forged_simulated_venue_is_not_the_simulated_venue() -> None:
    forged = _forged_member(ExecutionVenue, "SIMULATED")
    assert forged == ExecutionVenue.SIMULATED
    assert forged is not ExecutionVenue.SIMULATED
    assert is_simulated_venue(forged) is False
    _assert_not_registrable(_adapter_class(venue=forged))


# =========================================================================== layer 2: constant


def test_live_routing_permitted_is_exactly_false() -> None:
    assert routing_lock.LIVE_ROUTING_PERMITTED is False
    assert type(routing_lock.LIVE_ROUTING_PERMITTED) is bool


def test_live_routing_permitted_is_bound_once_to_the_literal_false_in_the_source() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))
    name = "LIVE_ROUTING_PERMITTED"
    bindings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
    ]
    assert len(bindings) == 1, "the constant must be bound exactly once"
    assignments = [
        statement
        for statement in tree.body
        if (isinstance(statement, ast.AnnAssign) and statement.target is bindings[0])
        or (isinstance(statement, ast.Assign) and statement.targets == [bindings[0]])
    ]
    assert len(assignments) == 1, "the constant must be a plain module-level assignment"
    value = assignments[0].value
    assert isinstance(value, ast.Constant), "the constant must be a literal, not a computed value"
    assert value.value is False
    # No other way of binding the name exists in the module.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            assert name not in node.names
        if isinstance(node, ast.alias):
            assert name not in (node.name, node.asname)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name != name
        if isinstance(node, ast.arg):
            assert node.arg != name


# =========================================================================== parse_run_mode


def test_parse_run_mode_accepts_exactly_replay_and_paper() -> None:
    assert parse_run_mode("REPLAY") is RunMode.REPLAY
    assert parse_run_mode("PAPER") is RunMode.PAPER
    assert parse_run_mode(RunMode.REPLAY) is RunMode.REPLAY
    assert parse_run_mode(RunMode.PAPER) is RunMode.PAPER


@pytest.mark.parametrize(
    "value",
    [
        "LIVE",
        "live",
        "REAL",
        "PROD",
        "DEMO",
        "",
        None,
        1,
        True,
        " PAPER",
        "paper",
        # further attempts
        "PAPER ",
        "PAPER\n",
        "\tREPLAY",
        "PAPER\x00",
        "﻿PAPER",
        "Paper",
        "replay",
        "RunMode.PAPER",
        "PAPER,LIVE",
        "PAPER|LIVE",
        "LIVE_PAPER",
        "SIMULATED",
        "TRADING",
        "0",
        "1",
        "true",
        0,
        False,
        1.0,
        b"PAPER",
        ["PAPER"],
        ("PAPER",),
        {"mode": "PAPER"},
        ExecutionVenue.SIMULATED,
        RunMode,
        _RunModeWithLive.LIVE,
        _EqualToEverything(),  # compares equal to "PAPER", but is not that string
        # equal to an accepted value without being the genuine member or an exact ``str``
        _PaperString("PAPER"),
        _PaperString("REPLAY"),
        _LookalikeMode.PAPER,
        _LookalikeMode.REPLAY,
        _RunModeWithLive.PAPER,
    ],
)
def test_parse_run_mode_refuses_everything_else(value: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        parse_run_mode(value)


def test_parse_run_mode_is_case_and_whitespace_exact() -> None:
    spellings = _case_variants("paper") + _case_variants("replay") + _case_variants("live")
    assert len(spellings) == 32 + 64 + 16
    accepted = []
    for spelling in spellings:
        for candidate in (spelling, f" {spelling}", f"{spelling} ", f"{spelling}\n"):
            try:
                accepted.append((candidate, parse_run_mode(candidate)))
            except LiveRoutingLockedError:
                continue
    assert accepted == [("PAPER", RunMode.PAPER), ("REPLAY", RunMode.REPLAY)]


def test_a_refusal_is_a_gie_error_with_the_stable_live_locked_code() -> None:
    with pytest.raises(GieError) as caught:
        parse_run_mode("LIVE")
    error = caught.value
    assert isinstance(error, LiveRoutingLockedError)
    assert error.code == "GIE.ROUTING.LIVE_LOCKED"
    journal_form = error.to_dict()
    assert journal_form["code"] == "GIE.ROUTING.LIVE_LOCKED"
    assert json.loads(json.dumps(journal_form, allow_nan=False)) == journal_form  # JSON scalars only


# =========================================================================== RoutingGuard: construction


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("REPLAY", RunMode.REPLAY),
        ("PAPER", RunMode.PAPER),
        (RunMode.REPLAY, RunMode.REPLAY),
        (RunMode.PAPER, RunMode.PAPER),
    ],
)
def test_a_guard_can_be_built_for_replay_and_paper_only(mode: object, expected: RunMode) -> None:
    guard = RoutingGuard(mode)
    assert guard.mode is expected
    assert type(guard) is RoutingGuard


@pytest.mark.parametrize(
    "mode",
    [
        "LIVE",
        "live",
        "REAL",
        "PROD",
        "DEMO",
        "",
        None,
        1,
        True,
        " PAPER",
        "paper",
        "SIMULATED",
        b"PAPER",
        _PaperString("PAPER"),
        _LookalikeMode.PAPER,
        _RunModeWithLive.LIVE,
        ExecutionVenue.SIMULATED,
        _EqualToEverything(),
    ],
)
def test_a_guard_cannot_be_built_with_any_other_mode(mode: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard(mode)


def test_routing_guard_cannot_be_subclassed() -> None:
    # A subclass could override ``authorize`` / ``authorized_methods``; the refusal comes at class
    # creation, before any instance can exist.
    def plain_subclass() -> type:
        class OverridingGuard(RoutingGuard):
            def authorize(self, adapter: object) -> None:
                return None

        return OverridingGuard

    def subclass_with_other_bases() -> type:
        return type("MixedGuard", (_EqualToEverything, RoutingGuard), {})

    def subclass_with_keywords() -> type:
        return type.__new__(type, "KeywordGuard", (RoutingGuard,), {"__slots__": ()}, unlocked=True)

    for attempt in (plain_subclass, subclass_with_other_bases, subclass_with_keywords):
        with pytest.raises(LiveRoutingLockedError) as caught:
            attempt()
        assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"


def test_a_subclass_whose_creation_was_refused_cannot_be_used_as_a_guard() -> None:
    # "RoutingGuard is final."  The refusal is raised while the class statement runs; if the class
    # object nevertheless came into existence, it must not be possible to build a guard from it -
    # otherwise catching the error and picking the class up again yields a guard that overrides
    # ``authorize`` and still passes ``isinstance(guard, RoutingGuard)``.
    def define_permissive_guard() -> None:
        class PermissiveGuard(RoutingGuard):
            __slots__ = ()

            def authorized_methods(self, adapter: object) -> dict[str, FunctionType]:
                return {name: _inert_method(name) for name in _DOCUMENTED_ADAPTER_METHODS}

            def authorize(self, adapter: object) -> None:
                return None

    with pytest.raises(LiveRoutingLockedError) as caught:
        define_permissive_guard()
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    survivors = [cls for cls in RoutingGuard.__subclasses__() if cls.__name__ == "PermissiveGuard"]
    for survivor in survivors:
        for mode in ("PAPER", "REPLAY", RunMode.PAPER):
            with pytest.raises(LiveRoutingLockedError):
                survivor(mode)


def test_routing_guard_is_final_by_its_metaclass() -> None:
    metaclass = type(RoutingGuard)
    assert metaclass is not type
    assert issubclass(metaclass, type)
    # The metaclass refuses before the class object exists: no subclass is left behind.
    subclasses_before = list(RoutingGuard.__subclasses__())
    with pytest.raises(LiveRoutingLockedError, match="may not be subclassed"):
        metaclass("ByMetaclass", (RoutingGuard,), {"__slots__": ()})
    assert RoutingGuard.__subclasses__() == subclasses_before
    # A class of the same metaclass that does not derive from the guard is no subclass and is allowed.
    unrelated = metaclass("Unrelated", (), {})
    assert not issubclass(unrelated, RoutingGuard)


def test_a_subclass_created_behind_the_metaclass_is_refused_by_init_subclass() -> None:
    # ``type.__new__`` called directly with the guard's metaclass skips the metaclass's own ``__new__``;
    # ``__init_subclass__`` still refuses the class.
    metaclass = type(RoutingGuard)
    with pytest.raises(LiveRoutingLockedError, match="may not be subclassed") as caught:
        type.__new__(metaclass, "BehindTheMetaclass", (RoutingGuard,), {"__slots__": ()})
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"


class _NotAGuard:
    """SYNTHETIC FIXTURE: an object that offers a slot named like the guard's and nothing else."""

    __slots__ = ("_mode",)


def test_the_guard_methods_refuse_an_object_that_is_not_exactly_a_routing_guard() -> None:
    # "an object whose exact type is not this class cannot be initialised as a guard", and the
    # unbound methods the gateway calls check the exact type too.
    registered = _registered_adapter_class()
    impostor = _NotAGuard()
    with pytest.raises(LiveRoutingLockedError, match="may not be subclassed"):
        RoutingGuard.__init__(impostor, "PAPER")  # type: ignore[arg-type]
    assert not hasattr(impostor, "_mode")
    object.__setattr__(impostor, "_mode", RunMode.PAPER)
    with pytest.raises(LiveRoutingLockedError, match="may not be subclassed"):
        RoutingGuard.authorized_methods(impostor, registered())  # type: ignore[arg-type]
    # The genuine guard admits the same adapter: the refusal is about the guard's type alone.
    _assert_admitted(RoutingGuard("PAPER"), registered())


def test_a_guard_cannot_be_given_a_mode_twice_or_carry_new_attributes_even_through_its_own_init() -> None:
    guard = RoutingGuard("PAPER")
    with pytest.raises(LiveRoutingLockedError, match="immutable"):
        RoutingGuard.__init__(guard, "REPLAY")
    assert guard.mode is RunMode.PAPER
    assert RoutingGuard.__slots__ == ("_mode",)
    with pytest.raises(LiveRoutingLockedError, match="immutable"):
        guard.__dict__ = {}  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("mode", "LIVE"),
        ("mode", RunMode.REPLAY),
        ("_mode", "LIVE"),
        ("_mode", RunMode.REPLAY),  # not even to the other genuine mode
        ("_mode", RunMode.PAPER),  # nor to the value it already has
        ("authorize", lambda adapter: None),
        ("authorized_methods", lambda adapter: {}),
        ("anything_else", 1),
    ],
)
def test_a_guard_is_immutable(name: str, value: object) -> None:
    guard = RoutingGuard("PAPER")
    with pytest.raises(LiveRoutingLockedError):
        setattr(guard, name, value)
    with pytest.raises(LiveRoutingLockedError):
        delattr(guard, name)
    assert guard.mode is RunMode.PAPER
    assert not hasattr(guard, "__dict__")  # no per-instance attributes besides the run mode
    assert not hasattr(guard, "anything_else")
    _assert_refused(guard, _adapter_class("Unregistered")())  # still the guard it was


def test_a_guard_whose_mode_was_forced_reports_it_instead_of_returning_a_non_mode() -> None:
    # ``object.__setattr__`` goes around the guard's own ``__setattr__``.  Whatever then sits in the
    # slot, ``mode`` never returns anything but a genuine run mode.
    for forced in ("LIVE", "PAPER", _LookalikeMode.PAPER, _forged_member(RunMode, "LIVE"), None):
        guard = RoutingGuard("PAPER")
        object.__setattr__(guard, "_mode", forced)
        with pytest.raises(LiveRoutingLockedError):
            _ = guard.mode


# =========================================================================== register_simulated_adapter


def test_a_simulated_adapter_class_can_be_registered_and_is_returned_unchanged() -> None:
    adapter_type = _adapter_class()
    attributes_before = dict(vars(adapter_type))
    before = registered_adapter_types()
    assert adapter_type not in before
    assert register_simulated_adapter(adapter_type) is adapter_type
    assert registered_adapter_types() == before | {adapter_type}
    assert dict(vars(adapter_type)) == attributes_before  # the class itself is not modified
    # Registering again changes nothing.
    assert register_simulated_adapter(adapter_type) is adapter_type
    assert registered_adapter_types() == before | {adapter_type}
    assert _assert_admitted(RoutingGuard("PAPER"), adapter_type()) == _own_functions(adapter_type)


def test_register_simulated_adapter_works_as_a_class_decorator() -> None:
    @register_simulated_adapter
    class DecoratedSimulator:
        """SYNTHETIC FIXTURE adapter; executes nothing."""

        __slots__ = ()
        venue = ExecutionVenue.SIMULATED
        sends_real_orders = False

        def submit(self, request: object) -> None:
            return None

        def cancel(self, request: object) -> None:
            return None

        def replace(self, request: object) -> None:
            return None

        def on_tick(self, tick: object) -> None:
            return None

        def poll(self) -> tuple[()]:
            return ()

        def open_positions(self) -> tuple[()]:
            return ()

    assert isinstance(DecoratedSimulator, type)
    assert DecoratedSimulator in registered_adapter_types()
    methods = _assert_admitted(RoutingGuard("REPLAY"), DecoratedSimulator())
    assert methods["poll"] is vars(DecoratedSimulator)["poll"]


def test_registered_adapter_types_is_an_immutable_snapshot() -> None:
    snapshot = registered_adapter_types()
    assert isinstance(snapshot, frozenset)
    adapter_type = _registered_adapter_class()
    assert adapter_type not in snapshot  # the earlier snapshot did not change
    assert adapter_type in registered_adapter_types()
    # Nothing can be smuggled into the registry through the returned value.
    unregistered = _adapter_class("Smuggled")
    assert not hasattr(snapshot, "add")
    assert unregistered not in registered_adapter_types()
    _assert_refused(RoutingGuard("PAPER"), unregistered())


@pytest.mark.parametrize(
    "not_a_class",
    [
        None,
        "PaperSimulator",
        1,
        object(),
        lambda: None,
        {"venue": ExecutionVenue.SIMULATED, "sends_real_orders": False},
        ExecutionVenue.SIMULATED,
        routing_lock,
    ],
)
def test_only_classes_can_be_registered(not_a_class: Any) -> None:
    _assert_not_registrable(not_a_class)


def test_an_instance_of_a_valid_adapter_class_cannot_be_registered() -> None:
    _assert_not_registrable(_adapter_class()())


@pytest.mark.parametrize(
    "module",
    ["gie.execution.simulator", "gie.execution.sim.fills", "gie.backtesting.engine", "tests.gie.fixtures"],
)
def test_a_class_from_an_allow_listed_package_can_be_registered(module: str) -> None:
    adapter_type = _adapter_class(__module__=module)
    assert adapter_type.__module__ == module
    assert register_simulated_adapter(adapter_type) is adapter_type
    _assert_admitted(RoutingGuard("PAPER"), adapter_type())


@pytest.mark.parametrize(
    "module",
    [
        "__main__",
        "builtins",
        "gie",
        "gie.core.routing_lock",  # not even the lock module itself
        "gie.contracts.guarded",
        "gie.strategy.signals",
        "gie.risk.gate",
        "gie.execution",  # the prefix is the package *contents*: "gie.execution."
        "gie.backtesting",
        "tests",
        "gie.executionx.simulator",
        "gie.execution_live.router",
        "gie.backtestingx.engine",
        "testsuite.adapters",
        "tests_live.adapters",
        "mytests.adapters",
        "thirdparty.gie.execution.simulator",
        "x.tests.adapters",
        "Tests.adapters",
        "TESTS.ADAPTERS",
        "GIE.EXECUTION.simulator",
        "gie.Execution.simulator",
        " tests.adapters",
        "\ttests.adapters",
        ".tests.adapters",
        "",
        None,
        42,
        b"tests.adapters",
        ("tests.",),
        ["tests.adapters"],
    ],
)
def test_a_class_from_any_other_module_cannot_be_registered(module: Any) -> None:
    adapter_type = _adapter_class("DefinedElsewhere", __module__=module)
    assert adapter_type.__module__ == module
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


# --------------------------------------------------------------------------- where a class really lives


def _functions_living_in(module: str) -> dict[str, FunctionType]:
    """SYNTHETIC FIXTURE: inert adapter methods whose globals are those of the module named *module*."""
    module_globals: dict[str, Any] = {"__name__": module}
    functions: dict[str, FunctionType] = {}
    for name in _DOCUMENTED_ADAPTER_METHODS:
        template = _inert_method(name)
        functions[name] = FunctionType(
            template.__code__, module_globals, name, template.__defaults__, template.__closure__
        )
    return functions


def _register_from(module: str, adapter_type: Any) -> Any:
    """Call ``register_simulated_adapter`` from code whose module (its globals' ``__name__``) is *module*."""

    def registrar(candidate: Any) -> Any:
        return register_simulated_adapter(candidate)

    module_globals: dict[str, Any] = {
        "__name__": module,
        "register_simulated_adapter": register_simulated_adapter,
    }
    relocated = FunctionType(registrar.__code__, module_globals, "registrar")
    return relocated(adapter_type)


_ALLOWED_PLACES = ["gie.execution.simulator", "gie.backtesting.engine", "tests.gie.fixtures"]
_FORBIDDEN_PLACES = [
    "__main__",
    "gie.strategy.signals",
    "gie.contracts.guarded",
    "gie.core.routing_lock",
    "gie.execution",
    "brokerbridge.orders",
    "testsuite.adapters",
]


@pytest.mark.parametrize("place", _ALLOWED_PLACES)
def test_a_class_registered_from_an_allowed_module_with_methods_living_there_is_admitted(place: str) -> None:
    adapter_type = _adapter_class("Relocated", __module__=place, **_functions_living_in(place))
    assert all(
        function.__globals__["__name__"] == place for function in _own_functions(adapter_type).values()
    )
    assert _register_from(place, adapter_type) is adapter_type
    assert adapter_type in registered_adapter_types()
    assert _assert_admitted(RoutingGuard("PAPER"), adapter_type()) == _own_functions(adapter_type)


@pytest.mark.parametrize("place", _FORBIDDEN_PLACES)
def test_a_spoofed_module_name_does_not_make_methods_from_another_module_registrable(place: str) -> None:
    # ``__module__`` says "tests."; the method functions really live in *place*.
    adapter_type = _adapter_class("Spoofed", __module__="tests.gie.fixtures", **_functions_living_in(place))
    assert adapter_type.__module__ == "tests.gie.fixtures"
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


@pytest.mark.parametrize("method", _DOCUMENTED_ADAPTER_METHODS)
def test_a_single_method_living_in_another_module_makes_the_class_unregistrable(method: str) -> None:
    foreign = _functions_living_in("brokerbridge.orders")[method]
    adapter_type = _adapter_class("OneForeignMethod", **{method: foreign})
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


@pytest.mark.parametrize("place", _FORBIDDEN_PLACES)
def test_a_registration_performed_from_a_module_outside_the_allow_list_is_refused(place: str) -> None:
    # The class itself is impeccable (defined here, in ``tests.``); only the registering code is not.
    adapter_type = _adapter_class("RegisteredFromElsewhere")
    before = registered_adapter_types()
    with pytest.raises(LiveRoutingLockedError, match="registered from") as caught:
        _register_from(place, adapter_type)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    assert registered_adapter_types() == before
    _assert_refused(RoutingGuard("PAPER"), adapter_type())
    # The same class registered from an allowed module is admitted: the refusal was about the caller.
    assert _register_from("tests.gie.fixtures", adapter_type) is adapter_type
    _assert_admitted(RoutingGuard("PAPER"), adapter_type())


# --------------------------------------------------------------------------- functions changed in place


def _with_keyword_default(name: str, default: Any) -> FunctionType:
    """SYNTHETIC FIXTURE: an inert method with a keyword-only default."""

    def method(self: object, *arguments: object, route: Any = default) -> tuple[str, int]:
        return (name, len(arguments))

    method.__name__ = name
    return method


def _change_code(function: FunctionType) -> None:
    replacement = _inert_method("other").__code__  # same free variables, other code object
    assert replacement is not function.__code__
    function.__code__ = replacement


def _change_defaults(function: FunctionType) -> None:
    function.__defaults__ = ("route-for-real",)


def _change_keyword_defaults(function: FunctionType) -> None:
    function.__kwdefaults__ = {"route": "LIVE"}


def _change_keyword_defaults_in_place(function: FunctionType) -> None:
    assert function.__kwdefaults__ is not None
    function.__kwdefaults__["route"] = "LIVE"


def _change_closure_cell(function: FunctionType) -> None:
    assert function.__closure__ is not None
    function.__closure__[0].cell_contents = "submit-for-real"


_IN_PLACE_CHANGES = [
    _change_code,
    _change_defaults,
    _change_keyword_defaults,
    _change_keyword_defaults_in_place,
    _change_closure_cell,
]


@pytest.mark.parametrize("change", _IN_PLACE_CHANGES, ids=lambda change: change.__name__.removeprefix("_"))
@pytest.mark.parametrize("method", ["submit", "poll"])
def test_a_registered_method_changed_in_place_after_registration_is_refused(
    change: Callable[[FunctionType], None], method: str
) -> None:
    # The class still holds the very same function object - only its code, defaults or closure changed.
    adapter_type = _registered_adapter_class(**{method: _with_keyword_default(method, "PAPER")})
    function = vars(adapter_type)[method]
    guard = RoutingGuard("PAPER")
    existing = adapter_type()
    assert _assert_admitted(guard, existing)[method] is function
    change(function)
    assert vars(adapter_type)[method] is function
    assert adapter_type in registered_adapter_types()
    error = _assert_refused(guard, existing)
    assert "modified after registration" in str(error)
    _assert_refused(RoutingGuard("REPLAY"), adapter_type())
    # Registering the changed class again does not launder the change.
    with contextlib.suppress(LiveRoutingLockedError):
        register_simulated_adapter(adapter_type)
    _assert_refused(guard, existing)


def test_restoring_the_registered_code_defaults_and_closure_restores_admission() -> None:
    adapter_type = _registered_adapter_class(submit=_with_keyword_default("submit", "PAPER"))
    function = vars(adapter_type)["submit"]
    guard = RoutingGuard("PAPER")
    code, defaults, keyword_defaults = function.__code__, function.__defaults__, function.__kwdefaults__
    assert function.__closure__ is not None
    cell_contents = function.__closure__[0].cell_contents
    for change in _IN_PLACE_CHANGES:
        change(function)
        _assert_refused(guard, adapter_type())
        function.__code__, function.__defaults__ = code, defaults
        function.__kwdefaults__ = None if keyword_defaults is None else dict(keyword_defaults)
        function.__closure__[0].cell_contents = cell_contents
        _assert_admitted(guard, adapter_type())


def test_a_keyword_default_replaced_by_an_object_that_merely_compares_equal_is_refused() -> None:
    # "code, defaults and closures of the registered methods are remembered and compared": a
    # default replaced by another object is a modification, whatever that object's ``==`` says
    # (defaults and closure cells are compared by identity; the keyword defaults must be too).
    adapter_type = _registered_adapter_class(submit=_with_keyword_default("submit", "PAPER"))
    function = vars(adapter_type)["submit"]
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, adapter_type())
    impostor = _EqualToEverything()
    function.__kwdefaults__ = {"route": impostor}
    assert function.__kwdefaults__["route"] is impostor
    _assert_refused(guard, adapter_type())


@pytest.mark.parametrize(
    "declarations",
    [
        {"venue": _MISSING, "sends_real_orders": _MISSING},
        {"venue": _MISSING},
        {"sends_real_orders": _MISSING},
    ],
    ids=["no-declarations", "no-venue", "no-sends_real_orders"],
)
def test_a_class_without_the_declarations_cannot_be_registered(declarations: dict[str, Any]) -> None:
    adapter_type = _adapter_class(**declarations)
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


@pytest.mark.parametrize(
    "sends_real_orders", [True, None, 0, 1, 0.0, "", "False", "false", [], (), _EqualToEverything()]
)
def test_a_class_must_declare_sends_real_orders_as_the_literal_false(sends_real_orders: Any) -> None:
    adapter_type = _adapter_class(sends_real_orders=sends_real_orders)
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


@pytest.mark.parametrize(
    "venue",
    [
        "LIVE",
        "SIMULATED",  # the plain string is not the vocabulary member
        "simulated",
        "DEMO",
        "",
        None,
        1,
        True,
        ["SIMULATED"],
        _LookalikeVenue.SIMULATED,  # equal as a string, but another vocabulary
        _VenueWithLive.SIMULATED,
        _VenueWithLive.LIVE,
        ExecutionVenue,  # the vocabulary itself instead of its member
        RunMode.PAPER,
        _EqualToEverything(),
        _PaperString("SIMULATED"),
    ],
)
def test_a_class_must_declare_the_simulated_venue_member_itself(venue: Any) -> None:
    adapter_type = _adapter_class(venue=venue)
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


def test_a_property_is_not_a_declaration() -> None:
    for declarations in (
        {"sends_real_orders": property(lambda self: False)},
        {"venue": property(lambda self: ExecutionVenue.SIMULATED)},
    ):
        adapter_type = _adapter_class(**declarations)
        _assert_not_registrable(adapter_type)
        _assert_refused(RoutingGuard("PAPER"), adapter_type())


def test_a_class_without_slots_cannot_be_registered() -> None:
    adapter_type = _adapter_class("WithInstanceDict", __slots__=_MISSING)
    assert hasattr(adapter_type(), "__dict__")
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


def test_slots_must_be_defined_in_the_whole_hierarchy() -> None:
    # One class without ``__slots__`` anywhere in the hierarchy gives every instance a ``__dict__``.
    unslotted_base = type("UnslottedBase", (), {})
    derived_from_unslotted = _adapter_class("DerivedFromUnslotted", bases=(unslotted_base,))
    unslotted_leaf = type("UnslottedLeaf", (_adapter_class("SlottedBase"),), {})
    middle = type("UnslottedMiddle", (_adapter_class("SlottedRoot"),), {})
    slotted_below_unslotted = type("SlottedLeaf", (middle,), {"__slots__": ()})
    for adapter_type in (derived_from_unslotted, unslotted_leaf, slotted_below_unslotted):
        assert hasattr(adapter_type(), "__dict__")
        _assert_not_registrable(adapter_type)
        _assert_refused(RoutingGuard("PAPER"), adapter_type())


def test_a_dict_slot_is_an_instance_dict() -> None:
    adapter_type = _adapter_class("DictSlot", __slots__=("__dict__",))
    adapter = adapter_type()
    adapter.submit = lambda *arguments: "replaced on the instance"  # type: ignore[attr-defined]
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter)


def test_a_fully_slotted_hierarchy_can_be_registered_and_may_inherit_its_declarations() -> None:
    base = _adapter_class("BaseSimulator")
    derived = register_simulated_adapter(type("DerivedSimulator", (base,), {"__slots__": ()}))
    guard = RoutingGuard("PAPER")
    # The registered functions are the inherited ones.
    assert _assert_admitted(guard, derived()) == _own_functions(base)
    # The base was not registered: admission is per class.
    _assert_refused(guard, base())


def test_nothing_can_be_set_on_an_instance_of_an_admitted_class() -> None:
    # The point of the ``__slots__`` rule: no method and no declaration can be replaced per instance.
    adapter_type = _registered_adapter_class()
    adapter = adapter_type()
    for name in ("venue", "sends_real_orders", *ADAPTER_METHODS, "anything_else"):
        with pytest.raises(AttributeError):
            setattr(adapter, name, "replaced on the instance")
    assert adapter.venue is ExecutionVenue.SIMULATED
    _assert_admitted(RoutingGuard("PAPER"), adapter)


@pytest.mark.parametrize("slot", ["venue", "sends_real_orders", *_DOCUMENTED_ADAPTER_METHODS])
def test_a_slot_cannot_stand_in_for_a_declaration_or_a_method(slot: str) -> None:
    # A slot of that name in a subclass hides the class-level declaration or function and would
    # make it settable per instance.
    base = _adapter_class("BaseSimulator")
    adapter_type = type("SlotOverride", (base,), {"__slots__": (slot,)})
    adapter = adapter_type()
    setattr(adapter, slot, False if slot == "sends_real_orders" else ExecutionVenue.SIMULATED)
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter)


@pytest.mark.parametrize("missing", _DOCUMENTED_ADAPTER_METHODS)
def test_a_class_must_define_all_six_adapter_methods(missing: str) -> None:
    adapter_type = _adapter_class("Incomplete", **{missing: _MISSING})
    assert not hasattr(adapter_type, missing)
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


class _CallableObject:
    """SYNTHETIC FIXTURE: callable, but not a plain function."""

    def __call__(self, *arguments: object) -> None:
        return None


@pytest.mark.parametrize(
    "not_a_plain_function",
    [
        None,
        "submit",
        1,
        classmethod(_inert_method("submit")),
        property(_inert_method("submit")),
        functools.partial(_inert_method("submit"), None),
        _CallableObject(),
        len,
        print,
        _CallableObject,
        RoutingGuard("PAPER").authorize,  # a bound method
    ],
    ids=[
        "None",
        "str",
        "int",
        "classmethod",
        "property",
        "partial",
        "callable-object",
        "builtin-len",
        "builtin-print",
        "class",
        "bound-method",
    ],
)
@pytest.mark.parametrize("method", ["submit", "poll"])
def test_adapter_methods_must_be_plain_functions(method: str, not_a_plain_function: Any) -> None:
    adapter_type = _adapter_class("NotPlain", **{method: not_a_plain_function})
    _assert_not_registrable(adapter_type)
    _assert_refused(RoutingGuard("PAPER"), adapter_type())


def test_a_refused_registration_does_not_disturb_registered_classes() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    for refused in (
        _adapter_class("NoSlots", __slots__=_MISSING),
        _adapter_class("Live", venue="LIVE"),
        _adapter_class("Elsewhere", __module__="__main__"),
        _adapter_class("Incomplete", submit=_MISSING),
        registered(),
    ):
        _assert_not_registrable(refused)
    assert _assert_admitted(guard, registered()) == _own_functions(registered)


# =========================================================================== layer 3: the guard admits


@pytest.mark.parametrize("mode", ["REPLAY", "PAPER"])
def test_authorize_admits_an_instance_of_a_registered_simulated_adapter(mode: str) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard(mode)
    adapter = adapter_type()
    for _ in range(3):
        assert guard.authorize(adapter) is None  # type: ignore[func-returns-value]
    assert guard.authorize(adapter_type()) is None  # type: ignore[func-returns-value]


def test_authorized_methods_returns_the_registered_functions() -> None:
    adapter_type = _registered_adapter_class()
    registered_functions = _own_functions(adapter_type)
    guard = RoutingGuard("PAPER")
    adapter = adapter_type()
    methods = guard.authorized_methods(adapter)
    assert type(methods) is dict
    assert set(methods) == set(_DOCUMENTED_ADAPTER_METHODS)
    for name in _DOCUMENTED_ADAPTER_METHODS:
        assert type(methods[name]) is FunctionType
        assert methods[name] is registered_functions[name]
        assert methods[name] is vars(adapter_type)[name]
    # They are what the gateway calls the adapter with: plain functions taking the adapter.
    assert methods["submit"](adapter, "request") == ("submit", 1)
    assert methods["poll"](adapter) == ("poll", 0)
    # The same functions for every instance and every guard.
    assert RoutingGuard("REPLAY").authorized_methods(adapter_type()) == methods


def test_the_result_of_authorized_methods_cannot_be_used_to_change_the_registry() -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    adapter = adapter_type()
    first = guard.authorized_methods(adapter)
    genuine = dict(first)
    first["submit"] = _inert_method("submit")  # a caller tampers with what it was handed
    del first["cancel"]
    first["route_live"] = _inert_method("route_live")
    second = guard.authorized_methods(adapter)
    assert second is not first
    assert second == genuine
    assert second["submit"] is vars(adapter_type)["submit"]


# =========================================================================== layer 3: the guard refuses


@pytest.mark.parametrize("mode", ["REPLAY", "PAPER"])
def test_authorize_rejects_an_unregistered_adapter_type(mode: str) -> None:
    _registered_adapter_class("RegisteredSimulator")  # some other type being registered does not help
    unregistered = _adapter_class("UnregisteredSimulator")  # satisfies every registration rule
    _assert_refused(RoutingGuard(mode), unregistered())


def test_authorize_rejects_an_instance_of_a_subclass_of_a_registered_type() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    plain_subclass = type("PlainSubclass", (registered,), {"__slots__": ()})  # inherits everything
    live_subclass = type(
        "LiveSubclass",
        (registered,),
        {"__slots__": (), "venue": "LIVE", "sends_real_orders": True, "submit": _inert_method("submit")},
    )
    _assert_admitted(guard, registered())  # the registered type itself is admitted
    for subclass in (plain_subclass, live_subclass):
        assert issubclass(subclass, registered)
        assert isinstance(subclass(), registered)  # an isinstance check would be fooled
        _assert_refused(guard, subclass())
        assert subclass not in registered_adapter_types()


def test_authorize_goes_by_type_identity_not_by_name() -> None:
    registered = _registered_adapter_class("PaperSimulator")
    twin = _adapter_class("PaperSimulator")
    assert (twin.__name__, twin.__qualname__, twin.__module__) == (
        registered.__name__,
        registered.__qualname__,
        registered.__module__,
    )
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, registered())
    _assert_refused(guard, twin())


def _impersonator_of(registered: type) -> type:
    """SYNTHETIC FIXTURE: a valid adapter class that hashes and compares like *registered*."""

    class Impersonating(type):
        def __hash__(cls) -> int:
            return hash(registered)

        def __eq__(cls, other: object) -> bool:
            return True

    impersonator: Any = Impersonating("NeverRegistered", (), _adapter_namespace())
    assert impersonator == registered  # an == / dict / set lookup would be fooled
    assert hash(impersonator) == hash(registered)
    assert impersonator in {registered: None}
    assert impersonator is not registered
    result: type = impersonator
    return result


def test_authorize_is_not_fooled_by_a_class_that_compares_equal_to_a_registered_class() -> None:
    registered = _registered_adapter_class()
    impersonator = _impersonator_of(registered)
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, registered())
    _assert_refused(guard, impersonator())


def test_registering_a_class_that_compares_equal_to_a_registered_class_does_not_disturb_that_class() -> None:
    # "Membership is decided by object identity, never by == / hash."  The impersonator is a valid
    # simulated adapter class of its own; registering it must add *it* and leave the entry of the
    # class it compares equal to alone.
    registered = _registered_adapter_class()
    genuine_functions = _own_functions(registered)
    impersonator = _impersonator_of(registered)
    guard = RoutingGuard("PAPER")
    assert register_simulated_adapter(impersonator) is impersonator
    # The genuine class is still admitted, with its own functions ...
    assert _assert_admitted(guard, registered()) == genuine_functions
    # ... and the newly registered class is admitted with its own, not with the other's.
    assert _assert_admitted(guard, impersonator()) == _own_functions(impersonator)


def test_authorize_is_not_fooled_by_an_object_that_claims_to_be_of_a_registered_class() -> None:
    registered = _registered_adapter_class()
    impostor_type = _adapter_class("Impostor", __class__=registered)
    impostor = impostor_type()
    assert isinstance(impostor, registered)  # an isinstance check would be fooled
    assert impostor.__class__ is registered
    assert type(impostor) is impostor_type
    _assert_refused(RoutingGuard("PAPER"), impostor)


@pytest.mark.parametrize(
    "not_an_adapter",
    [None, "PaperSimulator", 0, 1, True, object(), ExecutionVenue.SIMULATED, RunMode.PAPER, [], {}],
)
def test_authorize_rejects_objects_that_are_not_adapters(not_an_adapter: Any) -> None:
    _registered_adapter_class()
    _assert_refused(RoutingGuard("PAPER"), not_an_adapter)


def test_authorize_rejects_the_registered_class_itself_and_other_guards() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    _assert_refused(guard, registered)  # the class, not an instance of it
    _assert_refused(guard, guard)
    _assert_refused(guard, registered_adapter_types())
    _assert_refused(guard, guard.authorized_methods(registered()))


@pytest.mark.parametrize("method", _DOCUMENTED_ADAPTER_METHODS)
def test_a_method_replaced_on_the_class_after_registration_is_detected(method: str) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    existing = registered()
    original = vars(registered)[method]
    assert _assert_admitted(guard, existing)[method] is original
    setattr(registered, method, _inert_method(method))  # e.g. a function that routes for real
    assert registered in registered_adapter_types()  # still registered - the guard must re-check
    _assert_refused(guard, existing)
    _assert_refused(guard, registered())
    _assert_refused(RoutingGuard("REPLAY"), existing)  # also by a guard that is built afterwards
    setattr(registered, method, original)  # exactly the registered function again
    assert _assert_admitted(guard, existing)[method] is original


def test_a_method_replaced_by_an_identical_copy_is_still_a_replacement() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    original = vars(registered)["submit"]
    copy = FunctionType(
        original.__code__,
        original.__globals__,
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    assert copy is not original
    assert copy.__code__ is original.__code__
    registered.submit = copy  # type: ignore[attr-defined]
    _assert_refused(guard, registered())


@pytest.mark.parametrize(
    "replacement",
    [None, "submit", classmethod(_inert_method("submit")), staticmethod(_inert_method("submit")), len],
    ids=["None", "str", "classmethod", "staticmethod", "builtin"],
)
def test_a_method_replaced_by_something_that_is_no_function_is_detected(replacement: Any) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, registered())
    registered.submit = replacement  # type: ignore[attr-defined]
    _assert_refused(guard, registered())


@pytest.mark.parametrize("method", ["submit", "open_positions"])
def test_a_method_deleted_after_registration_is_detected(method: str) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    existing = registered()
    _assert_admitted(guard, existing)
    delattr(registered, method)
    _assert_refused(guard, existing)


def test_a_method_changed_through_a_base_class_after_registration_is_detected() -> None:
    base = _adapter_class("BaseSimulator")
    derived = register_simulated_adapter(type("DerivedSimulator", (base,), {"__slots__": ()}))
    guard = RoutingGuard("PAPER")
    existing = derived()
    original = vars(base)["cancel"]
    _assert_admitted(guard, existing)
    base.cancel = _inert_method("cancel")  # type: ignore[attr-defined]
    _assert_refused(guard, existing)
    base.cancel = original  # type: ignore[attr-defined]
    _assert_admitted(guard, existing)
    # Overriding the inherited function on the registered class itself is a modification too.
    derived.cancel = _inert_method("cancel")  # type: ignore[attr-defined]
    _assert_refused(guard, existing)


def test_registering_a_modified_class_again_does_not_launder_the_modification() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    original = vars(registered)["submit"]
    registered.submit = _inert_method("submit")  # type: ignore[attr-defined]
    with contextlib.suppress(LiveRoutingLockedError):  # refusing the second registration is just as good
        register_simulated_adapter(registered)
    _assert_refused(guard, registered())
    registered.submit = original  # type: ignore[attr-defined]
    assert _assert_admitted(guard, registered())["submit"] is original


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("sends_real_orders", True),
        ("sends_real_orders", None),
        ("sends_real_orders", 0),
        ("sends_real_orders", "False"),
        ("venue", "LIVE"),
        ("venue", "SIMULATED"),
        ("venue", None),
        ("venue", _LookalikeVenue.SIMULATED),
        ("venue", _VenueWithLive.LIVE),
    ],
)
def test_a_declaration_changed_after_registration_is_detected(attribute: str, value: Any) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    existing = registered()
    _assert_admitted(guard, existing)
    original = getattr(registered, attribute)
    setattr(registered, attribute, value)
    assert registered in registered_adapter_types()  # still registered - the guard must re-check
    _assert_refused(guard, existing)
    _assert_refused(guard, registered())
    setattr(registered, attribute, original)
    _assert_admitted(guard, existing)


@pytest.mark.parametrize("attribute", ["venue", "sends_real_orders"])
def test_a_declaration_deleted_after_registration_is_detected(attribute: str) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    existing = registered()
    _assert_admitted(guard, existing)
    delattr(registered, attribute)
    _assert_refused(guard, existing)
    _assert_refused(guard, registered())


def test_a_declaration_that_differs_between_class_and_instance_is_never_admitted() -> None:
    class TwoFaced:
        """Descriptor that answers ``False`` on one kind of access and ``True`` on the other."""

        def __init__(self, on_class: bool, on_instance: bool) -> None:
            self.on_class = on_class
            self.on_instance = on_instance

        def __get__(self, instance: object, owner: type) -> bool:
            return self.on_class if instance is None else self.on_instance

    guard = RoutingGuard("PAPER")
    honest_to_the_registry = _adapter_class("HonestToRegistry", sends_real_orders=TwoFaced(False, True))
    assert honest_to_the_registry.sends_real_orders is False  # type: ignore[attr-defined]
    assert honest_to_the_registry().sends_real_orders is True
    with contextlib.suppress(LiveRoutingLockedError):  # refusing the registration is just as good
        register_simulated_adapter(honest_to_the_registry)
    _assert_refused(guard, honest_to_the_registry())

    honest_to_the_guard = _adapter_class("HonestToGuard", sends_real_orders=TwoFaced(True, False))
    _assert_not_registrable(honest_to_the_guard)
    _assert_refused(guard, honest_to_the_guard())


def _instance_with_dict_through_a_base() -> tuple[type, Any]:
    """An adapter class whose every class defines ``__slots__`` but whose base still gives a ``__dict__``."""
    adapter_type = _adapter_class("ThreadLocalSimulator", bases=(threading.local,))
    with contextlib.suppress(LiveRoutingLockedError):  # refusing the registration is just as good
        register_simulated_adapter(adapter_type)
    return adapter_type, adapter_type()


def _instance_with_dict_through_the_registry() -> tuple[type, Any]:
    """An unslotted class written straight into the registry, as if the registration check had a hole."""
    adapter_type = _adapter_class("ForcedIntoRegistry", __slots__=_MISSING)
    routing_lock._SIMULATED_ADAPTER_TYPES[adapter_type] = _own_functions(adapter_type)
    assert adapter_type in registered_adapter_types()
    return adapter_type, adapter_type()


@pytest.mark.parametrize(
    "make", [_instance_with_dict_through_a_base, _instance_with_dict_through_the_registry]
)
@pytest.mark.parametrize("attribute", ["submit", "venue", "sends_real_orders", "unrelated_note"])
def test_an_instance_that_somehow_carries_attributes_is_refused(
    make: Callable[[], tuple[type, Any]], attribute: str
) -> None:
    # The guard does not rely on the registration check alone.
    adapter_type, adapter = make()
    assert hasattr(adapter, "__dict__")
    values = {
        "submit": _inert_method("submit"),  # a method replaced on the instance
        "venue": ExecutionVenue.SIMULATED,  # even a "harmless" value is an instance attribute
        "sends_real_orders": False,
        "unrelated_note": "anything",
    }
    setattr(adapter, attribute, values[attribute])
    assert vars(adapter) == {attribute: values[attribute]}
    _assert_refused(RoutingGuard("PAPER"), adapter)
    assert adapter_type.venue is ExecutionVenue.SIMULATED  # the class itself declares everything correctly


def test_a_guard_refusal_is_a_gie_error_with_the_stable_live_locked_code() -> None:
    with pytest.raises(GieError) as caught:
        RoutingGuard("PAPER").authorize(_adapter_class("UnregisteredSimulator")())
    assert isinstance(caught.value, LiveRoutingLockedError)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    journal_form = caught.value.to_dict()
    assert json.loads(json.dumps(journal_form, allow_nan=False)) == journal_form


def test_a_type_that_is_no_longer_registered_is_no_longer_admitted() -> None:
    # The guard consults the registry on every call; an earlier admission is not remembered.
    # (The nested registry copy is the same mechanism the autouse fixture uses for isolation.)
    guard = RoutingGuard("PAPER")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routing_lock, "_SIMULATED_ADAPTER_TYPES", dict(routing_lock._SIMULATED_ADAPTER_TYPES))
        short_lived = _registered_adapter_class("ShortLived")
        adapter = short_lived()
        _assert_admitted(guard, adapter)
    assert short_lived not in registered_adapter_types()
    _assert_refused(guard, adapter)
    _assert_refused(RoutingGuard("PAPER"), short_lived())


# =========================================================================== self-check


def test_the_self_check_passes_on_the_intact_module() -> None:
    for _ in range(3):
        assert assert_live_routing_locked() is None  # type: ignore[func-returns-value]
    _assert_everything_is_locked()


@pytest.mark.parametrize("tampered", [True, 1, 0, None, "False", "", "True", [], 0.0])
def test_the_self_check_raises_when_the_constant_is_not_the_literal_false(
    monkeypatch: pytest.MonkeyPatch, tampered: Any
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, adapter_type())
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, "LIVE_ROUTING_PERMITTED", tampered)
        # A tampered constant unlocks nothing: everything fails closed, simulated adapters included.
        _assert_fails_closed(guard, adapter_type())
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode("LIVE")
    # Restored afterwards.
    assert routing_lock.LIVE_ROUTING_PERMITTED is False
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize(
    ("attribute", "tampered"),
    [
        ("RunMode", _RunModeWithLive),
        ("RunMode", _RunModeWithLiveValue),
        ("RunMode", _LookalikeMode),  # a perfect copy is still not the genuine vocabulary
        ("ExecutionVenue", _VenueWithLive),
        ("ExecutionVenue", _VenueWithBroker),
        ("ExecutionVenue", _VenueReplaced),
        ("ExecutionVenue", _LookalikeVenue),
        ("RunMode", ExecutionVenue),
        ("ExecutionVenue", RunMode),
    ],
)
def test_the_self_check_raises_when_a_vocabulary_is_replaced(
    monkeypatch: pytest.MonkeyPatch, attribute: str, tampered: type[Enum]
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    _assert_admitted(guard, adapter_type())
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, attribute, tampered)
        # Fail closed: no guard can be built - in particular not for the smuggled-in mode - and an
        # existing guard admits nothing any more.
        _assert_fails_closed(guard, adapter_type())
    # Restored afterwards.
    assert routing_lock.RunMode is RunMode
    assert routing_lock.ExecutionVenue is ExecutionVenue
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize(
    ("attribute", "tampered"),
    [
        ("RunMode", _RunModeWithLiveAlias),
        ("ExecutionVenue", _VenueWithLiveAlias),
        ("RunMode", _RunModeWithRenamedMember),
        ("ExecutionVenue", _VenueWithRenamedMember),
    ],
    ids=[
        "RunMode.LIVE-alias-of-PAPER",
        "ExecutionVenue.LIVE-alias-of-SIMULATED",
        "RunMode.PAPER-renamed-to-LIVE",
        "ExecutionVenue.SIMULATED-renamed-to-LIVE",
    ],
)
def test_the_self_check_raises_when_a_vocabulary_gains_a_live_name_as_an_alias(
    monkeypatch: pytest.MonkeyPatch, attribute: str, tampered: Any
) -> None:
    # "RunMode only contains REPLAY and PAPER and ExecutionVenue only contains SIMULATED."  A
    # vocabulary in which the *name* LIVE appears contains something else, even if that name is an
    # alias or a renamed member and the set of wire values is unchanged: ``RunMode.LIVE`` resolves.
    assert "LIVE" in tampered.__members__
    assert {member.value for member in tampered} <= {"REPLAY", "PAPER", "SIMULATED"}
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, attribute, tampered)
        _assert_fails_closed(guard, adapter_type())
    assert_live_routing_locked()


@pytest.mark.parametrize(
    ("vocabulary", "genuine"), [(RunMode, "PAPER"), (RunMode, "REPLAY"), (ExecutionVenue, "SIMULATED")]
)
def test_the_self_check_detects_an_alias_injected_through_the_member_map(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any, genuine: str
) -> None:
    # The genuine enum object stays in place; only its internal name table gains the alias LIVE.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setitem(vocabulary._member_map_, "LIVE", vocabulary[genuine])
        assert vocabulary["LIVE"] is vocabulary[genuine]  # the alias really resolves now
        assert "LIVE" in vocabulary.__members__
        _assert_fails_closed(guard, adapter_type())
    assert "LIVE" not in vocabulary.__members__
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize(
    ("vocabulary", "genuine"), [(RunMode, "PAPER"), (RunMode, "REPLAY"), (ExecutionVenue, "SIMULATED")]
)
def test_the_self_check_detects_a_value_injected_through_the_value_map(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any, genuine: str
) -> None:
    # After this manipulation the wire value "LIVE" would decode to a genuine member.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setitem(vocabulary._value2member_map_, "LIVE", vocabulary[genuine])
        assert vocabulary("LIVE") is vocabulary[genuine]  # the lookup by value really is fooled now
        _assert_fails_closed(guard, adapter_type())
    with pytest.raises(ValueError, match="LIVE"):
        vocabulary("LIVE")
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
@pytest.mark.parametrize(
    "tables", [("_member_map_",), ("_value2member_map_",), ("_member_map_", "_value2member_map_")]
)
def test_the_self_check_detects_a_new_member_injected_into_the_enum_internals(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any, tables: tuple[str, ...]
) -> None:
    forged = _forged_member(vocabulary, "LIVE")
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        for table in tables:
            patch.setitem(getattr(vocabulary, table), "LIVE", forged)
        _assert_fails_closed(guard, adapter_type())
        # The injected object is not accepted anywhere, lock intact or not.
        assert is_run_mode(forged) is False
        assert is_simulated_venue(forged) is False
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode(forged)
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_the_self_check_detects_a_duplicated_name_in_the_member_list(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    genuine_names = list(vocabulary._member_names_)
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, "_member_names_", [*genuine_names, genuine_names[0]])
        assert len(list(vocabulary)) == len(genuine_names) + 1  # iteration really is changed
        _assert_fails_closed(guard, adapter_type())
    assert vocabulary._member_names_ == genuine_names
    assert_live_routing_locked()


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_the_self_check_reports_an_unknown_name_in_the_member_list_as_a_broken_lock(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any
) -> None:
    # The self-check "raises unless every static layer of the lock is intact"; like every refusal
    # of the lock that is a LiveRoutingLockedError (all errors derive from GieError), not an
    # internal KeyError of the enum machinery.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    genuine_names = list(vocabulary._member_names_)
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, "_member_names_", [*genuine_names, "LIVE"])
        _assert_fails_closed(guard, adapter_type())
    assert vocabulary._member_names_ == genuine_names
    assert_live_routing_locked()


@pytest.mark.parametrize(
    ("member", "attribute", "tampered"),
    [
        (RunMode.PAPER, "_value_", "LIVE"),
        (RunMode.REPLAY, "_value_", "LIVE"),
        (RunMode.PAPER, "_name_", "LIVE"),
        (RunMode.PAPER, "_value_", "REPLAY"),
        (ExecutionVenue.SIMULATED, "_value_", "LIVE"),
        (ExecutionVenue.SIMULATED, "_name_", "LIVE"),
        (ExecutionVenue.SIMULATED, "_value_", "MT5"),
    ],
)
def test_the_self_check_detects_a_genuine_member_whose_name_or_wire_value_was_changed(
    monkeypatch: pytest.MonkeyPatch, member: Any, attribute: str, tampered: str
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(member, attribute, tampered)
        assert tampered in (member.value, member.name)
        _assert_fails_closed(guard, adapter_type())
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize(
    ("attribute", "tampered"),
    [
        ("_RUN_MODES", (RunMode.REPLAY, RunMode.PAPER, _RunModeWithLive.LIVE)),
        ("_RUN_MODES", (RunMode.REPLAY, RunMode.PAPER, _forged_member(RunMode, "LIVE"))),
        ("_RUN_MODES", (_LookalikeMode.REPLAY, _LookalikeMode.PAPER)),
        ("_VENUES", (ExecutionVenue.SIMULATED, _VenueWithLive.LIVE)),
        ("_VENUES", (ExecutionVenue.SIMULATED, _forged_member(ExecutionVenue, "LIVE"))),
        ("_VENUES", (_LookalikeVenue.SIMULATED,)),
    ],
)
def test_the_self_check_detects_a_change_of_the_captured_genuine_members(
    monkeypatch: pytest.MonkeyPatch, attribute: str, tampered: tuple[Any, ...]
) -> None:
    # The identity checks compare against the members captured at import; widening that capture
    # would make a foreign object "genuine".
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, attribute, tampered)
        _assert_fails_closed(guard, adapter_type())
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize(
    ("vocabulary", "alias", "target"),
    [
        (RunMode, "LIVE", "PAPER"),
        (RunMode, "REAL", "REPLAY"),
        (ExecutionVenue, "LIVE", "SIMULATED"),
        (ExecutionVenue, "MT5_DEMO", "SIMULATED"),
    ],
)
def test_the_self_check_detects_an_alias_attribute_added_to_the_enum_class(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any, alias: str, target: str
) -> None:
    # ``RunMode.LIVE = RunMode.PAPER`` is a plain class attribute: the enum tables (``__members__``,
    # ``_member_map_``, iteration) do not show it, yet ``RunMode.LIVE`` resolves to a genuine member.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, alias, vocabulary[target], raising=False)
        assert getattr(vocabulary, alias) is vocabulary[target]  # the alias really resolves
        assert alias not in vocabulary.__members__  # ... and the enum tables do not show it
        assert len(list(vocabulary)) == len(vocabulary.__members__)
        _assert_fails_closed(guard, adapter_type())
    assert not hasattr(vocabulary, alias)
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_the_self_check_detects_a_forged_member_added_as_a_class_attribute(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, "LIVE", _forged_member(vocabulary, "LIVE"), raising=False)
        _assert_fails_closed(guard, adapter_type())
    assert not hasattr(vocabulary, "LIVE")
    assert_live_routing_locked()


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_a_plain_class_attribute_that_is_no_member_is_not_mistaken_for_an_alias(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any
) -> None:
    # Guards the alias test against being a blanket refusal of every class attribute.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, "synthetic_fixture_note", "not a member", raising=False)
        assert_live_routing_locked()
        _assert_admitted(guard, adapter_type())


class _ContainsNothing:
    """SYNTHETIC FIXTURE: answers ``in`` with no, but is no mapping - the enum machinery chokes on it."""

    def __contains__(self, item: object) -> bool:
        return False


class _NotIterable:
    """SYNTHETIC FIXTURE: something that cannot be iterated."""

    __iter__ = None


@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
@pytest.mark.parametrize(
    ("table", "broken"),
    [("_member_map_", _ContainsNothing()), ("_member_names_", _NotIterable()), ("_value2member_map_", 7)],
    ids=["member-map-not-a-mapping", "member-names-not-iterable", "value-map-not-a-mapping"],
)
def test_an_error_of_the_enum_machinery_is_reported_as_a_broken_lock(
    monkeypatch: pytest.MonkeyPatch, vocabulary: Any, table: str, broken: Any
) -> None:
    # A tampered enum can make the enum machinery itself raise; the self-check reports that as
    # LiveRoutingLockedError (fail closed, a GieError) and never lets the internal error escape.
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    adapter = adapter_type()
    with monkeypatch.context() as patch:
        patch.setattr(vocabulary, table, broken)
        with pytest.raises(LiveRoutingLockedError, match="vocabulary has been changed") as caught:
            assert_live_routing_locked()
        assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
        _assert_fails_closed(guard, adapter)
    assert_live_routing_locked()
    _assert_admitted(guard, adapter_type())


# =========================================================================== no configuration unlocks


def test_environment_variables_do_not_change_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_everything_is_locked()
    adapter_type = _registered_adapter_class()
    for name, value in _HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    assert os.environ["GIE_LIVE"] == "1"  # the settings really are in the environment
    assert os.environ["GIE_MODE"] == "LIVE"

    _assert_everything_is_locked()
    # Simulated routing keeps working exactly as before; nothing else became possible.
    for mode in ("REPLAY", "PAPER"):
        _assert_admitted(RoutingGuard(mode), adapter_type())
    assert parse_run_mode("PAPER") is RunMode.PAPER
    for name in _HOSTILE_ENVIRONMENT:
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode(os.environ[name])
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode(name)


def test_a_module_loaded_under_a_hostile_environment_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    # Execute the source again under a private name: whatever the module might read while it is
    # being imported, it reads now.  The real module (and sys.modules) stay untouched.
    spec = importlib.util.spec_from_file_location("_routing_lock_copy_for_environment_test", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh is not routing_lock
    assert fresh.RunMode is not RunMode  # really a second, independent execution of the source
    assert "_routing_lock_copy_for_environment_test" not in sys.modules
    _assert_everything_is_locked(fresh)


def _probe_lock_state() -> dict[str, Any]:
    """Observable state of the lock as JSON values; also executed in a fresh interpreter (see below)."""
    lock = routing_lock
    words = ["LIVE", "live", "REAL", "PROD", "DEMO"]

    def refused(action: Callable[..., Any], *arguments: object) -> bool:
        try:
            action(*arguments)
        except LiveRoutingLockedError:
            return True
        return False

    simulator = lock.register_simulated_adapter(_adapter_class("ProbeSimulator"))
    unregistered = _adapter_class("Unregistered")
    claims_live = _adapter_class("ClaimsLive", venue="LIVE", sends_real_orders=True)
    defined_elsewhere = _adapter_class("DefinedElsewhere", __module__="__main__")
    lock.assert_live_routing_locked()
    guard = lock.RoutingGuard("PAPER")
    guard.authorize(simulator())
    return {
        "permitted": lock.LIVE_ROUTING_PERMITTED,
        "run_modes": sorted(lock.RunMode.__members__),
        "venues": sorted(lock.ExecutionVenue.__members__),
        "guard_mode": guard.mode.value,
        "parse_refused": {word: refused(lock.parse_run_mode, word) for word in words},
        "guard_refused": {word: refused(lock.RoutingGuard, word) for word in words},
        "unregistered_refused": refused(guard.authorize, unregistered()),
        "live_registration_refused": refused(lock.register_simulated_adapter, claims_live),
        "live_adapter_refused": refused(guard.authorize, claims_live()),
        "foreign_module_registration_refused": refused(lock.register_simulated_adapter, defined_elsewhere),
        "authorized_methods": sorted(guard.authorized_methods(simulator())),
        "registered": sorted(cls.__name__ for cls in lock.registered_adapter_types()),
    }


#: Runs in a new interpreter.  This test module is imported there under its package name, so the
#: SYNTHETIC FIXTURE adapters are defined in a ``tests.`` module exactly as in the test run.
_SUBPROCESS_PROBE = """
import json
import sys

from tests.gie.core import test_routing_lock as probe

json.dump(probe._probe_lock_state(), sys.stdout, sort_keys=True)
"""


def _probe_in_a_new_interpreter(extra_environment: dict[str, str]) -> dict[str, Any]:
    environment = {name: value for name, value in os.environ.items() if name not in _HOSTILE_ENVIRONMENT}
    environment.update(extra_environment)
    python_path = [str(_REPO_ROOT), *filter(None, [environment.get("PYTHONPATH", "")])]
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_PROBE],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


def test_a_new_process_started_with_a_hostile_environment_is_locked() -> None:
    all_refused = dict.fromkeys(["LIVE", "live", "REAL", "PROD", "DEMO"], True)
    expected = {
        "permitted": False,
        "run_modes": ["PAPER", "REPLAY"],
        "venues": ["SIMULATED"],
        "guard_mode": "PAPER",
        "parse_refused": all_refused,
        "guard_refused": all_refused,
        "unregistered_refused": True,
        "live_registration_refused": True,
        "live_adapter_refused": True,
        "foreign_module_registration_refused": True,
        "authorized_methods": sorted(_DOCUMENTED_ADAPTER_METHODS),
        "registered": ["ProbeSimulator"],
    }
    clean = _probe_in_a_new_interpreter({})
    hostile = _probe_in_a_new_interpreter(_HOSTILE_ENVIRONMENT)
    assert clean == expected
    assert hostile == expected  # byte for byte the same behaviour


def _routing_lock_ast() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_the_module_imports_nothing_that_reads_environment_files_or_command_line() -> None:
    imported = _imported_module_names(_routing_lock_ast())
    assert imported, "no import found - the source was not parsed as expected"
    assert "gie.core.errors" in imported
    forbidden = {
        # named in the acceptance criteria
        "os",
        "sys",
        "pathlib",
        "json",
        "tomllib",
        "configparser",
        "argparse",
        "dotenv",
        # further ways to reach settings, files, other processes or dynamically loaded code
        "io",
        "importlib",
        "subprocess",
        "socket",
        "builtins",
        "runpy",
        "ctypes",
        "pickle",
        "yaml",
        "toml",
        "tomli",
        "getpass",
        "shutil",
        "tempfile",
        "glob",
        "inspect",
    }
    offending = sorted(name for name in imported if name.lstrip(".").split(".")[0] in forbidden)
    assert offending == []


def test_the_module_only_depends_on_the_standard_library_and_gie_core() -> None:
    for name in _imported_module_names(_routing_lock_ast()):
        assert not name.startswith("."), f"relative import {name!r}"
        top_level = name.split(".")[0]
        assert top_level in sys.stdlib_module_names or name.startswith("gie.core."), name


def test_the_module_never_opens_a_file_reads_the_environment_or_runs_dynamic_code() -> None:
    forbidden_calls = {
        "open",
        "read_text",
        "read_bytes",
        "eval",
        "exec",
        "compile",
        "__import__",
        "import_module",
        "globals",
        "input",
    }
    forbidden_names = {"environ", "environb", "getenv", "argv"}
    offending: list[tuple[str, int]] = []
    calls = 0
    for node in ast.walk(_routing_lock_ast()):
        if isinstance(node, ast.Call):
            calls += 1
            function = node.func
            called = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            if called in forbidden_calls:
                offending.append((f"{called}()", node.lineno))
        if isinstance(node, ast.Name) and node.id in forbidden_names | {"open"}:
            offending.append((node.id, node.lineno))  # not even a reference to ``open``
        if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            offending.append((node.attr, node.lineno))
    assert calls > 0, "no call found - the source was not parsed as expected"
    assert offending == []


# =========================================================================== contracts: no live order


def _order_event_wire() -> dict[str, Any]:
    """Wire form of the SYNTHETIC FIXTURE order event of ``tests/gie/samples.py``."""
    return contracts.to_wire(samples.order_event())


def _from_wire(wire: Any) -> Any:
    """``contracts.from_wire`` without pinning a contract type (the envelope decides)."""
    return contracts.from_wire(wire)


def _from_json(text: str | bytes) -> Any:
    return contracts.from_json(text)


def test_the_sample_order_event_is_simulated_and_decodes() -> None:
    # Guards the refusal tests below against being vacuous: without the manipulation the very
    # same wire dict is valid.
    wire = _order_event_wire()
    assert wire["contract"] == "OrderEvent"
    assert wire["venue"] == "SIMULATED"
    assert wire["mode"] == "REPLAY"
    decoded = _from_wire(wire)
    assert decoded == samples.order_event()
    assert isinstance(decoded, contracts.OrderEvent)
    assert decoded.venue is ExecutionVenue.SIMULATED
    assert decoded.mode is RunMode.REPLAY
    assert _from_json(contracts.to_json(samples.order_event())) == decoded


@pytest.mark.parametrize(
    "venue",
    [
        "LIVE",
        "live",
        "REAL",
        "DEMO",
        "PROD",
        "MT5",
        "BROKER",
        "",
        "simulated",
        " SIMULATED",
        None,
        1,
        True,
        ["SIMULATED"],
    ],
)
def test_an_order_event_with_any_other_venue_cannot_be_decoded(venue: Any) -> None:
    wire = _order_event_wire()
    wire["venue"] = venue
    with pytest.raises(ContractValidationError) as caught:
        _from_wire(wire)
    assert caught.value.details.get("path") == "venue"  # refused because of the venue, nothing else
    with pytest.raises(ContractValidationError):
        contracts.from_wire(wire, contracts.OrderEvent)


@pytest.mark.parametrize(
    "mode",
    ["LIVE", "live", "REAL", "DEMO", "PROD", "", "paper", " PAPER", "SIMULATED", None, 1, True, ["PAPER"]],
)
def test_an_order_event_with_any_other_run_mode_cannot_be_decoded(mode: Any) -> None:
    wire = _order_event_wire()
    wire["mode"] = mode
    with pytest.raises(ContractValidationError) as caught:
        _from_wire(wire)
    assert caught.value.details.get("path") == "mode"  # refused because of the mode, nothing else


@pytest.mark.parametrize("field", ["venue", "mode"])
def test_an_order_event_without_venue_or_run_mode_cannot_be_decoded(field: str) -> None:
    wire = _order_event_wire()
    del wire[field]
    with pytest.raises(ContractValidationError):
        _from_wire(wire)  # a missing value is never defaulted


@pytest.mark.parametrize(("field", "genuine"), [("venue", "SIMULATED"), ("mode", "REPLAY")])
def test_a_live_order_event_cannot_be_decoded_from_json_text(field: str, genuine: str) -> None:
    text = contracts.to_json(samples.order_event())
    needle = f'"{field}":"{genuine}"'
    assert text.count(needle) == 1
    tampered = text.replace(needle, f'"{field}":"LIVE"')
    assert _from_json(text) == samples.order_event()
    with pytest.raises(ContractValidationError) as caught:
        _from_json(tampered)
    assert caught.value.details.get("path") == field
    with pytest.raises(ContractValidationError):
        _from_json(tampered.encode("utf-8"))


@pytest.mark.parametrize("field", ["venue", "mode"])
@pytest.mark.parametrize("value", ["LIVE", "REAL", "DEMO", None, _VenueWithLive.LIVE, _RunModeWithLive.LIVE])
def test_a_live_order_event_cannot_be_constructed(field: str, value: Any) -> None:
    # A record validates on construction, so a live order does not even exist in memory.
    assert samples.order_event().venue is ExecutionVenue.SIMULATED  # the unmodified sample is fine
    with pytest.raises(ContractValidationError) as caught:
        samples.order_event(**{field: value})
    assert caught.value.details.get("path") == field


@pytest.mark.parametrize(
    ("field", "vocabulary", "word"),
    [
        ("mode", RunMode, "LIVE"),
        ("mode", RunMode, "PAPER"),
        ("venue", ExecutionVenue, "LIVE"),
        ("venue", ExecutionVenue, "SIMULATED"),
    ],
)
def test_an_order_event_cannot_be_constructed_with_a_forged_member(
    field: str, vocabulary: Any, word: str
) -> None:
    # "No live mode or real venue exists in the vocabulary ... checked by identity": an object of
    # the genuine enum class that is not one of its members is not accepted, whatever it equals.
    forged = _forged_member(vocabulary, word)
    with pytest.raises(ContractValidationError) as caught:
        samples.order_event(**{field: forged})
    assert caught.value.details.get("path") == field


def test_no_event_of_an_order_lifecycle_can_be_decoded_with_a_live_venue_or_mode() -> None:
    events = [*samples.order_lifecycle(), *samples.protective_stop_lifecycle()]
    assert len(events) >= 4
    for event in events:
        wire = contracts.to_wire(event)
        assert _from_wire(wire) == event  # valid as it is
        for field in ("venue", "mode"):
            tampered = dict(wire)
            tampered[field] = "LIVE"
            with pytest.raises(ContractValidationError) as caught:
                _from_wire(tampered)
            assert caught.value.details.get("path") == field


def test_no_contract_record_can_be_decoded_with_a_live_run_mode() -> None:
    checked: set[str] = set()
    records = {**{f"sample/{name}": record for name, record in samples.all_samples().items()}}
    for contract_name, variants in samples.variants().items():
        records.update({f"{contract_name}/{name}": record for name, record in variants.items()})
    for name, record in records.items():
        wire = contracts.to_wire(record)
        if "mode" not in wire:
            continue
        assert _from_wire(wire) == record
        for word in ("LIVE", "live", "REAL", "DEMO"):
            tampered = dict(wire)
            tampered["mode"] = word
            with pytest.raises(ContractValidationError) as caught:
                _from_wire(tampered)
            assert caught.value.details.get("path") == "mode", name
        checked.add(wire["contract"])
    # Every contract that carries a run mode was exercised (Tick and ModelVersion carry none).
    assert "OrderEvent" in checked
    assert len(checked) >= 6


def test_the_published_order_event_schemas_only_know_the_simulated_venue_and_the_two_run_modes() -> None:
    schema_directory = Path(contracts.__file__).resolve().parent / "schemas"
    schema_files = sorted(schema_directory.glob("OrderEvent-*.schema.json"))
    assert schema_files, "no published OrderEvent schema found"
    for schema_file in schema_files:
        properties = json.loads(schema_file.read_text(encoding="utf-8"))["properties"]
        assert properties["venue"]["enum"] == ["SIMULATED"], schema_file.name
        assert sorted(properties["mode"]["enum"]) == ["PAPER", "REPLAY"], schema_file.name
