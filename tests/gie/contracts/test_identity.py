"""Identity functions of the shared contracts (S00-ASG-22), part of acceptance test S00-ACC-02.

SYNTHETIC FIXTURE DATA ONLY.  Identifiers, hashes, labels and prices in this module are invented
test values or come from the labelled synthetic builders of ``tests/gie/samples.py``.  The account
alias used for external positions is a fixture alias, not a broker login.

Expected behaviour: ``specifications/SHARED_CONTRACTS.md`` (revision 2) section 4, the module
docstring of ``gie.contracts.identity`` and ``reviews/session_00/rework_2_change_notice.md`` section 10
(every part validated, failure only as a ``GieError``; ``run_id(mode, manifest_sha256)``; an account
alias that looks like an account number is refused):

* form ``<prefix>_<32 lower-case hex>``, the hex part being the first 128 bits of the SHA-256 of the
  canonical JSON of ``[prefix, [identity parts...]]`` - pinned below with ``hashlib`` over payloads
  written by hand (a decimal part is the object ``{"d": "<canonical decimal>"}``, see ``gie.core.ids``);
* deterministic, sensitive to every identity part, inputs validated, no live run mode.
"""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import re
from decimal import Decimal as D
from typing import Any

import pytest

from gie.contracts import (
    ModelStatus,
    OrderEventType,
    OrderState,
    RunMode,
    identity,
    to_wire,
)
from gie.core.canonical import INT64_MAX
from gie.core.errors import ContractValidationError, GieError, LiveRoutingLockedError
from gie.core.timeutil import MAX_TIME_NS
from tests.gie import samples

pytestmark = [pytest.mark.acceptance("S00-ACC-02"), pytest.mark.requirement("S00-ASG-22")]

T = 1_795_685_100_000_000_000
SHA = "ab" * 32
SHA2 = "cd" * 32


def _fixture_id(prefix: str, digit: str) -> str:
    return f"{prefix}_{digit * 32}"


RUN, RUN2 = _fixture_id("run", "1"), _fixture_id("run", "2")
MEV, MEV2, MEV3 = _fixture_id("mev", "1"), _fixture_id("mev", "2"), _fixture_id("mev", "3")
OPP, OPP2 = _fixture_id("opp", "1"), _fixture_id("opp", "2")
CND, CND2 = _fixture_id("cnd", "1"), _fixture_id("cnd", "2")
PLN, PLN2 = _fixture_id("pln", "1"), _fixture_id("pln", "2")
RSK, RSK2 = _fixture_id("rsk", "1"), _fixture_id("rsk", "2")
ORD, ORD2 = _fixture_id("ord", "1"), _fixture_id("ord", "2")
POS, POS2 = _fixture_id("pos", "1"), _fixture_id("pos", "2")
MDL, MDL2 = _fixture_id("mdl", "1"), _fixture_id("mdl", "2")

#: function name -> (id prefix, valid arguments, alternative arguments that differ in every part).
FUNCTIONS: dict[str, tuple[str, tuple[Any, ...], tuple[Any, ...]]] = {
    "run_id": ("run", ("REPLAY", SHA), ("PAPER", SHA2)),
    "market_event_id": (
        "mev",
        ("liquidity_sweep", "XAUUSD", "M5", T, (D("2648.4"),)),
        ("breakout", "XAGUSD", "M15", T + 1, (D("2648.5"),)),
    ),
    "evaluation_slot_id": (
        "mev",
        ("sweep_reversal", "liquidity_sweep", "XAUUSD", "M5", T),
        ("cavemen", "breakout", "XAGUSD", "M15", T + 1),
    ),
    "evidence_id": ("evd", ("sweep_reversal", MEV, 0), ("cavemen", MEV2, 1)),
    "opportunity_id": (
        "opp",
        ("sweep_reversal_long", "BUY", "SCALP", (MEV,)),
        ("sweep_reversal_short", "SELL", "INTRADAY", (MEV2,)),
    ),
    "candidate_id": ("cnd", (OPP, MDL, 0), (OPP2, MDL2, 1)),
    "alternative_group_id": ("grp", (OPP,), (OPP2,)),
    "plan_id": ("pln", (CND, "LIMIT", 0, 0), (CND2, "MARKET", 1, 1)),
    "risk_decision_id": ("rsk", (RUN, PLN, 0), (RUN2, PLN2, 1)),
    "reservation_id": ("rsv", (RSK,), (RSK2,)),
    "client_order_id": ("ord", (RUN, RSK, "ENTRY", 0), (RUN2, POS, "PROTECTIVE_STOP", 1)),
    "broker_order_id": ("brk", (ORD,), (ORD2,)),
    "order_event_id": ("oev", (ORD, 0), (ORD2, 1)),
    "position_id": ("pos", (RUN, ORD), (RUN2, ORD2)),
    "external_position_id": (
        "pos",
        ("fixture-account", "FIXTURE-TICKET-1"),
        ("fixture-account-2", "FIXTURE-TICKET-2"),
    ),
    "model_version_id": ("mdl", ("fixture_net_r", "0.1.0", SHA), ("fixture_shadow", "0.2.0", SHA2)),
}


def _call(name: str, args: tuple[Any, ...]) -> str:
    result: str = getattr(identity, name)(*args)
    return result


def _reference(prefix: str, canonical_payload: str) -> str:
    """Independent computation: first 128 bits of the SHA-256 of the canonical JSON payload."""
    return f"{prefix}_{hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest()[:32]}"


# =========================================================================== all functions
def test_the_table_covers_every_function_of_the_identity_module() -> None:
    public = {
        name
        for name, member in inspect.getmembers(identity, inspect.isfunction)
        if member.__module__ == identity.__name__ and not name.startswith("_")
    }
    assert public == set(FUNCTIONS)
    assert len(public) == 16


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_identifier_form_and_determinism(name: str) -> None:
    prefix, args, other = FUNCTIONS[name]
    first = _call(name, args)
    assert re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", first)
    assert _call(name, args) == first
    assert _call(name, tuple(args)) == first
    assert _call(name, other) != first
    assert _call(name, other) == _call(name, other)


