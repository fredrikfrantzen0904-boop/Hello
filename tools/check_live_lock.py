"""Static scan of the paper-only routing lock - layer 4 of ``gie/core/routing_lock.py``.

Requirements: G08-03 (live lock), G08-10 / M-AUTH-19 / OWN-14 (real execution stays disabled),
S12-ASG-06 (check live lock); acceptance test S00-ACC-04.

Real order routing is locked for the whole project phase.  The run-time layers of the lock
(vocabulary, constant, guard and gateway) stop mistakes while the program runs; everything they
cannot see - a broker call written into GIE code, a broker SDK import, a process or network escape,
a weakened lock module - is only detectable by reading the source.  That is what this tool does, in
CI and on demand.  It is **deny by default**: what it cannot read or cannot verify is a violation.

Usage::

    python -m tools.check_live_lock [--root PATH] [--json] [--update-pins]

One line per violation (``path:line: RULE message``) on standard output and exit status 1; with no
violation a one-line ``OK`` summary and exit status 0; exit status 2 for a command-line error.
``--json`` prints one JSON document instead.  ``--update-pins`` rewrites
``specifications/lock_pins.json`` (refused while a pinned file breaks a rule) and then checks.

How it works
------------
Python files are parsed with :mod:`ast` only.  Scanned code is never imported or executed and no
regular expression is run over code.  Every ``*.py`` (and ``*.pyw``) file below ``gie/`` and
``dashboard/`` is scanned by the deny rules; ``tools/`` and ``tests/`` are not (they legitimately
start processes), except for LL013.  Paths are POSIX and relative to the scanned root; line 0 means
"the file as a whole".

Names are resolved through the import table of each file before a rule looks at them:
``import x as y``, ``from x import y as z``, relative imports, ``from x import *``,
``importlib.import_module("x")`` / ``__import__("x")`` with a literal name, ``sys.modules["x"]``,
plain aliases (``run = eval``, ``mode = RunMode``) and re-assignments of anything that rests on an
import (``api = mt5``, ``send = api.order_send``).  The resolution is flow-insensitive and ignores
scopes, so it over-approximates: a name counts as everything it is bound to anywhere in the file.
A file in which one name acquires more meanings than the scan follows is reported (LL000), not
guessed.  String arguments are folded when they are fixed in the source (literals, ``"a" + "b"``,
f-strings without real placeholders).

Rules
-----
LL000  Scanned code cannot be verified: syntax error, undecodable or unreadable source, a file
       nested too deeply to analyse, names that cannot be resolved - and importable files that are
       not source at all (extension modules, byte code outside ``__pycache__``).  Reported, never a
       crash.
LL001  Broker order-entry names (:data:`ORDER_ENTRY_NAMES`): any attribute access, any call, any
       ``from ... import`` name and any literal attribute name passed to ``getattr`` and its
       relatives.  **Names are matched syntactically** - the scan cannot know what an object is - so
       GIE's own simulated adapters use the neutral names ``submit`` / ``cancel`` / ``replace`` of
       ``gie.contracts.interfaces.ExecutionAdapter`` and never a broker's spelling.  ``Buy``,
       ``Sell`` and their limit / stop variants are flagged as attributes (``trade.Buy(...)``) and
       as imported names; a bare local name ``Buy`` is too generic to flag.  ``order_calc_margin``
       is read-only and deliberately not in the set.  Inside the broker facade the rule still
       applies, and what cannot be verified there is a violation too: ``getattr`` with a computed
       name, ``vars()``, ``__dict__`` and ``from <sdk> import *``.
LL002  Broker SDK imports (:data:`BROKER_SDK_MODULES`) are allowed in exactly one module, the
       read-only facade ``gie/market_data/broker_readonly.py``, and nowhere else.
LL003  Escapes.  (a) Network modules (:data:`NETWORK_MODULES`) below ``gie/``, unless
       :data:`NETWORK_EXEMPTIONS` allows the module for the path prefix (market data and
       intermarket may read feeds).  (b) Process and native modules
       (:data:`PROCESS_AND_NATIVE_MODULES`) and the process functions of ``os`` in all scanned
       code; no exemption exists.  (c) Code the scan cannot follow, in all scanned code: ``eval``,
       ``exec``, the built-in ``compile`` and dynamic imports whose module name is not a literal.
LL004  ``LIVE_ROUTING_PERMITTED`` is bound exactly once, at module level of the lock module, to the
       literal ``False`` (optionally annotated ``Final``); never rebound, augmented, deleted,
       declared ``global`` or written through ``globals()`` / ``setattr``.
LL005  ``RunMode`` has exactly the members REPLAY and PAPER, ``ExecutionVenue`` exactly SIMULATED,
       each assigned the string equal to its name; the classes contain nothing else and are defined
       once, in the lock module only.
LL006  The lock module imports only from :data:`LOCK_IMPORT_ALLOWLIST` and uses no ``open``,
       ``eval``, ``exec``, ``compile``, ``__import__``, no name ``os`` and no attribute ``environ``,
       ``getenv`` or ``argv``: the lock reads its state from nowhere.
LL007  The lock exists: the lock module defines the constant, ``RunMode``, ``ExecutionVenue``,
       ``parse_run_mode``, ``assert_live_routing_locked``, ``register_simulated_adapter`` and
       ``RoutingGuard.authorize``; the gateway module defines ``GuardedExecutionAdapter`` with
       ``submit``, ``cancel``, ``replace`` and ``poll``.
LL008  Tampering outside the lock module: writing ``LIVE_ROUTING_PERMITTED``; writing any attribute
       of the lock module, the gateway module, ``RoutingGuard``, ``GuardedExecutionAdapter``,
       ``RunMode`` or ``ExecutionVenue``; redefining the guard or the gateway; touching the
       registry or enum internals (:data:`LOCK_INTERNAL_NAMES`, and outside the gateway
       :data:`GATEWAY_INTERNAL_NAMES`); ``extend_enum`` / ``aenum``; assigning ``__module__``,
       ``__class__`` or ``__slots__`` of anything.
LL009  ``object.__setattr__`` / ``object.__delattr__`` (and any explicit ``__setattr__`` /
       ``__delattr__`` call that does not go through zero-argument ``super()``) defeat frozen
       records and the immutable guard.  Allowed only in :data:`SETATTR_EXEMPT_FILES`.
LL010  Outside ``gie/contracts/`` no class subclasses ``RoutingGuard``, ``GuardedExecutionAdapter``,
       ``RunMode``, ``ExecutionVenue``, ``Record`` or a contract record imported from
       ``gie.contracts`` (:data:`CONTRACT_RECORD_NAMES`).
LL011  ``register_simulated_adapter`` is referenced only where simulated adapters may live:
       ``gie/execution/``, ``gie/backtesting/``, ``gie/contracts/`` and the lock module.
LL012  Adapter calls.  In the packages that can hold an order gateway
       (:data:`GATEWAY_PACKAGE_PREFIXES`) ``<expr>.submit(...)``, ``<expr>.cancel(...)`` and
       ``<expr>.replace(...)`` are allowed only on a gateway.  The scan has no types, so a gateway
       is recognised by **naming convention**: the receiver is a name or attribute called
       ``gateway``, ``order_gateway``, ``_gateway`` or ``_order_gateway``
       (a ``GuardedExecutionAdapter``).  Files that reference ``register_simulated_adapter`` are
       adapter modules and are exempt.  ``str.replace`` would be a constant false positive, so a
       ``.replace`` call is *not* flagged when (heuristic) the receiver is a string literal or
       f-string, or the call has two literal string arguments, optionally followed by a third
       literal (a string or the integer count) - such a call cannot carry an order.  Provably
       harmless and therefore not flagged either: the receiver is the built-in ``str`` / ``bytes``
       class, or the call is one of :data:`STDLIB_REPLACE_FUNCTIONS` (``dataclasses.replace(...)``)
       on a module imported under that name and not rebound in the file.
LL013  No string literal names the production tree (the read-only directory of CLAUDE.md rule 2),
       in any case, in Python files below ``gie/``, ``dashboard/``, ``tools/`` (except
       ``tools/audit/``) and ``tests/`` (except ``tests/gie/tools/``): code never addresses it.
LL014  ``specifications/lock_pins.json`` pins the SHA-256 of the bytes of the lock module and the
       gateway module.  A changed file fails until the pin is updated - which needs a decision-log
       entry and the owner's approval.
LL015  No file-drop channel: below ``gie/`` and ``dashboard/`` no code writes to the file system.
       A simulated adapter that writes an order file into a terminal's exchange folder would be a
       real routing channel that leaves the process without any network call.  Reported:
       ``open()`` / ``io.open()`` / ``Path.open()`` (any call of a function or method named
       ``open``) with a mode that contains ``w``, ``a``, ``x`` or ``+``, and a mode that is not a
       string fixed in the source (it cannot be verified); ``os.open`` with any flag but the
       read-only ones (:data:`READ_ONLY_OPEN_FLAGS`); the path methods
       (:data:`PATH_WRITE_METHODS`) ``write_text``, ``write_bytes``, ``touch``, ``mkdir``,
       ``unlink``, ``rmdir``, ``symlink_to``, ``hardlink_to``, ``link_to``, ``rename`` and
       ``replace``; the ``os`` functions of :data:`OS_WRITE_FUNCTIONS`; and everything in
       ``shutil`` and ``tempfile`` (:data:`FILE_WRITE_MODULES`: every import, every use and every
       literal ``getattr``).  The only exempt file is the journal module
       (:data:`FILE_WRITE_EXEMPT_FILES`); every addition is a coordinator decision recorded in
       ``docs/DECISION_LOG.md``.  The scan has no types, so the path methods are matched **by
       name** on any receiver.  ``rename`` / ``replace`` are matched by name *and signature*:
       ``Path.rename(target)`` / ``Path.replace(target)`` take exactly one argument, so a call with
       exactly one argument (positional or ``target=``), a call with ``*`` / ``**`` arguments and
       an uncalled reference are reported; a call with any other arity (``str.replace(old, new)``,
       ``datetime.replace(microsecond=0)``) cannot be the path method and is not.  Not reported
       either: the order gateway by the naming convention of LL012 (``gateway.replace(request)``
       amends an order), the built-in ``str`` / ``bytes`` classes and ``dataclasses.replace`` /
       ``copy.replace`` on the imported module.  The mode of an ``open`` call is its second
       positional argument when the function is a plain name (``open``, ``io.open`` imported by
       name) or an attribute of an imported module or class (``io.open``, ``codecs.open``,
       ``Path.open(path, mode)``), else its first (``path.open(mode)``); ``mode=`` wins over both.

Differences from the minimum rule text (each covered by a test)
---------------------------------------------------------------
Stricter:

* LL000 also reports importable non-source files and files whose names cannot be resolved.
* Literal dynamic imports count as imports for LL002, LL003, LL006 and LL008.
* LL003 (b) includes ``_ctypes``, ``_posixsubprocess``, ``_winapi``, ``os.posix_spawn*``,
  ``os.forkpty``, the modules ``posix`` / ``nt`` when imported, ``asyncio`` subprocess functions and
  ``concurrent.futures.ProcessPoolExecutor``; (a) includes ``_socket``, ``_ssl``, ``socketserver``,
  ``xmlrpc``, ``imaplib``, ``poplib``, ``nntplib`` and the ``asyncio`` stream functions (treated as
  ``socket``); (c) is not in the original list at all - without it every import rule could be
  bypassed with one ``exec``.
* LL005: the vocabulary classes may contain nothing but their members (no methods, decorators,
  keywords), and the names ``RunMode`` / ``ExecutionVenue`` may not be bound to anything else
  anywhere (function, assignment, renamed import).
* LL008 also covers the gateway module as a protected module, redefinition of ``RoutingGuard`` /
  ``GuardedExecutionAdapter`` outside their modules, writes of the constant through a mapping
  (``vars(module)[...]``, ``__dict__.update``), bindings of ``__module__`` / ``__class__`` /
  ``__name__`` and a ``"__module__"`` dictionary key or keyword (they spoof the module allow-lists
  of the lock and of the record framework).
* LL009 reports every explicit ``__setattr__`` / ``__delattr__``, not only the one of ``object``.
* LL010 also reports ``type("X", (RoutingGuard,), {})``; LL012 also reports ``<expr>.submit`` used
  without a call and ``getattr(<expr>, "submit")``.
* LL015 also reports ``Path.link_to`` and ``os.renames`` / ``removedirs`` / ``mkfifo`` / ``mknod``
  / ``ftruncate``, references to a write function without a call and literal ``getattr`` access
  to one.

Wider (provably harmless cases only):

* LL009 exempts the two pinned lock files in addition to the record framework: their content is
  fixed by LL014, and an immutable guard may need ``object.__setattr__`` in its constructor.
* LL012 does not flag ``str.replace(...)`` / ``bytes.replace(...)`` on the built-in classes, the
  functions of :data:`STDLIB_REPLACE_FUNCTIONS`, an integer as third literal argument of
  ``.replace`` and its ``count=`` keyword.

Honest limits
-------------
A static scan is not a sandbox.  It does not follow values through function calls, containers or
attributes of objects, it cannot see strings that are computed at run time, and it does not read
code outside the scanned trees (installed packages, ``tools/``).  A person who sets out to defeat it
can; the pinned hashes, the tests and review exist for that.  What the scan guarantees is that the
ordinary ways of placing a real order - and the ordinary ways of switching the lock off - cannot be
written into GIE code by mistake or convenience without failing CI.

LL015 sees the writes that are spelled in the scanned code.  A write through a C extension or a
subprocess is covered by the existing rules (LL003 forbids ``ctypes``, ``cffi``, ``subprocess``
and the process functions of ``os``) or not at all: a function of an installed package that writes
a file (``numpy.save``, ``DataFrame.to_csv``, ``zipfile.ZipFile(..., "w")``, ``sqlite3.connect``,
``logging.FileHandler``, ``shelve``, ``dbm``) is not recognised, and neither is a write method
reached through a value the resolution does not follow (``opener = path.open`` then
``opener("w")``) or a mode that only a function call returns at run time *and* that is passed to a
function not named ``open``.  Like LL012 the rule knows names, not types: an object with a method
``touch`` or ``unlink`` that is not a path, and a one-argument ``.replace(x)`` of an object that is
not a path, are reported; that is deliberately not relaxed here.

LL012 knows names, not types.  In the gateway packages it therefore also reports objects that are
not adapters but have a method called ``submit``, ``cancel`` or ``replace`` - a timer scheduler
(``scheduler.cancel(handle)``), an executor (``pool.submit(job)``), ``datetime.replace(...)``.  The
rule is deliberately not relaxed for them here; that is a decision for the coordinator.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# ============================================================================== configuration
#: Rule ids and their one-line titles (also written to the ``--json`` output).
RULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "LL000": "scanned code cannot be parsed",
        "LL001": "broker order-entry name",
        "LL002": "broker SDK import outside the read-only facade",
        "LL003": "process, native, network or dynamic-code escape",
        "LL004": "lock constant is not the literal False, bound once",
        "LL005": "lock vocabulary changed or redefined",
        "LL006": "lock module is not isolated",
        "LL007": "part of the lock is missing",
        "LL008": "tampering with the lock",
        "LL009": "write that bypasses a frozen record or the immutable guard",
        "LL010": "subclass of a locked class or of a contract record",
        "LL011": "adapter registration outside the adapter packages",
        "LL012": "adapter call that does not go through the order gateway",
        "LL013": "string literal names the production tree",
        "LL014": "pinned lock file changed or pins invalid",
        "LL015": "file-system write (file-drop channel)",
    }
)

LOCK_MODULE_PATH: Final = "gie/core/routing_lock.py"
GATEWAY_MODULE_PATH: Final = "gie/contracts/guarded.py"
RECORD_FRAMEWORK_PATH: Final = "gie/contracts/_spec.py"
#: The single module that may import a broker SDK (read-only market data; may not exist yet).
BROKER_FACADE_PATH: Final = "gie/market_data/broker_readonly.py"
PINS_PATH: Final = "specifications/lock_pins.json"
#: Files whose SHA-256 must be pinned (LL014).
PINNED_FILES: Final[tuple[str, ...]] = (LOCK_MODULE_PATH, GATEWAY_MODULE_PATH)
PINS_SCHEMA_VERSION: Final = 1

LOCK_MODULE: Final = "gie.core.routing_lock"
GATEWAY_MODULE: Final = "gie.contracts.guarded"
LOCK_CONSTANT: Final = "LIVE_ROUTING_PERMITTED"
REGISTRATION_NAME: Final = "register_simulated_adapter"

#: Trees scanned by every deny rule.
DENY_ROOTS: Final[tuple[str, ...]] = ("gie", "dashboard")
#: Trees scanned by LL013 only (they legitimately use subprocess and similar).
MARKER_ONLY_ROOTS: Final[tuple[str, ...]] = ("tools", "tests")
#: Path prefixes that may name the production tree: the audit tool and the tests of the tools.
MARKER_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("tools/audit/", "tests/gie/tools/")
# Assembled at import so that this file does not contain the literal it searches for (LL013 scans tools/).
_PRODUCTION_TREE_MARKER: Final = "_".join(("ftmo", "eurusd"))

SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyw"})
#: Importable files without source: nothing in them can be scanned (LL000).
NATIVE_SUFFIXES: Final[frozenset[str]] = frozenset({".so", ".pyd", ".dll", ".dylib", ".pyx"})
#: Byte code is importable without source only outside ``__pycache__``.
BYTECODE_SUFFIXES: Final[frozenset[str]] = frozenset({".pyc", ".pyo"})

#: LL001 - order-entry names that are too generic as bare names; flagged as attributes and imports.
ORDER_ENTRY_ATTRIBUTE_ONLY_NAMES: Final[frozenset[str]] = frozenset(
    {"Buy", "Sell", "BuyLimit", "SellLimit", "BuyStop", "SellStop"}
)
#: LL001 - order-entry functions, methods and messages of real broker APIs (MetaTrader 5 Python and
#: MQL5 ``CTrade``, MetaTrader 4, Interactive Brokers, cTrader Open API, OANDA, Alpaca, ccxt, Binance,
#: FIX).  Matched syntactically and case-sensitively.  ``order_calc_margin`` is read-only: not listed.
ORDER_ENTRY_NAMES: Final[frozenset[str]] = ORDER_ENTRY_ATTRIBUTE_ONLY_NAMES | frozenset(
    {
        "order_send",
        "order_check",
        "OrderSend",
        "OrderSendAsync",
        "OrderCheck",
        "PositionOpen",
        "PositionClose",
        "PositionModify",
        "OrderModify",
        "OrderDelete",
        "OrderClose",
        "placeOrder",
        "place_order",
        "create_order",
        "create_market_order",
        "create_limit_order",
        "submit_order",
        "cancel_order",
        "replace_order",
        "NewOrderSingle",
        "OrderCancelRequest",
        "OrderCreate",
        "ProtoOANewOrderReq",
        "send_new_order",
        "market_order",
        "limit_order",
        "stop_order",
    }
)

#: LL002 - top-level modules of broker and exchange SDKs (compared without regard to case).
BROKER_SDK_MODULES: Final[frozenset[str]] = frozenset(
    {
        "MetaTrader5",
        "MetaTrader4",
        "mt5",
        "ctrader_open_api",
        "ib_insync",
        "ibapi",
        "oandapyV20",
        "ccxt",
        "alpaca",
        "alpaca_trade_api",
        "quickfix",
        "simplefix",
        "binance",
        "tradingview_ta",
        "dwx_client",
        "pymt5adapter",
    }
)
_BROKER_SDK_FOLDED: Final[frozenset[str]] = frozenset(name.casefold() for name in BROKER_SDK_MODULES)
#: Scanned code lives in these namespaces; a module *inside* them is scanned itself, not an SDK.
_PROJECT_NAMESPACES: Final[tuple[str, ...]] = ("gie", "dashboard")

#: LL003 (a) - modules that reach the network.  Denied below ``gie/`` unless exempt for the path.
NETWORK_MODULES: Final[frozenset[str]] = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "websockets",
        "zmq",
        "grpc",
        "smtplib",
        "ftplib",
        "telnetlib",
        "paramiko",
        # stricter than the minimum list: the same capability under other names
        "_socket",
        "_ssl",
        "socketserver",
        "xmlrpc",
        "imaplib",
        "poplib",
        "nntplib",
    }
)
#: What a feed reader may import (market data may read feeds; it may never send orders: LL001, LL002).
FEED_MODULES: Final[frozenset[str]] = frozenset(
    {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "websockets", "zmq"}
)
#: LL003 (a) - the explicit exemptions: path prefix -> network modules allowed below it.
NETWORK_EXEMPTIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "gie/market_data/": FEED_MODULES,
        "gie/intermarket/": FEED_MODULES,
    }
)
#: ``asyncio`` functions that open connections; treated as the module ``socket``.
_NETWORK_FUNCTIONS: Final[tuple[str, ...]] = (
    "asyncio.open_connection",
    "asyncio.start_server",
    "asyncio.open_unix_connection",
    "asyncio.start_unix_server",
)
#: LL003 (b) - modules that start processes or load native code.  Denied in all scanned code.
PROCESS_AND_NATIVE_MODULES: Final[frozenset[str]] = frozenset(
    {
        "ctypes",
        "cffi",
        "subprocess",
        "multiprocessing",
        "pty",
        # stricter than the minimum list: the private modules behind them
        "_ctypes",
        "_posixsubprocess",
        "_winapi",
    }
)
#: The module ``os`` under all its names.
_OS_MODULES: Final[frozenset[str]] = frozenset({"os", "posix", "nt"})
#: Names that are the module only when imported (``nt`` is too plausible as a variable name).
_OS_PRIVATE_MODULES: Final[frozenset[str]] = frozenset({"posix", "nt"})
_OS_PROCESS_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"system", "popen", "startfile", "fork", "forkpty", "posix_spawn", "posix_spawnp"}
)
#: ``os.exec*`` and ``os.spawn*``.
_OS_PROCESS_PREFIXES: Final[tuple[str, ...]] = ("exec", "spawn")
#: Process creation outside ``os`` / ``subprocess``.
_PROCESS_FUNCTIONS: Final[tuple[str, ...]] = (
    "asyncio.subprocess",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "concurrent.futures.process",
    "concurrent.futures.ProcessPoolExecutor",
)
#: LL003 (c) - built-ins that run code the scan cannot read.
_DYNAMIC_CODE_BUILTINS: Final[tuple[str, ...]] = ("eval", "exec", "compile")
#: Final names of the functions that import a module by name (``importlib.import_module``).
_DYNAMIC_IMPORT_FUNCTIONS: Final[frozenset[str]] = frozenset({"__import__", "import_module"})

#: LL005 - the complete vocabulary of the lock: class -> members (value equals name).
LOCK_VOCABULARY: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"RunMode": ("REPLAY", "PAPER"), "ExecutionVenue": ("SIMULATED",)}
)
_VOCABULARY_BASES: Final[frozenset[str]] = frozenset({"str", "Enum", "StrEnum"})
_VOCABULARY_ENUM_BASES: Final[frozenset[str]] = frozenset({"Enum", "StrEnum"})
_VOCABULARY_DECORATORS: Final[frozenset[str]] = frozenset({"unique", "final"})

#: LL006 - everything the lock module may import.
LOCK_IMPORT_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"__future__", "threading", "enum", "types", "typing", "collections.abc", "gie.core.errors"}
)
_LOCK_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset(
    {"open", "eval", "exec", "compile", "__import__", "os"}
)
_LOCK_FORBIDDEN_ATTRIBUTES: Final[frozenset[str]] = frozenset({"environ", "getenv", "argv"})
#: Accepted annotations of the lock constant ("optionally annotated Final"); module prefixes are dropped.
_LOCK_ANNOTATIONS: Final[frozenset[str]] = frozenset({"Final", "Final[bool]", "Final[Literal[False]]"})

#: LL007 - what must exist: class -> required methods, and module-level functions.
LOCK_REQUIRED_CLASSES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"RunMode": (), "ExecutionVenue": (), "RoutingGuard": ("authorize",)}
)
LOCK_REQUIRED_FUNCTIONS: Final[tuple[str, ...]] = (
    "parse_run_mode",
    "assert_live_routing_locked",
    REGISTRATION_NAME,
)
GATEWAY_REQUIRED_CLASSES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"GuardedExecutionAdapter": ("submit", "cancel", "replace", "poll")}
)

#: LL008 - the classes of the lock and the file each is defined in.
LOCK_CLASS_HOMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "RunMode": LOCK_MODULE_PATH,
        "ExecutionVenue": LOCK_MODULE_PATH,
        "RoutingGuard": LOCK_MODULE_PATH,
        "GuardedExecutionAdapter": GATEWAY_MODULE_PATH,
    }
)
_PROTECTED_MODULES: Final[tuple[str, ...]] = (LOCK_MODULE, GATEWAY_MODULE)
#: LL008 - registry and enum internals: no access outside the lock module.
LOCK_INTERNAL_NAMES: Final[frozenset[str]] = frozenset(
    {"_SIMULATED_ADAPTER_TYPES", "_member_map_", "_value2member_map_", "_member_names_"}
)
#: LL008 - slots of the order gateway: no access outside the gateway module.
GATEWAY_INTERNAL_NAMES: Final[frozenset[str]] = frozenset({"_adapter", "_guard"})
#: LL008 - attributes that decide what an object claims to be.
IDENTITY_ATTRIBUTES: Final[frozenset[str]] = frozenset({"__module__", "__class__", "__slots__"})
#: LL008 - plain names that change ``__module__`` / ``__class__`` when bound (``__slots__`` in a class
#: body is the required, legitimate spelling and is not listed).
_IDENTITY_NAMES: Final[frozenset[str]] = frozenset({"__module__", "__class__", "__name__"})
_MAPPING_WRITERS: Final[frozenset[str]] = frozenset(
    {"update", "setdefault", "pop", "__setitem__", "__delitem__"}
)

#: LL009 - the only files that may call ``object.__setattr__``: the record framework (normalising
#: validated fields of frozen records) and the two pinned lock files.
SETATTR_EXEMPT_FILES: Final[frozenset[str]] = frozenset(
    {RECORD_FRAMEWORK_PATH, LOCK_MODULE_PATH, GATEWAY_MODULE_PATH}
)
_SETATTR_NAMES: Final[frozenset[str]] = frozenset({"__setattr__", "__delattr__"})

#: LL010 - never a base class outside ``gie/contracts/``, wherever the name comes from.
FINAL_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {"RoutingGuard", "GuardedExecutionAdapter", "RunMode", "ExecutionVenue", "Record"}
)
#: LL010 - never a base class outside ``gie/contracts/`` when imported from ``gie.contracts``.
CONTRACT_RECORD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "Tick",
        "Evidence",
        "Candidate",
        "TradePlan",
        "RiskDecision",
        "OrderEvent",
        "Position",
        "ModelVersion",
        "ClockReading",
    }
)
_CONTRACTS_PREFIX: Final = "gie/contracts/"

#: LL011 - where ``register_simulated_adapter`` may be referenced.
REGISTRATION_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "gie/execution/",
    "gie/backtesting/",
    _CONTRACTS_PREFIX,
)
REGISTRATION_ALLOWED_FILES: Final[frozenset[str]] = frozenset({LOCK_MODULE_PATH})

#: LL012 - the packages that can hold an order gateway.
GATEWAY_PACKAGE_PREFIXES: Final[tuple[str, ...]] = (
    "gie/execution/",
    "gie/reflex/",
    "gie/decision/",
    "gie/risk/",
    "gie/backtesting/",
)
#: LL012 - the methods of an execution adapter that place, cancel or change an order.
ADAPTER_CALL_NAMES: Final[frozenset[str]] = frozenset({"submit", "cancel", "replace"})
#: LL012 - naming convention: a variable or attribute with one of these names is the order gateway.
GATEWAY_NAMES: Final[frozenset[str]] = frozenset({"gateway", "order_gateway", "_gateway", "_order_gateway"})
#: LL012 - standard-library functions named ``replace``; calling them on the imported module is not
#: an adapter call.
STDLIB_REPLACE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"dataclasses.replace", "copy.replace", "os.replace"}
)
_TEXT_CLASSES: Final[frozenset[str]] = frozenset({"str", "bytes"})

#: LL015 - the only files that may write to the file system.  The journal writes the run's records.
#: Every addition is a coordinator decision recorded in docs/DECISION_LOG.md (D-011).
FILE_WRITE_EXEMPT_FILES: Final[frozenset[str]] = frozenset({"gie/contracts/journal.py"})
#: LL015 - modules whose every function copies, moves, deletes or creates files.
FILE_WRITE_MODULES: Final[frozenset[str]] = frozenset({"shutil", "tempfile"})
#: LL015 - functions of ``os`` that write to the file system (``os.open`` is checked by its flags).
OS_WRITE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "rename",
        "replace",
        "remove",
        "unlink",
        "mkdir",
        "makedirs",
        "rmdir",
        "symlink",
        "link",
        "truncate",
        # stricter than the minimum list: the same capability under other names
        "renames",
        "removedirs",
        "mkfifo",
        "mknod",
        "ftruncate",
    }
)
#: LL015 - methods of ``pathlib.Path`` that write; matched by name on any receiver.
PATH_WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "write_text",
        "write_bytes",
        "touch",
        "mkdir",
        "unlink",
        "rmdir",
        "symlink_to",
        "hardlink_to",
        "link_to",
        "rename",
        "replace",
    }
)
#: LL015 - path methods whose names are common elsewhere; matched by the path signature (one argument).
_SIGNATURE_MATCHED_METHODS: Final[frozenset[str]] = frozenset({"rename", "replace"})
#: LL015 - characters of an ``open`` mode that write.
WRITE_MODE_CHARACTERS: Final[frozenset[str]] = frozenset("wax+")
#: LL015 - flags of ``os.open`` that do not write; any other flag (or a computed one) is reported.
READ_ONLY_OPEN_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "O_RDONLY",
        "O_BINARY",
        "O_TEXT",
        "O_CLOEXEC",
        "O_NOFOLLOW",
        "O_NONBLOCK",
        "O_NOCTTY",
        "O_DIRECTORY",
        "O_NOINHERIT",
        "O_NOATIME",
        "O_PATH",
        "O_SEQUENTIAL",
        "O_RANDOM",
    }
)
#: LL015 - ``replace`` functions of the standard library that do not touch files.
_HARMLESS_REPLACE_FUNCTIONS: Final[frozenset[str]] = frozenset({"dataclasses.replace", "copy.replace"})

#: Functions that take an attribute name as a string.
_REFLECTIVE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "getattr",
        "hasattr",
        "setattr",
        "delattr",
        "getattr_static",
        "attrgetter",
        "methodcaller",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
    }
)
_WRITING_FUNCTIONS: Final[frozenset[str]] = frozenset({"setattr", "delattr", "__setattr__", "__delattr__"})
#: Passes of the re-assignment resolution (``a = mt5``, ``b = a`` needs two).
_ALIAS_PASSES: Final = 3
#: More meanings than this for one name cannot be followed; the file is then reported (LL000).
_MAX_MEANINGS: Final = 64


# ============================================================================== results
@dataclass(frozen=True)
class Violation:
    """One finding.  ``path`` is POSIX and relative to the scanned root; ``line`` 0 is the whole file."""

    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


@dataclass(frozen=True)
class _Report:
    violations: list[Violation]
    files_scanned: int


def _order(violation: Violation) -> tuple[str, int, str, str]:
    return (violation.path, violation.line, violation.rule, violation.message)


# ============================================================================== small AST helpers
def _static_str(node: ast.AST | None, *, allow_bytes: bool = False) -> str | None:
    """The value of a string expression that is fixed in the source, else ``None``.

    Folded: a literal (the parser already joins adjacent literals), ``+`` of two
    foldable operands, and an f-string whose placeholders are themselves foldable
    and carry no conversion or format specification.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if allow_bytes and isinstance(node.value, bytes):
            return node.value.decode("latin-1")
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_str(node.left, allow_bytes=allow_bytes)
        if left is None:
            return None
        right = _static_str(node.right, allow_bytes=allow_bytes)
        return None if right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            part = _static_str(value, allow_bytes=allow_bytes)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return _static_str(node.value, allow_bytes=allow_bytes)
    return None


