"""Tests for ``tools/check_live_lock.py`` - the static-scan layer of the paper-only routing lock.

Requirements: G08-03 (live lock), G08-10 / M-AUTH-19 / OWN-14 (real execution stays disabled),
S12-ASG-06 (check live lock); acceptance test S00-ACC-04 "Paper-only routing lock".  Every test of
this module carries these markers.

The expected behaviour is taken from the rule list of the tool's module documentation (LL000-LL015),
from ``gie/core/routing_lock.py`` (layer 4), ``docs/ARCHITECTURE.md`` section 6 and
``specifications/SHARED_CONTRACTS.md`` section 9.3 - not from the implementation.

How the cases are written: every case is a small source text; a trailing comment ``# !LL001``
(or ``# !LL001, LL002``) on a line states that exactly these rules must be reported for exactly
that line.  A line without such a comment must not be reported, and no other file of the tree may
be reported either.  A source text without any marker is a negative control: nothing may fire.

SYNTHETIC FIXTURE: every tree, module, "lock", "gateway", "adapter" and "broker call" in this file
is an invented, inert source text written below pytest's ``tmp_path``.  The texts are only parsed by
the scanner; nothing in them is imported or executed, no broker SDK is installed or contacted, and
they contain no credentials.  The path of the production tree appears in fixtures only because
rule LL013 searches for it; nothing here addresses that tree.  Nothing in this file is market data
or performance evidence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from tools import check_live_lock as cll

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.requirement("G08-03", "G08-10", "M-AUTH-19", "OWN-14", "S12-ASG-06"),
    pytest.mark.acceptance("S00-ACC-04"),
]

LOCK = "gie/core/routing_lock.py"
GATEWAY = "gie/contracts/guarded.py"
FRAMEWORK = "gie/contracts/_spec.py"
FACADE = "gie/market_data/broker_readonly.py"
PINS = "specifications/lock_pins.json"
WHOLE_FILE = 0

Found = set[tuple[str, int]]


def src(text: str) -> str:
    """Dedent one SYNTHETIC FIXTURE source text."""
    return textwrap.dedent(text).lstrip("\n")


# ------------------------------------------------------------------------------------------------
# SYNTHETIC FIXTURE: the smallest tree that satisfies every rule.  The "lock" and the "gateway" are
# inert stand-ins with the structure the rules require; they are not copies of the real files.
# ------------------------------------------------------------------------------------------------
LOCK_SOURCE = src(
    '''
    """SYNTHETIC FIXTURE: inert stand-in for the lock module (not the real lock)."""

    from __future__ import annotations

    import threading
    from collections.abc import Iterable
    from enum import Enum
    from types import FunctionType
    from typing import Final

    from gie.core.errors import LiveRoutingLockedError

    LIVE_ROUTING_PERMITTED: Final = False


    class RunMode(str, Enum):
        """Fixture vocabulary."""

        REPLAY = "REPLAY"
        PAPER = "PAPER"


    class ExecutionVenue(str, Enum):
        SIMULATED = "SIMULATED"


    _registry_lock = threading.Lock()
    _SIMULATED_ADAPTER_TYPES: dict[type, dict[str, FunctionType]] = {}


    def parse_run_mode(value: object) -> RunMode:
        raise LiveRoutingLockedError("fixture")


    def assert_live_routing_locked() -> None:
        if LIVE_ROUTING_PERMITTED is not False:
            raise LiveRoutingLockedError("fixture")
        getattr(RunMode, "_value2member_map_", {})


    def register_simulated_adapter(adapter_type: type) -> type:
        return adapter_type


    def registered(types: Iterable[type]) -> frozenset[type]:
        return frozenset(_SIMULATED_ADAPTER_TYPES)


    class RoutingGuard:
        __slots__ = ("_mode",)

        def __init_subclass__(cls, **kwargs: object) -> None:
            raise LiveRoutingLockedError("fixture")

        def __setattr__(self, name: str, value: object) -> None:
            super().__setattr__(name, value)

        def authorize(self, adapter: object) -> None:
            assert_live_routing_locked()
    '''
)
GATEWAY_SOURCE = src(
    '''
    """SYNTHETIC FIXTURE: inert stand-in for the order gateway."""

    from __future__ import annotations

    from gie.core.routing_lock import RoutingGuard


    class GuardedExecutionAdapter:
        __slots__ = ("_adapter", "_guard")

        def __init__(self, adapter: object, guard: RoutingGuard) -> None:
            self._adapter = adapter
            self._guard = guard

        def submit(self, request: object) -> None:
            RoutingGuard.authorize(self._guard, self._adapter)

        def cancel(self, request: object) -> None:
            RoutingGuard.authorize(self._guard, self._adapter)

        def replace(self, request: object) -> None:
            RoutingGuard.authorize(self._guard, self._adapter)

        def poll(self) -> tuple[object, ...]:
            return ()
    '''
)
BASELINE: Mapping[str, str] = {
    "gie/__init__.py": '"""SYNTHETIC FIXTURE package."""\n',
    "gie/core/__init__.py": '"""SYNTHETIC FIXTURE package."""\n',
    "gie/core/errors.py": src(
        '''
        """SYNTHETIC FIXTURE errors."""


        class LiveRoutingLockedError(Exception):
            pass
        '''
    ),
    LOCK: LOCK_SOURCE,
    "gie/contracts/__init__.py": src(
        '''
        """SYNTHETIC FIXTURE: re-exports under the genuine names are the normal way to use the lock."""

        from gie.contracts.guarded import GuardedExecutionAdapter
        from gie.core.routing_lock import ExecutionVenue, RoutingGuard, RunMode
        '''
    ),
    FRAMEWORK: src(
        '''
        """SYNTHETIC FIXTURE: stand-in for the record framework, the one user of object.__setattr__."""


        class Record:
            __slots__ = ()

            def normalise(self) -> None:
                object.__setattr__(self, "value", 1)
        '''
    ),
    GATEWAY: GATEWAY_SOURCE,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pins(root: Path, overrides: Mapping[str, str] | None = None) -> None:
    """Pin the lock and the gateway of a fixture tree; the hashes are computed here, not by the tool."""
    files = {path: sha256(root / path) for path in (LOCK, GATEWAY) if (root / path).is_file()}
    files.update(overrides or {})
    target = root / PINS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def write_tree(
    root: Path, files: Mapping[str, str | bytes | None] | None = None, *, pin: bool = True
) -> Path:
    """Write the baseline plus *files* (``None`` removes a baseline file) and pin the result."""
    merged: dict[str, str | bytes | None] = {**BASELINE, **(files or {})}
    for relative, content in merged.items():
        if content is None:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    if pin:
        write_pins(root)
    return root


def marked(source: str) -> Found:
    """The ``(rule, line)`` pairs a fixture source announces with ``# !RULE`` comments."""
    expected: Found = set()
    for number, line in enumerate(source.splitlines(), start=1):
        _code, marker, rules = line.partition("# !")
        if marker:
            expected.update((rule.strip(), number) for rule in rules.split(","))
    return expected


def assert_marked(
    root: Path,
    path: str,
    source: str,
    *,
    extra: Iterable[tuple[str, int]] = (),
    others: Mapping[str, str | bytes | None] | None = None,
) -> None:
    """Scan the baseline with *source* at *path*: exactly the marked lines (plus *extra*) are reported."""
    write_tree(root, {**(others or {}), path: source})
    violations = cll.check(root)
    elsewhere = [violation.render() for violation in violations if violation.path != path]
    assert elsewhere == [], "only the file under test may be reported"
    found = {(violation.rule, violation.line) for violation in violations}
    assert found == marked(source) | set(extra), "\n".join(violation.render() for violation in violations)


def edit(source: str, *replacements: tuple[str, str], append: str = "") -> str:
    """Replace text that occurs exactly once in a fixture source and optionally append to it."""
    for old, new in replacements:
        assert source.count(old) == 1, f"fixture text must occur once: {old!r}"
        source = source.replace(old, new)
    return source + (("\n\n" + src(append)) if append else "")


def case(name: str, path: str, text: str, *extra: tuple[str, int]) -> object:
    return pytest.param(path, src(text), set(extra), id=name)


# ================================================================================================
# The clean tree and the real repository
# ================================================================================================
def test_clean_tree_has_no_violation(tmp_path: Path) -> None:
    assert cll.check(write_tree(tmp_path)) == []


@pytest.fixture(scope="module")
def repository_violations() -> list[cll.Violation]:
    return cll.check(REPO_ROOT)


@pytest.mark.parametrize("rule", [rule for rule in sorted(cll.RULES) if rule != "LL014"])
def test_real_repository_passes_the_rule(repository_violations: list[cll.Violation], rule: str) -> None:
    """The repository itself obeys every rule (LL014 is excluded: pins follow the lock files)."""
    found = [violation.render() for violation in repository_violations if violation.rule == rule]
    assert found == []


def test_real_repository_is_actually_scanned() -> None:
    """Guard against a vacuous pass: the lock and the gateway of the repository are among the files."""
    sources, _opaque = cll._discover(REPO_ROOT, "gie")
    assert {LOCK, GATEWAY, FRAMEWORK} <= set(sources)
    assert len(sources) >= 30


def test_rule_ids_are_exactly_ll000_to_ll015() -> None:
    assert sorted(cll.RULES) == [f"LL{number:03d}" for number in range(16)]
    assert all(rule in (cll.__doc__ or "") for rule in cll.RULES), "every rule is documented in the tool"


# ================================================================================================
# LL000 - code that cannot be scanned
# ================================================================================================
LL000_CASES = [
    case("syntax-error", "gie/execution/broken.py", "def broken(:  # !LL000\n    pass\n"),
    case(
        "syntax-error-on-a-later-line",
        "dashboard/gie/panel.py",
        """
        VALUE = 1
        if VALUE  # !LL000
            pass
        """,
    ),
    case("unterminated-string", "gie/risk/text.py", "TEXT = 'open  # !LL000\n"),
    case("syntax-error-under-tools", "tools/helper.py", "class :  # !LL000\n"),
    case("syntax-error-under-tests", "tests/gie/core/test_fixture.py", "return return  # !LL000\n"),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL000_CASES)
def test_ll000_syntax_error_is_a_violation_not_a_crash(
    tmp_path: Path, path: str, source: str, extra: Found
) -> None:
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"VALUE = 1\x00\n", id="null-byte"),
        pytest.param(b"# -*- coding: utf-8 -*-\nVALUE = '\xff\xfe'\n", id="undecodable"),
        pytest.param(b"# -*- coding: no-such-codec -*-\nVALUE = 1\n", id="unknown-encoding"),
        pytest.param(b"VALUE = " + b"(" * 400 + b"1" + b")" * 400 + b"\n", id="too-many-parentheses"),
    ],
)
def test_ll000_unreadable_source_is_a_violation(tmp_path: Path, content: bytes) -> None:
    path = "gie/learning/odd.py"
    write_tree(tmp_path, {path: content})
    assert {(violation.rule, violation.path) for violation in cll.check(tmp_path)} == {("LL000", path)}


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(" + ".join(['"a"'] * 6000), id="long-concatenation"),
        pytest.param("a if c else " * 3000 + "b", id="deep-conditional-chain"),
        pytest.param(".".join(["a"] * 6000), id="long-attribute-chain"),
    ],
)
def test_ll000_deeply_nested_source_never_crashes(tmp_path: Path, expression: str) -> None:
    """Whether the interpreter can parse such a file differs by version; the scan may only say LL000."""
    path = "gie/learning/deep.py"
    write_tree(tmp_path, {path: f"VALUE = {expression}\n"})
    assert {(violation.rule, violation.path) for violation in cll.check(tmp_path)} <= {("LL000", path)}


def test_ll000_names_that_cannot_be_resolved_are_reported_not_guessed(tmp_path: Path) -> None:
    """Deny by default: a file whose aliases of an import multiply beyond the limit is not verified."""
    lines = ["import terminal", "", "x = terminal.api"]
    for number in range(12):
        lines += [f"y = x.left{number}", f"x = y.right{number}"]
    path = "gie/learning/tangle.py"
    write_tree(tmp_path, {path: "\n".join(lines) + "\n"})
    assert {(v.rule, v.path, v.line) for v in cll.check(tmp_path)} == {("LL000", path, WHOLE_FILE)}


def test_ll000_ordinary_reassignment_of_variables_is_not_a_resolution_problem(tmp_path: Path) -> None:
    """Walking a linked structure re-assigns the same variables many times; nothing may be reported."""
    lines = ["from gie.contracts import *", "import json", "", "", "def walk(node, codec=json):"]
    for number in range(40):
        lines += [
            f"    following = node.next{number}",
            "    node = following.previous",
            "    following = node",
            "    node = node.parent or following.parent",
            "    codec = codec.decoder if node else json.encoder",
        ]
    lines.append("    return node, following, codec")
    write_tree(tmp_path, {"gie/learning/walk.py": "\n".join(lines) + "\n"})
    assert cll.check(tmp_path) == []


def test_ll000_parser_warnings_are_not_violations_and_do_not_escape(tmp_path: Path) -> None:
    """An invalid escape sequence is a SyntaxWarning; pytest turns warnings into errors."""
    write_tree(tmp_path, {"gie/learning/pattern.py": 'PATTERN = "\\d+"\n'})
    assert cll.check(tmp_path) == []