def _sensitivity_cases() -> list[Any]:
    cases = []
    for name, (_prefix, args, other) in sorted(FUNCTIONS.items()):
        assert len(args) == len(other)
        for index in range(len(args)):
            changed = (*args[:index], other[index], *args[index + 1 :])
            cases.append(pytest.param(name, args, changed, id=f"{name}-part{index}"))
    return cases


@pytest.mark.parametrize(("name", "args", "changed"), _sensitivity_cases())
def test_identifier_depends_on_every_identity_part(
    name: str, args: tuple[Any, ...], changed: tuple[Any, ...]
) -> None:
    assert args != changed
    assert _call(name, changed) != _call(name, args)


def test_identifiers_of_different_kinds_never_collide() -> None:
    produced = [_call(name, args) for name, (_prefix, args, _other) in FUNCTIONS.items()]
    produced += [_call(name, other) for name, (_prefix, _args, other) in FUNCTIONS.items()]
    assert len(set(produced)) == len(produced)


@pytest.mark.parametrize(("left", "right"), [((1, 0), (0, 1)), ((1, 1), (11, 0)), ((0, 10), (1, 0))])
def test_integer_parts_are_not_concatenated(left: tuple[int, int], right: tuple[int, int]) -> None:
    assert identity.plan_id(CND, "LIMIT", *left) != identity.plan_id(CND, "LIMIT", *right)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("ab", "c"), ("a", "bc")),
        (("a:b", "c"), ("a", "b:c")),
        (("a", "b.c"), ("a.b", "c")),
        (("a-", "b"), ("a", "-b")),
    ],
)
def test_text_parts_are_not_concatenated(first: tuple[str, str], second: tuple[str, str]) -> None:
    assert identity.external_position_id(*first) != identity.external_position_id(*second)


# =========================================================================== pinned values
def test_pinned_run_id() -> None:
    payload = '["run",["REPLAY","' + "0" * 64 + '"]]'
    assert identity.run_id("REPLAY", "0" * 64) == _reference("run", payload)
    assert identity.run_id(RunMode.REPLAY, "0" * 64) == _reference("run", payload)
    assert identity.run_id("PAPER", "0" * 64) == _reference("run", '["run",["PAPER","' + "0" * 64 + '"]]')


def _manifest_content_sha256(wire: dict[str, Any]) -> str:
    """Independent computation of the manifest hash: canonical JSON of every field except run_id."""
    content = {
        key: value for key, value in wire.items() if key not in ("run_id", "contract", "schema_version")
    }
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_sample_run_id_is_derived_from_the_sample_manifest() -> None:
    manifest = samples.run_manifest()
    wire = to_wire(manifest)
    expected = identity.run_id(manifest.mode, _manifest_content_sha256(wire))
    assert manifest.run_id == samples.RUN_ID == expected
    # Any other manifest content is another run.
    other = _manifest_content_sha256({**wire, "seed": wire["seed"] + 1})
    assert identity.run_id(manifest.mode, other) != samples.RUN_ID
    assert identity.run_id("PAPER", _manifest_content_sha256(wire)) != samples.RUN_ID


def test_pinned_candidate_id_with_and_without_scorer() -> None:
    assert identity.candidate_id(OPP, None, 3) == _reference("cnd", f'["cnd",["{OPP}",null,3]]')
    assert identity.candidate_id(OPP, MDL, 0) == _reference("cnd", f'["cnd",["{OPP}","{MDL}",0]]')


def test_pinned_client_order_id_and_its_descendants() -> None:
    order = _reference("ord", f'["ord",["{RUN}","{RSK}","ENTRY",0]]')
    assert identity.client_order_id(RUN, RSK, "ENTRY", 0) == order
    assert identity.order_event_id(order, 12) == _reference("oev", f'["oev",["{order}",12]]')
    assert identity.broker_order_id(order) == _reference("brk", f'["brk",["{order}"]]')
    assert identity.position_id(RUN, order) == _reference("pos", f'["pos",["{RUN}","{order}"]]')


def test_pinned_market_event_id_sorts_and_de_duplicates_identity_prices() -> None:
    payload = f'["mev",["liquidity_sweep","XAUUSD","M5",{T},[{{"d":"2647.9"}},{{"d":"2648.4"}}]]]'
    assert T > 2**53  # nanosecond instants are written as exact integer digits
    prices = [D("2648.40"), D("2647.9"), D("2648.4")]
    assert identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, prices) == _reference(
        "mev", payload
    )
    assert identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, []) == _reference(
        "mev", f'["mev",["liquidity_sweep","XAUUSD","M5",{T},[]]]'
    )


def test_pinned_evaluation_slot_external_position_and_remaining_ids() -> None:
    assert identity.evaluation_slot_id("orb_vwap", "breakout", "XAUUSD", "M5", T) == _reference(
        "mev", f'["mev",["slot","orb_vwap","breakout","XAUUSD","M5",{T}]]'
    )
    assert identity.external_position_id("fixture-account", "FIXTURE-TICKET-1") == _reference(
        "pos", '["pos",["external","fixture-account","FIXTURE-TICKET-1"]]'
    )
    assert identity.evidence_id("sweep_reversal", MEV, 2) == _reference(
        "evd", f'["evd",["sweep_reversal","{MEV}",2]]'
    )
    assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", [MEV2, MEV]) == _reference(
        "opp", f'["opp",["sweep_reversal_long","BUY","SCALP",["{MEV}","{MEV2}"]]]'
    )
    assert identity.alternative_group_id(OPP) == _reference("grp", f'["grp",["{OPP}"]]')
    assert identity.plan_id(CND, "LIMIT", 1, 2) == _reference("pln", f'["pln",["{CND}","LIMIT",1,2]]')
    assert identity.risk_decision_id(RUN, PLN, 4) == _reference("rsk", f'["rsk",["{RUN}","{PLN}",4]]')
    assert identity.reservation_id(RSK) == _reference("rsv", f'["rsv",["{RSK}"]]')
    assert identity.model_version_id("fixture_net_r", "0.1.0", SHA) == _reference(
        "mdl", f'["mdl",["fixture_net_r","0.1.0","{SHA}"]]'
    )