def _is_write(node: ast.Name | ast.Attribute | ast.Subscript) -> bool:
    return isinstance(node.ctx, ast.Store | ast.Del)


def _final_names(candidates: Iterable[str]) -> frozenset[str]:
    return frozenset(candidate.rsplit(".", 1)[-1] for candidate in candidates)


def _has_component(candidates: Iterable[str], names: Iterable[str]) -> bool:
    wanted = frozenset(names)
    return any(part in wanted for candidate in candidates for part in candidate.split("."))


def _top_level(qualified: str) -> str:
    return qualified.split(".", maxsplit=1)[0]


def _is_within(qualified: str, prefix: str) -> bool:
    return qualified == prefix or qualified.startswith(prefix + ".")


def _is_protected(candidates: Iterable[str]) -> bool:
    """True when an expression may be the lock module, the gateway module or one of the lock classes."""
    resolved = tuple(candidates)
    return _has_component(resolved, LOCK_CLASS_HOMES) or any(
        _is_within(candidate, module) for candidate in resolved for module in _PROTECTED_MODULES
    )


def _is_plain_super(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
        and not node.args
        and not node.keywords
    )


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and _static_str(statement.value) is not None


def _split_chain(node: ast.AST) -> tuple[list[str], ast.AST]:
    """``a.b.c`` -> ``(["b", "c"], a)``; an expression without attributes is its own root."""
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    attributes.reverse()
    return attributes, node