@pytest.mark.parametrize(
    "path",
    [
        "gie/execution/fast.so",
        "gie/native.pyd",
        "gie/execution/bridge.DLL",
        "gie/x/lib.dylib",
        "dashboard/gie/a.pyx",
    ],
)
def test_ll000_native_modules_cannot_be_scanned(tmp_path: Path, path: str) -> None:
    write_tree(tmp_path, {path: b"SYNTHETIC FIXTURE: not a real binary"})
    assert {(v.rule, v.path, v.line) for v in cll.check(tmp_path)} == {("LL000", path, WHOLE_FILE)}


def test_ll000_bytecode_is_importable_only_outside_the_cache(tmp_path: Path) -> None:
    sourceless, cached = "gie/execution/hidden.pyc", "gie/execution/__pycache__/sim.cpython-313.pyc"
    write_tree(tmp_path, {sourceless: b"SYNTHETIC FIXTURE", cached: b"SYNTHETIC FIXTURE"})
    assert {(v.rule, v.path) for v in cll.check(tmp_path)} == {("LL000", sourceless)}


def test_pyw_files_are_scanned_like_py_files(tmp_path: Path) -> None:
    assert_marked(tmp_path, "gie/execution/window.pyw", "import subprocess  # !LL003\n")


def test_a_file_with_a_syntax_error_does_not_hide_other_files(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"gie/execution/broken.py": "def broken(:\n", "gie/execution/other.py": "import MetaTrader5\n"},
    )
    assert {(v.rule, v.path, v.line) for v in cll.check(tmp_path)} == {
        ("LL000", "gie/execution/broken.py", 1),
        ("LL002", "gie/execution/other.py", 1),
    }


# ================================================================================================
# LL001 - broker order-entry names
# ================================================================================================
REQUIRED_ORDER_ENTRY_NAMES = {
    "order_send", "order_check", "OrderSend", "OrderSendAsync", "OrderCheck", "PositionOpen",
    "PositionClose", "PositionModify", "OrderModify", "OrderDelete", "OrderClose", "Buy", "Sell",
    "BuyLimit", "SellLimit", "BuyStop", "SellStop", "placeOrder", "place_order", "create_order",
    "create_market_order", "create_limit_order", "submit_order", "cancel_order", "replace_order",
    "NewOrderSingle", "OrderCancelRequest", "OrderCreate", "ProtoOANewOrderReq", "send_new_order",
    "market_order", "limit_order", "stop_order",
}  # fmt: skip
ATTRIBUTE_ONLY_NAMES = {"Buy", "Sell", "BuyLimit", "SellLimit", "BuyStop", "SellStop"}


def test_ll001_documented_name_set() -> None:
    assert isinstance(cll.ORDER_ENTRY_NAMES, frozenset)
    assert REQUIRED_ORDER_ENTRY_NAMES <= cll.ORDER_ENTRY_NAMES
    assert "order_calc_margin" not in cll.ORDER_ENTRY_NAMES, "read-only margin calculation is allowed"
    assert not {"submit", "cancel", "replace"} & cll.ORDER_ENTRY_NAMES, (
        "the neutral adapter names stay usable"
    )
    documentation = " ".join((cll.__doc__ or "").split())
    assert "matched syntactically" in documentation
    assert "``submit`` / ``cancel`` / ``replace``" in documentation
    assert "ExecutionAdapter" in documentation


def test_ll001_every_name_is_reported_as_attribute_call(tmp_path: Path) -> None:
    names = sorted(cll.ORDER_ENTRY_NAMES)
    source = "".join(f"api.{name}(1)  # !LL001\n" for name in names)
    assert_marked(tmp_path, "gie/decision/calls.py", source)
    assert len(marked(source)) == len(names) >= len(REQUIRED_ORDER_ENTRY_NAMES)


def test_ll001_every_specific_name_is_reported_as_bare_call_and_as_import(tmp_path: Path) -> None:
    names = sorted(cll.ORDER_ENTRY_NAMES - ATTRIBUTE_ONLY_NAMES)
    calls = "".join(f"{name}(1)  # !LL001\n" for name in names)
    imports = "".join(f"from terminal import {name}  # !LL001\n" for name in sorted(cll.ORDER_ENTRY_NAMES))
    assert_marked(tmp_path / "calls", "gie/decision/calls.py", calls)
    assert_marked(tmp_path / "imports", "gie/decision/imports.py", imports)


LL001_CASES = [
    case(
        "aliased-sdk-call-inside-the-facade",
        FACADE,
        """
        import MetaTrader5 as mt5


        def send(request):
            return mt5.order_send(request)  # !LL001
        """,
    ),
    case(
        "aliased-sdk-call-outside-the-facade",
        "gie/execution/engine.py",
        """
        import MetaTrader5 as mt5  # !LL002

        mt5.order_send({})  # !LL001
        """,
    ),
    case("import-from", "gie/execution/engine.py", "from MetaTrader5 import order_send  # !LL001, LL002\n"),
    case(
        "import-from-with-alias-then-call",
        FACADE,
        """
        from MetaTrader5 import order_send as push  # !LL001

        push({})  # !LL001
        """,
    ),
    case(
        "getattr-with-literal",
        FACADE,
        """
        import MetaTrader5 as mt5

        send = getattr(mt5, 'order_send')  # !LL001
        check = getattr(mt5, "order_check", None)  # !LL001
        """,
    ),
    case(
        "getattr-with-folded-literal",
        FACADE,
        """
        import MetaTrader5 as mt5

        send = getattr(mt5, "order_" + "send")  # !LL001
        other = getattr(mt5, f"Order{'Send'}")  # !LL001
        """,
    ),
    case(
        "attribute-chain",
        "gie/decision/plan.py",
        """
        class Planner:
            def go(self):
                return self.broker.session.OrderSend(1)  # !LL001
        """,
    ),
    case(
        "function-class-lambda-comprehension-coroutine",
        "gie/execution/engine.py",
        """
        class Engine:
            def run(self, trade, sizes):
                return [trade.Buy(size) for size in sizes]  # !LL001

            handler = lambda self, api: api.submit_order()  # !LL001


        def outer():
            def inner(client):
                return {key: client.create_order(key) for key in "ab"}  # !LL001

            return inner


        async def later(session):
            await session.cancel_order(1)  # !LL001
        """,
    ),
    case(
        "bare-calls",
        "gie/risk/sizing.py",
        """
        place_order(1)  # !LL001
        OrderSend(symbol="XAUUSD")  # !LL001
        """,
    ),
    case(
        "alias-through-assignment",
        FACADE,
        """
        import MetaTrader5 as mt5

        api = mt5
        send = api.order_send  # !LL001
        send({})  # !LL001
        """,
    ),
    case(
        "attrgetter-and-methodcaller",
        "gie/learning/tools.py",
        """
        from operator import attrgetter, methodcaller

        fetch = attrgetter("trade.order_send")  # !LL001
        call = methodcaller("OrderSend", 1)  # !LL001
        """,
    ),
    case(
        "attribute-without-call-and-attribute-store",
        "gie/learning/hooks.py",
        """
        def wire(client):
            hook = client.replace_order  # !LL001
            client.market_order = None  # !LL001
            return hook
        """,
    ),
    case(
        "generic-name-after-star-import",
        "gie/risk/star.py",
        """
        from terminal import *

        Sell(1)  # !LL001
        """,
    ),
    case(
        "reflection-in-the-facade-cannot-be-verified",
        FACADE,
        """
        import MetaTrader5 as mt5


        def call(name):
            return getattr(mt5, name)()  # !LL001


        table = vars(mt5)  # !LL001
        raw = mt5.__dict__  # !LL001
        """,
    ),
    case(
        "dashboard",
        "dashboard/gie/panel.py",
        """
        def click(terminal):
            terminal.OrderClose(1)  # !LL001
        """,
    ),
    case(
        "star-import-in-the-facade-cannot-be-verified",
        FACADE,
        """
        from MetaTrader5 import *  # !LL001

        push = order_send
        push({})  # !LL001
        order_check({})  # !LL001
        """,
    ),
    case(
        "plain-alias-of-a-specific-name",
        "gie/risk/alias.py",
        """
        send = place_order
        later = send
        later(1)  # !LL001
        """,
    ),
]
LL001_CONTROLS = [
    case(
        "read-only-sdk-functions-in-the-facade",
        FACADE,
        """
        import MetaTrader5 as mt5

        margin = mt5.order_calc_margin(0, "XAUUSD", 0.01, 2000.0)
        ticks = mt5.copy_ticks_from("XAUUSD", 0, 100, 0)
        info = getattr(mt5, "symbol_info_tick")("XAUUSD")
        """,
    ),
    case(
        "bare-buy-and-sell-are-too-generic",
        "gie/risk/sides.py",
        """
        def Sell(volume):
            return -volume


        Buy = Sell
        total = Buy(1) + Sell(2)
        """,
    ),
    case(
        "variables-named-like-order-types",
        "gie/execution/orders.py",
        """
        stop_order = {"type": "STOP"}
        limit_order = dict(stop_order)
        market_order = [stop_order, limit_order]
        """,
    ),
    case(
        "computed-getattr-outside-the-facade",
        "gie/learning/read.py",
        """
        def read(record, name):
            return getattr(record, name)
        """,
    ),
    case(
        "strings-and-comments-are-not-code",
        "gie/learning/notes.py",
        '''
        """Never call order_send or OrderSend from GIE."""

        NOTE = "order_send is locked"  # mt5.order_send(...) stays forbidden
        ''',
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL001_CASES)
def test_ll001_order_entry_names_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source), "a positive case must announce at least one line"
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL001_CONTROLS)
def test_ll001_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


# ================================================================================================
# LL002 - broker SDK imports
# ================================================================================================
REQUIRED_BROKER_SDKS = {
    "MetaTrader5", "MetaTrader4", "mt5", "ctrader_open_api", "ib_insync", "ibapi", "oandapyV20", "ccxt",
    "alpaca", "alpaca_trade_api", "quickfix", "simplefix", "binance", "tradingview_ta", "dwx_client",
    "pymt5adapter",
}  # fmt: skip


def test_ll002_documented_sdk_set() -> None:
    assert REQUIRED_BROKER_SDKS <= cll.BROKER_SDK_MODULES
    assert cll.BROKER_FACADE_PATH == FACADE


@pytest.mark.parametrize("sdk", sorted(REQUIRED_BROKER_SDKS))
def test_ll002_sdk_import_is_allowed_only_in_the_facade(tmp_path: Path, sdk: str) -> None:
    for number, path in enumerate(("gie/execution/x.py", "gie/market_data/feed.py", "dashboard/gie/x.py")):
        assert_marked(tmp_path / str(number), path, f"import {sdk}  # !LL002\n")
    assert_marked(tmp_path / "facade", FACADE, f"import {sdk}\nfrom {sdk} import version\n")


LL002_CASES = [
    case(
        "from-submodule",
        "gie/intermarket/x.py",
        "from ccxt.async_support import kraken as exchange  # !LL002\n",
    ),
    case("submodule-with-alias", "gie/risk/x.py", "import alpaca.trading.client as client  # !LL002\n"),
    case(
        "lazy-import-in-function-and-try",
        "gie/execution/lazy.py",
        """
        def connect():
            import ib_insync  # !LL002

            return ib_insync


        try:
            import quickfix as fix  # !LL002
        except ImportError:
            fix = None
        """,
    ),
    case(
        "dynamic-import-with-literal-name",
        "gie/execution/dynamic.py",
        """
        import importlib
        from importlib import import_module as load

        first = importlib.import_module("MetaTrader5")  # !LL002
        second = __import__("ib_insync")  # !LL002
        third = load("ccxt.pro")  # !LL002
        """,
    ),
    case("case-is-ignored", "gie/execution/x.py", "import metatrader5  # !LL002\n"),
    case("vendored-sdk", "gie/execution/x.py", "from vendor import MetaTrader5  # !LL002\n"),
    case("several-on-one-line", "gie/execution/x.py", "import json, ccxt, binance  # !LL002\n"),
]
LL002_CONTROLS = [
    case("project-module-with-a-similar-name", "gie/intermarket/feeds.py", "from . import binance_feed\n"),
    case("ordinary-imports", "gie/execution/x.py", "import json\nfrom decimal import Decimal\n"),
    case("name-in-a-string", "gie/execution/x.py", 'SDK = "MetaTrader5"\n'),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL002_CASES)
def test_ll002_sdk_imports_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL002_CONTROLS)
def test_ll002_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


def test_ll002_order_entry_rule_still_applies_inside_the_facade(tmp_path: Path) -> None:
    source = src(
        """
        import MetaTrader5
        from MetaTrader5 import order_check  # !LL001

        MetaTrader5.order_send({})  # !LL001
        """
    )
    assert_marked(tmp_path, FACADE, source)


# ================================================================================================
# LL003 - process, native, network and dynamic-code escapes
# ================================================================================================
REQUIRED_NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "websockets", "zmq",
    "grpc", "smtplib", "ftplib", "telnetlib", "paramiko",
}  # fmt: skip
REQUIRED_FEED_MODULES = {
    "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "websockets", "zmq",
}  # fmt: skip
REQUIRED_PROCESS_MODULES = {"ctypes", "cffi", "subprocess", "multiprocessing", "pty"}
FEED_PACKAGES = ("gie/market_data/", "gie/intermarket/")
OTHER_PACKAGES = (
    "gie/core/",
    "gie/contracts/",
    "gie/execution/",
    "gie/reflex/",
    "gie/risk/",
    "gie/learning/",
)