# =========================================================================== input validation
_BAD_IDS = ["", "x", None, 7, b"mev_" + b"1" * 32]


def _bad_id_variants(valid: str) -> list[Any]:
    prefix = valid[:3]
    return [
        valid[:-1],
        valid + "0",
        valid.upper(),
        prefix + "_" + "g" * 32,
        prefix + "-" + "1" * 32,
        valid + "\n",
        " " + valid,
        "zzz_" + "1" * 32,
        *_BAD_IDS,
    ]


def _invalid_cases() -> list[Any]:
    cases: list[tuple[str, tuple[Any, ...]]] = []

    def replace(name: str, index: int, values: list[Any]) -> None:
        args = FUNCTIONS[name][1]
        cases.extend((name, (*args[:index], value, *args[index + 1 :])) for value in values)

    bad_counts = [-1, -(2**63), True, False, 1.0, 0.0, "0", None, D("1")]
    bad_text = ["", None, 5, "line\nbreak", "tab\there", "nul\x00", b"bytes"]
    bad_sha = [
        SHA.upper(),
        SHA[:-1],
        SHA + "0",
        "g" * 64,
        SHA + "\n",
        " " + SHA[1:],
        "",
        None,
        123,
        bytes(32),
    ]

    # ---- wrong-prefix and malformed identifiers
    replace("evidence_id", 1, [OPP, *_bad_id_variants(MEV)])
    replace("opportunity_id", 3, [(), [], (OPP,), (MEV, RUN), (MEV, None), MEV, [MEV.upper()], [[MEV]]])
    replace("candidate_id", 0, [MEV, CND, *_bad_id_variants(OPP)])
    replace("candidate_id", 1, [OPP, "none", "", 0, MDL.upper(), MDL[:-1]])
    replace("alternative_group_id", 0, [CND, MEV, *_bad_id_variants(OPP)])
    replace("plan_id", 0, [OPP, PLN, *_bad_id_variants(CND)])
    replace("risk_decision_id", 0, [PLN, RSK, *_bad_id_variants(RUN)])
    replace("risk_decision_id", 1, [CND, RUN, *_bad_id_variants(PLN)])
    replace("reservation_id", 0, [PLN, ORD, *_bad_id_variants(RSK)])
    replace("client_order_id", 0, [RSK, POS, *_bad_id_variants(RUN)])
    replace(
        "client_order_id",
        1,
        [PLN, ORD, RUN, CND, OPP, MDL, "rsk_", "pos_", "rsk_bad", *_bad_id_variants(RSK)],
    )
    replace("client_order_id", 1, [POS[:-1], POS.upper(), POS + "\n", ["rsk_"], ("pos_",)])
    replace("broker_order_id", 0, [RSK, POS, *_bad_id_variants(ORD)])
    replace("order_event_id", 0, [RSK, POS, *_bad_id_variants(ORD)])
    replace("position_id", 0, [ORD, *_bad_id_variants(RUN)])
    replace("position_id", 1, [RSK, RUN, *_bad_id_variants(ORD)])
    # ---- counts
    replace("market_event_id", 3, bad_counts)
    replace("evaluation_slot_id", 4, bad_counts)
    replace("evidence_id", 2, bad_counts)
    replace("candidate_id", 2, bad_counts)
    replace("plan_id", 2, bad_counts)
    replace("plan_id", 3, bad_counts)
    replace("risk_decision_id", 2, bad_counts)
    replace("client_order_id", 3, bad_counts)
    replace("order_event_id", 1, bad_counts)
    # ---- text parts
    for index in (0, 1, 2):
        replace("market_event_id", index, bad_text)
        replace("opportunity_id", index, bad_text)
    for index in (0, 1, 2, 3):
        replace("evaluation_slot_id", index, bad_text)
    replace("evidence_id", 0, bad_text)
    replace("plan_id", 1, bad_text)
    replace("client_order_id", 2, bad_text)
    replace("model_version_id", 0, bad_text)
    replace("model_version_id", 1, bad_text)
    # ---- prices
    replace(
        "market_event_id",
        4,
        [[2648.4], [2648], ["2648.4"], [None], [D("1"), 1.5], [True], [{}], [[D("1")]], [D("1"), "x"]],
    )
    # ---- hashes and labels
    replace("run_id", 1, bad_sha)
    replace("model_version_id", 2, bad_sha)
    # ---- external references
    bad_reference = ["", "has space", "a/b", "a\\b", "a" * 65, "b\n", "\tb", "café", "a,b", None, 5, b"a"]
    replace("external_position_id", 0, bad_reference)
    replace("external_position_id", 1, bad_reference)
    return [pytest.param(name, args, id=f"{name}-{index}") for index, (name, args) in enumerate(cases)]


@pytest.mark.parametrize(("name", "args"), _invalid_cases())
def test_invalid_input_is_refused_with_a_contract_error(name: str, args: tuple[Any, ...]) -> None:
    with pytest.raises(ContractValidationError) as caught:
        _call(name, args)
    assert caught.value.code == "GIE.CONTRACT.INVALID"


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_the_valid_arguments_of_the_table_are_accepted(name: str) -> None:
    # Guards the test above: its cases differ from these accepted arguments in exactly one part.
    _prefix, args, other = FUNCTIONS[name]
    assert _call(name, args).startswith(FUNCTIONS[name][0] + "_")
    assert _call(name, other).startswith(FUNCTIONS[name][0] + "_")


@pytest.mark.parametrize(
    "prices",
    [
        pytest.param([D("NaN")], id="nan"),
        pytest.param([D("NaN"), D("2648.4")], id="nan-and-price"),
        pytest.param([D("2648.4"), D("NaN"), D("2647.9")], id="nan-between-prices"),
        pytest.param([D("sNaN")], id="signalling-nan"),
        pytest.param([D("Infinity")], id="infinity"),
        pytest.param([D("-Infinity"), D("1")], id="negative-infinity"),
        pytest.param([D("1E+30")], id="beyond-18-integer-digits"),
        pytest.param([D("1E-20")], id="beyond-12-fraction-digits"),
    ],
)
def test_non_finite_or_unrepresentable_identity_price_is_refused_with_a_gie_error(prices: list[D]) -> None:
    # Section 2: NaN and infinities are invalid everywhere.  Section 5: all errors derive from GieError
    # (a decimal.InvalidOperation or TypeError escaping from here is not a contract error).
    with pytest.raises(GieError):
        identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, prices)


