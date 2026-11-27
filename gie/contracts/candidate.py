"""Candidate contract - one scored opportunity in one direction."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from gie.contracts import identity
from gie.contracts._spec import (
    DurationNs,
    EnumOf,
    Float,
    Id,
    Int,
    ListOf,
    Obj,
    Opt,
    Record,
    Str,
    TimeNs,
    fld,
    inv,
    record,
)
from gie.contracts.common import (
    CODE_PATTERN,
    PRICE,
    Horizon,
    MarketSession,
    Produced,
    Side,
    reason_codes_field,
    symbol_field,
)
from gie.contracts.registry import contract


class CandidateStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class QuantilePoint(Record):
    """One point of the predictive distribution of net R."""

    q_frac: float = fld(
        Float(gt=0.0, lt=1.0, label="probability"), "Cumulative probability.", unit="fraction"
    )
    value_r: float = fld(Float(label="R multiple"), "Net R at this quantile.", unit="R")


@record
@dataclass(frozen=True, slots=True, kw_only=True)
class Destination(Record):
    """A price the market may travel to under a named scenario."""

    label: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Scenario or destination name.")
    price: Decimal = fld(PRICE, "Destination price.", unit="USD per troy ounce")
    probability_frac: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="probability")),
        "Estimated probability of reaching it.",
        unit="fraction",
    )
    expected_time_to_reach_ns: int | None = fld(
        Opt(DurationNs()), "Expected time until it is reached.", unit="ns"
    )


_SCORED_FIELDS = ("expected_net_r", "net_r_stdev")
_MODEL_ONLY_FIELDS = ("win_probability_frac", "expected_cost_r", "expected_holding_time_ns", "quality_score")


def _quantiles_monotone(points: tuple[QuantilePoint, ...]) -> bool:
    return all(
        earlier.q_frac < later.q_frac and earlier.value_r <= later.value_r
        for earlier, later in itertools.pairwise(points)
    )


@contract("Candidate", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate(Produced):
    """Opportunity built from de-duplicated evidence, with its predictive estimates.

    Stream: ``candidate/<producer>``.  Ordering time: ``decision_time_ns``.
    All estimates are *net of expected costs* and expressed in R (multiples of
    the amount risked).  An unscored candidate (no model available) carries
    null estimates and cannot pass an expectancy gate.
    """

    candidate_id: str = fld(Id("cnd"), "identity.candidate_id(setup_type, side, evidence_ids, revision).")
    symbol: str = symbol_field("Always XAUUSD: GIE trades only gold.", gold_only=True)
    side: Side = fld(EnumOf(Side), "Direction of the opportunity.")
    setup_type: str = fld(
        Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Setup name from the feature dictionary."
    )
    horizon: Horizon = fld(EnumOf(Horizon), "Scalp or intraday.")
    session: MarketSession = fld(EnumOf(MarketSession), "Broker session at decision time.")
    evidence_ids: tuple[str, ...] = fld(
        ListOf(
            Id("evd"),
            min_items=1,
            max_items=64,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Evidence the candidate is built from (already de-duplicated by market event).",
    )
    reference_price: Decimal = fld(
        PRICE,
        "Executable price at decision time: ask for BUY, bid for SELL.",
        unit="USD per troy ounce",
    )
    status: CandidateStatus = fld(EnumOf(CandidateStatus), "Lifecycle status of this revision.")
    status_reasons: tuple[str, ...] = reason_codes_field("Why the candidate was rejected or withdrawn.")
    model_version_id: str | None = fld(
        Opt(Id("mdl")), "Model (or rule set) version that produced the estimates; null when unscored."
    )
    expected_net_r: float | None = fld(Opt(Float(label="R multiple")), "Expected net result.", unit="R")
    net_r_stdev: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Predictive standard deviation of the net result.", unit="R"
    )
    net_r_quantiles: tuple[QuantilePoint, ...] = fld(
        ListOf(Obj(QuantilePoint), max_items=32), "Optional predictive quantiles, ascending."
    )
    win_probability_frac: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="probability")),
        "Estimated probability of a positive net result.",
        unit="fraction",
    )
    expected_cost_r: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")),
        "Expected spread, commission and slippage already deducted in expected_net_r.",
        unit="R",
    )
    expected_holding_time_ns: int | None = fld(Opt(DurationNs()), "Expected holding time.", unit="ns")
    quality_score: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="score")),
        "Model quality of the opportunity in this direction.",
        unit="0..1",
    )
    destinations: tuple[Destination, ...] = fld(
        ListOf(Obj(Destination), max_items=16), "Scenario price destinations."
    )
    valid_until_ns: int = fld(
        TimeNs(),
        "Expiry instant: the candidate must not be ranked at or after it.",
        unit="ns since epoch, UTC",
    )
    revision: int = fld(Int(ge=0, label="revision"), "0 for the first record, +1 per update.")
    supersedes_candidate_id: str | None = fld(
        Opt(Id("cnd")), "Previous revision replaced by this record; null for revision 0."
    )

    INVARIANTS = (
        inv(
            "CND-1",
            "candidate_id equals identity.candidate_id(setup_type, side, evidence_ids, revision)",
            lambda r: (
                r.candidate_id
                == identity.candidate_id(r.setup_type, r.side.value, r.evidence_ids, r.revision)
            ),
        ),
        inv(
            "CND-2",
            "model_version_id, expected_net_r and net_r_stdev are all null (unscored) or all set (scored)",
            lambda r: (
                len(
                    {
                        r.model_version_id is None,
                        *(getattr(r, name) is None for name in _SCORED_FIELDS),
                    }
                )
                == 1
            ),
        ),
        inv(
            "CND-3",
            "an unscored candidate has no model-derived estimates, quantiles or destinations",
            lambda r: (
                r.model_version_id is not None
                or (
                    all(getattr(r, name) is None for name in _MODEL_ONLY_FIELDS)
                    and not r.net_r_quantiles
                    and not r.destinations
                )
            ),
        ),
        inv(
            "CND-4",
            "quantiles have strictly increasing q_frac and non-decreasing value_r",
            lambda r: _quantiles_monotone(r.net_r_quantiles),
        ),
        inv(
            "CND-5",
            "when status is AVAILABLE, valid_until_ns is later than decision_time_ns",
            lambda r: r.status is not CandidateStatus.AVAILABLE or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "CND-6",
            "status_reasons is non-empty when status is REJECTED or WITHDRAWN and empty when AVAILABLE",
            lambda r: (
                bool(r.status_reasons)
                if r.status in (CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN)
                else (not r.status_reasons if r.status is CandidateStatus.AVAILABLE else True)
            ),
        ),
        inv(
            "CND-7",
            "supersedes_candidate_id is set if and only if revision > 0",
            lambda r: (r.supersedes_candidate_id is not None) == (r.revision > 0),
        ),
    )
