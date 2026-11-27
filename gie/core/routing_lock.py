"""Paper-only routing lock (requirements G08-03, G08-10, M-AUTH-13, M-AUTH-19, S00-ACC-04, OWN-14).

Real order routing is **locked for the whole project phase**.  "Real" means any
order message that leaves the GIE process towards a broker, exchange or
terminal - including demo, trial and challenge accounts.  Paper trading in GIE
is simulated *inside* the process against observed prices.

The lock has independent layers, so no single mistake unlocks it:

1. **Vocabulary.**  :class:`RunMode` only contains ``REPLAY`` and ``PAPER`` and
   :class:`ExecutionVenue` only contains ``SIMULATED``.  The shared contracts
   use these enums, so a live order event cannot be constructed or decoded.
2. **Constant.**  :data:`LIVE_ROUTING_PERMITTED` is the literal ``False``.  It is
   not read from the environment, a file, a command line or any setting.
3. **Guard.**  An execution engine reaches an adapter only through
   ``gie.contracts.guarded.GuardedExecutionAdapter``, which calls
   :meth:`RoutingGuard.authorize` before every operation.  The guard admits
   only adapter *classes* that were registered from an allow-listed module -
   judged by where the registering code and the method functions really live,
   not only by the class's self-declared ``__module__`` - that declare the
   simulated venue, have no per-instance attributes and whose methods are
   still the unmodified functions that were registered.
4. **Static scan.**  ``tools/check_live_lock.py`` (run in CI) fails the build if
   broker order-entry calls, broker SDK imports, process or network escapes
   appear in GIE code, or if this module is weakened or bypassed.

What the layers can and cannot do: layers 1-3 stop mistakes and casual bypasses
at run time; Python cannot stop someone who deliberately rewrites this module
or lies inside an adapter's ``submit`` method.  Those are caught by layer 4
(pinned hash of this file, forbidden calls and imports), by the tests and by
review.  Unlocking is impossible by configuration: it needs a code change to
this module, a new major version of the OrderEvent contract and a recorded
owner approval - none of which is in scope of this project phase.
"""

from __future__ import annotations

import threading
from enum import Enum
from types import CodeType, FunctionType
from typing import Any, Final

from gie.core.errors import LiveRoutingLockedError

#: Project-phase lock.  Must be the literal ``False``.
LIVE_ROUTING_PERMITTED: Final = False


class RunMode(str, Enum):
    """How a run obtains market data.  There is intentionally no live-trading member."""

    REPLAY = "REPLAY"
    PAPER = "PAPER"


class ExecutionVenue(str, Enum):
    """Where orders are executed.  There is intentionally no real venue."""

    SIMULATED = "SIMULATED"


# The genuine members, captured at import.  Every check below compares against these
# objects by identity, so a member injected into the enum later is never accepted.
_RUN_MODES: Final[tuple[RunMode, ...]] = (RunMode.REPLAY, RunMode.PAPER)
_VENUES: Final[tuple[ExecutionVenue, ...]] = (ExecutionVenue.SIMULATED,)

#: Only classes defined in these packages may be registered as simulated adapters.
ALLOWED_ADAPTER_MODULE_PREFIXES: Final[tuple[str, ...]] = ("gie.execution.", "gie.backtesting.", "tests.")

#: Methods of an execution adapter (``gie.contracts.interfaces.ExecutionAdapter``).
ADAPTER_METHODS: Final[tuple[str, ...]] = ("submit", "cancel", "replace", "on_tick", "poll", "open_positions")


def is_run_mode(value: object) -> bool:
    """True only for the genuine REPLAY / PAPER members (identity, not equality)."""
    return any(value is mode for mode in _RUN_MODES)


def is_simulated_venue(value: object) -> bool:
    """True only for the genuine SIMULATED member (identity, not equality)."""
    return any(value is venue for venue in _VENUES)