def test_ll003_documented_sets_and_exemptions() -> None:
    assert REQUIRED_NETWORK_MODULES <= cll.NETWORK_MODULES
    assert REQUIRED_PROCESS_MODULES <= cll.PROCESS_AND_NATIVE_MODULES
    assert dict(cll.NETWORK_EXEMPTIONS) == dict.fromkeys(FEED_PACKAGES, frozenset(REQUIRED_FEED_MODULES))
    exempt = set().union(*cll.NETWORK_EXEMPTIONS.values())
    assert not exempt & cll.PROCESS_AND_NATIVE_MODULES, "no package may start processes or load native code"


@pytest.mark.parametrize("module", sorted(REQUIRED_NETWORK_MODULES))
def test_ll003_network_module_is_denied_below_gie(tmp_path: Path, module: str) -> None:
    for number, package in enumerate(OTHER_PACKAGES):
        assert_marked(tmp_path / str(number), f"{package}net.py", f"import {module}  # !LL003\n")


@pytest.mark.parametrize("module", sorted(REQUIRED_NETWORK_MODULES))
@pytest.mark.parametrize("package", FEED_PACKAGES)
def test_ll003_feed_packages_may_import_exactly_the_exempt_modules(
    tmp_path: Path, package: str, module: str
) -> None:
    marker = "" if module in REQUIRED_FEED_MODULES else "  # !LL003"
    assert_marked(tmp_path, f"{package}feed.py", f"import {module}{marker}\n")


@pytest.mark.parametrize("module", sorted(REQUIRED_PROCESS_MODULES))
def test_ll003_process_and_native_modules_are_denied_everywhere(tmp_path: Path, module: str) -> None:
    packages = (*FEED_PACKAGES, *OTHER_PACKAGES, "dashboard/gie/")
    for number, package in enumerate(packages):
        assert_marked(tmp_path / str(number), f"{package}native.py", f"import {module}  # !LL003\n")


EXTRA_PROCESS_MODULES = ["_ctypes", "_posixsubprocess", "_winapi"]
EXTRA_NETWORK_MODULES = ["_socket", "_ssl", "imaplib", "nntplib", "poplib", "socketserver", "xmlrpc"]


@pytest.mark.parametrize("module", EXTRA_PROCESS_MODULES)
def test_ll003_private_process_modules_are_denied_like_the_public_ones(tmp_path: Path, module: str) -> None:
    for number, package in enumerate(("gie/market_data/", "gie/execution/", "dashboard/gie/")):
        assert_marked(tmp_path / str(number), f"{package}native.py", f"import {module}  # !LL003\n")


@pytest.mark.parametrize("module", EXTRA_NETWORK_MODULES)
def test_ll003_other_network_modules_are_denied_even_for_feeds(tmp_path: Path, module: str) -> None:
    for number, package in enumerate(("gie/execution/", *FEED_PACKAGES)):
        assert_marked(tmp_path / str(number), f"{package}net.py", f"import {module}.client  # !LL003\n")


@pytest.mark.parametrize(
    "function",
    [
        "system",
        "popen",
        "startfile",
        "execv",
        "execlp",
        "execvpe",
        "spawnl",
        "spawnvpe",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
    ],
)
def test_ll003_os_process_functions(tmp_path: Path, function: str) -> None:
    source = src(
        f"""
        import os
        import os as operating
        from os import {function}  # !LL003
        from os import {function} as run  # !LL003

        os.{function}("x")  # !LL003
        operating.{function}("x")  # !LL003
        run("x")  # !LL003
        getattr(os, "{function}")("x")  # !LL003
        handle = os.{function}  # !LL003
        """
    )
    for number, path in enumerate(
        ("gie/market_data/run.py", "gie/backtesting/run.py", "dashboard/gie/run.py")
    ):
        assert_marked(tmp_path / str(number), path, source)


LL003_CASES = [
    case("http-submodule", "gie/execution/x.py", "import http.client  # !LL003\n"),
    case("from-urllib", "gie/risk/x.py", "from urllib.request import urlopen  # !LL003\n"),
    case("aliased", "gie/reflex/x.py", "import socket as wire  # !LL003\n"),
    case("subprocess-name", "gie/market_data/x.py", "from subprocess import run  # !LL003\n"),
    case("multiprocessing-name", "gie/intermarket/x.py", "from multiprocessing import Process  # !LL003\n"),
    case("ctypes-submodule", "gie/core/x.py", "import ctypes.util  # !LL003\n"),
    case(
        "star-import-of-os",
        "gie/execution/x.py",
        """
        from os import *

        system("x")  # !LL003
        """,
    ),
    case(
        "literal-dynamic-import",
        "gie/execution/x.py",
        """
        import importlib

        wire = importlib.import_module("socket")  # !LL003
        shell = __import__("subprocess")  # !LL003
        """,
    ),
    case(
        "computed-dynamic-import",
        "gie/learning/plugins.py",
        """
        import importlib
        from importlib import import_module as load


        def first(name):
            return importlib.import_module(name)  # !LL003


        def second(name):
            return load("gie." + name)  # !LL003


        def third(name):
            return __import__(name)  # !LL003


        codec = importlib.import_module("json")
        """,
    ),
    case(
        "eval-exec-compile",
        "gie/learning/dynamic.py",
        """
        import builtins

        value = eval("1 + 1")  # !LL003
        exec("x = 1")  # !LL003
        code = compile("1", "<fixture>", "eval")  # !LL003
        builtins.exec("y = 2")  # !LL003
        run = eval
        run("3")  # !LL003
        """,
    ),
    case("dynamic-code-in-the-dashboard", "dashboard/gie/x.py", "exec('x = 1')  # !LL003\n"),
    case(
        "process-pool",
        "gie/backtesting/pool.py",
        """
        import concurrent.futures
        from concurrent.futures import ProcessPoolExecutor  # !LL003
        from concurrent.futures import ThreadPoolExecutor

        pool = concurrent.futures.ProcessPoolExecutor()  # !LL003
        """,
    ),
    case(
        "asyncio-subprocess-and-streams",
        "gie/execution/aio.py",
        """
        import asyncio


        async def run():
            await asyncio.create_subprocess_exec("x")  # !LL003
            await asyncio.create_subprocess_shell("x")  # !LL003
            await asyncio.open_connection("host", 1)  # !LL003
        """,
    ),
    case(
        "os-under-its-private-names",
        "gie/execution/private.py",
        """
        import nt
        import posix as low

        nt.system("x")  # !LL003
        low.fork()  # !LL003
        """,
    ),
    case(
        "eval-stays-eval-even-when-redefined",
        "gie/learning/shadow.py",
        """
        def eval(expression):
            return expression


        eval("1")  # !LL003
        """,
    ),
    case(
        "asyncio-subprocess-is-not-exempt-for-feeds",
        "gie/market_data/aio.py",
        """
        import asyncio


        async def run():
            await asyncio.open_connection("host", 1)
            await asyncio.create_subprocess_exec("x")  # !LL003
        """,
    ),
]
LL003_CONTROLS = [
    case(
        "harmless-os-functions",
        "gie/core/files.py",
        """
        import os

        here = os.getcwd()
        joined = os.path.join(here, "data")
        present = os.path.exists(joined) and os.listdir(joined)
        size = os.stat(joined).st_size
        """,
    ),
    case(
        "compile-and-eval-of-other-objects",
        "gie/prediction/model.py",
        """
        import re
        from re import compile

        PATTERN = re.compile("a+")
        OTHER = compile("b+")


        def score(model, frame):
            model.eval()
            return frame.eval("a + b")
        """,
    ),
    case(
        "network-in-the-dashboard-is-outside-the-rule",
        "dashboard/gie/server.py",
        "import http.server\nimport socket\n",
    ),
    case(
        "feed-reader",
        "gie/market_data/feed.py",
        """
        import asyncio
        import http.client
        import socket
        import ssl
        from urllib.parse import urlparse

        import websockets
        import zmq


        async def read():
            return await asyncio.open_connection("feed.invalid", 443)
        """,
    ),
    case(
        "variables-that-only-look-like-os",
        "gie/reflex/ticks.py",
        """
        def advance(nt, posix):
            return nt.execution_id, nt.spawn_time, posix.system
        """,
    ),
    case(
        "a-local-function-named-compile",
        "gie/decision/plans.py",
        """
        def compile(plan):
            return plan


        compiled = compile("plan")
        """,
    ),
    case(
        "threads-are-not-processes",
        "gie/execution/workers.py",
        "import threading\nfrom concurrent.futures import ThreadPoolExecutor\n",
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL003_CASES)
def test_ll003_escapes_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL003_CONTROLS)
def test_ll003_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


# ================================================================================================
# LL004 - the lock constant
# ================================================================================================
CONSTANT = "LIVE_ROUTING_PERMITTED: Final = False"
NOT_BOUND = ("LL004", WHOLE_FILE)
NOT_DEFINED = ("LL007", WHOLE_FILE)


def lock_case(
    name: str, *replacements: tuple[str, str], append: str = "", extra: Iterable[tuple[str, int]] = ()
) -> object:
    return pytest.param(edit(LOCK_SOURCE, *replacements, append=append), set(extra), id=name)


LL004_CASES = [
    lock_case("true", (CONSTANT, "LIVE_ROUTING_PERMITTED: Final = True  # !LL004"), extra=[NOT_BOUND]),
    lock_case("zero-is-not-false", (CONSTANT, "LIVE_ROUTING_PERMITTED = 0  # !LL004"), extra=[NOT_BOUND]),
    lock_case("none", (CONSTANT, "LIVE_ROUTING_PERMITTED = None  # !LL004"), extra=[NOT_BOUND]),
    lock_case("expression", (CONSTANT, "LIVE_ROUTING_PERMITTED = not True  # !LL004"), extra=[NOT_BOUND]),
    lock_case(
        "computed",
        (CONSTANT, 'LIVE_ROUTING_PERMITTED = bool(threading.active_count() and "")  # !LL004'),
        extra=[NOT_BOUND],
    ),
    lock_case(
        "annotation-other-than-final",
        (CONSTANT, "LIVE_ROUTING_PERMITTED: bool = False  # !LL004"),
        extra=[NOT_BOUND],
    ),
    lock_case("removed", (CONSTANT, "# the constant was removed"), extra=[NOT_BOUND, NOT_DEFINED]),
    lock_case(
        "conditional",
        (CONSTANT, "if True:\n    LIVE_ROUTING_PERMITTED = False  # !LL004"),
        extra=[NOT_BOUND, NOT_DEFINED],
    ),
    lock_case("tuple", (CONSTANT, "LIVE_ROUTING_PERMITTED, OTHER = False, 1  # !LL004"), extra=[NOT_BOUND]),
    lock_case("chained", (CONSTANT, "LIVE_ROUTING_PERMITTED = OTHER = False  # !LL004"), extra=[NOT_BOUND]),
    lock_case("bound-twice", append="LIVE_ROUTING_PERMITTED = False  # !LL004"),
    lock_case(
        "rebound-in-a-function",
        append="""
        def unlock() -> None:
            global LIVE_ROUTING_PERMITTED  # !LL004
            LIVE_ROUTING_PERMITTED = True  # !LL004
        """,
    ),
    lock_case("augmented", append="LIVE_ROUTING_PERMITTED |= True  # !LL004"),
    lock_case("deleted", append="del LIVE_ROUTING_PERMITTED  # !LL004"),
    lock_case("annotated-again", append="LIVE_ROUTING_PERMITTED: Final = False  # !LL004"),
    lock_case("globals-subscript", append='globals()["LIVE_ROUTING_PERMITTED"] = True  # !LL004'),
    lock_case("globals-update-keyword", append="globals().update(LIVE_ROUTING_PERMITTED=True)  # !LL004"),
    lock_case(
        "globals-update-mapping", append='globals().update({"LIVE_ROUTING_PERMITTED": True})  # !LL004'
    ),
    lock_case("setattr", append='setattr(threading, "LIVE_ROUTING_PERMITTED", True)  # !LL004'),
    lock_case(
        "import-rebinding",
        append="from gie.core.errors import LiveRoutingLockedError as LIVE_ROUTING_PERMITTED  # !LL004",
    ),
    lock_case(
        "function-with-the-name",
        append="""
        def LIVE_ROUTING_PERMITTED() -> bool:  # !LL004
            return True
        """,
    ),
    lock_case(
        "walrus",
        append="""
        if (LIVE_ROUTING_PERMITTED := True):  # !LL004
            pass
        """,
    ),
    lock_case(
        "loop-target",
        append="""
        for LIVE_ROUTING_PERMITTED in (True,):  # !LL004
            pass
        """,
    ),
]
LL004_CONTROLS = [
    lock_case("plain-assignment", (CONSTANT, "LIVE_ROUTING_PERMITTED = False")),
    lock_case("final-bool", (CONSTANT, "LIVE_ROUTING_PERMITTED: Final[bool] = False")),
    lock_case("typing-final", (CONSTANT, "import typing\n\nLIVE_ROUTING_PERMITTED: typing.Final = False")),
    lock_case(
        "reading-the-constant",
        append="""
        def is_locked() -> bool:
            return LIVE_ROUTING_PERMITTED is False
        """,
    ),
]