def _alias_sources(value: ast.expr) -> Iterator[tuple[list[str], ast.AST]]:
    """The attribute chains an assigned value may be: both arms of ``a if c else b`` and of ``a or b``."""
    if isinstance(value, ast.IfExp):
        yield from _alias_sources(value.body)
        yield from _alias_sources(value.orelse)
    elif isinstance(value, ast.BoolOp):
        for operand in value.values:
            yield from _alias_sources(operand)
    elif isinstance(value, ast.NamedExpr):
        yield from _alias_sources(value.value)
    else:
        yield _split_chain(value)


def _alias_pairs(node: ast.AST) -> Iterator[tuple[str, ast.expr]]:
    """``name = value`` bindings of one statement or expression (for the re-assignment resolution)."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id, node.value
            elif (
                isinstance(target, ast.Tuple | ast.List)
                and isinstance(node.value, ast.Tuple | ast.List)
                and len(target.elts) == len(node.value.elts)
            ):
                for element, value in zip(target.elts, node.value.elts, strict=True):
                    if isinstance(element, ast.Name):
                        yield element.id, value
    elif (
        isinstance(node, ast.AnnAssign | ast.NamedExpr)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        yield node.target.id, node.value


def _imported_bindings(node: ast.Import | ast.ImportFrom) -> Iterator[tuple[str, str]]:
    """``(bound name, imported name)`` of an import statement; star imports bind nothing by name."""
    for alias in node.names:
        if alias.name == "*":
            continue
        if isinstance(node, ast.Import):
            yield (alias.asname or _top_level(alias.name)), alias.name
        else:
            yield (alias.asname or alias.name), alias.name


# ============================================================================== name resolution
class _UnresolvableError(Exception):
    """A name is re-assigned in more ways than the scan follows; the file cannot be verified."""


@dataclass(frozen=True)
class _ImportRef:
    """One imported module or name, static or dynamic with a literal module name."""

    line: int
    #: ``a.b.c`` for ``import a.b.c`` and for ``from a.b import c``; ``a.b`` for ``from a.b import *``.
    qualified: str
    #: The module the statement names: ``a.b.c`` resp. ``a.b``.
    module: str
    #: The name imported from ``module``; ``None`` for ``import module`` and dynamic imports.
    name: str | None
    dynamic: bool = False


class _Names:
    """Import and alias table of one file: what a name may stand for (over-approximation)."""

    def __init__(self, path: str, nodes: Sequence[ast.AST]) -> None:
        self._package = ".".join(PurePosixPath(path).parts[:-1])
        #: name -> dotted names it may stand for through imports (and assignments of such names)
        self._origins: dict[str, set[str]] = {}
        #: name -> dotted names it is bound to by import statements only
        self._import_origins: dict[str, set[str]] = {}
        #: name -> plain names it may be an alias of (``run = eval``)
        self._aliases: dict[str, set[str]] = {}
        self._star_modules: set[str] = set()
        self._rebound: set[str] = set()
        #: Every import of the file, in source order of discovery.
        self.imports: list[_ImportRef] = []
        #: Lines of dynamic imports whose module name is not a literal.
        self.unverifiable_imports: list[int] = []
        for node in nodes:
            if isinstance(node, ast.Import):
                self._add_import(node)
            elif isinstance(node, ast.ImportFrom):
                self._add_import_from(node)
            elif isinstance(node, ast.Name):
                if _is_write(node):
                    self._rebound.add(node.id)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self._rebound.add(node.name)
            elif isinstance(node, ast.arg):
                self._rebound.add(node.arg)
        for _ in range(_ALIAS_PASSES):
            for node in nodes:
                for name, value in _alias_pairs(node):
                    self._add_alias(name, value)
        for node in nodes:
            if isinstance(node, ast.Call):
                self._add_dynamic_import(node)

    # ------------------------------------------------------------------ construction
    def _bind(self, name: str, origin: str) -> None:
        self._origins.setdefault(name, set()).add(origin)
        self._import_origins.setdefault(name, set()).add(origin)

    def _add_alias(self, name: str, value: ast.expr) -> None:
        """Record what ``name = value`` adds to the meanings of *name*.

        Only two things are followed, so that the table cannot grow without bound:
        a plain alias (``run = eval``, ``mode = RunMode``) and a chain that rests
        on an import or a module expression (``send = mt5.order_send``).  A
        chain on an ordinary variable (``node = node.parent``) could not match a
        rule and is ignored.
        """
        for attributes, root in _alias_sources(value):
            anchored: Iterable[str]
            if not isinstance(root, ast.Name):
                anchored = self._module_expression(root)
            elif root.id == name:
                continue
            else:
                anchored = self._origins.get(root.id, ())
                if not attributes:
                    plain = self._aliases.setdefault(name, set())
                    plain.add(root.id)
                    plain.update(self._aliases.get(root.id, ()))
                    plain.discard(name)
            extended = {".".join((found, *attributes)) for found in anchored}
            if extended:
                origins = self._origins.setdefault(name, set())
                origins.update(extended)
                if len(origins) > _MAX_MEANINGS:
                    raise _UnresolvableError

    def _add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(_ImportRef(node.lineno, alias.name, alias.name, None))
            if alias.asname is None:
                top = _top_level(alias.name)
                self._bind(top, top)
            else:
                self._bind(alias.asname, alias.name)

    def _add_import_from(self, node: ast.ImportFrom) -> None:
        module = self._absolute(node.module, node.level)
        for alias in node.names:
            if alias.name == "*":
                self._star_modules.add(module)
                self.imports.append(_ImportRef(node.lineno, module, module, "*"))
                continue
            qualified = f"{module}.{alias.name}" if module else alias.name
            self.imports.append(_ImportRef(node.lineno, qualified, module, alias.name))
            self._bind(alias.asname or alias.name, qualified)

    def _absolute(self, module: str | None, level: int) -> str:
        if level == 0:
            return module or ""
        base = self._package.split(".") if self._package else []
        if level > 1:
            base = base[: max(0, len(base) - (level - 1))]
        return ".".join([*base, *([module] if module else [])])

    def _add_dynamic_import(self, call: ast.Call) -> None:
        if not self._is_dynamic_import(call):
            return
        module = self._dynamic_import_module(call)
        if module is None:
            self.unverifiable_imports.append(call.lineno)
        else:
            self.imports.append(_ImportRef(call.lineno, module, module, None, dynamic=True))

    def _is_dynamic_import(self, call: ast.Call) -> bool:
        return bool(self.callee_names(call.func) & _DYNAMIC_IMPORT_FUNCTIONS)

    @staticmethod
    def _dynamic_import_module(call: ast.Call) -> str | None:
        """The literal, absolute module name of a dynamic import, else ``None`` (cannot be verified)."""
        argument: ast.expr | None = call.args[0] if call.args else None
        for keyword in call.keywords:
            if keyword.arg == "name" and argument is None:
                argument = keyword.value
        module = _static_str(argument)
        if module is None or not module or module.startswith("."):
            return None
        return module

    # ------------------------------------------------------------------ queries
    def candidates(self, node: ast.AST) -> frozenset[str]:
        """Every dotted name the expression may stand for, including the name as written.

        Empty for expressions that are not a name, an attribute chain or one of the
        recognised module expressions - such an expression cannot be resolved.
        """
        attributes, root = _split_chain(node)
        return frozenset(".".join((found, *attributes)) for found in self._roots(root))

    def _roots(self, node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            plain = {node.id, *self._aliases.get(node.id, ())}
            found = set(plain)
            found.update(self._origins.get(node.id, ()))
            found.update(f"{module}.{name}" for module in self._star_modules for name in plain)
            return found
        if isinstance(node, ast.IfExp):
            return set(self.candidates(node.body) | self.candidates(node.orelse))
        if isinstance(node, ast.BoolOp):
            return {candidate for value in node.values for candidate in self.candidates(value)}
        if isinstance(node, ast.NamedExpr):
            return set(self.candidates(node.value))
        return self._module_expression(node)

    def _module_expression(self, node: ast.AST) -> set[str]:
        """``sys.modules["a.b"]`` and ``importlib.import_module("a.b")`` stand for the module ``a.b``."""
        if isinstance(node, ast.Subscript):
            key = _static_str(node.slice)
            if key and "sys.modules" in self.candidates(node.value):
                return {key}
        elif isinstance(node, ast.Call) and self._is_dynamic_import(node):
            module = self._dynamic_import_module(node)
            if module is not None:
                return {module, _top_level(module)}
        return set()

    def callee_names(self, function: ast.expr) -> frozenset[str]:
        """The final names a called expression may have: as written and through imports and aliases."""
        written = {function.attr} if isinstance(function, ast.Attribute) else set()
        return _final_names(self.candidates(function)) | written

    def import_origins(self, name: str) -> frozenset[str]:
        """What *name* is bound to by import statements (empty when it is not imported)."""
        return frozenset(self._import_origins.get(name, ()))

    def is_rebound(self, name: str) -> bool:
        """True when *name* is assigned, deleted, defined or a parameter somewhere in the file."""
        return name in self._rebound


@dataclass(frozen=True)
class _Source:
    """One parsed file."""

    path: str
    tree: ast.Module
    nodes: tuple[ast.AST, ...]
    names: _Names

    def hit(self, rule: str, where: ast.AST | int, message: str) -> Violation:
        line = where if isinstance(where, int) else int(getattr(where, "lineno", 0))
        return Violation(rule, self.path, line, message)


def _reflective_literals(source: _Source) -> Iterator[tuple[ast.Call, frozenset[str], str]]:
    """Literal attribute names handed to ``getattr`` and its relatives.

    Yields ``(call, reflective function names the callee may be, attribute name)``;
    a dotted literal (``attrgetter("a.b")``) yields each part.
    """
    for node in source.nodes:
        if not isinstance(node, ast.Call):
            continue
        functions = source.names.callee_names(node.func) & _REFLECTIVE_FUNCTIONS
        if not functions:
            continue
        for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
            text = _static_str(argument)
            if text is not None:
                for part in text.split("."):
                    yield node, functions, part


# ============================================================================== LL001 - LL003
def _order_entry(source: _Source) -> Iterator[Violation]:
    names = source.names
    for node in source.nodes:
        if isinstance(node, ast.Attribute):
            if node.attr in ORDER_ENTRY_NAMES:
                yield source.hit("LL001", node, f"attribute '.{node.attr}' is a broker order-entry name")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called = _called_order_entry(names, node.func)
            if called is not None:
                yield source.hit("LL001", node, f"call of the broker order-entry function '{called}'")
    for reference in names.imports:
        if reference.name in ORDER_ENTRY_NAMES:
            yield source.hit(
                "LL001", reference.line, f"import of the broker order-entry name '{reference.name}'"
            )
    for call, _functions, literal in _reflective_literals(source):
        if literal in ORDER_ENTRY_NAMES:
            yield source.hit("LL001", call, f"reflective access to the broker order-entry name '{literal}'")
    if source.path == BROKER_FACADE_PATH:
        yield from _facade_reflection(source)


def _called_order_entry(names: _Names, function: ast.Name) -> str | None:
    """The order-entry name a called plain name stands for, directly or through an import or alias."""
    for candidate in sorted(names.candidates(function)):
        final = candidate.rsplit(".", 1)[-1]
        # A dotted candidate is an attribute of something; an undotted one is a plain name, and the
        # generic names (Buy, Sell, ...) are not flagged as plain names.
        if final in ORDER_ENTRY_NAMES and ("." in candidate or final not in ORDER_ENTRY_ATTRIBUTE_ONLY_NAMES):
            return final
    return None


def _facade_reflection(source: _Source) -> Iterator[Violation]:
    """Reflection in the broker facade that would let an order-entry name pass unseen."""
    message = "in the broker facade cannot be verified against the order-entry names"
    for reference in source.names.imports:
        if reference.name == "*" and not _is_project_module(reference.module):
            yield source.hit("LL001", reference.line, f"'from {reference.module} import *' {message}")
    for node in source.nodes:
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            yield source.hit("LL001", node, f"'.__dict__' {message}")
        if not isinstance(node, ast.Call):
            continue
        functions = source.names.callee_names(node.func)
        opaque = functions & {"vars", "globals", "locals", "__getattribute__", "__getattr__"}
        if opaque:
            yield source.hit("LL001", node, f"'{min(opaque)}(...)' {message}")
        elif functions & {"getattr", "getattr_static"}:
            if len(node.args) < 2 or _static_str(node.args[1]) is None:
                yield source.hit("LL001", node, f"getattr with a computed attribute name {message}")
        elif functions & {"attrgetter", "methodcaller"}:
            checked = node.args[:1] if "methodcaller" in functions else node.args
            if not checked or any(_static_str(argument) is None for argument in checked):
                yield source.hit("LL001", node, f"attrgetter / methodcaller with a computed name {message}")


def _is_project_module(qualified: str) -> bool:
    return any(_is_within(qualified, namespace) for namespace in _PROJECT_NAMESPACES)


def _broker_sdk(source: _Source) -> Iterator[Violation]:
    if source.path == BROKER_FACADE_PATH:
        return
    for reference in source.names.imports:
        if _is_project_module(reference.qualified):
            continue
        for part in reference.qualified.split("."):
            if part.casefold() in _BROKER_SDK_FOLDED:
                yield source.hit(
                    "LL002",
                    reference.line,
                    f"import of the broker SDK '{part}' (allowed only in {BROKER_FACADE_PATH})",
                )
                break


def _is_os_process_function(name: str) -> bool:
    return name in _OS_PROCESS_FUNCTIONS or name.startswith(_OS_PROCESS_PREFIXES)


def _function_escape(qualified: str) -> tuple[str, str] | None:
    """``(group, name)`` when the dotted name is a process or network function, else ``None``."""
    parts = qualified.split(".")
    if len(parts) >= 2 and parts[0] in _OS_MODULES and _is_os_process_function(parts[1]):
        return "process", f"os.{parts[1]}"
    for name in _PROCESS_FUNCTIONS:
        if _is_within(qualified, name):
            return "process", name
    for name in _NETWORK_FUNCTIONS:
        if _is_within(qualified, name):
            return "network", name
    return None


def _import_escape(qualified: str) -> tuple[str, str] | None:
    """``(group, name)`` when importing the dotted name is an escape, else ``None``."""
    top = _top_level(qualified)
    if top in PROCESS_AND_NATIVE_MODULES:
        return "process", top
    function = _function_escape(qualified)
    if function is not None:
        return function
    if top in NETWORK_MODULES:
        return "network", top
    return None


def _network_allowed(path: str, name: str) -> bool:
    """LL003 (a) applies below ``gie/`` only; *name* is a network module or an asyncio stream function."""
    if not path.startswith("gie/"):
        return True
    module = "socket" if name in _NETWORK_FUNCTIONS else name
    return any(
        path.startswith(prefix) and module in allowed for prefix, allowed in NETWORK_EXEMPTIONS.items()
    )


def _escape_hit(source: _Source, where: ast.AST | int, escape: tuple[str, str], how: str) -> Violation | None:
    group, name = escape
    if group == "network":
        if _network_allowed(source.path, name):
            return None
        return source.hit("LL003", where, f"{how} '{name}' reaches the network (not exempt for this package)")
    return source.hit("LL003", where, f"{how} '{name}' starts a process or loads native code")


def _dynamic_code_builtin(names: _Names, function: ast.expr) -> str | None:
    candidates = names.candidates(function)
    for name in _DYNAMIC_CODE_BUILTINS:
        if f"builtins.{name}" in candidates:
            return name
        if name in candidates:
            # "compile" is a common name: "from re import compile" or a local function is not the built-in.
            shadowed = isinstance(function, ast.Name) and function.id == name
            shadowed = shadowed and (
                bool(names.import_origins(name)) or (name == "compile" and names.is_rebound(name))
            )
            if not shadowed:
                return name
    return None


def _function_escapes(names: _Names, node: ast.Name | ast.Attribute) -> Iterator[tuple[str, str]]:
    """Process and network functions a name or attribute chain may stand for."""
    _attributes, root = _split_chain(node)
    unimported: str | None = None
    if isinstance(root, ast.Name) and root.id in _OS_PRIVATE_MODULES and not names.import_origins(root.id):
        unimported = root.id
    for candidate in sorted(names.candidates(node)):
        if unimported is not None and _top_level(candidate) == unimported:
            continue
        escape = _function_escape(candidate)
        if escape is not None:
            yield escape


def _escapes(source: _Source) -> Iterator[Violation]:
    names = source.names
    found: list[Violation | None] = []
    for reference in names.imports:
        escape = _import_escape(reference.qualified)
        if escape is not None:
            found.append(_escape_hit(source, reference.line, escape, "import of"))
    for line in names.unverifiable_imports:
        found.append(
            source.hit("LL003", line, "dynamic import with a computed module name cannot be scanned")
        )
    for node in source.nodes:
        if isinstance(node, ast.Name | ast.Attribute):
            found.extend(
                _escape_hit(source, node, escape, "use of") for escape in _function_escapes(names, node)
            )
        elif isinstance(node, ast.Call):
            builtin = _dynamic_code_builtin(names, node.func)
            if builtin is not None:
                found.append(source.hit("LL003", node, f"'{builtin}(...)' runs code that cannot be scanned"))
            if len(node.args) >= 2 and "getattr" in names.callee_names(node.func):
                attribute = _static_str(node.args[1])
                for candidate in sorted(names.candidates(node.args[0])) if attribute else ():
                    escape = _function_escape(f"{candidate}.{attribute}")
                    if escape is not None:
                        found.append(_escape_hit(source, node, escape, "reflective use of"))
    yield from (violation for violation in found if violation is not None)


# ============================================================================== LL004 - LL007
def _mapping_write_names(call: ast.Call) -> Iterator[str]:
    """Literal keys written by ``mapping.update(...)`` and similar calls."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr in _MAPPING_WRITERS):
        return
    for keyword in call.keywords:
        if keyword.arg is not None:
            yield keyword.arg
    for argument in call.args:
        if isinstance(argument, ast.Dict):
            for key in argument.keys:
                text = _static_str(key)
                if text is not None:
                    yield text
        else:
            text = _static_str(argument)
            if text is not None:
                yield text