# =========================================================================== every part validated (rework 2)
#: Well-formed strings outside the vocabulary of the record field each part comes from.
WRONG_VOCABULARY: dict[tuple[str, int], list[str]] = {}
_KIND = ["Liquidity_sweep", "l", "liquidity-sweep", "1sweep", "liquidity sweep", "a" * 49, "LIQUIDITY_SWEEP"]
_SYMBOL = ["xauusd", "XAU/USD", "XAU USD", "_XAU", "-XAU", "X" * 25, "XAUüSD"]
_TERM = ["m5", "5M", "M-5", "M 5", "M" * 49, "_M5"]
_ENGINE = ["Sweep_reversal", "s", "a" * 33, "sweep.reversal", "sweep-reversal", "9sweep"]
for _index, _values in enumerate((_KIND, _SYMBOL, _TERM)):
    WRONG_VOCABULARY[("market_event_id", _index)] = _values
    WRONG_VOCABULARY[("evaluation_slot_id", _index + 1)] = _values
WRONG_VOCABULARY[("evaluation_slot_id", 0)] = _ENGINE
WRONG_VOCABULARY[("evidence_id", 0)] = _ENGINE
WRONG_VOCABULARY[("opportunity_id", 0)] = _KIND
WRONG_VOCABULARY[("opportunity_id", 1)] = ["buy", "Buy", "B-UY", "BUY SELL", "1BUY"]
WRONG_VOCABULARY[("opportunity_id", 2)] = ["scalp", "Scalp", "SCALP-1", "S" * 49]
WRONG_VOCABULARY[("plan_id", 1)] = ["limit", "Limit", "LIMIT ORDER", "LIMIT-1"]
WRONG_VOCABULARY[("client_order_id", 2)] = ["entry", "PROTECTIVE-STOP", "Entry", "ENTRY "]
WRONG_VOCABULARY[("model_version_id", 0)] = _KIND
WRONG_VOCABULARY[("model_version_id", 1)] = [
    "1.0",
    "01.0.0",
    "1.00.0",
    "v1.0.0",
    "1.0.0-rc1",
    "1.0.0.0",
    "1234567890.0.0",
    "1.0.0 ",
    "-1.0.0",
]


@pytest.mark.parametrize(
    ("name", "index", "value"),
    [
        pytest.param(name, index, value, id=f"{name}-part{index}-{number}")
        for (name, index), values in sorted(WRONG_VOCABULARY.items())
        for number, value in enumerate(values)
    ],
)
def test_a_part_outside_its_field_vocabulary_is_refused(name: str, index: int, value: str) -> None:
    args = FUNCTIONS[name][1]
    with pytest.raises(ContractValidationError) as caught:
        _call(name, (*args[:index], value, *args[index + 1 :]))
    assert caught.value.code == "GIE.CONTRACT.INVALID"


class _Opaque:
    """An arbitrary object that is none of the accepted part types."""


#: Hostile values for any part.  None of them is a valid value of any part of any function.
HOSTILE_PARTS: list[Any] = [
    _Opaque(),
    1.5,
    float("nan"),
    float("inf"),
    -float("inf"),
    D("NaN"),
    D("sNaN"),
    D("Infinity"),
    D("-Infinity"),
    -1,
    2**63,
    2**64,
    10**30,
    -(2**70),
    True,
    b"x",
    bytearray(b"x"),
    {"a": 1},
    "\x00",
    "é",
    "A" * 10_000,
    _fixture_id("mev", "1") + "\x00",
]


@pytest.mark.parametrize(
    ("name", "index"),
    [
        pytest.param(name, index, id=f"{name}-part{index}")
        for name, (_prefix, args, _other) in sorted(FUNCTIONS.items())
        for index in range(len(args))
    ],
)
def test_every_part_of_every_function_fails_only_with_a_gie_error(name: str, index: int) -> None:
    args = FUNCTIONS[name][1]
    for value in HOSTILE_PARTS:
        # pytest.raises(GieError) lets any other exception (TypeError, decimal.InvalidOperation, ...)
        # through, which fails the test.
        with pytest.raises(GieError):
            _call(name, (*args[:index], value, *args[index + 1 :]))


#: (function, index of the count part, largest accepted value)
COUNT_PARTS = [
    ("market_event_id", 3, MAX_TIME_NS),
    ("evaluation_slot_id", 4, MAX_TIME_NS),
    ("evidence_id", 2, INT64_MAX),
    ("candidate_id", 2, INT64_MAX),
    ("plan_id", 2, INT64_MAX),
    ("plan_id", 3, INT64_MAX),
    ("risk_decision_id", 2, INT64_MAX),
    ("client_order_id", 3, INT64_MAX),
    ("order_event_id", 1, INT64_MAX),
]


@pytest.mark.parametrize(
    ("name", "index", "maximum"), [pytest.param(*case, id=f"{case[0]}-part{case[1]}") for case in COUNT_PARTS]
)
def test_integer_parts_are_bounded_at_both_ends(name: str, index: int, maximum: int) -> None:
    args = FUNCTIONS[name][1]

    def call(value: object) -> str:
        return _call(name, (*args[:index], value, *args[index + 1 :]))

    assert call(0) != call(maximum)
    assert re.fullmatch(r"[a-z]{3}_[0-9a-f]{32}", call(maximum))
    for value in (-1, maximum + 1, 2**64, -(2**63)):
        with pytest.raises(ContractValidationError):
            call(value)


@pytest.mark.parametrize(
    "price",
    [D("NaN"), D("-NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), D("1E+18"), D("1E-13")],
    ids=str,
)
def test_a_non_finite_or_unrepresentable_price_fails_with_a_contract_error(price: D) -> None:
    for prices in ([price], [D("2648.4"), price], (price, price), iter([price])):
        with pytest.raises(ContractValidationError):
            identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, prices)


def test_the_extreme_representable_prices_are_accepted() -> None:
    for price in (D("0.000000000001"), D("999999999999999999.999999999999"), D("-0.000000000001")):
        assert identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, [price]).startswith("mev_")


