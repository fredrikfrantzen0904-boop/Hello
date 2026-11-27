"""Independent tests for ``gie.core.routing_lock`` - acceptance test S00-ACC-04 "Paper-only routing lock".

Requirements: G08-03 (live lock), G08-10 (never enable real execution during this project
phase), M-AUTH-13 (paper/replay only), M-AUTH-14 (execution interfaces exist, real order
routing stays disabled) and OWN-14 (live execution remains disabled).

The expected behaviour is taken from the module docstring of ``gie/core/routing_lock.py``
and from ``specifications/SHARED_CONTRACTS.md`` section 9, not from the implementation:

1. vocabulary - ``RunMode`` only contains REPLAY and PAPER, ``ExecutionVenue`` only contains
   SIMULATED, and the shared contracts cannot construct or decode a live order;
2. constant - ``LIVE_ROUTING_PERMITTED`` is the literal ``False`` and is not read from the
   environment, a file, a command line or any setting;
3. guard - ``RoutingGuard.authorize`` only admits adapter *types* that were registered with
   ``register_simulated_adapter`` and that declare the simulated venue.

SYNTHETIC FIXTURE: the adapter classes built in this file are inert fakes.  They execute
nothing, connect to nothing and carry no credentials.  Every test works on a private copy of
the adapter registry, so no registration leaks into other tests.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gie import contracts
from gie.core import routing_lock
from gie.core.errors import ContractValidationError, GieError, LiveRoutingLockedError
from gie.core.routing_lock import (
    ExecutionVenue,
    RoutingGuard,
    RunMode,
    assert_live_routing_locked,
    parse_run_mode,
    register_simulated_adapter,
    registered_adapter_types,
)
from tests.gie import samples

_MODULE_PATH = Path(routing_lock.__file__).resolve()
_REPO_ROOT = _MODULE_PATH.parents[2]

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


@pytest.fixture(autouse=True)
def _isolated_adapter_registry() -> Iterator[None]:
    """Run every test on a private copy of the adapter registry and prove nothing leaked."""
    before = registered_adapter_types()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routing_lock, "_SIMULATED_ADAPTER_TYPES", set(before))
        yield
    assert registered_adapter_types() == before


def _adapter_class(name: str = "PaperSimulator", **declarations: Any) -> type:
    """SYNTHETIC FIXTURE: a new inert adapter class; by default it declares a valid simulator."""
    namespace: dict[str, Any] = {"venue": ExecutionVenue.SIMULATED, "sends_real_orders": False}
    namespace.update(declarations)
    return type(name, (), {key: value for key, value in namespace.items() if value is not _MISSING})


def _registered_adapter_class(name: str = "PaperSimulator") -> type:
    return register_simulated_adapter(_adapter_class(name))


def _case_variants(word: str) -> list[str]:
    """Every upper/lower-case spelling of *word*."""
    return ["".join(letters) for letters in itertools.product(*((c.lower(), c.upper()) for c in word))]


def _assert_everything_is_locked(module: ModuleType = routing_lock) -> None:
    """The observable behaviour of an intact lock, used as the "nothing changed" yardstick."""
    assert module.LIVE_ROUTING_PERMITTED is False
    assert {member.name: member.value for member in module.RunMode} == {"REPLAY": "REPLAY", "PAPER": "PAPER"}
    assert {member.name: member.value for member in module.ExecutionVenue} == {"SIMULATED": "SIMULATED"}
    module.assert_live_routing_locked()
    for word in _LIVE_WORDS:
        with pytest.raises(LiveRoutingLockedError):
            module.parse_run_mode(word)
        with pytest.raises(LiveRoutingLockedError):
            module.RoutingGuard(word)
    guard = module.RoutingGuard("PAPER")
    assert guard.mode is module.RunMode.PAPER
    unregistered = type(
        "Unregistered", (), {"venue": module.ExecutionVenue.SIMULATED, "sends_real_orders": False}
    )
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(unregistered())
    claims_live = type("ClaimsLive", (), {"venue": "LIVE", "sends_real_orders": True})
    with pytest.raises(LiveRoutingLockedError):
        module.register_simulated_adapter(claims_live)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(claims_live())
    assert claims_live not in module.registered_adapter_types()


# =========================================================================== layer 1: vocabulary


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_run_mode_has_exactly_replay_and_paper() -> None:
    assert {member.name: member.value for member in RunMode} == {"REPLAY": "REPLAY", "PAPER": "PAPER"}
    assert set(RunMode.__members__) == {"REPLAY", "PAPER"}  # __members__ would also reveal aliases
    assert len(RunMode) == 2


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_execution_venue_has_exactly_simulated() -> None:
    assert {member.name: member.value for member in ExecutionVenue} == {"SIMULATED": "SIMULATED"}
    assert set(ExecutionVenue.__members__) == {"SIMULATED"}
    assert len(ExecutionVenue) == 1


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
@pytest.mark.parametrize("word", ["LIVE", "live", "REAL", "PROD", "DEMO", "MT5", "BROKER", "TERMINAL"])
def test_no_live_word_resolves_to_a_member(vocabulary: type[Enum], word: str) -> None:
    with pytest.raises(ValueError, match=word):
        vocabulary(word)
    assert word not in vocabulary.__members__
    assert not hasattr(vocabulary, word)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("vocabulary", [RunMode, ExecutionVenue])
def test_the_vocabularies_cannot_be_extended_by_subclassing(vocabulary: type[Enum]) -> None:
    def subclass_with_a_live_member() -> type[Enum]:
        class Extended(vocabulary):  # type: ignore[valid-type,misc]
            LIVE = "LIVE"

        return Extended

    with pytest.raises(TypeError):
        subclass_with_a_live_member()


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_shared_contracts_use_the_lock_vocabulary() -> None:
    assert contracts.RunMode is RunMode
    assert contracts.ExecutionVenue is ExecutionVenue
    assert routing_lock.RunMode is RunMode
    assert routing_lock.ExecutionVenue is ExecutionVenue


# =========================================================================== layer 2: constant


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_live_routing_permitted_is_exactly_false() -> None:
    assert routing_lock.LIVE_ROUTING_PERMITTED is False
    assert type(routing_lock.LIVE_ROUTING_PERMITTED) is bool


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_parse_run_mode_accepts_exactly_replay_and_paper() -> None:
    assert parse_run_mode("REPLAY") is RunMode.REPLAY
    assert parse_run_mode("PAPER") is RunMode.PAPER
    assert parse_run_mode(RunMode.REPLAY) is RunMode.REPLAY
    assert parse_run_mode(RunMode.PAPER) is RunMode.PAPER


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
        "\ufeffPAPER",
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
    ],
)
def test_parse_run_mode_refuses_everything_else(value: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        parse_run_mode(value)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_a_refusal_is_a_gie_error_with_the_stable_live_locked_code() -> None:
    with pytest.raises(GieError) as caught:
        parse_run_mode("LIVE")
    error = caught.value
    assert isinstance(error, LiveRoutingLockedError)
    assert error.code == "GIE.ROUTING.LIVE_LOCKED"
    journal_form = error.to_dict()
    assert journal_form["code"] == "GIE.ROUTING.LIVE_LOCKED"
    assert json.loads(json.dumps(journal_form, allow_nan=False)) == journal_form  # JSON scalars only


# =========================================================================== RoutingGuard construction


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    "mode",
    ["LIVE", "live", "REAL", "PROD", "DEMO", "", None, 1, True, " PAPER", "paper", "SIMULATED", b"PAPER"],
)
def test_a_guard_cannot_be_built_with_any_other_mode(mode: object) -> None:
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard(mode)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_mode_of_a_guard_cannot_be_reassigned() -> None:
    guard = RoutingGuard("PAPER")
    with pytest.raises(AttributeError):
        guard.mode = "LIVE"  # type: ignore[misc,assignment]
    assert guard.mode is RunMode.PAPER


# =========================================================================== register_simulated_adapter


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_a_simulated_adapter_class_can_be_registered_and_is_returned_unchanged() -> None:
    adapter_type = _adapter_class()
    before = registered_adapter_types()
    assert adapter_type not in before
    assert register_simulated_adapter(adapter_type) is adapter_type
    assert registered_adapter_types() == before | {adapter_type}
    # Registering again changes nothing.
    assert register_simulated_adapter(adapter_type) is adapter_type
    assert registered_adapter_types() == before | {adapter_type}


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_register_simulated_adapter_works_as_a_class_decorator() -> None:
    @register_simulated_adapter
    class DecoratedSimulator:
        """SYNTHETIC FIXTURE adapter; executes nothing."""

        venue = ExecutionVenue.SIMULATED
        sends_real_orders = False

    assert isinstance(DecoratedSimulator, type)
    assert DecoratedSimulator in registered_adapter_types()
    RoutingGuard("REPLAY").authorize(DecoratedSimulator())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(unregistered())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    before = registered_adapter_types()
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(not_a_class)
    assert registered_adapter_types() == before


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_an_instance_of_a_valid_adapter_class_cannot_be_registered() -> None:
    before = registered_adapter_types()
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(_adapter_class()())
    assert registered_adapter_types() == before


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    before = registered_adapter_types()
    adapter_type = _adapter_class(**declarations)
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(adapter_type)
    assert registered_adapter_types() == before
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    "sends_real_orders", [True, None, 0, 1, 0.0, "", "False", "false", [], (), _EqualToEverything()]
)
def test_a_class_must_declare_sends_real_orders_as_the_literal_false(sends_real_orders: Any) -> None:
    before = registered_adapter_types()
    adapter_type = _adapter_class(sends_real_orders=sends_real_orders)
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(adapter_type)
    assert registered_adapter_types() == before
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    ],
)
def test_a_class_must_declare_the_simulated_venue_member_itself(venue: Any) -> None:
    before = registered_adapter_types()
    adapter_type = _adapter_class(venue=venue)
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(adapter_type)
    assert registered_adapter_types() == before
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_lookalike_venue_really_is_equal_but_not_identical() -> None:
    # Guards the parametrised test above against being vacuous: the lookalike passes an equality
    # check, so only an identity check on the vocabulary member keeps it out.
    lookalike: Any = _LookalikeVenue.SIMULATED
    assert lookalike == ExecutionVenue.SIMULATED
    assert lookalike is not ExecutionVenue.SIMULATED


# =========================================================================== layer 3: authorize


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("mode", ["REPLAY", "PAPER"])
def test_authorize_admits_an_instance_of_a_registered_simulated_adapter(mode: str) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard(mode)
    adapter = adapter_type()
    for _ in range(3):
        guard.authorize(adapter)  # returns normally; a refusal would raise
    guard.authorize(adapter_type())  # any instance of the registered type
    assert vars(adapter) == {}  # the adapter itself is not modified


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("mode", ["REPLAY", "PAPER"])
def test_authorize_rejects_an_unregistered_adapter_type(mode: str) -> None:
    _registered_adapter_class("RegisteredSimulator")  # some other type being registered does not help
    unregistered = _adapter_class("UnregisteredSimulator")  # declares everything correctly
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard(mode).authorize(unregistered())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_authorize_rejects_an_instance_of_a_subclass_of_a_registered_type() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")

    class PlainSubclass(registered):  # type: ignore[valid-type,misc]
        """Inherits every declaration unchanged - still a different type."""

    class LiveSubclass(registered):  # type: ignore[valid-type,misc]
        """What the rule exists for: a subclass may override behaviour and declarations."""

        venue = "LIVE"
        sends_real_orders = True

    guard.authorize(registered())  # the registered type itself is admitted
    for subclass in (PlainSubclass, LiveSubclass):
        assert issubclass(subclass, registered)
        with pytest.raises(LiveRoutingLockedError):
            guard.authorize(subclass())
    assert PlainSubclass not in registered_adapter_types()
    assert LiveSubclass not in registered_adapter_types()


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_authorize_rejects_an_instance_of_the_unregistered_base_of_a_registered_type() -> None:
    base = _adapter_class("BaseSimulator")
    derived = register_simulated_adapter(type("DerivedSimulator", (base,), {}))
    guard = RoutingGuard("PAPER")
    guard.authorize(derived())  # declarations may be inherited by the registered class
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(base())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_authorize_goes_by_type_identity_not_by_name() -> None:
    registered = _registered_adapter_class("PaperSimulator")
    twin = _adapter_class("PaperSimulator")
    assert (twin.__name__, twin.__qualname__, twin.__module__) == (
        registered.__name__,
        registered.__qualname__,
        registered.__module__,
    )
    guard = RoutingGuard("PAPER")
    guard.authorize(registered())
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(twin())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_authorize_is_not_fooled_by_an_object_that_claims_to_be_of_a_registered_class() -> None:
    registered = _registered_adapter_class()

    class Impostor:
        """SYNTHETIC FIXTURE: reports the registered class as its ``__class__`` without being one."""

        venue = ExecutionVenue.SIMULATED
        sends_real_orders = False
        __class__ = registered

    impostor = Impostor()
    assert isinstance(impostor, registered)  # an isinstance check would be fooled
    assert type(impostor) is Impostor
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(impostor)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    "not_an_adapter",
    [None, "PaperSimulator", 0, 1, True, object(), ExecutionVenue.SIMULATED, RunMode.PAPER, [], {}],
)
def test_authorize_rejects_objects_that_are_not_adapters(not_an_adapter: Any) -> None:
    _registered_adapter_class()
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(not_an_adapter)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_authorize_rejects_the_registered_class_itself_and_other_guards() -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(registered)  # the class, not an instance of it
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(guard)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(registered_adapter_types())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    "venue",
    [
        "LIVE",
        "SIMULATED",
        None,
        "",
        _LookalikeVenue.SIMULATED,
        _VenueWithLive.LIVE,
        ExecutionVenue,
        _EqualToEverything(),
    ],
)
def test_an_instance_attribute_cannot_override_the_declared_venue(venue: Any) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    adapter, untouched = registered(), registered()
    guard.authorize(adapter)
    adapter.venue = venue
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(adapter)
    guard.authorize(untouched)  # other instances of the type are not affected
    del adapter.venue  # the class declaration is visible again
    guard.authorize(adapter)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("sends_real_orders", [True, None, 0, 1, "", "False", [], _EqualToEverything()])
def test_an_instance_attribute_cannot_override_sends_real_orders(sends_real_orders: Any) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    adapter, untouched = registered(), registered()
    guard.authorize(adapter)
    adapter.sends_real_orders = sends_real_orders
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(adapter)
    guard.authorize(untouched)
    del adapter.sends_real_orders
    guard.authorize(adapter)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("sends_real_orders", True),
        ("sends_real_orders", None),
        ("sends_real_orders", 0),
        ("venue", "LIVE"),
        ("venue", "SIMULATED"),
        ("venue", None),
        ("venue", _LookalikeVenue.SIMULATED),
    ],
)
def test_a_registered_class_mutated_at_class_level_is_no_longer_admitted(attribute: str, value: Any) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    existing = registered()
    guard.authorize(existing)
    original = getattr(registered, attribute)
    setattr(registered, attribute, value)
    assert registered in registered_adapter_types()  # still registered - the guard must re-check
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(existing)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(registered())
    setattr(registered, attribute, original)
    guard.authorize(existing)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("attribute", ["venue", "sends_real_orders"])
def test_a_registered_class_whose_declaration_was_deleted_is_no_longer_admitted(attribute: str) -> None:
    registered = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    guard.authorize(registered())
    delattr(registered, attribute)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(registered())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(honest_to_the_registry())

    honest_to_the_guard = _adapter_class("HonestToGuard", sends_real_orders=TwoFaced(True, False))
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(honest_to_the_guard)
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(honest_to_the_guard())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_a_property_is_not_a_declaration() -> None:
    adapter_type = _adapter_class(sends_real_orders=property(lambda self: False))
    with pytest.raises(LiveRoutingLockedError):
        register_simulated_adapter(adapter_type)
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_a_guard_refusal_is_a_gie_error_with_the_stable_live_locked_code() -> None:
    with pytest.raises(GieError) as caught:
        RoutingGuard("PAPER").authorize(_adapter_class("UnregisteredSimulator")())
    assert isinstance(caught.value, LiveRoutingLockedError)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    journal_form = caught.value.to_dict()
    assert json.loads(json.dumps(journal_form, allow_nan=False)) == journal_form


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_a_type_that_is_no_longer_registered_is_no_longer_admitted() -> None:
    # The guard consults the registry on every call; an earlier admission is not remembered.
    # (The nested registry copy is the same mechanism the autouse fixture uses for isolation.)
    baseline = registered_adapter_types()
    guard = RoutingGuard("PAPER")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(routing_lock, "_SIMULATED_ADAPTER_TYPES", set(baseline))
        short_lived = _registered_adapter_class("ShortLived")
        adapter = short_lived()
        guard.authorize(adapter)
    assert short_lived not in registered_adapter_types()
    with pytest.raises(LiveRoutingLockedError):
        guard.authorize(adapter)
    with pytest.raises(LiveRoutingLockedError):
        RoutingGuard("PAPER").authorize(short_lived())


# =========================================================================== self-check


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_self_check_passes_on_the_intact_module() -> None:
    for _ in range(3):
        assert_live_routing_locked()  # returns normally; a broken lock would raise
    _assert_everything_is_locked()


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("tampered", [True, 1, 0, None, "False", "", "True", [], 0.0])
def test_the_self_check_raises_when_the_constant_is_not_the_literal_false(
    monkeypatch: pytest.MonkeyPatch, tampered: Any
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    guard.authorize(adapter_type())
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, "LIVE_ROUTING_PERMITTED", tampered)
        with pytest.raises(LiveRoutingLockedError):
            assert_live_routing_locked()
        # A tampered constant unlocks nothing: everything fails closed, simulated adapters included.
        for mode in ("PAPER", "REPLAY", "LIVE", RunMode.PAPER):
            with pytest.raises(LiveRoutingLockedError):
                RoutingGuard(mode)
        with pytest.raises(LiveRoutingLockedError):
            guard.authorize(adapter_type())
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode("LIVE")
    # Restored afterwards.
    assert routing_lock.LIVE_ROUTING_PERMITTED is False
    assert_live_routing_locked()
    guard.authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    ("attribute", "tampered"),
    [
        ("RunMode", _RunModeWithLive),
        ("RunMode", _RunModeWithLiveValue),
        ("ExecutionVenue", _VenueWithLive),
        ("ExecutionVenue", _VenueWithBroker),
        ("ExecutionVenue", _VenueReplaced),
    ],
)
def test_the_self_check_raises_when_a_vocabulary_is_replaced_by_an_extended_one(
    monkeypatch: pytest.MonkeyPatch, attribute: str, tampered: type[Enum]
) -> None:
    adapter_type = _registered_adapter_class()
    guard = RoutingGuard("PAPER")
    guard.authorize(adapter_type())
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, attribute, tampered)
        with pytest.raises(LiveRoutingLockedError):
            assert_live_routing_locked()
        # Fail closed: no guard can be built - in particular not for the smuggled-in mode - and an
        # existing guard admits nothing any more.
        for mode in ("PAPER", "REPLAY", "LIVE"):
            with pytest.raises(LiveRoutingLockedError):
                RoutingGuard(mode)
        with pytest.raises(LiveRoutingLockedError):
            guard.authorize(adapter_type())
    # Restored afterwards.
    assert routing_lock.RunMode is RunMode
    assert routing_lock.ExecutionVenue is ExecutionVenue
    assert_live_routing_locked()
    guard.authorize(adapter_type())


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize(
    ("attribute", "tampered"),
    [("RunMode", _RunModeWithLiveAlias), ("ExecutionVenue", _VenueWithLiveAlias)],
    ids=["RunMode.LIVE-alias-of-PAPER", "ExecutionVenue.LIVE-alias-of-SIMULATED"],
)
def test_the_self_check_raises_when_a_vocabulary_gains_a_live_name_as_an_alias(
    monkeypatch: pytest.MonkeyPatch, attribute: str, tampered: Any
) -> None:
    # "RunMode only contains REPLAY and PAPER and ExecutionVenue only contains SIMULATED."  A
    # vocabulary to which the *name* LIVE was added contains more than that, even if the new name
    # is an alias and the set of wire values is unchanged: ``RunMode.LIVE`` then resolves.
    assert "LIVE" in tampered.__members__
    assert {member.value for member in tampered} <= {"REPLAY", "PAPER", "SIMULATED"}
    with monkeypatch.context() as patch:
        patch.setattr(routing_lock, attribute, tampered)
        with pytest.raises(LiveRoutingLockedError):
            assert_live_routing_locked()
    assert_live_routing_locked()


# =========================================================================== no configuration unlocks


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
        RoutingGuard(mode).authorize(adapter_type())
    assert parse_run_mode("PAPER") is RunMode.PAPER
    for name in _HOSTILE_ENVIRONMENT:
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode(os.environ[name])
        with pytest.raises(LiveRoutingLockedError):
            parse_run_mode(name)


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


_SUBPROCESS_PROBE = """
import json
import sys