def _constant_writes(
    source: _Source, *, accepted: ast.AST | None, in_lock: bool
) -> Iterator[tuple[ast.AST, str]]:
    """Every construct that binds, rebinds or deletes the lock constant, except *accepted*."""
    name = LOCK_CONSTANT
    for node in source.nodes:
        if isinstance(node, ast.Name):
            if node.id == name and _is_write(node) and node is not accepted:
                yield node, "is deleted" if isinstance(node.ctx, ast.Del) else "is assigned"
        elif isinstance(node, ast.Attribute):
            if node.attr == name and _is_write(node):
                yield node, "is written as an attribute"
        elif isinstance(node, ast.Subscript):
            if _is_write(node) and _static_str(node.slice) == name:
                yield node, "is written through a mapping"
        elif isinstance(node, ast.Global | ast.Nonlocal):
            if name in node.names:
                yield node, "is declared global"
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == name:
                yield node, "is redefined as a function or class"
        elif isinstance(node, ast.arg):
            if node.arg == name:
                yield node, "is shadowed by a parameter"
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            if node.name == name:
                yield node, "is bound by an except clause or a pattern"
        elif isinstance(node, ast.MatchMapping):
            if node.rest == name:
                yield node, "is bound by a pattern"
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for bound, imported in _imported_bindings(node):
                # Outside the lock module, importing the genuine constant under its own name is a read.
                if bound == name and (in_lock or isinstance(node, ast.Import) or imported != name):
                    yield node, "is bound by an import"
        elif isinstance(node, ast.Call) and name in _mapping_write_names(node):
            yield node, "is written through a mapping"
    for call, functions, literal in _reflective_literals(source):
        if literal == name and functions & _WRITING_FUNCTIONS:
            yield call, "is written with setattr / delattr"