# =========================================================================== scope of each identifier
#: Module docstring table, last column: what the identifier names and where it is unique.
DOCUMENTED_SCOPE = {
    "run_id": "run, everywhere",
    "market_event_id": "event, everywhere",
    "evaluation_slot_id": "slot, everywhere",
    "evidence_id": "record, one run",
    "opportunity_id": "opportunity, one run",
    "candidate_id": "record, one run",
    "alternative_group": "group, one run",
    "plan_id": "record, one run",
    "risk_decision_id": "record, everywhere",
    "reservation_id": "record, everywhere",
    "client_order_id": "order, everywhere",
    "broker_order_id": "order, everywhere",
    "order_event_id": "record, everywhere",
    "position_id": "position, everywhere",
    "model_version_id": "model, everywhere",
}


def test_the_module_docstring_states_the_scope_of_every_identifier() -> None:
    doc = identity.__doc__ or ""
    found: dict[str, str] = {}
    for line in doc.splitlines():
        match = re.match(r"^(\w+)\s.*\s(\w+, (?:one run|everywhere))\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    assert found == DOCUMENTED_SCOPE
    assert "(run_id, id)" in doc


def _chain(run: str) -> dict[str, str]:
    """Every identifier of one opportunity, derived for *run* from the same market data."""
    event = identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, [D("2648.4")])
    slot = identity.evaluation_slot_id("orb_vwap", "breakout", "XAUUSD", "M5", T)
    opportunity = identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", [event])
    candidate = identity.candidate_id(opportunity, MDL, 0)
    plan = identity.plan_id(candidate, "LIMIT", 0, 0)
    decision = identity.risk_decision_id(run, plan, 0)
    order = identity.client_order_id(run, decision, "ENTRY", 0)
    return {
        "market_event_id": event,
        "evaluation_slot_id": slot,
        "evidence_id": identity.evidence_id("sweep_reversal", event, 0),
        "opportunity_id": opportunity,
        "candidate_id": candidate,
        "alternative_group": identity.alternative_group_id(opportunity),
        "plan_id": plan,
        "model_version_id": identity.model_version_id("fixture_net_r", "0.1.0", SHA),
        "external_position_id": identity.external_position_id("fixture-account", "987654321"),
        "risk_decision_id": decision,
        "reservation_id": identity.reservation_id(decision),
        "client_order_id": order,
        "broker_order_id": identity.broker_order_id(order),
        "order_event_id": identity.order_event_id(order, 0),
        "position_id": identity.position_id(run, order),
    }


def test_identifiers_without_a_run_are_equal_across_runs_and_the_others_differ() -> None:
    first, second = _chain(identity.run_id("REPLAY", SHA)), _chain(identity.run_id("PAPER", SHA))
    # Derived from what they describe: the same in every run (market events, slots, models) ...
    shared = {"market_event_id", "evaluation_slot_id", "model_version_id", "external_position_id"}
    # ... or containing no run although named per run: such a record is keyed by (run_id, id).
    per_run_key = {name for name, scope in DOCUMENTED_SCOPE.items() if scope.endswith("one run")}
    contains_run = set(first) - shared - per_run_key
    assert contains_run == {
        "risk_decision_id",
        "reservation_id",
        "client_order_id",
        "broker_order_id",
        "order_event_id",
        "position_id",
    }
    for name in shared | per_run_key:
        assert first[name] == second[name], name
    for name in contains_run:
        assert first[name] != second[name], name
        assert DOCUMENTED_SCOPE[name].endswith("everywhere")


def test_records_of_two_runs_over_the_same_data_share_the_run_free_identifiers() -> None:
    other_run = identity.run_id("REPLAY", SHA2)
    assert other_run != samples.RUN_ID
    for build, field in (
        (samples.evidence, "evidence_id"),
        (samples.candidate, "candidate_id"),
        (samples.trade_plan, "plan_id"),
    ):
        assert getattr(build(), field) == getattr(build(run_id=other_run), field)


# =========================================================================== run_id and the routing lock
@pytest.mark.requirement("G08-03", "G08-10")
@pytest.mark.parametrize(
    "mode",
    [
        "LIVE",
        "live",
        "Live",
        "REAL",
        "DEMO",
        "PRODUCTION",
        "replay",
        "paper",
        " REPLAY",
        "PAPER ",
        "",
        None,
        0,
        1,
        True,
    ],
)
def test_run_id_refuses_every_mode_that_is_not_replay_or_paper(mode: Any) -> None:
    with pytest.raises(LiveRoutingLockedError) as caught:
        identity.run_id(mode, SHA)
    assert caught.value.code == "GIE.ROUTING.LIVE_LOCKED"
    assert isinstance(caught.value, GieError)


@pytest.mark.requirement("G08-03", "G08-10")
def test_run_id_accepts_exactly_the_two_run_modes() -> None:
    assert [mode.value for mode in RunMode] == ["REPLAY", "PAPER"]
    replay, paper = identity.run_id("REPLAY", SHA), identity.run_id("PAPER", SHA)
    assert replay != paper
    assert identity.run_id(RunMode.REPLAY, SHA) == replay
    assert identity.run_id(RunMode.PAPER, SHA) == paper


@pytest.mark.parametrize("manifest_sha256", ["0" * 64, "f" * 64, "0123456789abcdef" * 4, SHA, SHA2])
def test_run_id_accepts_any_lower_case_sha256(manifest_sha256: str) -> None:
    assert re.fullmatch(r"run_[0-9a-f]{32}", identity.run_id("REPLAY", manifest_sha256))
    assert identity.run_id("REPLAY", manifest_sha256) == _reference(
        "run", f'["run",["REPLAY","{manifest_sha256}"]]'
    )


def test_run_id_takes_the_mode_and_the_manifest_hash_only() -> None:
    assert list(inspect.signature(identity.run_id).parameters) == ["mode", "manifest_sha256"]


