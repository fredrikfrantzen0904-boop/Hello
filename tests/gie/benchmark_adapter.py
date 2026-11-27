"""A do-nothing simulated execution adapter for ``tools/benchmark_foundation.py``.

SYNTHETIC FIXTURE - not an execution model.  It exists only so that the cost of
the routing guard can be measured against a registered adapter class; it
simulates nothing and is not used by any test.  It lives under ``tests`` because
the routing lock admits adapter classes only from the execution and backtesting
packages and from tests.
"""

from __future__ import annotations

from gie.contracts import OrderEvent, Position, Tick
from gie.core.routing_lock import ExecutionVenue, register_simulated_adapter


@register_simulated_adapter
class BenchmarkSimulatedAdapter:
    """Accepts every call and does nothing (labelled synthetic fixture)."""

    __slots__ = ()

    venue = ExecutionVenue.SIMULATED
    sends_real_orders = False

    def submit(self, request: OrderEvent) -> None:
        return None

    def cancel(self, request: OrderEvent) -> None:
        return None

    def replace(self, request: OrderEvent) -> None:
        return None

    def on_tick(self, tick: Tick) -> None:
        return None

    def poll(self) -> tuple[OrderEvent, ...]:
        return ()

    def open_positions(self) -> tuple[Position, ...]:
        return ()