def _annotation_text(node: ast.expr) -> str | None:
    """``Final``, ``typing.Final[bool]`` ... with module prefixes dropped; ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is False:
        return "False"
    if isinstance(node, ast.Subscript):
        outer, inner = _annotation_text(node.value), _annotation_text(node.slice)
        return None if outer is None or inner is None else f"{outer}[{inner}]"
    return None


def _lock_binding_target(statement: ast.stmt) -> ast.Name | None:
    """The target when *statement* is ``LIVE_ROUTING_PERMITTED[: Final] = False``, else ``None``."""
    target: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value = statement.targets[0], statement.value
    elif isinstance(statement, ast.AnnAssign) and _annotation_text(statement.annotation) in _LOCK_ANNOTATIONS:
        target, value = statement.target, statement.value
    is_false = isinstance(value, ast.Constant) and value.value is False
    if isinstance(target, ast.Name) and target.id == LOCK_CONSTANT and is_false:
        return target
    return None


def _lock_constant(source: _Source) -> Iterator[Violation]:
    accepted: ast.Name | None = None
    for statement in source.tree.body:
        accepted = _lock_binding_target(statement)
        if accepted is not None:
            break
    if accepted is None:
        yield source.hit(
            "LL004",
            0,
            f"{LOCK_CONSTANT} is not bound at module level to the literal False (optionally Final)",
        )
    for node, what in _constant_writes(source, accepted=accepted, in_lock=True):
        yield source.hit("LL004", node, f"{LOCK_CONSTANT} {what}; it must be bound exactly once, to False")


def _vocabulary_class(source: _Source, node: ast.ClassDef, members: tuple[str, ...]) -> Iterator[Violation]:
    wanted = " and ".join(members)
    for decorator in node.decorator_list:
        text = _annotation_text(decorator)
        if text not in _VOCABULARY_DECORATORS:
            yield source.hit("LL005", decorator, f"class {node.name} has a decorator that may replace it")
    bases = [_annotation_text(base) for base in node.bases]
    if node.keywords or not set(bases) <= _VOCABULARY_BASES or not set(bases) & _VOCABULARY_ENUM_BASES:
        yield source.hit(
            "LL005", node, f"class {node.name} must be a plain str / Enum class without keywords"
        )
    found: list[str] = []
    for statement in node.body:
        if isinstance(statement, ast.Pass) or _is_docstring(statement):
            continue
        target = (
            statement.targets[0]
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1
            else None
        )
        if not isinstance(statement, ast.Assign) or not isinstance(target, ast.Name):
            yield source.hit("LL005", statement, f"class {node.name} may contain nothing but {wanted}")
            continue
        found.append(target.id)
        if _static_str(statement.value) != target.id or not isinstance(statement.value, ast.Constant):
            yield source.hit(
                "LL005", statement, f"{node.name}.{target.id} must be assigned the string '{target.id}'"
            )
    if sorted(found) != sorted(members):
        yield source.hit(
            "LL005",
            node,
            f"class {node.name} must have exactly the members {wanted} (found: {', '.join(found) or 'none'})",
        )


def _lock_vocabulary(source: _Source) -> Iterator[Violation]:
    names = source.names
    for class_name, members in LOCK_VOCABULARY.items():
        definitions = [n for n in source.nodes if isinstance(n, ast.ClassDef) and n.name == class_name]
        at_module_level = [
            n for n in source.tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
        ]
        if len(definitions) != 1 or len(at_module_level) != 1:
            yield source.hit(
                "LL005",
                definitions[-1] if definitions else 0,
                f"class {class_name} must be defined once, at module level (found {len(definitions)} times)",
            )
        for definition in definitions:
            yield from _vocabulary_class(source, definition, members)
    for node in source.nodes:
        if isinstance(node, ast.Name):
            if node.id in LOCK_VOCABULARY and _is_write(node):
                yield source.hit("LL005", node, f"{node.id} is rebound or deleted in the lock module")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in LOCK_VOCABULARY:
                yield source.hit("LL005", node, f"{node.name} is redefined as a function")
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for bound, _imported in _imported_bindings(node):
                if bound in LOCK_VOCABULARY:
                    yield source.hit("LL005", node, f"{bound} is rebound by an import in the lock module")
        elif isinstance(node, ast.Attribute):
            if _is_write(node) and _has_component(names.candidates(node.value), LOCK_VOCABULARY):
                yield source.hit("LL005", node, f"attribute '.{node.attr}' of the lock vocabulary is written")
        elif isinstance(node, ast.Call):
            writes = names.callee_names(node.func) & _WRITING_FUNCTIONS
            targets = [*node.args[:1], *([node.func.value] if isinstance(node.func, ast.Attribute) else [])]
            if writes and any(_has_component(names.candidates(t), LOCK_VOCABULARY) for t in targets):
                yield source.hit("LL005", node, "the lock vocabulary is written with setattr / delattr")


def _reserved_vocabulary(source: _Source) -> Iterator[Violation]:
    """LL005 outside the lock module: nothing else may be called ``RunMode`` or ``ExecutionVenue``."""
    for node, name in _foreign_definitions(source):
        if name in LOCK_VOCABULARY:
            yield source.hit(
                "LL005",
                node,
                f"{name} is defined or rebound outside {LOCK_MODULE_PATH}; the vocabulary is unique",
            )


def _foreign_definitions(source: _Source) -> Iterator[tuple[ast.AST, str]]:
    """Definitions and re-bindings of a lock class name in a file that is not its home."""
    for node in source.nodes:
        bound: list[str] = []
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            bound.append(node.name)
        elif isinstance(node, ast.Name):
            if _is_write(node):
                bound.append(node.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            # Importing a lock class under its own name is the normal way to use it.
            bound.extend(
                name
                for name, imported in _imported_bindings(node)
                if isinstance(node, ast.Import) or imported != name
            )
        for name in bound:
            if LOCK_CLASS_HOMES.get(name, source.path) != source.path:
                yield node, name


def _lock_import_allowed(reference: _ImportRef) -> bool:
    if reference.dynamic:
        return False
    if reference.module in LOCK_IMPORT_ALLOWLIST:
        return True
    return reference.name not in (None, "*") and reference.qualified in LOCK_IMPORT_ALLOWLIST


def _lock_isolation(source: _Source) -> Iterator[Violation]:
    names = source.names
    for reference in names.imports:
        if not _lock_import_allowed(reference):
            yield source.hit(
                "LL006",
                reference.line,
                f"the lock module imports '{reference.qualified}' (not on the allowlist)",
            )
    for line in names.unverifiable_imports:
        yield source.hit("LL006", line, "the lock module imports dynamically")
    for node in source.nodes:
        if isinstance(node, ast.Name):
            if node.id in _LOCK_FORBIDDEN_NAMES:
                yield source.hit(
                    "LL006", node, f"the lock module uses '{node.id}'; it must read from nowhere"
                )
        elif isinstance(node, ast.Attribute) and node.attr in _LOCK_FORBIDDEN_ATTRIBUTES:
            yield source.hit(
                "LL006", node, f"the lock module reads '.{node.attr}'; it must read from nowhere"
            )
    for call, _functions, literal in _reflective_literals(source):
        if literal in _LOCK_FORBIDDEN_ATTRIBUTES:
            yield source.hit("LL006", call, f"the lock module reads '{literal}' reflectively")


def _definitions(
    source: _Source,
    *,
    classes: Mapping[str, tuple[str, ...]],
    functions: Sequence[str] = (),
    constants: Sequence[str] = (),
) -> Iterator[Violation]:
    """LL007: the named module-level definitions exist (plain ``def`` / ``class`` / assignment)."""
    body = source.tree.body
    defined_functions = {statement.name for statement in body if isinstance(statement, ast.FunctionDef)}
    defined_classes = {statement.name: statement for statement in body if isinstance(statement, ast.ClassDef)}
    assigned = {name for statement in body for name, _value in _alias_pairs(statement)}
    for name in constants:
        if name not in assigned:
            yield source.hit("LL007", 0, f"{name} is not defined at module level")
    for name in functions:
        if name not in defined_functions:
            yield source.hit("LL007", 0, f"function {name} is not defined at module level")
    for name, methods in classes.items():
        definition = defined_classes.get(name)
        if definition is None:
            yield source.hit("LL007", 0, f"class {name} is not defined at module level")
            continue
        present = {statement.name for statement in definition.body if isinstance(statement, ast.FunctionDef)}
        for method in methods:
            if method not in present:
                yield source.hit("LL007", definition, f"class {name} does not define the method {method}")


def _lock_definitions(source: _Source) -> Iterator[Violation]:
    return _definitions(
        source, classes=LOCK_REQUIRED_CLASSES, functions=LOCK_REQUIRED_FUNCTIONS, constants=(LOCK_CONSTANT,)
    )


def _gateway_definitions(source: _Source) -> Iterator[Violation]:
    return _definitions(source, classes=GATEWAY_REQUIRED_CLASSES)


# ============================================================================== LL008 - LL013
def _tampering(source: _Source) -> Iterator[Violation]:
    names = source.names
    internals = LOCK_INTERNAL_NAMES
    if source.path != GATEWAY_MODULE_PATH:
        internals |= GATEWAY_INTERNAL_NAMES
    for node, what in _constant_writes(source, accepted=None, in_lock=False):
        yield source.hit("LL008", node, f"{LOCK_CONSTANT} {what} outside the lock module")
    for node, name in _foreign_definitions(source):
        if name not in LOCK_VOCABULARY:
            yield source.hit("LL008", node, f"{name} is defined or rebound outside {LOCK_CLASS_HOMES[name]}")
    for reference in names.imports:
        if _top_level(reference.qualified) == "aenum":
            yield source.hit("LL008", reference.line, "import of 'aenum' (enums that can be extended)")
        if reference.name in internals or reference.name == "extend_enum":
            yield source.hit("LL008", reference.line, f"import of the lock internal '{reference.name}'")
    for node in source.nodes:
        if isinstance(node, ast.Attribute):
            if node.attr in internals:
                yield source.hit("LL008", node, f"access to the lock internal '.{node.attr}'")
            if node.attr == "extend_enum":
                yield source.hit("LL008", node, "use of 'extend_enum'")
            if _is_write(node) and node.attr in IDENTITY_ATTRIBUTES:
                yield source.hit("LL008", node, f"'.{node.attr}' of an object is assigned or deleted")
            if _is_write(node) and _is_protected(names.candidates(node.value)):
                yield source.hit(
                    "LL008", node, f"attribute '.{node.attr}' of the lock is assigned or deleted"
                )
        elif isinstance(node, ast.Name):
            if node.id == "extend_enum" or node.id in LOCK_INTERNAL_NAMES:
                yield source.hit("LL008", node, f"use of '{node.id}'")
            if _is_write(node) and node.id in _IDENTITY_NAMES:
                yield source.hit("LL008", node, f"'{node.id}' is rebound (changes what a class claims to be)")
        elif isinstance(node, ast.Dict):
            if any(_static_str(key) == "__module__" for key in node.keys):
                yield source.hit("LL008", node, "a mapping sets '__module__'")
        elif isinstance(node, ast.Call):
            yield from _tampering_call(source, node)
    for call, functions, literal in _reflective_literals(source):
        if literal in internals or literal == "extend_enum":
            yield source.hit("LL008", call, f"reflective access to the lock internal '{literal}'")
        if literal in IDENTITY_ATTRIBUTES and functions & _WRITING_FUNCTIONS:
            yield source.hit("LL008", call, f"'{literal}' of an object is written with setattr / delattr")


def _tampering_call(source: _Source, call: ast.Call) -> Iterator[Violation]:
    names = source.names
    if any(keyword.arg == "__module__" for keyword in call.keywords):
        yield source.hit("LL008", call, "a keyword argument sets '__module__'")
    targets: list[ast.expr] = []
    if names.callee_names(call.func) & {"setattr", "delattr"}:
        targets.extend(call.args[:1])
    if isinstance(call.func, ast.Attribute) and call.func.attr in _SETATTR_NAMES:
        targets.extend([call.func.value, *call.args[:1]])
    if any(_is_protected(names.candidates(target)) for target in targets):
        yield source.hit("LL008", call, "an attribute of the lock is written with setattr / delattr")


def _frozen_records(source: _Source) -> Iterator[Violation]:
    if source.path in SETATTR_EXEMPT_FILES:
        return
    for node in source.nodes:
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _SETATTR_NAMES
            and not _is_plain_super(node.value)
        ):
            direct = "object." if f"object.{node.attr}" in source.names.candidates(node) else "explicit "
            yield source.hit(
                "LL009", node, f"{direct}{node.attr} bypasses frozen records and the immutable guard"
            )
    for call, _functions, literal in _reflective_literals(source):
        if literal in _SETATTR_NAMES:
            yield source.hit("LL009", call, f"reflective access to '{literal}' bypasses frozen records")


def _forbidden_base(names: _Names, base: ast.expr) -> str | None:
    while isinstance(base, ast.Subscript):  # Generic alias: Record[T]
        base = base.value
    for candidate in sorted(names.candidates(base)):
        final = candidate.rsplit(".", 1)[-1]
        if final in FINAL_CLASS_NAMES:
            return final
        if final in CONTRACT_RECORD_NAMES and _is_within(candidate, "gie.contracts"):
            return final
    return None


def _subclassing(source: _Source) -> Iterator[Violation]:
    if source.path.startswith(_CONTRACTS_PREFIX):
        return
    names = source.names
    for node in source.nodes:
        bases: Sequence[ast.expr] = ()
        label = "a class created with type()"
        if isinstance(node, ast.ClassDef):
            bases, label = node.bases, f"class {node.name}"
        elif (
            isinstance(node, ast.Call)
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Tuple | ast.List)
            and names.candidates(node.func) & {"type", "builtins.type", "types.new_class"}
        ):
            bases = node.args[1].elts
        for base in bases:
            forbidden = _forbidden_base(names, base)
            if forbidden is not None:
                yield source.hit("LL010", node, f"{label} subclasses {forbidden} outside {_CONTRACTS_PREFIX}")


def _registration_references(source: _Source) -> list[int]:
    """Lines on which the code refers to ``register_simulated_adapter`` (name, attribute or import)."""
    lines = [reference.line for reference in source.names.imports if reference.name == REGISTRATION_NAME]
    for node in source.nodes:
        if (isinstance(node, ast.Name) and node.id == REGISTRATION_NAME) or (
            isinstance(node, ast.Attribute) and node.attr == REGISTRATION_NAME
        ):
            lines.append(node.lineno)
    return lines


def _registration(source: _Source) -> Iterator[Violation]:
    if source.path in REGISTRATION_ALLOWED_FILES or source.path.startswith(REGISTRATION_ALLOWED_PREFIXES):
        return
    lines = _registration_references(source)
    lines.extend(
        call.lineno for call, _f, literal in _reflective_literals(source) if literal == REGISTRATION_NAME
    )
    for line in lines:
        yield source.hit(
            "LL011", line, f"{REGISTRATION_NAME} is referenced outside the execution and backtesting packages"
        )


def _is_text_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)


def _is_text_replace(call: ast.Call, receiver: ast.expr) -> bool:
    """The documented ``str.replace`` heuristic of LL012: such a call cannot carry an order."""
    if _is_text_literal(receiver) or isinstance(receiver, ast.JoinedStr):
        return True
    arguments = call.args
    if any(keyword.arg != "count" for keyword in call.keywords) or len(arguments) not in (2, 3):
        return False
    if not all(_is_text_literal(argument) for argument in arguments[:2]):
        return False
    if len(arguments) == 2:
        return True
    third = arguments[2]
    return _is_text_literal(third) or (isinstance(third, ast.Constant) and type(third.value) is int)


def _is_harmless_receiver(names: _Names, attribute: ast.Attribute) -> bool:
    """Provably not an adapter: ``str.replace`` / ``bytes.replace`` or a standard-library function."""
    receiver = attribute.value
    root = receiver
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or names.is_rebound(root.id):
        return False
    if attribute.attr != "replace":
        return False
    if receiver is root and root.id in _TEXT_CLASSES:
        return not names.import_origins(root.id)
    suffix = ast.unparse(attribute)[len(root.id) :]
    origins = names.import_origins(root.id)
    return bool(origins) and all(f"{origin}{suffix}" in STDLIB_REPLACE_FUNCTIONS for origin in origins)


def _is_gateway(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id in GATEWAY_NAMES) or (
        isinstance(node, ast.Attribute) and node.attr in GATEWAY_NAMES
    )


def _adapter_calls(source: _Source) -> Iterator[Violation]:
    if not source.path.startswith(GATEWAY_PACKAGE_PREFIXES) or _registration_references(source):
        return
    names = source.names
    advice = "is allowed only on the order gateway (a name ending in 'gateway', see LL012)"
    called = {id(node.func): node for node in source.nodes if isinstance(node, ast.Call)}
    for node in source.nodes:
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ADAPTER_CALL_NAMES
            and isinstance(node.ctx, ast.Load)
        ):
            if _is_gateway(node.value) or _is_harmless_receiver(names, node):
                continue
            call = called.get(id(node))
            if call is None:
                yield source.hit("LL012", node, f"reference to '.{node.attr}' {advice}")
            elif node.attr != "replace" or not _is_text_replace(call, node.value):
                yield source.hit("LL012", call, f"call of '.{node.attr}(...)' {advice}")
    for call, functions, literal in _reflective_literals(source):
        if (
            literal in ADAPTER_CALL_NAMES
            and "getattr" in functions
            and not (call.args and _is_gateway(call.args[0]))
        ):
            yield source.hit("LL012", call, f"reflective access to '.{literal}' {advice}")


def _production_path(source: _Source) -> Iterator[Violation]:
    for node in source.nodes:
        if isinstance(node, ast.Constant | ast.BinOp | ast.JoinedStr):
            text = _static_str(node, allow_bytes=True)
            if text is not None and _PRODUCTION_TREE_MARKER in text.casefold():
                yield source.hit(
                    "LL013", node, "a string literal names the production tree; code never addresses it"
                )


# ============================================================================== LL015
def _os_write_function(qualified: str) -> str | None:
    """``os.<name>`` when the dotted name is a writing function of ``os`` or ``os.open``, else ``None``."""
    parts = qualified.split(".")
    if len(parts) == 2 and parts[0] in _OS_MODULES and (parts[1] in OS_WRITE_FUNCTIONS or parts[1] == "open"):
        return f"os.{parts[1]}"
    return None


def _write_module(qualified: str) -> str | None:
    top = _top_level(qualified)
    return top if top in FILE_WRITE_MODULES else None


def _write_target(names: _Names, node: ast.AST) -> str | None:
    """The writing module or ``os`` function a name or attribute chain may stand for, else ``None``.

    Over-approximates like LL003: a name counts as everything it is bound to anywhere in the file;
    only the private spellings ``posix`` / ``nt`` of ``os`` count when they are imported.
    """
    _attributes, root = _split_chain(node)
    unimported = (
        isinstance(root, ast.Name) and root.id in _OS_PRIVATE_MODULES and not names.import_origins(root.id)
    )
    for candidate in sorted(names.candidates(node)):
        if unimported and _top_level(candidate) in _OS_PRIVATE_MODULES:
            continue
        found = _write_module(candidate) or _os_write_function(candidate)
        if found is not None:
            return found
    return None


def _is_read_only_flag(names: _Names, node: ast.expr) -> bool:
    """Whether an ``os.open`` flag expression is provably read-only: read-only flags joined by ``|``."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_read_only_flag(names, node.left) and _is_read_only_flag(names, node.right)
    if isinstance(node, ast.Constant):
        return type(node.value) is int and node.value == 0  # os.O_RDONLY is 0 on every platform
    if isinstance(node, ast.Name | ast.Attribute):
        finals = _final_names(names.candidates(node))
        other_flags = {name for name in finals if name.startswith("O_")} - READ_ONLY_OPEN_FLAGS
        return bool(finals & READ_ONLY_OPEN_FLAGS) and not other_flags
    return False