# =========================================================================== market events
def test_market_event_id_ignores_order_and_duplicates_of_identity_prices() -> None:
    prices = [D("2651.90"), D("2648.10"), D("2650.00")]
    expected = identity.market_event_id("opening_range", "XAUUSD", "M5", T, prices)
    for permutation in itertools.permutations(prices):
        assert identity.market_event_id("opening_range", "XAUUSD", "M5", T, permutation) == expected
        assert (
            identity.market_event_id("opening_range", "XAUUSD", "M5", T, [*permutation, *prices]) == expected
        )
    assert identity.market_event_id("opening_range", "XAUUSD", "M5", T, iter(prices)) == expected
    assert identity.market_event_id("opening_range", "XAUUSD", "M5", T, set(prices)) == expected
    assert (
        identity.market_event_id("opening_range", "XAUUSD", "M5", T, (p for p in reversed(prices)))
        == expected
    )


def test_market_event_id_treats_numerically_equal_prices_as_the_same_level() -> None:
    # Two engines must obtain the same id for the same level whether they hold 2648.4 or 2648.40.
    plain = identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, [D("2648.4")])
    for same in (D("2648.40"), D("2648.4000"), D("26484E-1"), D("2.6484E+3")):
        assert identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, [same]) == plain
        assert identity.market_event_id("liquidity_sweep", "XAUUSD", "M5", T, [same, D("2648.4")]) == plain
    assert identity.market_event_id("zero", "XAUUSD", "M5", T, [D("0")]) == identity.market_event_id(
        "zero", "XAUUSD", "M5", T, [D("-0.00")]
    )


def test_market_event_id_distinguishes_different_sets_of_identity_prices() -> None:
    def build(*prices: str) -> str:
        return identity.market_event_id("opening_range", "XAUUSD", "M5", T, [D(p) for p in prices])

    distinct = [
        build(),
        build("2648.1"),
        build("2651.9"),
        build("2648.1", "2651.9"),
        build("2648.1", "2651.9", "2650"),
        build("2648.10001"),
        build("-2648.1"),
        build("26481"),
    ]
    assert len(set(distinct)) == len(distinct)


def test_two_engines_reporting_the_same_event_obtain_the_same_market_event_id() -> None:
    variants = samples.variants()["Evidence"]
    first, second = variants["active_bullish"], variants["same_event_other_engine"]
    assert first.engine != second.engine
    assert first.producer != second.producer
    assert first.market_event_id == second.market_event_id
    assert first.evidence_id != second.evidence_id
    assert first.market_event_id == identity.market_event_id(
        first.kind,
        first.symbol,
        first.timeframe.value,
        first.anchor_time_ns,
        [level.price for level in first.levels],
    )


def test_non_identity_levels_do_not_take_part_in_the_market_event_id() -> None:
    observed = samples.variants()["Evidence"]["intermarket_real_yield"]
    assert [level.identity for level in observed.levels] == [False]
    assert observed.market_event_id == identity.market_event_id(
        observed.kind, observed.symbol, observed.timeframe.value, observed.anchor_time_ns, []
    )


def test_evaluation_slot_id_differs_per_engine_and_from_the_market_event() -> None:
    base = ("breakout", "XAUUSD", "M5", T)
    slots = {engine: identity.evaluation_slot_id(engine, *base) for engine in ("orb_vwap", "cavemen", "slot")}
    assert len(set(slots.values())) == 3
    event = identity.market_event_id(*base, [])
    assert event not in slots.values()
    # A market event whose kind happens to be the word "slot" is still not a slot.
    assert identity.market_event_id("slot", "XAUUSD", "M5", T, []) not in slots.values()
    # Parts shifted by one position cannot even be formed: "breakout" is not a symbol (rework 2).
    with pytest.raises(ContractValidationError):
        identity.market_event_id("slot", "breakout", "XAUUSD", T, [])


def test_inactive_and_missing_evidence_carry_the_engine_scoped_slot() -> None:
    variants = samples.variants()["Evidence"]
    for key in ("inactive", "missing"):
        record = variants[key]
        assert record.market_event_id == identity.evaluation_slot_id(
            record.engine, record.kind, record.symbol, record.timeframe.value, record.anchor_time_ns
        )
        other_engine = identity.evaluation_slot_id(
            "another_engine", record.kind, record.symbol, record.timeframe.value, record.anchor_time_ns
        )
        assert other_engine != record.market_event_id


# =========================================================================== opportunities, candidates, plans
def test_opportunity_id_ignores_order_and_duplicates_of_market_events() -> None:
    events = [MEV, MEV2, MEV3]
    expected = identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", events)
    for permutation in itertools.permutations(events):
        assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", permutation) == expected
        assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", [*permutation, MEV]) == expected
    assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", iter(events)) == expected
    assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", [MEV, MEV2]) != expected
    assert identity.opportunity_id("sweep_reversal_long", "BUY", "SCALP", [MEV]) != expected


def test_opportunity_id_is_independent_of_evidence_revisions_and_confirming_engines() -> None:
    variants = samples.variants()["Evidence"]
    revision0, revision1 = variants["active_bullish"], variants["invalidated_revision"]
    other_engine = variants["same_event_other_engine"]
    assert revision1.revision == revision0.revision + 1
    assert len({revision0.evidence_id, revision1.evidence_id, other_engine.evidence_id}) == 3
    built_on = [
        samples.candidate(market_event_ids=(evidence.market_event_id,), evidence_ids=(evidence.evidence_id,))
        for evidence in (revision0, revision1, other_engine)
    ]
    built_on.append(
        samples.candidate(
            market_event_ids=(revision0.market_event_id,),
            evidence_ids=(revision0.evidence_id, other_engine.evidence_id),
            contributions=(),
        )
    )
    assert len({candidate.opportunity_id for candidate in built_on}) == 1
    assert len({candidate.candidate_id for candidate in built_on}) == 1
    assert built_on[0].opportunity_id == identity.opportunity_id(
        "sweep_reversal_long", "BUY", "SCALP", [revision0.market_event_id]
    )


