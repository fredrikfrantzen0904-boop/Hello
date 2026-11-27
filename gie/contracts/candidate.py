"""Candidate contract - one scorer's view of one opportunity in one direction."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final, final

from gie.contracts import identity
from gie.contracts._spec import (
    DurationNs,
    EnumOf,
    Float,
    Id,
    Int,
    Invariant,
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
    REASON_CODE_PATTERN,
    Contribution,
    Horizon,
    MarketSession,
    Produced,
    Side,
    contributions_field,
    reason_codes_field,
    symbol_field,
)
from gie.contracts.model_version import ModelStatus
from gie.contracts.registry import contract
from gie.core.errors import IllegalTransitionError


class CandidateStatus(str, Enum):
    """Lifecycle of a candidate.

    * ``AVAILABLE`` - may be ranked and planned while it has not expired.
    * ``WITHDRAWN`` - its producer withdrew it because market behaviour or its
      evidence no longer supports it.
    * ``EXPIRED`` - it aged out without being withdrawn.
    * ``REJECTED`` - its producer found it not viable (reason codes say why).

    Only the candidate's producer (session 04) emits Candidate records.  The
    decision engine records *its* rejections in its own decision stream.
    """

    AVAILABLE = "AVAILABLE"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


#: ``status before -> statuses allowed after`` for revisions of one candidate.
CANDIDATE_STATUS_TRANSITIONS: Final[dict[CandidateStatus | None, frozenset[CandidateStatus]]] = {
    None: frozenset(CandidateStatus),
    CandidateStatus.AVAILABLE: frozenset(CandidateStatus),
    CandidateStatus.WITHDRAWN: frozenset(),
    CandidateStatus.EXPIRED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
}


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
    """A price the market may travel to under a named scenario.

    ``probability_frac`` is the estimated probability that the executable price
    touches ``price`` within ``horizon_ns`` after the decision time.  It is not
    conditional on any stop, and the probabilities of different destinations are
    touch probabilities, not exclusive scenario probabilities.
    """

    scenario: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Scenario name.")
    label: str = fld(Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Destination name.")
    price: Decimal = fld(PRICE, "Destination price.", unit="USD per troy ounce")
    probability_frac: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="probability")),
        "Probability of touching the price within horizon_ns; null when not estimated.",
        unit="fraction",
    )
    horizon_ns: int | None = fld(
        Opt(DurationNs()), "Horizon of the probability; set if and only if it is.", unit="ns"
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "DEST-1",
            "probability_frac and horizon_ns are both set or both null",
            lambda r: (r.probability_frac is None) == (r.horizon_ns is None),
        ),
    )


_SCORED_TOGETHER: Final[tuple[str, ...]] = ("model_status", "risk_unit_price", "expected_net_r")
_MODEL_ONLY: Final[tuple[str, ...]] = (
    "net_r_stdev",
    "expected_net_r_stderr",
    "support_count",
    "win_probability_frac",
    "expected_win_r",
    "expected_loss_r",
    "expected_cost_r",
    "expected_holding_time_ns",
    "quality_score",
    "memory_cutoff_time_ns",
    "baseline_r",
)


def _scored(candidate: Candidate) -> bool:
    return candidate.model_version_id is not None


def _quantiles_monotone(points: tuple[QuantilePoint, ...]) -> bool:
    return all(
        earlier.q_frac < later.q_frac and earlier.value_r <= later.value_r
        for earlier, later in itertools.pairwise(points)
    )


@final
@contract("Candidate", "1.0.0")
@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate(Produced):
    """Opportunity built from de-duplicated evidence, with one scorer's estimates.

    Stream: ``candidate/<producer>``.  Ordering time: ``decision_time_ns``.

    * ``opportunity_id`` is the stable identity of the opportunity (setup,
      direction, horizon, market events).  It does not change when evidence is
      revised, when another engine confirms the same event or when another model
      scores it.  ``candidate_id`` adds the scoring model and the revision, so an
      active model, a shadow model and a baseline can score the same opportunity
      side by side.
    * All ``*_r`` estimates are **net of expected costs** and expressed in R,
      where 1 R is the price distance ``risk_unit_price`` (the reference stop
      distance the model assumed).  Estimates for a concrete plan with another
      stop come from the plan scorer, not from rescaling these numbers.
    * ``net_r_stdev`` is the dispersion of a single outcome; the uncertainty of
      the *estimate* is ``expected_net_r_stderr`` with its ``support_count``.
    * An unscored candidate carries null estimates and ``unscored_reason``; it
      cannot pass an expectancy gate.
    """

    candidate_id: str = fld(Id("cnd"), "identity.candidate_id(opportunity_id, model_version_id, revision).")
    opportunity_id: str = fld(
        Id("opp"), "identity.opportunity_id(setup_type, side, horizon, market_event_ids)."
    )
    symbol: str = symbol_field("Always XAUUSD: GIE trades only gold.", gold_only=True)
    side: Side = fld(EnumOf(Side), "Direction of the opportunity.")
    setup_type: str = fld(
        Str(pattern=CODE_PATTERN, max_len=48, label="code"), "Setup name from the feature dictionary."
    )
    horizon: Horizon = fld(EnumOf(Horizon), "Scalp or intraday.")
    session: MarketSession = fld(EnumOf(MarketSession), "Broker session at decision time.")
    market_event_ids: tuple[str, ...] = fld(
        ListOf(
            Id("mev"),
            min_items=1,
            max_items=32,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Market events behind the opportunity (each counted once, whichever engines observed it).",
    )
    evidence_ids: tuple[str, ...] = fld(
        ListOf(
            Id("evd"),
            min_items=1,
            max_items=64,
            unique_key=lambda item: item,
            canonical_sort=lambda item: item,
            sorted_label="sorted ascending",
        ),
        "Evidence revisions this revision of the candidate was built from.",
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
    model_status: ModelStatus | None = fld(
        Opt(EnumOf(ModelStatus)),
        "Promotion status of that model at decision time (only ACTIVE counts as validated).",
    )
    unscored_reason: str | None = fld(
        Opt(Str(pattern=REASON_CODE_PATTERN, max_len=48, label="reason code")),
        "Why no estimate exists; set if and only if the candidate is unscored.",
    )
    risk_unit_price: Decimal | None = fld(
        Opt(PRICE),
        "Price distance that equals 1 R in every *_r field of this record.",
        unit="USD per troy ounce",
    )
    expected_net_r: float | None = fld(Opt(Float(label="R multiple")), "Expected net result.", unit="R")
    net_r_stdev: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")),
        "Predictive standard deviation of a single net result (outcome dispersion).",
        unit="R",
    )
    expected_net_r_stderr: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")),
        "Standard error of expected_net_r itself (uncertainty of the estimate).",
        unit="R",
    )
    support_count: int | None = fld(
        Opt(Int(ge=0, label="count")),
        "Effective number of historical cases behind the estimate (e.g. neighbours used).",
    )
    net_r_quantiles: tuple[QuantilePoint, ...] = fld(
        ListOf(Obj(QuantilePoint), max_items=32), "Optional predictive quantiles, ascending in q_frac."
    )
    win_probability_frac: float | None = fld(
        Opt(Float(ge=0.0, le=1.0, label="probability")),
        "Estimated probability of a positive net result.",
        unit="fraction",
    )
    expected_win_r: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")), "Expected net size of a winning outcome.", unit="R"
    )
    expected_loss_r: float | None = fld(
        Opt(Float(ge=0.0, label="R multiple")),
        "Expected net size of a losing outcome, as a positive number.",
        unit="R",
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
    memory_cutoff_time_ns: int | None = fld(
        Opt(TimeNs()),
        "Latest instant at which any retrieved historical case (neighbour) had become fully known, "
        "label included.  Prior-only retrieval means it is not later than as_of_time_ns.",
        unit="ns since epoch, UTC",
    )
    destinations: tuple[Destination, ...] = fld(
        ListOf(
            Obj(Destination),
            max_items=16,
            unique_key=lambda item: (item.scenario, item.label),
            canonical_sort=lambda item: (item.scenario, item.label),
            sorted_label="sorted by (scenario, label)",
        ),
        "Scenario price destinations.",
    )
    baseline_r: float | None = fld(
        Opt(Float(label="R multiple")),
        "Part of expected_net_r not attributed to any listed contribution.",
        unit="R",
    )
    contributions: tuple[Contribution, ...] = contributions_field(
        "Attribution of expected_net_r to evidence and features, as given by the scoring model."
    )
    valid_until_ns: int = fld(
        TimeNs(),
        "Expiry instant: the candidate must not be ranked at or after it.  It is not later than "
        "the earliest expiry of its evidence.",
        unit="ns since epoch, UTC",
    )
    revision: int = fld(Int(ge=0, label="revision"), "0 for the first record, +1 per update.")
    supersedes_candidate_id: str | None = fld(
        Opt(Id("cnd")), "Previous revision replaced by this record; null for revision 0."
    )

    INVARIANTS: ClassVar[tuple[Invariant, ...]] = (
        inv(
            "CND-1",
            "opportunity_id equals identity.opportunity_id(setup_type, side, horizon, market_event_ids)",
            lambda r: (
                r.opportunity_id
                == identity.opportunity_id(r.setup_type, r.side.value, r.horizon.value, r.market_event_ids)
            ),
        ),
        inv(
            "CND-2",
            "candidate_id equals identity.candidate_id(opportunity_id, model_version_id, revision)",
            lambda r: (
                r.candidate_id == identity.candidate_id(r.opportunity_id, r.model_version_id, r.revision)
            ),
        ),
        inv(
            "CND-3",
            "model_version_id, model_status, risk_unit_price and expected_net_r are all set (scored) "
            "or all null (unscored)",
            lambda r: all((getattr(r, name) is not None) == _scored(r) for name in _SCORED_TOGETHER),
        ),
        inv(
            "CND-4",
            "unscored_reason is set if and only if the candidate is unscored",
            lambda r: (r.unscored_reason is not None) != _scored(r),
        ),
        inv(
            "CND-5",
            "a scored candidate states its uncertainty: net_r_stdev, expected_net_r_stderr or at "
            "least two quantiles",
            lambda r: (
                not _scored(r)
                or r.net_r_stdev is not None
                or r.expected_net_r_stderr is not None
                or len(r.net_r_quantiles) >= 2
            ),
        ),
        inv(
            "CND-6",
            "an unscored candidate has no model-derived estimates, quantiles, destinations or contributions",
            lambda r: (
                _scored(r)
                or (
                    all(getattr(r, name) is None for name in _MODEL_ONLY)
                    and not r.net_r_quantiles
                    and not r.destinations
                    and not r.contributions
                )
            ),
        ),
        inv(
            "CND-7",
            "quantiles have strictly increasing q_frac and non-decreasing value_r",
            lambda r: _quantiles_monotone(r.net_r_quantiles),
        ),
        inv(
            "CND-8",
            "when status is AVAILABLE, valid_until_ns is later than decision_time_ns",
            lambda r: r.status is not CandidateStatus.AVAILABLE or r.valid_until_ns > r.decision_time_ns,
        ),
        inv(
            "CND-9",
            "status_reasons is non-empty when status is REJECTED or WITHDRAWN and empty when AVAILABLE",
            lambda r: (
                bool(r.status_reasons)
                if r.status in (CandidateStatus.REJECTED, CandidateStatus.WITHDRAWN)
                else (not r.status_reasons if r.status is CandidateStatus.AVAILABLE else True)
            ),
        ),
        inv(
            "CND-10",
            "supersedes_candidate_id is null for revision 0 and otherwise equals the id of the previous "
            "revision, identity.candidate_id(opportunity_id, model_version_id, revision - 1)",
            lambda r: (
                r.supersedes_candidate_id is None
                if r.revision == 0
                else r.supersedes_candidate_id
                == identity.candidate_id(r.opportunity_id, r.model_version_id, r.revision - 1)
            ),
        ),
        inv(
            "CND-11",
            "memory_cutoff_time_ns <= as_of_time_ns (retrieved historical cases are prior-only)",
            lambda r: r.memory_cutoff_time_ns is None or r.memory_cutoff_time_ns <= r.as_of_time_ns,
        ),
    )

    @property
    def scored(self) -> bool:
        return _scored(self)


_CANDIDATE_IMMUTABLE: Final[tuple[str, ...]] = (
    "run_id",
    "mode",
    "producer",
    "opportunity_id",
    "symbol",
    "side",
    "setup_type",
    "horizon",
    "market_event_ids",
    "model_version_id",
)


def validate_candidate_transition(previous: Candidate | None, revision: Candidate) -> None:
    """Check that *revision* may follow *previous* in the chain of one candidate."""

    def fail(rule: str) -> IllegalTransitionError:
        return IllegalTransitionError(
            f"illegal candidate transition: {rule}",
            candidate_id=revision.candidate_id,
            revision=revision.revision,
            rule=rule,
        )

    before = None if previous is None else previous.status
    if revision.status not in CANDIDATE_STATUS_TRANSITIONS[before]:
        assert before is not None
        raise fail(f"{before.value} is final; no revision may follow it")
    if previous is None:
        if revision.revision != 0:
            raise fail("the first record of a candidate has revision 0")
        return
    if revision.revision != previous.revision + 1:
        raise fail("revision must increase by exactly 1")
    if revision.supersedes_candidate_id != previous.candidate_id:
        raise fail("supersedes_candidate_id must name the previous revision")
    for name in _CANDIDATE_IMMUTABLE:
        if getattr(previous, name) != getattr(revision, name):
            raise fail(f"{name} may not change between revisions")
    for name in ("decision_time_ns", "as_of_time_ns", "seq"):
        if getattr(revision, name) < getattr(previous, name):
            raise fail(f"{name} may not decrease")