def _argument(call: ast.Call, position: int, keyword: str) -> tuple[bool, ast.expr | None]:
    """``(verifiable, argument)``: the keyword wins; ``*`` / ``**`` arguments cannot be verified."""
    for item in call.keywords:
        if item.arg == keyword:
            return True, item.value
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return False, None
    if any(item.arg is None for item in call.keywords):
        return False, None
    return True, call.args[position] if len(call.args) > position else None


def _open_mode_problem(names: _Names, function: ast.Name | ast.Attribute, call: ast.Call) -> str | None:
    """Why a call of a function named ``open`` writes or cannot be verified, else ``None``."""
    position = 1
    if isinstance(function, ast.Attribute):
        _attributes, root = _split_chain(function.value)
        anchored = isinstance(root, ast.Name) and bool(names.import_origins(root.id))
        position = 1 if anchored else 0  # a module function / unbound method, else ``path.open(mode)``
    verifiable, mode = _argument(call, position, "mode")
    if not verifiable:
        return "open(...) with * / ** arguments: the mode cannot be verified"
    if mode is None:
        return None  # the default mode "r" only reads
    text = _static_str(mode)
    if text is None:
        return "open(...) with a mode that is not a string literal: it cannot be verified"
    if WRITE_MODE_CHARACTERS & set(text):
        return f"open(...) with the mode {text!r} writes to the file system"
    return None