def parse_run_mode(value: object) -> RunMode:
    """Convert configuration input to a :class:`RunMode`; anything else is refused."""
    for mode in _RUN_MODES:
        if value is mode or (type(value) is str and value == mode.value):
            return mode
    raise LiveRoutingLockedError(
        "only REPLAY and PAPER runs exist in this project phase; real routing is locked",
        requested=repr(value)[:64],
    )


def _vocabulary_intact(enum_type: type[Enum], genuine: tuple[Enum, ...]) -> bool:
    try:
        return _vocabulary_matches(enum_type, genuine)
    except Exception:  # a tampered enum can make the enum machinery itself fail: that is "not intact"
        return False


def _vocabulary_matches(enum_type: type[Enum], genuine: tuple[Enum, ...]) -> bool:
    names = {member.name: member for member in genuine}
    members = dict(enum_type.__members__)  # includes aliases
    if set(members) != set(names) or any(members[name] is not names[name] for name in names):
        return False
    if list(getattr(enum_type, "_member_names_", ())) != [member.name for member in genuine]:
        return False
    if len(list(enum_type)) != len(genuine):
        return False
    value_map = getattr(enum_type, "_value2member_map_", {})
    member_map = getattr(enum_type, "_member_map_", {})
    return (
        set(value_map) == {member.value for member in genuine}
        and all(value_map[member.value] is member for member in genuine)
        and set(member_map) == set(names)
        and all(member.name == member.value for member in genuine)
    )


def assert_live_routing_locked() -> None:
    """Self-check: raise unless every static layer of the lock is intact."""
    if LIVE_ROUTING_PERMITTED is not False:
        raise LiveRoutingLockedError("LIVE_ROUTING_PERMITTED has been tampered with")
    if not _vocabulary_intact(RunMode, _RUN_MODES) or [m.value for m in _RUN_MODES] != ["REPLAY", "PAPER"]:
        raise LiveRoutingLockedError("RunMode vocabulary has been changed")
    if not _vocabulary_intact(ExecutionVenue, _VENUES) or [v.value for v in _VENUES] != ["SIMULATED"]:
        raise LiveRoutingLockedError("ExecutionVenue vocabulary has been changed")


_registry_lock = threading.Lock()


class _ByIdentity:
    """Registry key that equals nothing but itself, whatever ``__eq__`` / ``__hash__`` a class defines."""

    __slots__ = ("target",)

    def __init__(self, target: type) -> None:
        self.target: Final[type] = target


#: Admitted adapter classes with the method functions they had when registered.  Membership is
#: decided by object identity (``is``), never by ``==`` / ``hash``, which a metaclass could
#: override to impersonate - or to overwrite - an admitted class.
_SIMULATED_ADAPTER_TYPES: dict[Any, dict[str, FunctionType]] = {}

_Snapshot = tuple[FunctionType, CodeType, object, object, tuple[object, ...]]
#: What each registered method function looked like when it was registered (keyed by ``id``;
#: the function is kept alive by the entry).  A function mutated in place afterwards is refused.
_FUNCTION_SNAPSHOTS: dict[int, _Snapshot] = {}


def _key_target(key: object) -> object:
    return key.target if type(key) is _ByIdentity else key


def _admitted_methods(adapter_type: type) -> dict[str, FunctionType] | None:
    for key, methods in _SIMULATED_ADAPTER_TYPES.items():
        if _key_target(key) is adapter_type:
            return methods
    return None


def _closure_contents(function: FunctionType) -> tuple[object, ...]:
    contents: list[object] = []
    for cell in function.__closure__ or ():
        try:
            contents.append(cell.cell_contents)
        except ValueError:  # an empty cell
            contents.append(_closure_contents)
    return tuple(contents)


def _snapshot(function: FunctionType) -> _Snapshot:
    return (
        function,
        function.__code__,
        function.__defaults__,
        None if function.__kwdefaults__ is None else tuple(sorted(function.__kwdefaults__.items())),
        _closure_contents(function),
    )


