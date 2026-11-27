"""Measure the cost of the foundation primitives (requirements M-GATE-06, G03-10).

``python -m tools.benchmark_foundation --out FILE`` times contract construction
(validation), canonical encoding, strict decoding and the core primitives, and
writes count / min / mean / p50 / p95 / p99 / max per operation.  It also times
the enforcement path that every record or order passes: journal publication
(in memory, and to a file with and without fsync), the lifecycle ledger, the
entry and reducing-order authorisation checks and the routing guard.

What these numbers are: single-thread micro-benchmarks on *this* machine with
labelled synthetic sample records (``tests/gie/samples.py``).
What they are not: evidence about the owner's hardware, about full tick
processing, or about any trading result.  The proposed latency targets of the
specification are quoted next to the measurements only as a reference.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import os
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from gie.contracts import (
    FileJournal,
    LifecycleLedger,
    MemoryJournal,
    OrderEventType,
    OrderIntent,
    OrderState,
    RunRole,
    fingerprint,
    from_json,
    identity,
    ordering_key,
    to_json,
    to_wire,
    validate_entry_authorization,
    validate_order_transition,
    validate_position_transition,
    validate_reducing_order,
)
from gie.core.canonical import canonical_dumps
from gie.core.clock import ReplayClock
from gie.core.freshness import require_fresh
from gie.core.ids import make_id
from gie.core.latency import LatencyRecorder
from gie.core.ordering import SequenceTracker
from gie.core.params import load_research_parameters
from gie.core.routing_lock import RoutingGuard, RunMode
from gie.core.timeutil import NS_PER_MS, NS_PER_SECOND
from tests.gie import benchmark_adapter, samples

#: fsync is slow and its cost depends on the storage device; fewer samples are enough.
FSYNC_ITERATIONS = 300
#: Number of complete steps (tick, position snapshot, stop tightening) timed per journal kind.
STEP_ITERATIONS = 1500


def _time(name: str, operation: Callable[[int], Any], iterations: int, warmup: int) -> dict[str, Any]:
    for index in range(warmup):
        operation(index)
    recorder = LatencyRecorder(name, capacity=iterations)
    counter = time.perf_counter_ns
    gc.collect()
    for index in range(iterations):
        started = counter()
        operation(warmup + index)
        recorder.record(counter() - started)
    return recorder.summary().to_dict()


def _ignore_index(operation: Callable[[], Any]) -> Callable[[int], Any]:
    def call(_index: int) -> Any:
        return operation()

    return call


def _ticks(count: int) -> list[Any]:
    """*count* consecutive labelled fixture ticks of one stream, one millisecond apart."""
    return [
        samples.tick(
            seq=index,
            event_time_ns=samples.T0 + index * NS_PER_MS,
            receive_time_ns=samples.T0 + index * NS_PER_MS + 1,
        )
        for index in range(count)
    ]


def _journal_publish(journal: MemoryJournal, name: str, iterations: int, warmup: int) -> dict[str, Any]:
    ticks = _ticks(iterations + warmup)
    journal.publish(samples.run_manifest())
    return _time(name, lambda index: journal.publish(ticks[index]), iterations, warmup)


def _ratchet_steps(count: int) -> tuple[list[Any], list[list[Any]]]:
    """A run prefix with one open position, then *count* steps that each tighten its stop.

    One step is what a tick costs in the journal while a position is open and
    its stop is ratcheted: the tick, a position snapshot with a new peak, the
    replace request of the protective stop and the broker's confirmation.
    """
    story = samples.run_story()
    prefix: list[Any] = list(story[:19])  # up to the valued position; the stop is working
    stop: Any = next(
        record
        for record in reversed(prefix)
        if getattr(record, "intent", None) is OrderIntent.PROTECTIVE_STOP
    )
    held: Any = prefix[-1]
    steps: list[list[Any]] = []
    for index in range(count):
        t = samples.T0 + 5 * NS_PER_SECOND + (index + 1) * 10 * NS_PER_MS
        cents = Decimal(index + 1) / 100
        trigger = Decimal("2647.80") + cents
        quote = samples.tick(
            seq=4 + index,
            event_time_ns=t - NS_PER_MS,
            receive_time_ns=t,
            bid=Decimal("2650.60") + cents,
            ask=Decimal("2650.85") + cents,
        )
        snapshot = dataclasses.replace(
            held,
            revision=held.revision + 1 + index,
            seq=held.seq + 1 + index,
            decision_time_ns=t,
            as_of_time_ns=t,
            stop_price=trigger,
            mark_price=quote.bid,
            unrealized_net_pnl_amount=Decimal("38.00") + Decimal(index + 1),
            peak_net_pnl_amount=Decimal("36.00") + Decimal(index + 1),
            peak_time_ns=t,
        )
        order_seq = stop.order_seq + 1 + 2 * index
        common = {
            "seq": stop.seq + 1 + 2 * index,
            "order_seq": order_seq,
            "order_event_id": identity.order_event_id(stop.client_order_id, order_seq),
            "reason_code": "RATCHET",
            "trigger_price": trigger - Decimal("0.01"),
        }
        request = dataclasses.replace(
            stop,
            **common,
            event_type=OrderEventType.REPLACE_REQUESTED,
            order_state=OrderState.PENDING_REPLACE,
            requested_trigger_price=trigger,
            event_time_ns=t,
            receive_time_ns=t,
        )
        confirmed = dataclasses.replace(
            stop,
            **{
                **common,
                "seq": common["seq"] + 1,
                "order_seq": order_seq + 1,
                "order_event_id": identity.order_event_id(stop.client_order_id, order_seq + 1),
                "trigger_price": trigger,
            },
            event_type=OrderEventType.REPLACED,
            order_state=OrderState.WORKING,
            event_time_ns=t,
            receive_time_ns=t + 1,
        )
        steps.append([quote, snapshot, request, confirmed])
    return prefix, steps


def _time_steps(name: str, journal: MemoryJournal, iterations: int, warmup: int) -> dict[str, Any]:
    prefix, steps = _ratchet_steps(iterations + warmup)
    for record in prefix:
        journal.publish(record)

    def one_step(index: int) -> None:
        for record in steps[index]:
            journal.publish(record)
        journal.commit()

    return _time(name, one_step, iterations, warmup)


def _step_cases() -> list[dict[str, Any]]:
    """Cost of one whole step (4 records) in each journal kind; one synchronisation per step."""
    results = [
        _time_steps(
            "step: tick + position + stop tightening (4 records), MemoryJournal",
            MemoryJournal(),
            STEP_ITERATIONS,
            50,
        )
    ]
    with (
        tempfile.TemporaryDirectory(prefix="gie-bench-") as folder,
        FileJournal(Path(folder) / "step.jsonl", fsync=False) as journal,
    ):
        results.append(
            _time_steps(
                "step: same 4 records, FileJournal, one fsync per step (commit)",
                journal,
                FSYNC_ITERATIONS,
                20,
            )
        )
    return results


def _enforcement_path(iterations: int, warmup: int) -> list[dict[str, Any]]:
    """Cost of the checks every published record and every order passes."""
    results: list[dict[str, Any]] = []

    results.append(
        _journal_publish(MemoryJournal(), "journal: MemoryJournal.publish(tick)", iterations, warmup)
    )
    with tempfile.TemporaryDirectory(prefix="gie-bench-") as folder:
        with FileJournal(Path(folder) / "no_fsync.jsonl", fsync=False) as journal:
            results.append(
                _journal_publish(journal, "journal: FileJournal.publish(tick), no fsync", iterations, warmup)
            )
        with FileJournal(Path(folder) / "fsync.jsonl", fsync=True) as journal:
            results.append(
                _journal_publish(journal, "journal: FileJournal.publish(tick), fsync", FSYNC_ITERATIONS, 20)
            )

    plan = samples.trade_plan()
    decision = samples.risk_decision(plan)
    entry = samples.order_event()
    now_ns = entry.receive_time_ns
    results.append(
        _time(
            "gateway: validate_entry_authorization",
            lambda _i: validate_entry_authorization(
                plan, decision, entry, now_ns=now_ns, role=RunRole.RESEARCH
            ),
            iterations,
            warmup,
        )
    )

    held = samples.position()
    _stop_submit, stop_working, stop_request, _stop_replaced = samples.protective_stop_lifecycle()
    results.append(
        _time(
            "gateway: validate_reducing_order (protective stop)",
            lambda _i: validate_reducing_order(held, stop_request),
            iterations,
            warmup,
        )
    )
    results.append(
        _time(
            "gateway: validate_order_transition (stop replace request)",
            lambda _i: validate_order_transition(stop_working, stop_request),
            iterations,
            warmup,
        )
    )
    story_prefix, story_steps = _ratchet_steps(1)
    ledger = LifecycleLedger()
    for record in story_prefix:
        ledger.admit(record)
    next_request = story_steps[0][2]
    results.append(
        _time(
            "gateway: LifecycleLedger.check (next event of an order, referential run ledger)",
            lambda _i: ledger.check(next_request),
            iterations,
            warmup,
        )
    )

    opened, tightened, _closed = samples.position_lifecycle()
    results.append(
        _time(
            "reflex: validate_position_transition (stop tightened)",
            lambda _i: validate_position_transition(opened, tightened),
            iterations,
            warmup,
        )
    )

    guard = RoutingGuard(RunMode.REPLAY)
    adapter = benchmark_adapter.BenchmarkSimulatedAdapter()
    results.append(
        _time(
            "gateway: RoutingGuard.authorized_methods",
            lambda _i: guard.authorized_methods(adapter),
            iterations,
            warmup,
        )
    )
    return results


def run(iterations: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    warmup = max(iterations // 10, 100)

    for name, build in samples.BUILDERS.items():
        record = build()
        record_type = type(record)
        kwargs = {item.name: getattr(record, item.name) for item in dataclasses.fields(record)}  # type: ignore[arg-type]
        text = to_json(record)
        operations: list[tuple[str, Callable[[], Any]]] = [
            ("construct + validate", partial(record_type, **kwargs)),
            ("encode canonical JSON", partial(to_json, record)),
            ("decode + validate", partial(from_json, text)),
            ("fingerprint (sha256)", partial(fingerprint, record)),
            ("ordering key", partial(ordering_key, record)),
        ]
        for label, operation in operations:
            results.append(_time(f"{name}: {label}", _ignore_index(operation), iterations, warmup))

    tick_wire = to_wire(samples.tick())
    results.append(
        _time(
            "core: canonical_dumps(tick wire dict)", lambda _i: canonical_dumps(tick_wire), iterations, warmup
        )
    )
    results.append(
        _time("core: make_id (3 parts)", lambda i: make_id("pln", "fixture", i, "x"), iterations, warmup)
    )

    clock = ReplayClock(samples.T0)
    results.append(
        _time(
            "core: ReplayClock.advance_by + now_ns",
            lambda _i: (clock.advance_by(1000), clock.now_ns()),
            iterations,
            warmup,
        )
    )
    results.append(
        _time(
            "core: require_fresh",
            lambda _i: require_fresh(
                now_ns=samples.T0, reference_ns=samples.T0 - 5, max_age_ns=1000, what="x"
            ),
            iterations,
            warmup,
        )
    )
    tracker = SequenceTracker()
    results.append(
        _time("core: SequenceTracker.observe", lambda i: tracker.observe("s", i, "f"), iterations, warmup)
    )

    results.extend(_enforcement_path(iterations, warmup))
    results.extend(_step_cases())

    parameters = load_research_parameters()
    targets = {
        name: {"value_ns": parameters.integer(name), "requirement": parameters.get(name).requirement}
        for name in (
            "tick_processing_target_ns",
            "protection_decision_target_ns",
            "tactical_decision_target_ns",
        )
    }
    return {
        "schema_version": 1,
        "generated_by": "tools/benchmark_foundation.py",
        "label": "micro-benchmark on labelled SYNTHETIC FIXTURE records; development container only",
        "iterations_per_operation": iterations,
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "cpu_count": os.cpu_count(),
        },
        "proposed_targets_for_reference": targets,
        "percentile_method": "nearest rank on all samples",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the GIE foundation primitives.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20000)
    args = parser.parse_args(argv)
    payload = run(args.iterations)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row in payload["results"]:
        print(
            f"{row['name']:45s} p50 {row['p50_ns'] / 1000:8.1f} us  p99 {row['p99_ns'] / 1000:8.1f} us  "
            f"max {row['max_ns'] / 1000:9.1f} us"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