def _os_open_problem(names: _Names, call: ast.Call) -> str | None:
    verifiable, flags = _argument(call, 1, "flags")
    if verifiable and flags is not None and _is_read_only_flag(names, flags):
        return None
    return "os.open(...) with a flag that writes or cannot be verified"


def _is_path_signature(call: ast.Call | None) -> bool:
    """Whether a call may be ``Path.rename(target)`` / ``Path.replace(target)``: exactly one argument."""
    if call is None:
        return True  # an uncalled reference may be called with anything
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return True
    keywords = [keyword.arg for keyword in call.keywords]
    if None in keywords:
        return True
    return (len(call.args), keywords) in ((1, []), (0, ["target"]))


def _is_not_a_path(names: _Names, receiver: ast.expr, method: str) -> bool:
    """A receiver of ``.rename`` / ``.replace`` that is provably not a path (LL015)."""
    if _is_gateway(receiver):
        return True  # the order gateway by the naming convention of LL012: it amends an order
    if not isinstance(receiver, ast.Name) or names.is_rebound(receiver.id):
        return False
    origins = names.import_origins(receiver.id)
    if not origins:
        return receiver.id in _TEXT_CLASSES
    return all(f"{origin}.{method}" in _HARMLESS_REPLACE_FUNCTIONS for origin in origins)


def _file_write_problem(names: _Names, node: ast.Name | ast.Attribute, call: ast.Call | None) -> str | None:
    """The LL015 message for one outermost name or attribute chain (and its call), else ``None``."""
    target = _write_target(names, node)
    if target == "os.open":
        if call is None:
            return "reference to 'os.open' (it can write to the file system)"
        return _os_open_problem(names, call)
    if target is not None:
        return f"use of '{target}' (it writes to the file system)"
    if isinstance(node, ast.Attribute) and node.attr in PATH_WRITE_METHODS:
        if node.attr in _SIGNATURE_MATCHED_METHODS and (
            not _is_path_signature(call) or _is_not_a_path(names, node.value, node.attr)
        ):
            return None
        how = "reference to" if call is None else "call of"
        return f"{how} '.{node.attr}' (a path method that writes)"
    if call is not None and "open" in names.callee_names(node):
        return _open_mode_problem(names, node, call)
    return None