@pytest.mark.parametrize(("source", "extra"), LL004_CASES)
def test_ll004_constant_must_be_the_literal_false_bound_once(
    tmp_path: Path, source: str, extra: Found
) -> None:
    assert marked(source) | extra
    assert_marked(tmp_path, LOCK, source, extra=extra)


@pytest.mark.parametrize(("source", "extra"), LL004_CONTROLS)
def test_ll004_accepts_the_documented_forms(tmp_path: Path, source: str, extra: Found) -> None:
    assert not marked(source) | extra
    assert_marked(tmp_path, LOCK, source)


# ================================================================================================
# LL005 - the lock vocabulary
# ================================================================================================
RUN_MODE = "class RunMode(str, Enum):"
VENUE = "class ExecutionVenue(str, Enum):"
PAPER = '    PAPER = "PAPER"'
REPLAY = '    REPLAY = "REPLAY"'
SIMULATED = '    SIMULATED = "SIMULATED"'
FLAGGED_RUN_MODE = (RUN_MODE, RUN_MODE + "  # !LL005")
FLAGGED_VENUE = (VENUE, VENUE + "  # !LL005")

LL005_LOCK_CASES = [
    lock_case("run-mode-gains-a-member", FLAGGED_RUN_MODE, (PAPER, PAPER + '\n    LIVE = "LIVE"')),
    lock_case("run-mode-loses-a-member", FLAGGED_RUN_MODE, (PAPER + "\n", "")),
    lock_case("run-mode-member-renamed", FLAGGED_RUN_MODE, (PAPER, '    LIVE = "LIVE"')),
    lock_case("value-differs-from-name", (PAPER, '    PAPER = "LIVE"  # !LL005')),
    lock_case("value-is-not-a-string", (REPLAY, "    REPLAY = 1  # !LL005")),
    lock_case("value-is-computed", (REPLAY, "    REPLAY = str('REPLAY')  # !LL005")),
    lock_case("annotated-member", FLAGGED_RUN_MODE, (PAPER, '    PAPER: str = "PAPER"  # !LL005')),
    lock_case("venue-gains-a-member", FLAGGED_VENUE, (SIMULATED, SIMULATED + '\n    BROKER = "BROKER"')),
    lock_case("venue-member-renamed", FLAGGED_VENUE, (SIMULATED, '    LIVE = "LIVE"')),
    lock_case(
        "method-in-the-vocabulary",
        (
            PAPER,
            PAPER
            + "\n\n    @classmethod\n    def _missing_(cls, value):  # !LL005\n        return cls.PAPER",
        ),
    ),
    lock_case("decorator", (RUN_MODE, "@open_for_extension  # !LL005\n" + RUN_MODE)),
    lock_case("metaclass", (RUN_MODE, "class RunMode(str, Enum, metaclass=Open):  # !LL005")),
    lock_case("other-base", (VENUE, "class ExecutionVenue(Flag):  # !LL005")),
    lock_case("no-enum-base", (VENUE, "class ExecutionVenue(str):  # !LL005")),
    lock_case(
        "defined-twice",
        append="""
        class RunMode(str, Enum):  # !LL005
            REPLAY = "REPLAY"
            PAPER = "PAPER"
        """,
    ),
    lock_case("rebound", append="RunMode = None  # !LL005"),
    lock_case(
        "functional-api", append='ExecutionVenue = Enum("ExecutionVenue", ["SIMULATED", "BROKER"])  # !LL005'
    ),
    lock_case("attribute-written", append='RunMode.LIVE = "LIVE"  # !LL005'),
    lock_case("member-attribute-written", append='RunMode.PAPER._value_ = "LIVE"  # !LL005'),
    lock_case("setattr", append='setattr(ExecutionVenue, "BROKER", "BROKER")  # !LL005'),
    lock_case("deleted", append="del ExecutionVenue  # !LL005"),
]
LL005_FOREIGN_CASES = [
    case(
        "class-in-another-package",
        "gie/execution/modes.py",
        """
        from enum import Enum


        class RunMode(str, Enum):  # !LL005
            LIVE = "LIVE"
        """,
    ),
    case(
        "nested-class-in-the-dashboard",
        "dashboard/gie/modes.py",
        """
        def build():
            class ExecutionVenue:  # !LL005
                BROKER = "BROKER"

            return ExecutionVenue
        """,
    ),
    case(
        "functional-api",
        "gie/risk/modes.py",
        """
        from enum import Enum

        ExecutionVenue = Enum("ExecutionVenue", ["SIMULATED", "BROKER"])  # !LL005
        """,
    ),
    case(
        "import-under-the-reserved-name", "gie/risk/modes.py", "from enum import Enum as RunMode  # !LL005\n"
    ),
    case(
        "function-with-the-reserved-name",
        "gie/decision/modes.py",
        """
        def RunMode(value):  # !LL005
            return value
        """,
    ),
]
LL005_CONTROLS = [
    case(
        "using-the-genuine-vocabulary",
        "gie/execution/modes.py",
        """
        from gie.contracts import ExecutionVenue
        from gie.core.routing_lock import RunMode

        MODE = RunMode.PAPER
        VENUE = ExecutionVenue.SIMULATED
        NAMES = ["RunMode", "ExecutionVenue"]
        """,
    ),
    case(
        "other-enums",
        "gie/execution/sides.py",
        """
        from enum import Enum


        class RunModel(str, Enum):
            LIVE = "LIVE"
        """,
    ),
]


@pytest.mark.parametrize(("source", "extra"), LL005_LOCK_CASES)
def test_ll005_vocabulary_of_the_lock_is_exact(tmp_path: Path, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, LOCK, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL005_FOREIGN_CASES)
def test_ll005_vocabulary_is_defined_nowhere_else(
    tmp_path: Path, path: str, source: str, extra: Found
) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL005_CONTROLS)
def test_ll005_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


# ================================================================================================
# LL006 - isolation of the lock module
# ================================================================================================
LL006_CASES = [
    lock_case("import-os", append="import os  # !LL006"),
    lock_case("from-os-import-environ", append="from os import environ  # !LL006"),
    lock_case(
        "sys-argv",
        append="""
        import sys  # !LL006

        ARGUMENTS = sys.argv  # !LL006
        """,
    ),
    lock_case("harmless-module-that-is-not-on-the-allowlist", append="import json  # !LL006"),
    lock_case("package-instead-of-collections-abc", append="import collections  # !LL006"),
    lock_case("sibling-module", append="from . import clock  # !LL006"),
    lock_case("other-gie-module", append="from gie.core.clock import Clock  # !LL006"),
    lock_case("open", append='SETTING = open("lock.cfg").read()  # !LL006'),
    lock_case("eval", append='VALUE = eval("False")  # !LL003, LL006'),
    lock_case("exec", append='exec("x = 1")  # !LL003, LL006'),
    lock_case("compile", append='CODE = compile("1", "fixture", "eval")  # !LL003, LL006'),
    lock_case("dunder-import", append='MODULE = __import__("json")  # !LL006'),
    lock_case("reference-without-call", append="reader = open  # !LL006"),
    lock_case("getattr-on-os", append='HOME = getattr(os, "environ")  # !LL006'),
    lock_case("setattr-on-os", append='setattr(os, "linesep", "")  # !LL006'),
    lock_case(
        "getenv",
        append="""
        def read(settings: object) -> object:
            return settings.getenv("LIVE")  # !LL006
        """,
    ),
    lock_case(
        "environ-attribute",
        append="""
        def read(settings: object) -> object:
            return settings.environ["LIVE"]  # !LL006
        """,
    ),
    lock_case(
        "reflective-argv",
        append="""
        def read(settings: object) -> object:
            return getattr(settings, "argv")  # !LL006
        """,
    ),
]
LL006_CONTROLS = [
    lock_case("relative-import-of-the-errors", append="from .errors import LiveRoutingLockedError as Locked"),
    lock_case("errors-module", append="from gie.core import errors"),
    lock_case("collections-abc-module", append="import collections.abc"),
    lock_case("more-from-the-allowlist", append="import enum\nimport types\nfrom typing import Any, final"),
]


@pytest.mark.parametrize(("source", "extra"), LL006_CASES)
def test_ll006_lock_module_reads_from_nowhere(tmp_path: Path, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, LOCK, source, extra=extra)


@pytest.mark.parametrize(("source", "extra"), LL006_CONTROLS)
def test_ll006_allowlist_is_accepted(tmp_path: Path, source: str, extra: Found) -> None:
    assert not marked(source) | extra
    assert_marked(tmp_path, LOCK, source)


def test_ll006_documented_allowlist() -> None:
    assert (
        frozenset(
            {"__future__", "threading", "enum", "types", "typing", "collections.abc", "gie.core.errors"}
        )
        == cll.LOCK_IMPORT_ALLOWLIST
    )


# ================================================================================================
# LL007 - the lock exists
# ================================================================================================
GUARD = "class RoutingGuard:"
ADAPTER = "class GuardedExecutionAdapter:"
REGISTER = "def register_simulated_adapter(adapter_type: type) -> type:\n    return adapter_type"

LL007_LOCK_CASES = [
    lock_case("parse-run-mode-missing", ("def parse_run_mode(", "def parse_mode("), extra=[NOT_DEFINED]),
    lock_case(
        "self-check-missing", ("def assert_live_routing_locked(", "def self_check("), extra=[NOT_DEFINED]
    ),
    lock_case("registration-missing", ("def register_simulated_adapter(", "def admit("), extra=[NOT_DEFINED]),
    lock_case("guard-missing", (GUARD, "class Guard:"), extra=[NOT_DEFINED]),
    lock_case("authorize-missing", (GUARD, GUARD + "  # !LL007"), ("def authorize(", "def allow(")),
    lock_case(
        "authorize-is-a-coroutine",
        (GUARD, GUARD + "  # !LL007"),
        ("    def authorize(", "    async def authorize("),
    ),
    lock_case(
        "run-mode-missing", (RUN_MODE, "class Mode(str, Enum):"), extra=[NOT_DEFINED, ("LL005", WHOLE_FILE)]
    ),
    lock_case(
        "venue-missing", (VENUE, "class Venue(str, Enum):"), extra=[NOT_DEFINED, ("LL005", WHOLE_FILE)]
    ),
    lock_case(
        "function-is-a-coroutine", ("def parse_run_mode(", "async def parse_run_mode("), extra=[NOT_DEFINED]
    ),
    lock_case(
        "function-is-not-at-module-level",
        (REGISTER, "if True:\n" + textwrap.indent(REGISTER, "    ")),
        extra=[NOT_DEFINED],
    ),
]


@pytest.mark.parametrize(("source", "extra"), LL007_LOCK_CASES)
def test_ll007_lock_module_defines_every_part(tmp_path: Path, source: str, extra: Found) -> None:
    assert marked(source) | extra
    assert_marked(tmp_path, LOCK, source, extra=extra)


@pytest.mark.parametrize("method", ["submit", "cancel", "replace", "poll"])
def test_ll007_gateway_defines_every_method(tmp_path: Path, method: str) -> None:
    source = edit(
        GATEWAY_SOURCE, (ADAPTER, ADAPTER + "  # !LL007"), (f"def {method}(", f"def {method}_later(")
    )
    assert_marked(tmp_path, GATEWAY, source)


def test_ll007_gateway_class_is_required(tmp_path: Path) -> None:
    source = edit(GATEWAY_SOURCE, (ADAPTER, "class Gateway:"))
    assert_marked(tmp_path, GATEWAY, source, extra=[NOT_DEFINED])


@pytest.mark.parametrize("missing", [LOCK, GATEWAY])
def test_ll007_missing_lock_file(tmp_path: Path, missing: str) -> None:
    write_tree(tmp_path, {missing: None})
    found = {(violation.rule, violation.path, violation.line) for violation in cll.check(tmp_path)}
    assert ("LL007", missing, WHOLE_FILE) in found
    assert ("LL014", PINS, WHOLE_FILE) in found, "a lock file that is not pinned is a violation as well"
    assert {rule for rule, _path, _line in found} == {"LL007", "LL014"}


@pytest.mark.parametrize("unparsable", [LOCK, GATEWAY])
def test_ll007_lock_file_that_cannot_be_parsed_cannot_be_verified(tmp_path: Path, unparsable: str) -> None:
    write_tree(tmp_path, {unparsable: "def broken(:\n"})
    found = {(violation.rule, violation.path, violation.line) for violation in cll.check(tmp_path)}
    assert found == {("LL000", unparsable, 1), ("LL007", unparsable, WHOLE_FILE)}


def test_an_empty_directory_is_not_a_locked_repository(tmp_path: Path) -> None:
    """Deny by default: where there is no lock, the scan fails."""
    found = {(violation.rule, violation.path) for violation in cll.check(tmp_path)}
    assert found == {("LL007", LOCK), ("LL007", GATEWAY), ("LL014", PINS)}


