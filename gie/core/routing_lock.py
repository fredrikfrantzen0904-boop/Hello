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
   only adapter *classes* that were registered from an allow-listed module,
   declare the simulated venue, have no per-instance attributes and whose
   methods are still the functions that were registered.
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
from types import FunctionType
from typing import Final

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
    names = {member.name: member for member in genuine}
    members = dict(enum_type.__members__)  # includes aliases
    if set(members) != set(names) or any(members[name] is not names[name] for name in names):
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
#: Admitted adapter classes with the method functions they had when registered.  Membership is
#: decided by object identity (``is``), never by ``==`` / ``hash``, which a metaclass could
#: override to impersonate an admitted class.
_SIMULATED_ADAPTER_TYPES: dict[type, dict[str, FunctionType]] = {}


def _admitted_methods(adapter_type: type) -> dict[str, FunctionType] | None:
    for admitted, methods in _SIMULATED_ADAPTER_TYPES.items():
        if admitted is adapter_type:
            return methods
    return None


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

    The method functions are remembered; if the class is modified afterwards the
    guard refuses it.  Anything else is refused here.
    """
    if not isinstance(adapter_type, type):
        raise LiveRoutingLockedError("only classes can be registered as adapters")
    name = adapter_type.__qualname__
    module = getattr(adapter_type, "__module__", "")
    if not isinstance(module, str) or not module.startswith(ALLOWED_ADAPTER_MODULE_PREFIXES):
        raise LiveRoutingLockedError(
            "adapter classes may only be defined in the execution or backtesting packages (or tests)",
            adapter=name,
            module=str(module)[:80],
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
        methods[method_name] = function
    with _registry_lock:
        if _admitted_methods(adapter_type) is None:
            _SIMULATED_ADAPTER_TYPES[adapter_type] = methods
    return adapter_type


def registered_adapter_types() -> frozenset[type]:
    """Immutable snapshot of the admitted adapter classes."""
    with _registry_lock:
        return frozenset(_SIMULATED_ADAPTER_TYPES)


class RoutingGuard:
    """The single choke point between GIE and any execution adapter.

    The class is final and has no per-instance attributes besides the run mode.
    ``gie.contracts.guarded.GuardedExecutionAdapter`` calls :meth:`authorize`
    before every adapter operation and invokes the adapter only through the
    functions returned by :meth:`authorized_methods`.
    """

    __slots__ = ("_mode",)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise LiveRoutingLockedError("RoutingGuard may not be subclassed")

    def __init__(self, mode: object) -> None:
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
        declares the simulated venue and still has exactly the registered methods.
        """
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
            if getattr(adapter_type, method_name, None) is not function:
                raise LiveRoutingLockedError(
                    "execution adapter class was modified after registration",
                    adapter=name,
                    method=method_name,
                )
        return dict(methods)

    def authorize(self, adapter: object) -> None:
        """Raise :class:`LiveRoutingLockedError` unless *adapter* is an admitted simulator."""
        self.authorized_methods(adapter)