from gie.core import routing_lock as lock
from gie.core.errors import LiveRoutingLockedError

WORDS = ["LIVE", "live", "REAL", "PROD", "DEMO"]


def refused(action, *arguments):
    try:
        action(*arguments)
    except LiveRoutingLockedError:
        return True
    return False


class Unregistered:
    venue = lock.ExecutionVenue.SIMULATED
    sends_real_orders = False


class ClaimsLive:
    venue = "LIVE"
    sends_real_orders = True


@lock.register_simulated_adapter
class Simulator:
    venue = lock.ExecutionVenue.SIMULATED
    sends_real_orders = False


lock.assert_live_routing_locked()
guard = lock.RoutingGuard("PAPER")
guard.authorize(Simulator())
json.dump(
    {
        "permitted": lock.LIVE_ROUTING_PERMITTED,
        "run_modes": sorted(lock.RunMode.__members__),
        "venues": sorted(lock.ExecutionVenue.__members__),
        "guard_mode": guard.mode.value,
        "parse_refused": {word: refused(lock.parse_run_mode, word) for word in WORDS},
        "guard_refused": {word: refused(lock.RoutingGuard, word) for word in WORDS},
        "unregistered_refused": refused(guard.authorize, Unregistered()),
        "live_registration_refused": refused(lock.register_simulated_adapter, ClaimsLive),
        "live_adapter_refused": refused(guard.authorize, ClaimsLive()),
        "registered": sorted(cls.__name__ for cls in lock.registered_adapter_types()),
    },
    sys.stdout,
    sort_keys=True,
)
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
        "registered": ["Simulator"],
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
    }
    offending = sorted(name for name in imported if name.lstrip(".").split(".")[0] in forbidden)
    assert offending == []


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_module_only_depends_on_the_standard_library_and_gie_core() -> None:
    for name in _imported_module_names(_routing_lock_ast()):
        assert not name.startswith("."), f"relative import {name!r}"
        top_level = name.split(".")[0]
        assert top_level in sys.stdlib_module_names or name.startswith("gie.core."), name


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            offending.append((node.id, node.lineno))
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("field", ["venue", "mode"])
def test_an_order_event_without_venue_or_run_mode_cannot_be_decoded(field: str) -> None:
    wire = _order_event_wire()
    del wire[field]
    with pytest.raises(ContractValidationError):
        _from_wire(wire)  # a missing value is never defaulted


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
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


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
@pytest.mark.parametrize("field", ["venue", "mode"])
@pytest.mark.parametrize("value", ["LIVE", "REAL", "DEMO", None, _VenueWithLive.LIVE, _RunModeWithLive.LIVE])
def test_a_live_order_event_cannot_be_constructed(field: str, value: Any) -> None:
    # A record validates on construction, so a live order does not even exist in memory.
    assert samples.order_event().venue is ExecutionVenue.SIMULATED  # the unmodified sample is fine
    with pytest.raises(ContractValidationError) as caught:
        samples.order_event(**{field: value})
    assert caught.value.details.get("path") == field


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_no_contract_record_can_be_decoded_with_a_live_run_mode() -> None:
    checked: list[str] = []
    for name, record in samples.all_samples().items():
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
        checked.append(name)
    # Every contract that carries a run mode was exercised (Tick and ModelVersion carry none).
    assert "OrderEvent" in checked
    assert len(checked) >= 6


@pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-13", "M-AUTH-14", "OWN-14")
@pytest.mark.acceptance("S00-ACC-04")
def test_the_published_order_event_schemas_only_know_the_simulated_venue_and_the_two_run_modes() -> None:
    schema_directory = Path(contracts.__file__).resolve().parent / "schemas"
    schema_files = sorted(schema_directory.glob("OrderEvent-*.schema.json"))
    assert schema_files, "no published OrderEvent schema found"
    for schema_file in schema_files:
        properties = json.loads(schema_file.read_text(encoding="utf-8"))["properties"]
        assert properties["venue"]["enum"] == ["SIMULATED"], schema_file.name
        assert sorted(properties["mode"]["enum"]) == ["PAPER", "REPLAY"], schema_file.name