def _function_unchanged(function: FunctionType) -> bool:
    recorded = _FUNCTION_SNAPSHOTS.get(id(function))
    if recorded is None or recorded[0] is not function:
        return True  # nothing was recorded for this function (it was not admitted by registration)
    current = _snapshot(function)
    return (
        current[1] is recorded[1]
        and current[2] is recorded[2]
        and current[3] == recorded[3]
        and len(current[4]) == len(recorded[4])
        and all(now is then for now, then in zip(current[4], recorded[4], strict=True))
    )


def _allowed_module(name: object) -> bool:
    return isinstance(name, str) and name.startswith(ALLOWED_ADAPTER_MODULE_PREFIXES)


def _calling_module(depth: int) -> object:
    """``__name__`` of the module whose code is *depth* calls above the caller of this function.

    The frame is taken from a traceback, so that this module needs neither ``sys``
    nor ``inspect`` (it deliberately imports nothing that can read the environment,
    files or the command line).
    """
    try:
        raise LookupError
    except LookupError as probe:
        frame = probe.__traceback__.tb_frame if probe.__traceback__ is not None else None
    for _ in range(depth + 1):
        if frame is None:
            return None
        frame = frame.f_back
    return None if frame is None else frame.f_globals.get("__name__")


def _has_instance_dict(adapter_type: type) -> bool:
    return any("__dict__" in vars(klass) for klass in adapter_type.__mro__ if klass is not object)


def _check_declarations(owner: object, description: str) -> None:
    if not is_simulated_venue(getattr(owner, "venue", None)):
        raise LiveRoutingLockedError(f"{description} does not declare the simulated venue")
    if getattr(owner, "sends_real_orders", None) is not False:
        raise LiveRoutingLockedError(f"{description} must declare sends_real_orders = False")


def register_simulated_adapter(adapter_type: type) -> type:
    """Class decorator that admits a *simulated* execution adapter class.

    The class must

    * be defined in a module under :data:`ALLOWED_ADAPTER_MODULE_PREFIXES`;
    * declare ``venue = ExecutionVenue.SIMULATED`` and ``sends_real_orders = False``;
    * define ``__slots__`` in every class of its hierarchy, so that instances
      have no ``__dict__`` and no method can be replaced on an instance;
    * define all adapter methods as plain functions.

    ``__module__`` is only what a class says about itself, so two facts that a
    class cannot choose are checked as well: the module that performs the
    registration, and the module in which each method function was really
    defined (its globals).  All three must lie under the allowed packages.

    The method functions are remembered together with their code, defaults and
    closures; if the class or one of these functions is modified afterwards the
    guard refuses the adapter.  Anything else is refused here.
    """
    if not isinstance(adapter_type, type):
        raise LiveRoutingLockedError("only classes can be registered as adapters")
    name = adapter_type.__qualname__
    module = getattr(adapter_type, "__module__", "")
    if not _allowed_module(module):
        raise LiveRoutingLockedError(
            "adapter classes may only be defined in the execution or backtesting packages (or tests)",
            adapter=name,
            module=str(module)[:80],
        )
    registering_module = _calling_module(1)
    if not _allowed_module(registering_module):
        raise LiveRoutingLockedError(
            "adapter classes may only be registered from the execution or backtesting packages (or tests)",
            adapter=name,
            module=str(registering_module)[:80],
        )
    _check_declarations(adapter_type, f"adapter class {name}")
    if _has_instance_dict(adapter_type):
        raise LiveRoutingLockedError(
            "adapter classes must define __slots__ (instances may not carry their own attributes)",
            adapter=name,
        )
    methods: dict[str, FunctionType] = {}
    for method_name in ADAPTER_METHODS:
        function = getattr(adapter_type, method_name, None)
        if not isinstance(function, FunctionType):
            raise LiveRoutingLockedError(
                "adapter class must define every adapter method as a plain function",
                adapter=name,
                method=method_name,
            )
        defining_module = function.__globals__.get("__name__")
        if not _allowed_module(defining_module):
            raise LiveRoutingLockedError(
                "adapter methods may only be defined in the execution or backtesting packages (or tests)",
                adapter=name,
                method=method_name,
                module=str(defining_module)[:80],
            )
        methods[method_name] = function
    with _registry_lock:
        if _admitted_methods(adapter_type) is None:
            _SIMULATED_ADAPTER_TYPES[_ByIdentity(adapter_type)] = methods
            for function in methods.values():
                _FUNCTION_SNAPSHOTS[id(function)] = _snapshot(function)
    return adapter_type