def test_candidate_id_differs_per_scorer_and_per_revision() -> None:
    scorers = [None, MDL, MDL2]
    produced = {identity.candidate_id(OPP, scorer, revision) for scorer in scorers for revision in range(4)}
    assert len(produced) == 12
    variants = samples.variants()["Candidate"]
    scored, shadow, rejected = (
        variants["scored"],
        variants["scored_by_shadow_model"],
        variants["rejected_revision"],
    )
    assert shadow.model_status is ModelStatus.SHADOW
    assert scored.opportunity_id == shadow.opportunity_id == rejected.opportunity_id
    assert scored.model_version_id != shadow.model_version_id
    assert len({scored.candidate_id, shadow.candidate_id, rejected.candidate_id}) == 3
    assert rejected.supersedes_candidate_id == scored.candidate_id
    unscored = variants["unscored"]
    assert unscored.model_version_id is None
    assert unscored.candidate_id == identity.candidate_id(unscored.opportunity_id, None, 0)


def test_alternative_group_id_depends_only_on_the_opportunity() -> None:
    variants = samples.variants()
    shadow = variants["Candidate"]["scored_by_shadow_model"]
    rejected = variants["Candidate"]["rejected_revision"]
    plans = [
        *variants["TradePlan"].values(),
        samples.trade_plan(candidate_id=shadow.candidate_id),  # another scorer
        samples.trade_plan(candidate_id=rejected.candidate_id),  # another candidate revision
        samples.trade_plan(revision=1),  # another plan revision
        samples.trade_plan(variant=7),
    ]
    assert len({plan.opportunity_id for plan in plans}) == 1
    assert len({plan.alternative_group_id for plan in plans}) == 1
    assert plans[0].alternative_group_id == identity.alternative_group_id(plans[0].opportunity_id)
    assert len({plan.plan_id for plan in plans}) == len(plans)
    other = samples.unscored_candidate()
    assert other.opportunity_id != plans[0].opportunity_id
    assert identity.alternative_group_id(other.opportunity_id) != plans[0].alternative_group_id


# =========================================================================== orders and positions
@pytest.mark.parametrize("origin", [RSK, POS])
def test_client_order_id_accepts_a_risk_decision_or_a_position_origin(origin: str) -> None:
    assert re.fullmatch(r"ord_[0-9a-f]{32}", identity.client_order_id(RUN, origin, "ENTRY", 0))


@pytest.mark.parametrize(
    "origin", [RUN, MEV, OPP, CND, PLN, ORD, MDL, _fixture_id("evd", "1"), _fixture_id("grp", "1")]
)
def test_client_order_id_refuses_every_other_origin(origin: str) -> None:
    with pytest.raises(ContractValidationError, match="risk decision id or a position id"):
        identity.client_order_id(RUN, origin, "ENTRY", 0)


def test_entry_order_id_is_derived_from_its_risk_decision() -> None:
    entry = samples.order_event()
    assert entry.risk_decision_id is not None
    assert entry.client_order_id == identity.client_order_id(entry.run_id, entry.risk_decision_id, "ENTRY", 0)
    # A re-sent request recomputes the same key; another approval gives another order.
    assert samples.order_event().client_order_id == entry.client_order_id
    other_decision = samples.risk_decision(attempt=1)
    assert other_decision.decision_id != entry.risk_decision_id
    assert (
        identity.client_order_id(entry.run_id, other_decision.decision_id, "ENTRY", 0)
        != entry.client_order_id
    )


def test_protective_order_id_is_derived_from_its_position_and_attempt() -> None:
    stop = samples.protective_stop_lifecycle()[0]
    assert stop.position_id is not None
    assert stop.client_order_id == identity.client_order_id(
        stop.run_id, stop.position_id, "PROTECTIVE_STOP", 0
    )
    attempts = {
        identity.client_order_id(stop.run_id, stop.position_id, "PROTECTIVE_STOP", n) for n in range(5)
    }
    assert len(attempts) == 5
    assert identity.client_order_id(stop.run_id, stop.position_id, "TAKE_PROFIT", 0) not in attempts


def test_order_events_of_one_order_share_the_order_id_and_differ_in_event_id() -> None:
    for chain in (samples.order_lifecycle(), samples.protective_stop_lifecycle()):
        assert len({event.client_order_id for event in chain}) == 1
        assert len({event.order_event_id for event in chain}) == len(chain)
        for event in chain:
            assert event.order_event_id == identity.order_event_id(event.client_order_id, event.order_seq)
            if event.broker_order_id is not None:
                assert event.broker_order_id == identity.broker_order_id(event.client_order_id)


def test_gie_position_id_is_derived_from_run_and_opening_order() -> None:
    for position in samples.position_lifecycle():
        assert position.opening_client_order_id is not None
        assert position.position_id == identity.position_id(position.run_id, position.opening_client_order_id)
    assert len({position.position_id for position in samples.position_lifecycle()}) == 1


def test_external_position_id_uses_the_alias_and_never_collides_with_gie_positions() -> None:
    external = samples.external_position()
    assert external.external_ref is not None
    assert external.position_id == identity.external_position_id("fixture-account", external.external_ref)
    assert external.position_id != identity.external_position_id("other-alias", external.external_ref)
    assert external.position_id != samples.position().position_id
    assert identity.external_position_id("external", "x") != identity.external_position_id("x", "external")


@pytest.mark.parametrize("reference", ["A", "z", "0", "a" * 64, "FIXTURE-TICKET-1", "a.b_c:d-e", "1:2:3"])
def test_external_position_id_accepts_the_reference_pattern(reference: str) -> None:
    # Rework 2: the alias may no longer be digits only ("0"), so the alias side is checked separately.
    assert re.fullmatch(r"pos_[0-9a-f]{32}", identity.external_position_id("fixture-account", reference))
    if not reference.isdigit():
        assert re.fullmatch(r"pos_[0-9a-f]{32}", identity.external_position_id(reference, reference))