def _file_writes(source: _Source) -> Iterator[Violation]:
    if source.path in FILE_WRITE_EXEMPT_FILES:
        return
    names = source.names
    for reference in names.imports:
        found = _write_module(reference.qualified) or _os_write_function(reference.qualified)
        if found is not None:
            yield source.hit("LL015", reference.line, f"import of '{found}' (it writes to the file system)")
    called = {id(node.func): node for node in source.nodes if isinstance(node, ast.Call)}
    inner = {id(node.value) for node in source.nodes if isinstance(node, ast.Attribute)}
    for node in source.nodes:
        # the outermost attribute of a chain speaks for the whole chain
        if (
            isinstance(node, ast.Name | ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in inner
        ):
            call = called.get(id(node))
            problem = _file_write_problem(names, node, call)
            if problem is not None:
                yield source.hit("LL015", node if call is None else call, problem)
    for call, _functions, literal in _reflective_literals(source):
        receiver = call.args[0] if call.args else None
        owners = names.candidates(receiver) if receiver is not None else frozenset()
        via_module = any(_write_module(owner) or _os_write_function(f"{owner}.{literal}") for owner in owners)
        exempt = literal in _SIGNATURE_MATCHED_METHODS and receiver is not None and _is_gateway(receiver)
        if via_module or (literal in PATH_WRITE_METHODS and not exempt):
            yield source.hit(
                "LL015", call, f"reflective access to '{literal}' (it writes to the file system)"
            )


# ============================================================================== LL014
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_safe_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and "\\" not in path and not pure.is_absolute() and ".." not in pure.parts


def _read_pins(root: Path) -> dict[str, str] | str:
    """The pinned hashes, or a description of why the pin file is not valid."""
    try:
        document = json.loads((root / PINS_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "the pin file is missing"
    except (OSError, ValueError) as error:
        return f"the pin file cannot be read as JSON ({type(error).__name__})"
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"}:
        return "the pin file must be an object with exactly the keys 'schema_version' and 'files'"
    if type(document["schema_version"]) is not int or document["schema_version"] != PINS_SCHEMA_VERSION:
        return f"the pin file must have schema_version {PINS_SCHEMA_VERSION}"
    files = document["files"]
    if not isinstance(files, dict):
        return "'files' must map each pinned path to its SHA-256"
    for path, digest in files.items():
        if not _is_safe_relative(path):
            return f"pinned path {path!r} is not a relative POSIX path inside the repository"
        if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= set("0123456789abcdef")):
            return f"the pin of {path} is not a lower-case SHA-256"
    return files


def _pins(root: Path) -> Iterator[Violation]:
    pins = _read_pins(root)
    if isinstance(pins, str):
        yield Violation("LL014", PINS_PATH, 0, pins)
        return
    for path in PINNED_FILES:
        if path not in pins:
            yield Violation("LL014", PINS_PATH, 0, f"{path} is not pinned")
    for path, pinned in sorted(pins.items()):
        target = root / path
        if not target.is_file():
            yield Violation("LL014", path, 0, f"pinned file is missing (pinned in {PINS_PATH})")
            continue
        actual = _sha256(target)
        if actual != pinned:
            yield Violation(
                "LL014",
                path,
                0,
                f"SHA-256 {actual} differs from the pin {pinned} in {PINS_PATH}; a change of the lock "
                "needs a decision-log entry before the pin is updated (--update-pins)",
            )


def update_pins(root: Path) -> tuple[bool, list[str]]:
    """Rewrite the pin file with the current hashes; returns ``(written, lines of the notice)``.

    Nothing is written while a pinned file is missing or breaks any other rule:
    a weakened lock can never be made to pass by pinning it.
    """
    blocking = [v for v in _scan(root).violations if v.rule != "LL014" and v.path in PINNED_FILES]
    if blocking:
        lines = [
            f"PINS NOT UPDATED: {len(blocking)} violation(s) in the pinned files must be resolved first."
        ]
        return False, [*lines, *(f"  {violation.render()}" for violation in blocking)]
    previous = _read_pins(root)
    known = previous if isinstance(previous, dict) else {}
    paths = sorted({*PINNED_FILES, *(path for path in known if (root / path).is_file())})
    current = {path: _sha256(root / path) for path in paths}
    document = {"schema_version": PINS_SCHEMA_VERSION, "files": current}
    target = root / PINS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keys in the documented order (schema_version, files); paths sorted: the same bytes every time.
    target.write_bytes((json.dumps(document, indent=2) + "\n").encode("utf-8"))
    lines = [f"NOTICE: {PINS_PATH} WAS REWRITTEN."]
    for path in paths:
        before = known.get(path, "not pinned")
        change = "unchanged" if before == current[path] else f"{before} -> {current[path]}"
        lines.append(f"  {path}: {change}")
    lines.extend(
        [
            "A CHANGE OF THE ROUTING LOCK OR OF THE ORDER GATEWAY NEEDS A DECISION-LOG ENTRY",
            "(docs/DECISION_LOG.md) AND THE OWNER'S APPROVAL BEFORE IT IS MERGED.",
            "Do not commit new pins without one.",
        ]
    )
    return True, lines


# ============================================================================== driver
_Rule = Callable[[_Source], Iterable[Violation]]
#: Rules for every file below the deny roots.
_DENY_RULES: Final[tuple[_Rule, ...]] = (
    _order_entry,
    _broker_sdk,
    _escapes,
    _frozen_records,
    _subclassing,
    _registration,
    _adapter_calls,
    _production_path,
    _file_writes,
)
#: Rules for every such file except the lock module, which has its own.
_OUTSIDE_LOCK_RULES: Final[tuple[_Rule, ...]] = (_reserved_vocabulary, _tampering)
#: Rules of the two files that make up the lock (LL004 - LL007).
_LOCK_FILE_RULES: Final[Mapping[str, tuple[_Rule, ...]]] = MappingProxyType(
    {
        LOCK_MODULE_PATH: (_lock_constant, _lock_vocabulary, _lock_isolation, _lock_definitions),
        GATEWAY_MODULE_PATH: (_gateway_definitions,),
    }
)
_TOO_DEEP: Final = "the file is nested too deeply to be analysed"


def _discover(root: Path, top: str) -> tuple[list[str], list[str]]:
    """``(source files, importable files without source)`` below ``root/top``, in a fixed order."""
    sources: list[str] = []
    opaque: list[str] = []
    base = root / top
    if not base.is_dir():
        return sources, opaque
    for directory, subdirectories, files in os.walk(base):
        subdirectories.sort()
        in_cache = Path(directory).name == "__pycache__"
        for name in sorted(files):
            relative = (Path(directory) / name).relative_to(root).as_posix()
            suffix = Path(name).suffix.lower()
            if suffix in SOURCE_SUFFIXES:
                sources.append(relative)
            elif suffix in NATIVE_SUFFIXES or (suffix in BYTECODE_SUFFIXES and not in_cache):
                opaque.append(relative)
    return sources, opaque


def _load(root: Path, path: str) -> _Source | Violation:
    """Parse one file; a file that cannot be read or parsed is an LL000 violation, never an exception."""
    try:
        data = (root / path).read_bytes()
    except OSError as error:
        return Violation("LL000", path, 0, f"the file cannot be read ({type(error).__name__})")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escape sequences: not this tool's business
            tree = ast.parse(data, filename=path)
        nodes = tuple(ast.walk(tree))
        return _Source(path, tree, nodes, _Names(path, nodes))
    except SyntaxError as error:
        return Violation("LL000", path, int(error.lineno or 0), f"the file cannot be parsed: {error.msg}")
    except (ValueError, RecursionError, MemoryError) as error:
        return Violation("LL000", path, 0, f"the file cannot be parsed ({type(error).__name__})")
    except _UnresolvableError:
        return Violation("LL000", path, 0, "names are re-assigned in too many ways to be resolved")


def _apply(source: _Source, rules: Sequence[_Rule]) -> list[Violation]:
    found: list[Violation] = []
    for rule in rules:
        try:
            found.extend(rule(source))
        except RecursionError:
            found.append(Violation("LL000", source.path, 0, _TOO_DEEP))
    return found


def _scan(root: Path) -> _Report:
    violations: list[Violation] = []
    verified: set[str] = set()
    scanned = 0
    for top in DENY_ROOTS:
        paths, opaque = _discover(root, top)
        violations.extend(
            Violation("LL000", path, 0, "importable file without Python source cannot be scanned")
            for path in opaque
        )
        for path in paths:
            scanned += 1
            loaded = _load(root, path)  # one file in memory at a time
            if isinstance(loaded, Violation):
                violations.append(loaded)
                continue
            violations.extend(_apply(loaded, _DENY_RULES))
            if path != LOCK_MODULE_PATH:
                violations.extend(_apply(loaded, _OUTSIDE_LOCK_RULES))
            if path in _LOCK_FILE_RULES:
                violations.extend(_apply(loaded, _LOCK_FILE_RULES[path]))
                verified.add(path)
    for path in _LOCK_FILE_RULES:
        if path not in verified:
            unparsed = (root / path).is_file()
            reason = (
                "the file cannot be parsed, so the lock cannot be verified"
                if unparsed
                else "the file is missing"
            )
            violations.append(Violation("LL007", path, 0, reason))
    for top in MARKER_ONLY_ROOTS:
        for path in _discover(root, top)[0]:
            if path.startswith(MARKER_EXEMPT_PREFIXES):
                continue
            scanned += 1
            loaded = _load(root, path)
            violations.extend(
                [loaded] if isinstance(loaded, Violation) else _apply(loaded, (_production_path,))
            )
    violations.extend(_pins(root))
    return _Report(sorted(set(violations), key=_order), scanned)


def check(repo_root: Path) -> list[Violation]:
    """Scan the repository at *repo_root*; returns every violation, sorted by path, line and rule.

    Pure with respect to the repository: nothing is written, imported or executed.
    """
    return _scan(repo_root).violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_live_lock",
        description="Static scan of the paper-only routing lock (rules LL000-LL014).",
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this repository)"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON document")
    parser.add_argument(
        "--update-pins",
        action="store_true",
        help=f"rewrite {PINS_PATH} with the current hashes (needs a decision-log entry), then check",
    )
    arguments = parser.parse_args(argv)
    root: Path = arguments.root
    if not root.is_dir():
        print(f"check_live_lock: {root} is not a directory", file=sys.stderr)
        return 2
    if arguments.update_pins:
        _written, notice = update_pins(root)
        banner = "=" * 100
        print("\n".join([banner, *notice, banner]), file=sys.stderr)
    report = _scan(root)
    violations = report.violations
    if arguments.as_json:
        document = {
            "schema_version": 1,
            "tool": "tools/check_live_lock.py",
            "ok": not violations,
            "files_scanned": report.files_scanned,
            "violation_count": len(violations),
            "rules": dict(RULES),
            "violations": [asdict(violation) for violation in violations],
        }
        print(json.dumps(document, indent=2, sort_keys=True))
    elif violations:
        for violation in violations:
            print(violation.render())
        print(f"check_live_lock: FAILED - {len(violations)} violation(s)", file=sys.stderr)
    else:
        print(
            f"check_live_lock: OK - {report.files_scanned} Python files scanned, "
            f"no violation of {min(RULES)}-{max(RULES)}; the paper-only routing lock is intact"
        )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
