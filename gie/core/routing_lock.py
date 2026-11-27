"""Paper-only routing lock (requirements G08-03, G08-10, M-AUTH-13/14, S00-ACC-04, OWN-14).

Real order routing is **locked for the whole project phase**.  "Real" means any
order message that leaves the GIE process towards a broker, exchange or
terminal - including demo, trial and challenge accounts.  Paper trading in GIE
is simulated *inside* the process against observed prices.

The lock has independent layers, so no single mistake unlocks it:

1. **Vocabulary.**  :class:`RunMode` only contains ``REPLAY`` and ``PAPER`` and
   :class:`ExecutionVenue` only contains ``SIMULATED``.  The shared contracts
   use these enums, so a live order event cannot even be constructed or decoded.
2. **Constant.**  :data:`LIVE_ROUTING_PERMITTED` is the literal ``False``.  It is
   not read from the environment, a file, a command line or any setting.
3. **Guard.**  Every execution adapter call goes through
   :meth:`RoutingGuard.authorize`, which only admits adapter *types* that were
   registered with :func:`register_simulated_adapter` and that declare the
   simulated venue.
4. **Static scan.**  ``tools/check_live_lock.py`` (run in CI) fails the build if
   broker order-entry calls appear in GIE code or if this module is weakened.

Unlocking is deliberately impossible by configuration.  It would need a code
change to this module, a new major version of the OrderEvent contract and a
recorded owner approval - none of which is in scope of this project phase.
"""

from __future__ import annotations

import threading
from enum import Enum
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


def parse_run_mode(value: object) -> RunMode:
    """Convert configuration input to a :class:`RunMode`; anything else is refused."""
    if isinstance(value, RunMode):
        return value
    if isinstance(value, str):
        for mode in RunMode:
            if value == mode.value:
                return mode
    raise LiveRoutingLockedError(
        "only REPLAY and PAPER runs exist in this project phase; real routing is locked",
        requested=repr(value)[:64],
    )


def assert_live_routing_locked() -> None:
    """Start-up self-check: raise unless every static layer of the lock is intact."""
    if LIVE_ROUTING_PERMITTED is not False:
        raise LiveRoutingLockedError("LIVE_ROUTING_PERMITTED has been tampered with")
    # __members__ includes aliases: a member named LIVE that aliases PAPER must be caught too.
    run_modes = {name: member.value for name, member in RunMode.__members__.items()}
    if run_modes != {"REPLAY": "REPLAY", "PAPER": "PAPER"}:
        raise LiveRoutingLockedError("RunMode vocabulary has been changed")
    venues = {name: member.value for name, member in ExecutionVenue.__members__.items()}
    if venues != {"SIMULATED": "SIMULATED"}:
        raise LiveRoutingLockedError("ExecutionVenue vocabulary has been changed")


_registry_lock = threading.Lock()
#: Admitted adapter classes.  Membership is decided by object identity (``is``), never by
#: ``==`` / ``hash``, which a metaclass could override to impersonate an admitted class.
_SIMULATED_ADAPTER_TYPES: set[type] = set()


def _is_admitted(adapter_type: type) -> bool:
    return any(admitted is adapter_type for admitted in _SIMULATED_ADAPTER_TYPES)


def _declared_venue(adapter_type: type) -> object:
    return getattr(adapter_type, "venue", None)


def register_simulated_adapter(adapter_type: type) -> type:
    """Class decorator that admits a *simulated* execution adapter type.

    The class must declare ``venue = ExecutionVenue.SIMULATED`` and
    ``sends_real_orders = False`` as class attributes.  Anything else is refused.
    """
    if not isinstance(adapter_type, type):
        raise LiveRoutingLockedError("only classes can be registered as adapters")
    if _declared_venue(adapter_type) is not ExecutionVenue.SIMULATED:
        raise LiveRoutingLockedError(
            "adapter does not declare the simulated venue", adapter=adapter_type.__qualname__
        )
    if getattr(adapter_type, "sends_real_orders", None) is not False:
        raise LiveRoutingLockedError(
            "adapter must declare sends_real_orders = False", adapter=adapter_type.__qualname__
        )
    with _registry_lock:
        if not _is_admitted(adapter_type):
            _SIMULATED_ADAPTER_TYPES.add(adapter_type)
    return adapter_type


def registered_adapter_types() -> frozenset[type]:
    """Immutable snapshot of the admitted adapter classes."""
    with _registry_lock:
        return frozenset(_SIMULATED_ADAPTER_TYPES)


class RoutingGuard:
    """The single choke point between GIE and any execution adapter.

    An execution engine holds one guard and calls :meth:`authorize` before every
    adapter operation that could create or change an order.
    """

    def __init__(self, mode: object) -> None:
        assert_live_routing_locked()
        self._mode: Final[RunMode] = parse_run_mode(mode)

    @property
    def mode(self) -> RunMode:
        return self._mode

    def authorize(self, adapter: object) -> None:
        """Raise :class:`LiveRoutingLockedError` unless *adapter* is an admitted simulator."""
        assert_live_routing_locked()
        adapter_type = type(adapter)
        with _registry_lock:
            admitted = _is_admitted(adapter_type)
        if not admitted:
            raise LiveRoutingLockedError(
                "execution adapter type is not a registered simulated adapter",
                adapter=adapter_type.__qualname__,
            )
        # Re-check the declarations on the instance: an instance attribute must not
        # be able to override what the registered class declared.
        if getattr(adapter, "venue", None) is not ExecutionVenue.SIMULATED:
            raise LiveRoutingLockedError(
                "execution adapter venue is not SIMULATED", adapter=adapter_type.__qualname__
            )
        if getattr(adapter, "sends_real_orders", None) is not False:
            raise LiveRoutingLockedError(
                "execution adapter does not declare sends_real_orders = False",
                adapter=adapter_type.__qualname__,
            )