@pytest.mark.parametrize(
    "alias",
    ["fixture-account", "acct1234", "a1234b5678", "1:2:3", "1234-5678", "desk_7", "x9999", "A.1234.B"],
)
def test_external_position_id_accepts_an_ordinary_alias_with_a_numeric_ticket(alias: str) -> None:
    for ticket in ("987654321", "1", "0000000000000000000000000001"):
        assert re.fullmatch(r"pos_[0-9a-f]{32}", identity.external_position_id(alias, ticket))
    assert identity.external_position_id(alias, "987654321") == _reference(
        "pos", f'["pos",["external","{alias}","987654321"]]'
    )


@pytest.mark.parametrize(
    "alias",
    [
        pytest.param("0", id="one-digit"),
        pytest.param("1234", id="four-digits-only"),
        pytest.param("12345678", id="account-number"),
        pytest.param("9" * 64, id="sixty-four-digits"),
        pytest.param("acct12345", id="five-digits-at-the-end"),
        pytest.param("12345acct", id="five-digits-at-the-start"),
        pytest.param("ftmo-5123456-demo", id="seven-digits-inside"),
        pytest.param("a1234b56789", id="second-run-of-five"),
    ],
)
def test_external_position_id_refuses_an_alias_that_looks_like_an_account_number(alias: str) -> None:
    with pytest.raises(ContractValidationError) as caught:
        identity.external_position_id(alias, "987654321")
    assert caught.value.code == "GIE.CONTRACT.INVALID"
    assert "account number" in caught.value.message


# =========================================================================== records verify their identifiers
def _other_ids() -> dict[str, str]:
    event = identity.market_event_id("zz_other_kind", "XAUUSD", "M5", 1, [])
    opportunity = identity.opportunity_id("zz_other_setup", "BUY", "SCALP", [event])
    candidate = identity.candidate_id(opportunity, None, 0)
    plan = identity.plan_id(candidate, "LIMIT", 0, 0)
    decision = identity.risk_decision_id(samples.RUN_ID, plan, 0)
    order = identity.client_order_id(samples.RUN_ID, decision, "ENTRY", 0)
    return {
        "mev": event,
        "opp": opportunity,
        "cnd": candidate,
        "pln": plan,
        "rsk": decision,
        "ord": order,
        "evd": identity.evidence_id("zz_other_engine", event, 0),
        "grp": identity.alternative_group_id(opportunity),
        "rsv": identity.reservation_id(decision),
        "oev": identity.order_event_id(order, 0),
        "brk": identity.broker_order_id(order),
        "pos": identity.position_id(samples.RUN_ID, order),
        "mdl": identity.model_version_id("zz_other_model", "0.1.0", SHA),
        "run": identity.run_id("PAPER", "0" * 64),
    }


OTHER = _other_ids()

FOREIGN_IDENTIFIER_CASES = [
    pytest.param(samples.run_manifest, {"run_id": OTHER["run"]}, id="RunManifest.run_id"),
    pytest.param(samples.evidence, {"market_event_id": OTHER["mev"]}, id="Evidence.market_event_id"),
    pytest.param(samples.evidence, {"evidence_id": OTHER["evd"]}, id="Evidence.evidence_id"),
    pytest.param(
        samples.evidence,
        {"revision": 1, "supersedes_evidence_id": OTHER["evd"]},
        id="Evidence.supersedes_evidence_id",
    ),
    pytest.param(samples.candidate, {"opportunity_id": OTHER["opp"]}, id="Candidate.opportunity_id"),
    pytest.param(samples.candidate, {"candidate_id": OTHER["cnd"]}, id="Candidate.candidate_id"),
    pytest.param(
        samples.candidate,
        {"revision": 1, "supersedes_candidate_id": OTHER["cnd"]},
        id="Candidate.supersedes_candidate_id",
    ),
    pytest.param(samples.trade_plan, {"plan_id": OTHER["pln"]}, id="TradePlan.plan_id"),
    pytest.param(
        samples.trade_plan, {"alternative_group_id": OTHER["grp"]}, id="TradePlan.alternative_group_id"
    ),
    pytest.param(
        samples.trade_plan,
        {"revision": 1, "supersedes_plan_id": OTHER["pln"]},
        id="TradePlan.supersedes_plan_id",
    ),
    pytest.param(samples.risk_decision, {"decision_id": OTHER["rsk"]}, id="RiskDecision.decision_id"),
    pytest.param(samples.risk_decision, {"reservation_id": OTHER["rsv"]}, id="RiskDecision.reservation_id"),
    pytest.param(samples.order_event, {"client_order_id": OTHER["ord"]}, id="OrderEvent.client_order_id"),
    pytest.param(samples.order_event, {"order_event_id": OTHER["oev"]}, id="OrderEvent.order_event_id"),
    pytest.param(samples.position, {"position_id": OTHER["pos"]}, id="Position.position_id"),
    pytest.param(
        samples.model_version, {"model_version_id": OTHER["mdl"]}, id="ModelVersion.model_version_id"
    ),
]


@pytest.mark.parametrize(("build", "overrides"), FOREIGN_IDENTIFIER_CASES)
def test_record_refuses_an_identifier_that_was_not_derived_from_its_own_fields(
    build: Any, overrides: dict[str, Any]
) -> None:
    # Section 4: every identifier whose parts are fields of the record is verified on construction.
    assert build() is not None  # the same builder without the foreign identifier is valid
    with pytest.raises(ContractValidationError) as caught:
        build(**overrides)
    assert caught.value.details.get("invariant"), "the violated invariant must be named"


def test_order_event_refuses_a_broker_order_id_that_belongs_to_another_order() -> None:
    # Section 9.1: "Every identifier that can be recomputed from the record is verified"; the published
    # identity rule of OrderEvent (x-gie-behaviour) is broker_order_id = broker_order_id(client_order_id).
    genuine = samples.order_lifecycle()[1]
    assert genuine.broker_order_id == identity.broker_order_id(genuine.client_order_id)
    assert OTHER["brk"] != genuine.broker_order_id
    with pytest.raises(ContractValidationError):
        samples.order_event(
            seq=1,
            order_seq=1,
            event_type=OrderEventType.ACCEPTED,
            order_state=OrderState.WORKING,
            broker_order_id=OTHER["brk"],
        )
