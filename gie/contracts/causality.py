"""Derivation rules between records (causality, expiry and origin propagation).

A derived record must not know more, live longer or claim a better origin than
the records it was derived from.  Producers call :func:`check_derivation` when
they build a record from its inputs; QA runs it over journals.
"""

from __future__ import annotations

from gie.contracts._spec import Record
from gie.contracts.common import DataOrigin, Produced, origin_not_better_than
from gie.contracts.ordering import ordering_time_ns
from gie.contracts.tick import Tick
from gie.core.errors import ContractValidationError


def _watermark(record: Record) -> int:
    """Market-data watermark of an input: its receive time (tick) or its own watermark."""
    if isinstance(record, Tick):
        return record.receive_time_ns
    as_of: int | None = getattr(record, "as_of_time_ns", None)
    return ordering_time_ns(record) if as_of is None else as_of


def _origin(record: Record) -> DataOrigin | None:
    if isinstance(record, Tick):
        return record.origin
    origin: DataOrigin | None = getattr(record, "data_origin", None)
    return origin


def check_derivation(child: Produced, *inputs: Record) -> None:
    """Raise :class:`ContractValidationError` unless *child* may have been derived from *inputs*.

    Rules (section 3 of the contract specification):

    * same run and mode as every run-scoped input;
    * created no earlier than any input became available
      (``decision_time_ns >= ordering time of the input``);
    * its market-data watermark covers the inputs
      (``as_of_time_ns >= watermark of every input``);
    * it does not outlive its inputs
      (``valid_until_ns <= valid_until_ns of every input that has one``);
    * its data origin is not more trustworthy than the least trustworthy input.
    """
    if not inputs:
        raise ContractValidationError("a derived record needs at least one input")

    def fail(rule: str, **details: object) -> ContractValidationError:
        return ContractValidationError(
            f"derivation rule violated: {rule}",
            contract=type(child).__name__,
            rule=rule,
            **{
                key: (value if isinstance(value, (int, str)) else str(value))
                for key, value in details.items()
            },
        )

    child_expiry: int | None = getattr(child, "valid_until_ns", None)
    origins: list[DataOrigin] = []
    for item in inputs:
        name = type(item).__name__
        run_id = getattr(item, "run_id", None)
        if run_id is not None and (
            run_id != child.run_id or getattr(item, "mode", child.mode) is not child.mode
        ):
            raise fail("inputs belong to the same run and mode", input=name)
        if child.decision_time_ns < ordering_time_ns(item):
            raise fail("a record is not created before its inputs are available", input=name)
        if child.as_of_time_ns < _watermark(item):
            raise fail("as_of_time_ns covers the market-data watermark of every input", input=name)
        expiry: int | None = getattr(item, "valid_until_ns", None)
        if child_expiry is not None and expiry is not None and child_expiry > expiry:
            raise fail("a derived record does not outlive its inputs", input=name)
        origin = _origin(item)
        if origin is not None:
            origins.append(origin)
    if origins and not origin_not_better_than(child.data_origin, *origins):
        raise fail("data_origin is not more trustworthy than the inputs allow")
