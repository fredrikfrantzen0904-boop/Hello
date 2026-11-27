"""Measure the cost of the foundation primitives (requirements M-GATE-06, G03-10).

``python -m tools.benchmark_foundation --out FILE`` times contract construction
(validation), canonical encoding, strict decoding and the core primitives, and
writes count / min / mean / p50 / p95 / p99 / max per operation.

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
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from gie.contracts import fingerprint, from_json, ordering_key, to_json, to_wire
from gie.core.canonical import canonical_dumps
from gie.core.clock import ReplayClock
from gie.core.freshness import require_fresh
from gie.core.ids import make_id
from gie.core.latency import LatencyRecorder
from gie.core.ordering import SequenceTracker
from gie.core.params import load_research_parameters
from tests.gie import samples


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