def registered_adapter_types() -> frozenset[type]:
    """Immutable snapshot of the admitted adapter classes."""
    with _registry_lock:
        return frozenset(
            target for key in _SIMULATED_ADAPTER_TYPES if isinstance(target := _key_target(key), type)
        )


class _FinalType(type):
    """Metaclass of :class:`RoutingGuard`: a subclass is refused before its class object exists."""

    def __new__(
        mcls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any
    ) -> _FinalType:
        if any(isinstance(base, _FinalType) for base in bases):
            raise LiveRoutingLockedError("RoutingGuard may not be subclassed")
        return super().__new__(mcls, name, bases, namespace, **kwargs)


class RoutingGuard(metaclass=_FinalType):
    """The single choke point between GIE and any execution adapter.

    The class is final and has no per-instance attributes besides the run mode.
    ``gie.contracts.guarded.GuardedExecutionAdapter`` calls :meth:`authorize`
    before every adapter operation and invokes the adapter only through the
    functions returned by :meth:`authorized_methods`.

    "Final" is enforced three times, because Python has no final classes: the
    metaclass refuses a subclass before it exists, ``__init_subclass__`` refuses
    one created behind the metaclass's back, and an object whose exact type is
    not this class cannot be initialised as a guard.  The order gateway
    additionally accepts only the exact type and calls the methods unbound.
    """

    __slots__ = ("_mode",)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise LiveRoutingLockedError("RoutingGuard may not be subclassed")

    def __init__(self, mode: object) -> None:
        if type(self) is not RoutingGuard:
            raise LiveRoutingLockedError("RoutingGuard may not be subclassed")
        assert_live_routing_locked()
        self._mode = parse_run_mode(mode)

    def __setattr__(self, name: str, value: object) -> None:
        # The run mode is set exactly once, in __init__; afterwards the guard is immutable.
        if name != "_mode" or hasattr(self, "_mode"):
            raise LiveRoutingLockedError("RoutingGuard is immutable")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        raise LiveRoutingLockedError("RoutingGuard is immutable")

    @property
    def mode(self) -> RunMode:
        mode: RunMode = self._mode
        if not is_run_mode(mode):
            raise LiveRoutingLockedError("RoutingGuard holds an invalid run mode")
        return mode

    def authorized_methods(self, adapter: object) -> dict[str, FunctionType]:
        """Check *adapter* and return the registered method functions to call it with.

        Raises :class:`LiveRoutingLockedError` unless the adapter is an instance of
        an admitted class (exact type), carries no instance attributes, still
        declares the simulated venue and still has exactly the registered methods,
        unmodified.
        """
        if type(self) is not RoutingGuard:
            raise LiveRoutingLockedError("RoutingGuard may not be subclassed")
        assert_live_routing_locked()
        adapter_type = type(adapter)
        name = adapter_type.__qualname__
        with _registry_lock:
            methods = _admitted_methods(adapter_type)
        if methods is None:
            raise LiveRoutingLockedError(
                "execution adapter type is not a registered simulated adapter", adapter=name
            )
        if _has_instance_dict(adapter_type) or hasattr(adapter, "__dict__"):
            raise LiveRoutingLockedError("execution adapter instances may not carry attributes", adapter=name)
        _check_declarations(adapter_type, f"adapter class {name}")
        _check_declarations(adapter, f"adapter instance of {name}")
        for method_name, function in methods.items():
            if getattr(adapter_type, method_name, None) is not function or not _function_unchanged(function):
                raise LiveRoutingLockedError(
                    "execution adapter class was modified after registration",
                    adapter=name,
                    method=method_name,
                )
        return dict(methods)

    def authorize(self, adapter: object) -> None:
        """Raise :class:`LiveRoutingLockedError` unless *adapter* is an admitted simulator."""
        self.authorized_methods(adapter)