# ================================================================================================
# LL008 - tampering with the lock from outside
# ================================================================================================
LL008_CASES = [
    case(
        "constant-assigned-through-the-module",
        "gie/execution/hack.py",
        """
        from gie.core import routing_lock

        routing_lock.LIVE_ROUTING_PERMITTED = True  # !LL008
        """,
    ),
    case(
        "constant-augmented-annotated-deleted",
        "gie/risk/hack.py",
        """
        import gie.core.routing_lock as lock

        lock.LIVE_ROUTING_PERMITTED |= True  # !LL008
        lock.LIVE_ROUTING_PERMITTED: bool = True  # !LL008
        del lock.LIVE_ROUTING_PERMITTED  # !LL008
        """,
    ),
    case(
        "constant-as-a-name",
        "dashboard/gie/hack.py",
        """
        LIVE_ROUTING_PERMITTED = True  # !LL008


        def enable():
            global LIVE_ROUTING_PERMITTED  # !LL008
            LIVE_ROUTING_PERMITTED += 1  # !LL008
        """,
    ),
    case(
        "constant-through-setattr-and-delattr",
        "gie/learning/hack.py",
        """
        import gie.core.routing_lock as lock

        setattr(lock, "LIVE_ROUTING_PERMITTED", True)  # !LL008
        delattr(lock, "LIVE_ROUTING_PERMITTED")  # !LL008
        setattr(object(), "LIVE_" + "ROUTING_PERMITTED", True)  # !LL008
        """,
    ),
    case(
        "constant-through-the-module-dictionary",
        "gie/learning/hack.py",
        """
        import gie.core.routing_lock as lock

        vars(lock)["LIVE_ROUTING_PERMITTED"] = True  # !LL008
        lock.__dict__.update(LIVE_ROUTING_PERMITTED=True)  # !LL008
        lock.__dict__.update({"LIVE_ROUTING_PERMITTED": True})  # !LL008
        """,
    ),
    case(
        "constant-shadowed-by-an-import",
        "gie/risk/hack.py",
        "from settings import LIVE as LIVE_ROUTING_PERMITTED  # !LL008\n",
    ),
    case(
        "guard-method-replaced-through-an-alias",
        "gie/execution/hack.py",
        """
        from gie.core.routing_lock import RoutingGuard as Guard

        Guard.authorize = lambda self, adapter: None  # !LL008
        """,
    ),
    case(
        "guard-replaced-in-the-lock-module",
        "gie/execution/hack.py",
        """
        from gie.core import routing_lock

        routing_lock.RoutingGuard = object  # !LL008
        """,
    ),
    case(
        "guard-replaced-in-the-gateway-module",
        "gie/execution/hack.py",
        """
        import gie.contracts.guarded

        gie.contracts.guarded.RoutingGuard = object  # !LL008
        """,
    ),
    case(
        "gateway-method-replaced-with-setattr",
        "gie/execution/hack.py",
        """
        from gie.contracts import GuardedExecutionAdapter

        setattr(GuardedExecutionAdapter, "submit", print)  # !LL008
        """,
    ),
    case(
        "vocabulary-written",
        "gie/execution/hack.py",
        """
        from gie.core.routing_lock import ExecutionVenue, RunMode

        RunMode.PAPER.label = "paper"  # !LL008
        mode = RunMode
        mode.LIVE = "LIVE"  # !LL008
        delattr(ExecutionVenue, "SIMULATED")  # !LL008
        """,
    ),
    case(
        "explicit-setattr-of-the-metaclass",
        "gie/execution/hack.py",
        """
        from gie.core.routing_lock import RoutingGuard

        type.__setattr__(RoutingGuard, "authorize", None)  # !LL008, LL009
        """,
    ),
    case(
        "lock-module-through-sys-modules-and-importlib",
        "gie/execution/hack.py",
        """
        import importlib
        import sys

        sys.modules["gie.core.routing_lock"].RoutingGuard = object  # !LL008
        importlib.import_module("gie.core.routing_lock").RoutingGuard = object  # !LL008
        """,
    ),
    case(
        "registry",
        "gie/execution/hack.py",
        """
        from gie.core import routing_lock
        from gie.core.routing_lock import _SIMULATED_ADAPTER_TYPES  # !LL008

        registry = routing_lock._SIMULATED_ADAPTER_TYPES  # !LL008
        _SIMULATED_ADAPTER_TYPES.clear()  # !LL008
        """,
    ),
    case(
        "enum-internals",
        "gie/risk/hack.py",
        """
        from gie.core.routing_lock import ExecutionVenue, RunMode

        RunMode._member_map_["LIVE"] = RunMode.PAPER  # !LL008
        names = RunMode._member_names_  # !LL008
        table = ExecutionVenue._value2member_map_  # !LL008
        again = getattr(RunMode, "_member_map_")  # !LL008
        """,
    ),
    case(
        "gateway-internals",
        "gie/learning/peek.py",
        """
        def peek(gateway):
            raw = gateway._adapter  # !LL008
            guard = gateway._guard  # !LL008
            return raw, guard, getattr(gateway, "_adapter")  # !LL008
        """,
    ),
    case(
        "gateway-internals-used-to-reach-the-adapter",
        "gie/execution/bypass.py",
        """
        def bypass(gateway, event):
            gateway._adapter.submit(event)  # !LL008, LL012
        """,
    ),
    case(
        "extend-enum-and-aenum",
        "gie/execution/hack.py",
        """
        import importlib

        import aenum  # !LL008
        from aenum import extend_enum  # !LL008

        from gie.core.routing_lock import RunMode

        extend_enum(RunMode, "LIVE", "LIVE")  # !LL008
        aenum.extend_enum(RunMode, "LIVE", "LIVE")  # !LL008
        importlib.import_module("aenum")  # !LL008
        """,
    ),
    case(
        "identity-attributes-assigned",
        "gie/execution/hack.py",
        """
        def disguise(adapter, other, cls):
            adapter.__class__ = other  # !LL008
            cls.__module__ = "gie.execution.simulated"  # !LL008
            cls.__slots__ = ()  # !LL008
            setattr(cls, "__module__", "gie.execution.simulated")  # !LL008
            del cls.__slots__  # !LL008
        """,
    ),
    case(
        "module-name-spoofed",
        "gie/risk/hack.py",
        """
        __name__ = "gie.execution.simulated"  # !LL008


        class Adapter:
            __module__ = "gie.execution.simulated"  # !LL008


        class Proxy:
            __class__ = Adapter  # !LL008


        Other = type("Other", (), {"__module__": "gie.execution.simulated"})  # !LL008
        Third = dict(__module__="gie.execution.simulated")  # !LL008
        """,
    ),
    case(
        "guard-redefined",
        "gie/execution/guard.py",
        """
        class RoutingGuard:  # !LL008
            def authorize(self, adapter):
                return None
        """,
    ),
    case(
        "gateway-redefined-or-rebound",
        "gie/risk/gateway.py",
        """
        from somewhere import Fake as RoutingGuard  # !LL008


        class GuardedExecutionAdapter:  # !LL008
            pass


        GuardedExecutionAdapter = object  # !LL008
        """,
    ),
]
LL008_CONTROLS = [
    case(
        "reading-the-lock",
        "gie/execution/status.py",
        """
        from gie.core import routing_lock
        from gie.core.routing_lock import LIVE_ROUTING_PERMITTED, RoutingGuard, RunMode


        def status(mode: RunMode) -> dict[str, object]:
            guard = RoutingGuard(mode)
            return {
                "locked": LIVE_ROUTING_PERMITTED is False and not routing_lock.LIVE_ROUTING_PERMITTED,
                "mode": guard.mode.value,
                "label": "LIVE_ROUTING_PERMITTED stays False",
            }
        """,
    ),
    case(
        "slots-in-a-class-body-and-reading-identity",
        "gie/execution/simulated.py",
        """
        class Book:
            __slots__ = ("orders",)

            def describe(self) -> str:
                return f"{type(self).__module__}.{self.__class__.__name__}"


        if __name__ == "__main__":
            raise SystemExit(0)
        """,
    ),
    case(
        "own-attributes",
        "gie/execution/state.py",
        """
        class State:
            def __init__(self, mode):
                self.mode = mode
                self._guarded = True
                self._adapter_count = 0
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL008_CASES)
def test_ll008_tampering_is_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL008_CONTROLS)
def test_ll008_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


def test_ll008_gateway_internals_are_allowed_in_the_gateway_only(tmp_path: Path) -> None:
    """The baseline gateway uses ``self._adapter`` / ``self._guard``; the same text elsewhere is reported."""
    assert cll.check(write_tree(tmp_path / "home")) == []
    copy = GATEWAY_SOURCE.replace("class GuardedExecutionAdapter:", "class Wrapper:")
    found = {
        violation.rule
        for violation in cll.check(write_tree(tmp_path / "copy", {"gie/learning/wrapper.py": copy}))
    }
    assert found == {"LL008"}


# ================================================================================================
# LL009 - writes that bypass frozen records
# ================================================================================================
LL009_CASES = [
    case(
        "object-setattr-and-delattr",
        "gie/risk/patch.py",
        """
        def patch(record):
            object.__setattr__(record, "stop_price", 0)  # !LL009
            object.__delattr__(record, "stop_price")  # !LL009
        """,
    ),
    case("reference-without-call", "gie/execution/patch.py", "setter = object.__setattr__  # !LL009\n"),
    case(
        "through-the-builtins-module",
        "gie/execution/patch.py",
        """
        import builtins

        builtins.object.__setattr__(1, "a", 2)  # !LL009
        """,
    ),
    case(
        "through-the-class-hierarchy",
        "gie/decision/patch.py",
        """
        class Base:
            pass


        class Plan(Base):
            def widen(self, record):
                type(record).__mro__[-1].__setattr__(record, "stop_price", 0)  # !LL009
                super(Base, self).__setattr__("stop_price", 0)  # !LL009
                record.__setattr__("stop_price", 0)  # !LL009
        """,
    ),
    case("reflective", "gie/reflex/patch.py", 'setter = getattr(object, "__setattr__")  # !LL009\n'),
    case("dashboard", "dashboard/gie/patch.py", 'object.__setattr__(1, "a", 2)  # !LL009\n'),
    case(
        "contracts-other-than-the-framework",
        "gie/contracts/extra.py",
        'object.__setattr__(1, "a", 2)  # !LL009\n',
    ),
]
LL009_CONTROLS = [
    case(
        "defining-setattr-and-zero-argument-super",
        "gie/execution/immutable.py",
        """
        class Immutable:
            __slots__ = ("value",)

            def __setattr__(self, name, value):
                if name != "value":
                    raise AttributeError(name)
                super().__setattr__(name, value)

            def __delattr__(self, name):
                raise AttributeError(name)
        """,
    ),
    case(
        "ordinary-setattr",
        "gie/execution/plain.py",
        """
        def configure(target):
            setattr(target, "volume", 1)
            target.volume = 2
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL009_CASES)
def test_ll009_bypassing_frozen_records_is_reported(
    tmp_path: Path, path: str, source: str, extra: Found
) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL009_CONTROLS)
def test_ll009_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


def test_ll009_record_framework_is_the_documented_exception(tmp_path: Path) -> None:
    """The baseline framework calls ``object.__setattr__``; the identical text elsewhere is reported."""
    assert FRAMEWORK in cll.SETATTR_EXEMPT_FILES
    assert "object.__setattr__" in BASELINE[FRAMEWORK]
    assert cll.check(write_tree(tmp_path / "home")) == []
    moved = write_tree(
        tmp_path / "moved", {"gie/contracts/_other.py": BASELINE[FRAMEWORK].replace("Record", "Other")}
    )
    assert {(v.rule, v.path) for v in cll.check(moved)} == {("LL009", "gie/contracts/_other.py")}


def test_ll009_pinned_lock_files_may_initialise_their_own_slots(tmp_path: Path) -> None:
    """The lock and the gateway are fixed by LL014; an immutable guard may set its slots this way."""
    assert {LOCK, GATEWAY, FRAMEWORK} == set(cll.SETATTR_EXEMPT_FILES)
    lock = edit(
        LOCK_SOURCE,
        append="""
        class Sealed:
            __slots__ = ("value",)

            def __init__(self, value: object) -> None:
                object.__setattr__(self, "value", value)
        """,
    )
    gateway = edit(
        GATEWAY_SOURCE,
        ("        self._adapter = adapter", '        object.__setattr__(self, "_adapter", adapter)'),
    )
    assert cll.check(write_tree(tmp_path, {LOCK: lock, GATEWAY: gateway})) == []


# ================================================================================================
# LL010 - subclassing
# ================================================================================================
REQUIRED_CONTRACT_RECORDS = {
    "Tick", "Evidence", "Candidate", "TradePlan", "RiskDecision", "OrderEvent", "Position", "ModelVersion",
    "ClockReading",
}  # fmt: skip
LOCKED_CLASS_IMPORTS = {
    "RoutingGuard": "from gie.core.routing_lock import RoutingGuard",
    "GuardedExecutionAdapter": "from gie.contracts import GuardedExecutionAdapter",
    "RunMode": "from gie.core.routing_lock import RunMode",
    "ExecutionVenue": "from gie.contracts.common import ExecutionVenue",
    "Record": "from gie.contracts._spec import Record",
}


def test_ll010_documented_sets() -> None:
    assert set(LOCKED_CLASS_IMPORTS) <= cll.FINAL_CLASS_NAMES
    assert REQUIRED_CONTRACT_RECORDS <= cll.CONTRACT_RECORD_NAMES


@pytest.mark.parametrize("name", sorted(LOCKED_CLASS_IMPORTS))
@pytest.mark.parametrize("path", ["gie/execution/mine.py", "gie/core/mine.py", "dashboard/gie/mine.py"])
def test_ll010_locked_classes_are_never_subclassed(tmp_path: Path, path: str, name: str) -> None:
    source = f"{LOCKED_CLASS_IMPORTS[name]}\n\n\nclass Mine({name}):  # !LL010\n    pass\n"
    assert_marked(tmp_path, path, source)


@pytest.mark.parametrize("name", sorted(REQUIRED_CONTRACT_RECORDS))
def test_ll010_contract_records_are_never_subclassed_outside_contracts(tmp_path: Path, name: str) -> None:
    source = src(
        f"""
        import gie.contracts
        from gie import contracts
        from gie.contracts import {name}
        from gie.contracts.records import {name} as Base


        class First({name}):  # !LL010
            pass


        class Second(Base):  # !LL010
            pass


        class Third(contracts.{name}):  # !LL010
            pass


        class Fourth(gie.contracts.records.{name}, object):  # !LL010
            pass
        """
    )
    assert_marked(tmp_path, "gie/prediction/mine.py", source)


LL010_CASES = [
    case(
        "aliases",
        "gie/execution/mine.py",
        """
        import gie.core.routing_lock as lock
        from gie.core.routing_lock import RoutingGuard as Base


        class First(Base):  # !LL010
            pass


        class Second(lock.RoutingGuard):  # !LL010
            pass


        Alias = lock.RunMode


        class Third(Alias):  # !LL010
            pass
        """,
    ),
    case(
        "generic-alias-and-nested-class",
        "gie/learning/mine.py",
        """
        from gie.contracts._spec import Record


        def build():
            class Local(Record[int]):  # !LL010
                pass

            return Local
        """,
    ),
    case(
        "star-import",
        "gie/learning/mine.py",
        """
        from gie.contracts import *


        class Mine(OrderEvent):  # !LL010
            pass
        """,
    ),
    case(
        "class-created-with-type",
        "gie/execution/mine.py",
        """
        from gie.core.routing_lock import RoutingGuard

        Mine = type("Mine", (RoutingGuard,), {})  # !LL010
        """,
    ),
]
LL010_CONTROLS = [
    case(
        "inside-the-contracts-package",
        "gie/contracts/extra.py",
        """
        from gie.contracts._spec import Record
        from gie.contracts.tick import Tick


        class New(Record):
            pass


        class Extended(Tick):
            pass
        """,
    ),
    case(
        "unrelated-classes-with-generic-names",
        "gie/risk/book.py",
        """
        from somewhere import Tick


        class Position:
            pass


        class Long(Position):
            pass


        class Fast(Tick):
            pass
        """,
    ),
    case(
        "implementing-an-interface",
        "gie/execution/simulated.py",
        """
        from gie.contracts.interfaces import TickSink


        class Sink(TickSink):
            def on_tick(self, tick):
                return None
        """,
    ),
    case(
        "using-without-subclassing",
        "gie/execution/use.py",
        """
        from gie.contracts import OrderEvent, Position


        def describe(event: OrderEvent, position: Position) -> tuple[type, type]:
            return type(event), type(position)
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL010_CASES)
def test_ll010_subclassing_is_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL010_CONTROLS)
def test_ll010_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


# ================================================================================================
# LL011 - adapter registration
# ================================================================================================
ADAPTER_MODULE = src(
    """
    from gie.core.routing_lock import ExecutionVenue, register_simulated_adapter{mark}


    @register_simulated_adapter{mark}
    class Simulated:
        __slots__ = ("book",)
        venue = ExecutionVenue.SIMULATED
        sends_real_orders = False

        def submit(self, request):
            self.book.submit(request)

        def cancel(self, request):
            self.book.cancel(request)

        def replace(self, request):
            self.book.replace(request)
    """
)
#: LL015 - the one-argument ``.replace`` of the adapter module (name and signature of ``Path.replace``).
ADAPTER_REPLACE: Found = {
    ("LL015", number)
    for number, line in enumerate(ADAPTER_MODULE.splitlines(), start=1)
    if "self.book.replace(request)" in line
}


@pytest.mark.parametrize("package", ["gie/execution/", "gie/backtesting/", "gie/contracts/"])
def test_ll011_registration_is_allowed_in_the_adapter_packages(tmp_path: Path, package: str) -> None:
    """Also the LL012 exemption: an adapter module may call its own collaborators.

    LL015 still reports the one-argument ``self.book.replace(request)``: by name and signature it
    may be ``Path.replace(target)`` (documented limit of LL015, not relaxed for adapter modules).
    """
    assert_marked(tmp_path, f"{package}simulated.py", ADAPTER_MODULE.format(mark=""), extra=ADAPTER_REPLACE)


@pytest.mark.parametrize(
    "package",
    [
        "gie/risk/",
        "gie/reflex/",
        "gie/decision/",
        "gie/market_data/",
        "gie/learning/",
        "gie/core/",
        "dashboard/gie/",
    ],
)
def test_ll011_registration_is_denied_everywhere_else(tmp_path: Path, package: str) -> None:
    assert_marked(
        tmp_path, f"{package}simulated.py", ADAPTER_MODULE.format(mark="  # !LL011"), extra=ADAPTER_REPLACE
    )


LL011_CASES = [
    case(
        "attribute-of-the-lock-module",
        "gie/risk/admit.py",
        """
        from gie.core import routing_lock

        routing_lock.register_simulated_adapter(object)  # !LL011
        """,
    ),
    case(
        "aliased-import",
        "gie/reflex/admit.py",
        """
        from gie.core.routing_lock import register_simulated_adapter as admit  # !LL011


        @admit
        class Simulated:
            pass
        """,
    ),
    case(
        "reflective",
        "gie/decision/admit.py",
        """
        from gie.core import routing_lock

        admit = getattr(routing_lock, "register_simulated_adapter")  # !LL011
        """,
    ),
    case(
        "star-import",
        "gie/learning/admit.py",
        """
        from gie.core.routing_lock import *

        register_simulated_adapter(object)  # !LL011
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL011_CASES)
def test_ll011_references_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


def test_ll011_lock_module_itself_defines_the_function(tmp_path: Path) -> None:
    assert "def register_simulated_adapter" in LOCK_SOURCE
    assert cll.check(write_tree(tmp_path)) == []


# ================================================================================================
# LL012 - adapter calls outside the gateway
# ================================================================================================
GATEWAY_PACKAGES = ("gie/execution/", "gie/reflex/", "gie/decision/", "gie/risk/", "gie/backtesting/")
PACKAGES_WITHOUT_A_GATEWAY = (
    "gie/core/", "gie/contracts/", "gie/market_data/", "gie/intermarket/", "gie/learning/", "gie/prediction/",
    "gie/strategy_intelligence/", "dashboard/gie/",
)  # fmt: skip
RAW_CALLS = src(
    """
    def run(broker, event, self):
        broker.submit(event){mark}
        broker.cancel(event){mark}
        broker.replace(event){mark}
        self.adapter.submit(event, plan=None){mark}
        self.engine.broker.cancel(event){mark}
    """
)
#: LL015 - ``broker.replace(event)`` has the name and the signature of ``Path.replace(target)``.
RAW_REPLACE: Found = {
    ("LL015", number)
    for number, line in enumerate(RAW_CALLS.splitlines(), start=1)
    if "broker.replace(event)" in line
}


def test_ll012_documented_convention() -> None:
    assert set(GATEWAY_PACKAGES) == set(cll.GATEWAY_PACKAGE_PREFIXES)
    assert frozenset({"gateway", "order_gateway", "_gateway", "_order_gateway"}) == cll.GATEWAY_NAMES
    assert frozenset({"submit", "cancel", "replace"}) == cll.ADAPTER_CALL_NAMES
    documentation = " ".join((cll.__doc__ or "").split())
    assert "naming convention" in documentation
    assert "heuristic" in documentation
    assert all(name in documentation for name in cll.GATEWAY_NAMES)


@pytest.mark.parametrize("package", GATEWAY_PACKAGES)
def test_ll012_raw_adapter_calls_are_reported_in_the_gateway_packages(tmp_path: Path, package: str) -> None:
    assert_marked(tmp_path, f"{package}engine.py", RAW_CALLS.format(mark="  # !LL012"), extra=RAW_REPLACE)


@pytest.mark.parametrize("package", PACKAGES_WITHOUT_A_GATEWAY)
def test_ll012_applies_only_to_the_gateway_packages(tmp_path: Path, package: str) -> None:
    assert_marked(tmp_path, f"{package}engine.py", RAW_CALLS.format(mark=""), extra=RAW_REPLACE)


LL012_CASES = [
    case(
        "receivers-that-are-not-names",
        "gie/execution/engine.py",
        """
        def run(adapters, make, event):
            adapters[0].submit(event)  # !LL012
            make().cancel(event)  # !LL012
            (adapters or make).replace(event)  # !LL012, LL015
        """,
    ),
    case(
        "a-gateway-inside-another-name-is-not-a-gateway",
        "gie/reflex/engine.py",
        """
        def run(self, gateways, event):
            self.gateway.inner.submit(event)  # !LL012
            gateways.submit(event)  # !LL012
            self.gateway_adapter.cancel(event)  # !LL012
        """,
    ),
    case(
        "replace-that-is-not-provably-text",
        "gie/decision/engine.py",
        """
        def run(text, event, old, new):
            text.replace(old, new)  # !LL012
            event.replace("a", new)  # !LL012
            event.replace(new)  # !LL012, LL015
            event.replace("a", "b", position=new)  # !LL012
            event.replace("a", "b", "c", "d")  # !LL012
        """,
    ),
    case(
        "reference-without-call-and-reflective-access",
        "gie/risk/engine.py",
        """
        def run(broker, event):
            send = broker.submit  # !LL012
            send(event)
            getattr(broker, "cancel")(event)  # !LL012
        """,
    ),
    case(
        "a-module-named-like-the-standard-library-is-not-the-standard-library",
        "gie/execution/engine.py",
        """
        import gie.execution.raw as dataclasses


        def run(event):
            dataclasses.replace(event)  # !LL012, LL015
        """,
    ),
    case(
        "a-rebound-name-is-not-a-module",
        "gie/execution/engine.py",
        """
        import dataclasses


        def run(make, event, str):
            dataclasses = make()
            dataclasses.replace(event)  # !LL012, LL015
            str.replace(event, 1, 2)  # !LL012
        """,
    ),
    case(
        "rule-as-specified-other-objects-with-these-method-names",
        "gie/reflex/timers.py",
        """
        def run(scheduler, pool, stamp, handle):
            scheduler.cancel(handle)  # !LL012
            pool.submit(print)  # !LL012
            stamp.replace(microsecond=0)  # !LL012
        """,
    ),
]
LL012_CONTROLS = [
    case(
        "gateway-by-naming-convention",
        "gie/execution/engine.py",
        """
        class Engine:
            def run(self, gateway, order_gateway, context, event, plan, decision):
                gateway.submit(event, plan=plan, decision=decision)
                gateway.cancel(event)
                gateway.replace(event)
                order_gateway.submit(event)
                self.gateway.cancel(event)
                self._gateway.replace(event)
                self._order_gateway.submit(event)
                context.order_gateway.cancel(event)
                send = gateway.submit
                getattr(gateway, "cancel")(event)
        """,
    ),
    case(
        "a-variable-named-gateway",
        "gie/risk/engine.py",
        """
        def run(make, event):
            gateway = make()
            gateway.submit(event)
        """,
    ),
    case(
        "str-replace",
        "gie/decision/text.py",
        """
        def tidy(name, old, new):
            first = "a-b".replace(old, new)
            second = name.replace("-", "_")
            third = name.replace("-", "_", 1)
            fourth = f"{name}!".replace(old, new)
            fifth = name.strip().lower().replace(" ", "")
            sixth = str.replace(name, old, new)
            seventh = b"a-b".replace(b"-", b"_")
            eighth = name.replace("-", "_", count=1)
            ninth = bytes.replace(name, old, new)
            return first, second, third, fourth, fifth, sixth, seventh, eighth, ninth
        """,
    ),
    case(
        "dataclasses-replace",
        "gie/backtesting/plans.py",
        """
        import copy
        import dataclasses
        import dataclasses as dc
        from dataclasses import replace


        def revise(plan):
            first = dataclasses.replace(plan, stop_price=1)
            second = dc.replace(plan, stop_price=2)
            third = replace(plan, stop_price=3)
            fourth = copy.replace(plan, stop_price=4)
            return first, second, third, fourth
        """,
    ),
    case(
        "other-methods",
        "gie/reflex/engine.py",
        """
        def run(broker, event, scheduler):
            broker.on_tick(event)
            broker.poll()
            scheduler.call_after(1, print)
            submit = 1
            cancel = replace = submit
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL012_CASES)
def test_ll012_adapter_calls_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL012_CONTROLS)
def test_ll012_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


def test_ll012_gateway_module_itself_is_outside_the_rule(tmp_path: Path) -> None:
    source = GATEWAY_SOURCE + "\n\ndef forward(raw: object, event: object) -> None:\n    raw.submit(event)\n"
    assert_marked(tmp_path, GATEWAY, source)


# ================================================================================================
# LL013 - the production tree is never addressed by code
# ================================================================================================
PRODUCTION = "ftmo" + "_eurusd"
LL013_SOURCE = src(
    f'''
    """Reads D:/{PRODUCTION}/logs."""  # !LL013

    ROOT = "D:\\\\{PRODUCTION}"  # !LL013
    UPPER = "d:/{PRODUCTION.upper()}/data"  # !LL013
    RAW = rb"D:\\{PRODUCTION}"  # !LL013
    JOINED = "D:/ftmo" + "_eurusd"  # !LL013


    def path(name):
        return f"D:/{PRODUCTION}/{{name}}"  # !LL013
    '''
)
LL013_CONTROL = src(
    f"""
    # D:/{PRODUCTION} is read-only and is never addressed by code
    ROOT = "D:/gie_paper"
    PARTS = ("ftmo", "eurusd")
    """
)


@pytest.mark.parametrize(
    "path",
    [
        "gie/core/paths.py",
        "gie/market_data/paths.py",
        "dashboard/gie/paths.py",
        "tools/helper.py",
        "tools/sub/helper.py",
        "tests/conftest.py",
        "tests/gie/core/test_paths.py",
        "tests/gie/qa/test_paths.py",
    ],
)
def test_ll013_production_path_literal_is_reported(tmp_path: Path, path: str) -> None:
    assert len(marked(LL013_SOURCE)) == 6
    assert_marked(tmp_path / "positive", path, LL013_SOURCE)
    assert_marked(tmp_path / "control", path, LL013_CONTROL)


@pytest.mark.parametrize("path", ["tools/audit/gie_baseline_audit.py", "tests/gie/tools/test_audit.py"])
def test_ll013_audit_tool_and_tool_tests_are_exempt(tmp_path: Path, path: str) -> None:
    assert set(cll.MARKER_EXEMPT_PREFIXES) == {"tools/audit/", "tests/gie/tools/"}
    write_tree(tmp_path, {path: LL013_SOURCE})
    assert cll.check(tmp_path) == []


def test_ll013_other_trees_are_not_scanned_at_all(tmp_path: Path) -> None:
    write_tree(tmp_path, {"docs/example.py": LL013_SOURCE, "reviews/session_00/x.py": "def broken(:\n"})
    assert cll.check(tmp_path) == []


def test_tools_and_tests_are_outside_the_deny_rules(tmp_path: Path) -> None:
    """They legitimately start processes; only LL013 (and LL000) looks at them."""
    source = src(
        """
        import subprocess
        import MetaTrader5 as mt5

        subprocess.run(["true"], check=False)
        mt5.order_send({})
        exec("x = 1")
        LIVE_ROUTING_PERMITTED = True
        """
    )
    write_tree(tmp_path, {"tools/ci.py": source, "tests/gie/core/test_x.py": source})
    assert cll.check(tmp_path) == []


def test_the_tool_does_not_contain_the_literal_it_searches_for() -> None:
    assert PRODUCTION not in Path(cll.__file__).read_text(encoding="utf-8").casefold()


# ================================================================================================
# LL015 - no file-drop channel: no file-system write below gie/ and dashboard/
# ================================================================================================
JOURNAL = "gie/contracts/journal.py"

LL015_CASES = [
    case(
        "open-with-a-writing-mode",
        "gie/execution/drop.py",
        """
        import io
        from pathlib import Path


        def drop(folder, name):
            open(folder + "/order.txt", "w")  # !LL015
            open(name, "a")  # !LL015
            open(name, mode="x")  # !LL015
            open(name, "r+")  # !LL015
            open(name, "wb", encoding=None)  # !LL015
            io.open(name, "w", encoding="utf-8")  # !LL015
            Path(folder).open("w")  # !LL015
            Path(folder).open(mode="ab")  # !LL015
            folder.open("a+")  # !LL015
            Path.open(folder, "w")  # !LL015
        """,
    ),
    case(
        "open-with-a-mode-that-cannot-be-verified",
        "gie/backtesting/drop.py",
        """
        def drop(name, mode, options, settings):
            open(name, mode)  # !LL015
            open(name, mode=settings.mode)  # !LL015
            open(name, *options)  # !LL015
            open(name, **settings)  # !LL015
            name.open(mode)  # !LL015
            name.open("r" if mode else "w")  # !LL015
        """,
    ),
    case(
        "open-through-an-alias-or-a-renamed-module",
        "gie/reflex/drop.py",
        """
        import io as files

        writer = open


        def drop(name):
            writer(name, "w")  # !LL015
            files.open(name, "a")  # !LL015
        """,
    ),
    case(
        "os-open-with-a-flag-that-writes-or-cannot-be-verified",
        "gie/core/drop.py",
        """
        import os


        def drop(name, flags):
            os.open(name, os.O_WRONLY | os.O_CREAT)  # !LL015
            os.open(name, os.O_RDWR)  # !LL015
            os.open(name, os.O_RDONLY | os.O_APPEND)  # !LL015
            os.open(name, flags)  # !LL015
            os.open(name, 1)  # !LL015
            os.open(name, flags=os.O_TRUNC)  # !LL015
            opener = os.open  # !LL015
        """,
    ),
    case(
        "path-methods-that-write",
        "gie/intermarket/drop.py",
        """
        from pathlib import Path


        def drop(folder, target, text, parts):
            order = Path(folder) / "order.txt"
            order.write_text(text)  # !LL015
            order.write_bytes(b"synthetic")  # !LL015
            order.touch()  # !LL015
            order.parent.mkdir(parents=True)  # !LL015
            order.unlink(missing_ok=True)  # !LL015
            order.parent.rmdir()  # !LL015
            order.symlink_to(target)  # !LL015
            order.hardlink_to(target)  # !LL015
            order.rename(target)  # !LL015
            order.replace(target)  # !LL015
            order.replace(target=target)  # !LL015
            order.rename(*parts)  # !LL015
            move = order.replace  # !LL015
            write = order.write_text  # !LL015
            getattr(order, "write_text")(text)  # !LL015
        """,
    ),
    case(
        "os-functions-that-write",
        "gie/market_data/drop.py",
        """
        import os
        import os as system
        from os import makedirs as make_all, remove  # !LL015


        def drop(name, target):
            os.rename(name, target)  # !LL015
            os.replace(name, target)  # !LL015
            os.remove(name)  # !LL015
            os.unlink(name)  # !LL015
            os.mkdir(name)  # !LL015
            os.makedirs(name, exist_ok=True)  # !LL015
            os.rmdir(name)  # !LL015
            os.symlink(name, target)  # !LL015
            os.link(name, target)  # !LL015
            os.truncate(name, 0)  # !LL015
            system.remove(name)  # !LL015
            remove(name)  # !LL015
            make_all(name)  # !LL015
            getattr(os, "rename")(name, target)  # !LL015
        """,
    ),
    case(
        "everything-in-shutil-and-tempfile",
        "gie/learning/drop.py",
        """
        import importlib
        import shutil  # !LL015
        import tempfile as scratch  # !LL015
        from shutil import copyfile  # !LL015


        def drop(name, target):
            shutil.copy(name, target)  # !LL015
            shutil.move(name, target)  # !LL015
            scratch.mkstemp()  # !LL015
            copyfile(name, target)  # !LL015
            importlib.import_module("tempfile").mkdtemp()  # !LL015
            getattr(shutil, "disk_usage")(name)  # !LL015
        """,
    ),
    case(
        "os-replace-in-a-gateway-package-is-a-write-not-an-adapter-call",
        "gie/backtesting/plans.py",
        """
        import os


        def revise(source, target):
            os.replace(source, target)  # !LL015
        """,
    ),
    case(
        "dashboard-is-scanned-too",
        "dashboard/gie/export.py",
        """
        from pathlib import Path

        Path("report.csv").write_text("synthetic")  # !LL015
        """,
    ),
]
LL015_CONTROLS = [
    case(
        "read-only-opens",
        "gie/market_data/read.py",
        """
        import io
        import os
        from pathlib import Path


        def read(name, folder):
            open(name)
            open(name, "r")
            open(name, "rb")
            open(name, mode="rt", encoding="utf-8")
            io.open(name, "rb")
            Path(folder).open()
            Path(folder).open("rb")
            Path.open(folder, "r")
            folder.open(mode="r")
            Path(folder).read_text()
            Path(folder).read_bytes()
            os.open(name, os.O_RDONLY)
            os.open(name, os.O_RDONLY | os.O_CLOEXEC)
            os.open(name, flags=0)
            os.path.exists(name)
            os.listdir(folder)
            os.stat(name)
        """,
    ),
    case(
        "rename-and-replace-that-cannot-be-path-methods",
        "gie/core/text.py",
        """
        import copy
        import dataclasses


        def tidy(name, stamp, plan, frame, old, new):
            first = name.replace(old, new)
            second = stamp.replace(microsecond=0)
            third = dataclasses.replace(plan)
            fourth = copy.replace(plan)
            fifth = str.replace(name, old, new)
            sixth = frame.rename(columns={"a": "b"})
            seventh = bytes.replace
            return first, second, third, fourth, fifth, sixth, seventh
        """,
    ),
    case(
        "the-order-gateway-amends-an-order",
        "gie/execution/engine.py",
        """
        class Engine:
            def run(self, gateway, request):
                gateway.replace(request)
                self._order_gateway.replace(request)
                getattr(gateway, "replace")(request)
        """,
    ),
]


@pytest.mark.parametrize(("path", "source", "extra"), LL015_CASES)
def test_ll015_file_system_writes_are_reported(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


@pytest.mark.parametrize(("path", "source", "extra"), LL015_CONTROLS)
def test_ll015_does_not_fire_on_controls(tmp_path: Path, path: str, source: str, extra: Found) -> None:
    assert not marked(source)
    assert_marked(tmp_path, path, source, extra=extra)


JOURNAL_LIKE = src(
    """
    from pathlib import Path


    def append(path, data):
        with Path(path).open("ab") as handle:
            handle.write(data)
        Path(f"{path}.lock").touch()
    """
)


def test_ll015_only_the_journal_module_may_write(tmp_path: Path) -> None:
    assert frozenset({JOURNAL}) == cll.FILE_WRITE_EXEMPT_FILES
    write_tree(tmp_path, {JOURNAL: JOURNAL_LIKE, "gie/contracts/journal_copy.py": JOURNAL_LIKE})
    found = {(violation.rule, violation.path, violation.line) for violation in cll.check(tmp_path)}
    assert found == {
        ("LL015", "gie/contracts/journal_copy.py", 5),
        ("LL015", "gie/contracts/journal_copy.py", 7),
    }


def test_ll015_exemption_is_documented_as_a_coordinator_decision() -> None:
    text = Path(cll.__file__).read_text(encoding="utf-8")
    before = text[: text.index("FILE_WRITE_EXEMPT_FILES: Final")]
    comment = " ".join(line.strip("#: ") for line in before.splitlines()[-3:])
    assert "coordinator decision" in comment
    assert "docs/DECISION_LOG.md" in comment
    documentation = " ".join((cll.__doc__ or "").split())
    assert "file-drop" in documentation or "file drop" in documentation
    assert "C extension" in documentation
    assert "subprocess" in documentation
    assert {"shutil", "tempfile"} == set(cll.FILE_WRITE_MODULES)
    assert {"rename", "replace", "remove", "unlink", "mkdir", "makedirs", "rmdir", "symlink", "link"} | {
        "truncate"
    } <= cll.OS_WRITE_FUNCTIONS
    assert {
        "write_text", "write_bytes", "touch", "mkdir", "rename", "replace", "unlink", "rmdir", "symlink_to",
        "hardlink_to",
    } <= cll.PATH_WRITE_METHODS  # fmt: skip


def test_ll015_tools_and_tests_may_write(tmp_path: Path) -> None:
    source = 'from pathlib import Path\n\nPath("out.json").write_text("{}")\nopen("x", "w")\n'
    write_tree(tmp_path, {"tools/report.py": source, "tests/gie/core/test_x.py": source})
    assert cll.check(tmp_path) == []


# ================================================================================================
# LL014 - pinned lock files
# ================================================================================================
def test_ll014_pin_file_of_the_repository_has_the_documented_shape() -> None:
    document = json.loads((REPO_ROOT / PINS).read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "files"}
    assert document["schema_version"] == 1
    assert set(document["files"]) == {LOCK, GATEWAY}
    assert all(
        len(digest) == 64 and set(digest) <= set("0123456789abcdef") for digest in document["files"].values()
    )


@pytest.mark.parametrize("changed", [LOCK, GATEWAY])
def test_ll014_changed_file_no_longer_matches_its_pin(tmp_path: Path, changed: str) -> None:
    write_tree(tmp_path)
    target = tmp_path / changed
    target.write_bytes(target.read_bytes() + b"\n# SYNTHETIC FIXTURE: one comment more\n")
    violations = cll.check(tmp_path)
    assert [(v.rule, v.path, v.line) for v in violations] == [("LL014", changed, WHOLE_FILE)]
    assert sha256(target) in violations[0].message
    assert "decision-log" in violations[0].message


@pytest.mark.parametrize("changed", [LOCK, GATEWAY])
def test_ll014_pins_are_over_bytes_not_text(tmp_path: Path, changed: str) -> None:
    write_tree(tmp_path)
    target = tmp_path / changed
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert [(v.rule, v.path) for v in cll.check(tmp_path)] == [("LL014", changed)]


@pytest.mark.parametrize("pinned", [LOCK, GATEWAY])
def test_ll014_wrong_pin(tmp_path: Path, pinned: str) -> None:
    write_tree(tmp_path)
    write_pins(tmp_path, {pinned: "0" * 64})
    assert [(v.rule, v.path, v.line) for v in cll.check(tmp_path)] == [("LL014", pinned, WHOLE_FILE)]


def pins_document(root: Path, **changes: object) -> str:
    document: dict[str, object] = {
        "schema_version": 1,
        "files": {LOCK: sha256(root / LOCK), GATEWAY: sha256(root / GATEWAY)},
    }
    document.update(changes)
    return json.dumps(document)


INVALID_PINS = [
    pytest.param(lambda root: None, id="missing"),
    pytest.param(lambda root: "", id="empty"),
    pytest.param(lambda root: "{not json", id="not-json"),
    pytest.param(lambda root: "[]", id="not-an-object"),
    pytest.param(lambda root: b"\xff\xfe", id="not-utf8"),
    pytest.param(lambda root: pins_document(root, schema_version=2), id="other-schema-version"),
    pytest.param(lambda root: pins_document(root, schema_version="1"), id="schema-version-as-text"),
    pytest.param(lambda root: pins_document(root, schema_version=True), id="schema-version-as-bool"),
    pytest.param(lambda root: pins_document(root, note="approved"), id="unknown-key"),
    pytest.param(lambda root: json.dumps({"schema_version": 1}), id="no-files"),
    pytest.param(lambda root: pins_document(root, files=[LOCK, GATEWAY]), id="files-not-a-mapping"),
    pytest.param(
        lambda root: pins_document(root, files={LOCK: sha256(root / LOCK)}), id="gateway-not-pinned"
    ),
    pytest.param(
        lambda root: pins_document(root, files={GATEWAY: sha256(root / GATEWAY)}), id="lock-not-pinned"
    ),
    pytest.param(lambda root: pins_document(root, files={}), id="nothing-pinned"),
    pytest.param(
        lambda root: pins_document(
            root, files={LOCK: sha256(root / LOCK).upper(), GATEWAY: sha256(root / GATEWAY)}
        ),
        id="upper-case-digest",
    ),
    pytest.param(
        lambda root: pins_document(
            root, files={LOCK: sha256(root / LOCK)[:32], GATEWAY: sha256(root / GATEWAY)}
        ),
        id="short-digest",
    ),
    pytest.param(
        lambda root: pins_document(root, files={LOCK: None, GATEWAY: sha256(root / GATEWAY)}),
        id="digest-is-null",
    ),
    pytest.param(
        lambda root: pins_document(
            root,
            files={LOCK: sha256(root / LOCK), GATEWAY: sha256(root / GATEWAY), "../outside.py": "0" * 64},
        ),
        id="path-outside-the-repository",
    ),
]


@pytest.mark.parametrize("make", INVALID_PINS)
def test_ll014_invalid_pin_file_is_a_violation(tmp_path: Path, make: object) -> None:
    write_tree(tmp_path, pin=False)
    assert callable(make)
    content = make(tmp_path)
    if content is not None:
        target = tmp_path / PINS
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    found = [(v.rule, v.path, v.line) for v in cll.check(tmp_path)]
    assert found, "an invalid pin file must be reported"
    assert set(found) == {("LL014", PINS, WHOLE_FILE)}


def test_ll014_additional_pins_are_verified_too(tmp_path: Path) -> None:
    extra = "gie/contracts/authorization.py"
    write_tree(tmp_path, {extra: "VALUE = 1\n"})
    write_pins(tmp_path, {extra: sha256(tmp_path / extra)})
    assert cll.check(tmp_path) == []
    (tmp_path / extra).write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    assert [(v.rule, v.path) for v in cll.check(tmp_path)] == [("LL014", extra)]
    (tmp_path / extra).unlink()
    assert [(v.rule, v.path) for v in cll.check(tmp_path)] == [("LL014", extra)]


# ================================================================================================
# --update-pins
# ================================================================================================
def test_update_pins_rewrites_the_pin_file_and_says_so_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tree(tmp_path)
    (tmp_path / LOCK).write_text(
        LOCK_SOURCE + "\n# SYNTHETIC FIXTURE: reviewed change\n", encoding="utf-8", newline="\n"
    )
    assert cll.main(["--root", str(tmp_path)]) == 1
    capsys.readouterr()

    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("check_live_lock: OK")
    notice = captured.err
    assert "WAS REWRITTEN" in notice
    assert "DECISION-LOG ENTRY" in notice
    assert "docs/DECISION_LOG.md" in notice
    assert f"{LOCK}: " in notice
    assert sha256(tmp_path / LOCK) in notice
    assert f"{GATEWAY}: unchanged" in notice

    written = (tmp_path / PINS).read_bytes()
    assert json.loads(written) == {
        "schema_version": 1,
        "files": {LOCK: sha256(tmp_path / LOCK), GATEWAY: sha256(tmp_path / GATEWAY)},
    }
    assert written.endswith(b"\n")
    assert b"\r" not in written
    assert cll.check(tmp_path) == []

    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 0
    assert (tmp_path / PINS).read_bytes() == written, "updating twice gives the same bytes"


def test_update_pins_creates_a_missing_pin_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(tmp_path, pin=False)
    assert [violation.rule for violation in cll.check(tmp_path)] == ["LL014"]
    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 0
    assert "not pinned ->" in capsys.readouterr().err
    assert cll.check(tmp_path) == []


def test_update_pins_keeps_additional_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extra = "gie/contracts/authorization.py"
    write_tree(tmp_path, {extra: "VALUE = 1\n"})
    write_pins(tmp_path, {extra: "0" * 64})
    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 0
    capsys.readouterr()
    assert json.loads((tmp_path / PINS).read_text(encoding="utf-8"))["files"][extra] == sha256(
        tmp_path / extra
    )


WEAKENED_LOCKS = [
    pytest.param(LOCK, (CONSTANT, "LIVE_ROUTING_PERMITTED: Final = True"), "LL004", id="unlocked-constant"),
    pytest.param(LOCK, (PAPER, PAPER + '\n    LIVE = "LIVE"'), "LL005", id="live-mode"),
    pytest.param(LOCK, ("import threading", "import threading\nimport os"), "LL006", id="reads-environment"),
    pytest.param(LOCK, ("def authorize(", "def allow("), "LL007", id="guard-without-authorize"),
    pytest.param(GATEWAY, ("def submit(", "def send("), "LL007", id="gateway-without-submit"),
    pytest.param(
        GATEWAY, ("from __future__", "import subprocess\nfrom __future__"), "LL003", id="gateway-escape"
    ),
]


@pytest.mark.parametrize(("path", "change", "rule"), WEAKENED_LOCKS)
def test_update_pins_refuses_to_pin_a_weakened_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str, change: tuple[str, str], rule: str
) -> None:
    write_tree(tmp_path)
    before = (tmp_path / PINS).read_bytes()
    target = tmp_path / path
    target.write_text(edit(target.read_text(encoding="utf-8"), change), encoding="utf-8", newline="\n")

    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 1
    captured = capsys.readouterr()
    assert "PINS NOT UPDATED" in captured.err
    assert rule in captured.err
    assert (tmp_path / PINS).read_bytes() == before, "a weakened lock is never pinned"
    assert {violation.rule for violation in cll.check(tmp_path)} >= {rule, "LL014"}


def test_update_pins_refuses_when_a_lock_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tree(tmp_path)
    before = (tmp_path / PINS).read_bytes()
    (tmp_path / GATEWAY).unlink()
    assert cll.main(["--root", str(tmp_path), "--update-pins"]) == 1
    assert "PINS NOT UPDATED" in capsys.readouterr().err
    assert (tmp_path / PINS).read_bytes() == before


def test_update_pins_function_reports_what_it_did(tmp_path: Path) -> None:
    write_tree(tmp_path, pin=False)
    written, notice = cll.update_pins(tmp_path)
    assert written is True
    assert any("DECISION-LOG" in line for line in notice)
    assert (tmp_path / PINS).is_file()


# ================================================================================================
# Command line, output formats and the API
# ================================================================================================
DIRTY: Mapping[str, str] = {
    "gie/execution/b.py": "import MetaTrader5 as mt5\n\nmt5.order_send({})\nimport subprocess\n",
    "gie/execution/a.py": "broker.submit(1)\nexec('x')\n",
    "dashboard/gie/c.py": "def broken(:\n",
}
DIRTY_FOUND = [
    ("dashboard/gie/c.py", 1, "LL000"),
    ("gie/execution/a.py", 1, "LL012"),
    ("gie/execution/a.py", 2, "LL003"),
    ("gie/execution/b.py", 1, "LL002"),
    ("gie/execution/b.py", 3, "LL001"),
    ("gie/execution/b.py", 4, "LL003"),
]


def test_violation_is_a_frozen_dataclass_with_the_documented_fields() -> None:
    assert [field.name for field in dataclasses.fields(cll.Violation)] == ["rule", "path", "line", "message"]
    violation = cll.Violation("LL001", "gie/x.py", 3, "message")
    with pytest.raises(dataclasses.FrozenInstanceError):
        violation.line = 4  # type: ignore[misc]
    assert violation.render() == "gie/x.py:3: LL001 message"
    assert violation == cll.Violation("LL001", "gie/x.py", 3, "message")


def test_check_returns_sorted_violations_with_posix_paths_and_is_repeatable(tmp_path: Path) -> None:
    write_tree(tmp_path, DIRTY)
    first = cll.check(tmp_path)
    assert [(v.path, v.line, v.rule) for v in first] == DIRTY_FOUND
    assert all(isinstance(v, cll.Violation) and v.message and "\n" not in v.message for v in first)
    assert cll.check(tmp_path) == first
    assert cll.check(Path(str(tmp_path) + "/.")) == first


def test_check_never_imports_executes_or_changes_the_scanned_tree(tmp_path: Path) -> None:
    bomb = src(
        """
        from pathlib import Path

        Path(__file__).with_name("EXECUTED").write_text("SYNTHETIC FIXTURE: the scanner ran this file")
        raise RuntimeError("SYNTHETIC FIXTURE: the scanner executed scanned code")
        """
    )
    write_tree(tmp_path, {**DIRTY, "gie/execution/bomb.py": bomb, "gie/execution/__init__.py": bomb})
    snapshot = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    modules = set(sys.modules)

    cll.check(tmp_path)

    assert {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()} == snapshot
    assert not list(tmp_path.rglob("EXECUTED"))
    assert not list(tmp_path.rglob("__pycache__"))
    assert set(sys.modules) == modules


def test_main_clean_tree_prints_one_ok_line_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tree(tmp_path)
    assert cll.main(["--root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("check_live_lock: OK")
    assert f"{len(BASELINE)} Python files scanned" in lines[0]


def test_main_prints_one_line_per_violation_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tree(tmp_path, DIRTY)
    assert cll.main(["--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines == [violation.render() for violation in cll.check(tmp_path)]
    assert [line.split(" ")[0:2] for line in lines] == [
        [f"{path}:{line}:", rule] for path, line, rule in DIRTY_FOUND
    ]
    assert "FAILED" in captured.err
    assert f"{len(DIRTY_FOUND)} violation(s)" in captured.err


def test_main_json_output_is_one_valid_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(tmp_path, DIRTY)
    assert cll.main(["--root", str(tmp_path), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["schema_version"] == 1
    assert document["violation_count"] == len(DIRTY_FOUND)
    assert document["files_scanned"] == len(BASELINE) + len(DIRTY)
    assert document["rules"] == dict(cll.RULES)
    assert document["violations"] == [dataclasses.asdict(violation) for violation in cll.check(tmp_path)]
    assert [(v["path"], v["line"], v["rule"]) for v in document["violations"]] == DIRTY_FOUND


def test_main_json_output_of_a_clean_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(tmp_path)
    assert cll.main(["--root", str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ok"] is True
    assert document["violations"] == []
    assert document["violation_count"] == 0
    assert captured.err == ""


def test_main_json_stays_valid_together_with_update_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tree(tmp_path, pin=False)
    assert cll.main(["--root", str(tmp_path), "--json", "--update-pins"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "DECISION-LOG" in captured.err


def test_main_rejects_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cll.main(["--root", str(tmp_path / "absent")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a directory" in captured.err


def test_main_rejects_unknown_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cll.main(["--enable-live"])
    assert raised.value.code == 2
    capsys.readouterr()


def test_module_runs_as_a_script_with_the_documented_exit_codes(tmp_path: Path) -> None:
    def run(root: Path, *options: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "tools.check_live_lock", "--root", str(root), *options]
        return subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=120
        )

    clean = run(write_tree(tmp_path / "clean"))
    assert (clean.returncode, clean.stderr) == (0, "")
    assert clean.stdout.startswith("check_live_lock: OK")

    dirty = run(write_tree(tmp_path / "dirty", DIRTY), "--json")
    assert dirty.returncode == 1
    assert [(v["path"], v["line"], v["rule"]) for v in json.loads(dirty.stdout)["violations"]] == DIRTY_FOUND
