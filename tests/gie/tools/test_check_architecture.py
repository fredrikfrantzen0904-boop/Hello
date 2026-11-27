"""Tests for ``tools/check_architecture.py`` - rules AR000-AR009 of the dependency graph.

Requirements: S00-ASG-10 / S00-DEL-03 (dependency graph), S00-ASG-08 (repository layout),
S00-ASG-09 (ownership), COM-06 (subsystems build on the shared contracts only), G03-05 (no
third-party client can enter the foundation packages), S00-ASG-19 / S00-ASG-21 (time comes from the
injected clock), S00-ASG-22 (identifiers are deterministic), M-AUTH-10 (sessions do not overwrite
each other), S00-ASG-23 / G03-12 / G06-04 (freshness against the clock of the run, no
look-ahead tolerance), S11-ASG-16 (the dashboard is read-only).

The expected behaviour is taken from the rule texts in the module documentation of the tool, the
"Code rules" of ``CLAUDE.md`` and the comments of ``specifications/dependency_graph.yaml`` - not
from the implementation.

SYNTHETIC FIXTURE: every dependency graph, package, module, source snippet and git repository in
this module is invented for testing and lives below pytest's ``tmp_path``.  The snippets are only
ever written to disk and scanned; none of them is imported or executed.  The real repository is
only read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools import check_architecture as ca

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.requirement("S00-ASG-10")

GRAPH = ca.GRAPH_PATH
ENGINE = "gie/alpha/engine.py"

Found = set[tuple[str, str, int]]


# ============================================================================== synthetic fixtures
def fixture_graph() -> dict[str, Any]:
    """SYNTHETIC FIXTURE: a valid dependency graph with thirteen invented sessions and five packages."""
    sessions: dict[str, Any] = {
        "S00": {
            "name": "fixture coordinator",
            "wave": 1,
            "depends_on": [],
            "contract_only": [],
            "optional": [],
            "owns": [
                "gie/__init__.py",
                "gie/core/",
                "gie/contracts/",
                "gie/app/",
                "specifications/",
                "tools/",
                "tools/audit/",
            ],
        },
        "S01": {
            "name": "fixture alpha",
            "wave": 2,
            "depends_on": ["S00"],
            "contract_only": [],
            "optional": [],
            "owns": ["gie/alpha/", "tests/gie/alpha/"],
        },
        "S02": {
            "name": "fixture beta",
            "wave": 2,
            "depends_on": ["S00"],
            "contract_only": ["S01"],
            "optional": [],
            "owns": ["gie/beta/"],
        },
    }
    for number in range(3, 13):
        sessions[f"S{number:02d}"] = {
            "name": f"fixture session {number}",
            "wave": 3 if number < 9 else 4,
            "depends_on": ["S00"],
            "contract_only": [],
            "optional": ["S01"],
            "owns": [f"reviews/session_{number:02d}/"],
        }
    sessions["S11"]["owns"].append("dashboard/gie/")
    return {
        "schema_version": 1,
        "sessions": sessions,
        "packages": {
            "gie.core": {"session": "S00", "may_import": []},
            "gie.contracts": {"session": "S00", "may_import": ["gie.core"]},
            "gie.alpha": {"session": "S01", "may_import": ["gie.core", "gie.contracts"]},
            "gie.beta": {"session": "S02", "may_import": ["gie.core", "gie.contracts"]},
            "gie.app": {"session": "S00", "may_import": ["gie.*"]},
        },
        "third_party": {"gie.core": [], "gie.contracts": []},
        "protected_paths": {"base_commit": "0" * 40, "paths": {"Start": "1" * 40}},
    }


#: SYNTHETIC FIXTURE: a small tree that satisfies every rule.
BASE_FILES: dict[str, str] = {
    "gie/__init__.py": '"""SYNTHETIC FIXTURE root package."""\n',
    "gie/core/__init__.py": "",
    "gie/core/clock.py": "import time\n\n\ndef wall_ns() -> int:\n    return time.time_ns()\n",
    "gie/core/errors.py": "class FixtureError(Exception):\n    pass\n",
    "gie/contracts/__init__.py": "from gie.core import errors\n",
    "gie/contracts/records.py": "from gie.core.errors import FixtureError\n\nRECORD = FixtureError\n",
    "gie/alpha/__init__.py": "",
    "gie/alpha/helper.py": "VALUE = 1\n",
    ENGINE: "from gie.contracts import records\nfrom gie.core import clock\n\nfrom . import helper\n",
    "gie/alpha/sub/__init__.py": "",
    "gie/alpha/sub/deep.py": "from .. import helper\nfrom ..helper import VALUE\n",
    "gie/beta/__init__.py": "",
    "gie/beta/model.py": "class Thing:\n    pass\n",
    "gie/app/__init__.py": "import gie.alpha.engine\nimport gie.beta.model\nfrom gie import contracts\n",
}


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_graph(root: Path, document: object) -> None:
    write(root, GRAPH, yaml.safe_dump(document, sort_keys=False))


def make_tree(root: Path, *, document: object | None = None, files: dict[str, str] | None = None) -> Path:
    """Write the SYNTHETIC FIXTURE tree: the base files, *files* on top, and the graph."""
    for relative, text in {**BASE_FILES, **(files or {})}.items():
        write(root, relative, text)
    write_graph(root, fixture_graph() if document is None else document)
    return root


def found(violations: list[ca.Violation]) -> Found:
    return {(violation.rule, violation.path, violation.line) for violation in violations}


def of_rule(violations: list[ca.Violation], rule: str) -> list[ca.Violation]:
    return [violation for violation in violations if violation.rule == rule]


def check_engine(root: Path, snippet: str) -> list[ca.Violation]:
    """Scan the fixture tree with *snippet* as the content of the engine module of ``gie.alpha``."""
    make_tree(root, files={ENGINE: snippet})
    return ca.check(root, use_git=False)


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def git(cwd: Path, *arguments: str) -> str:
    """Run git inside a temporary fixture repository (never in the real one)."""
    assert REPO_ROOT not in (cwd, *cwd.parents), "fixture git commands must stay below tmp_path"
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=GIE Synthetic Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "init.defaultBranch=main",
            *arguments,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=git_environment(),
    )
    return completed.stdout.strip()


@pytest.fixture
def git_available() -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")


# ============================================================================== clean tree, tolerance
@pytest.mark.requirement("S00-DEL-03", "COM-06")
def test_clean_synthetic_tree_has_no_violation(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    report = ca.scan(root, use_git=False)
    assert report.violations == ()
    assert report.python_files == len(BASE_FILES)
    assert report.tracked_files is None
    assert len(report.notes) == 1
    assert "AR007" in report.notes[0]
    assert "skipped" in report.notes[0]


@pytest.mark.requirement("S00-DEL-03")
def test_keys_the_tool_does_not_know_are_tolerated(tmp_path: Path) -> None:
    document = fixture_graph()
    document["status"] = "PROPOSED"
    document["gates"] = {"start_gate": {"description": "synthetic", "requirements": ["FIX-01"]}}
    document["contract_extensions"] = [{"id": "CX-01", "proposed_by": ["S01"], "consumed_by": ["S02"]}]
    document["path_mapping"] = {"core/": "gie/core/"}
    document["docs_path_ignore"] = ["gie/app/"]
    document["something_added_later"] = {"nested": [1, 2, 3]}
    document["sessions"]["S10"]["early_scaffolding"] = True
    document["sessions"]["S10"]["integrates_later"] = ["S01", "S02"]
    document["sessions"]["S12"]["ongoing"] = True
    document["packages"]["gie.beta"]["read_only"] = True
    root = make_tree(tmp_path, document=document)
    assert ca.check(root, use_git=False) == []


def test_scanned_code_is_never_imported_or_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed.marker"
    side_effect = f"from pathlib import Path\n\nPath({str(marker)!r}).write_text('executed')\n"
    root = make_tree(tmp_path / "tree", files={"gie/alpha/side_effect.py": side_effect})
    assert ca.check(root, use_git=False) == []
    assert not marker.exists()
    assert "gie.alpha.side_effect" not in sys.modules


# ============================================================================== AR000
def test_ar000_file_that_cannot_be_parsed_is_reported_not_skipped(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/alpha/broken.py": "def broken(:\n    pass\n"})
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR000", "gie/alpha/broken.py", 1)}


def test_ar000_source_with_nul_bytes_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "gie/alpha/binary.py").write_bytes(b"VALUE = 1\n\x00\x00\n")
    assert found(ca.check(root, use_git=False)) == {("AR000", "gie/alpha/binary.py", 0)}


def test_ar000_symbolic_link_below_gie_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "tree")
    outside = tmp_path / "outside"
    write(outside, "hidden.py", "import time\n\nSTAMP = time.time()\n")
    try:
        (root / "gie/alpha/linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("BLOCKED: this platform or account cannot create symbolic links")
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR000", "gie/alpha/linked", 0)}
    assert "symbolic link" in violations[0].message


# ============================================================================== AR001
@pytest.mark.requirement("S00-DEL-03", "COM-06")
def test_ar001_directory_with_python_files_must_be_declared(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/gamma/__init__.py": "", "gie/gamma/work.py": "import gie.beta\n"})
    violations = ca.check(root, use_git=False)
    # Reported once; the imports of an undeclared package have no rule to be judged against.
    assert found(violations) == {("AR001", "gie/gamma/", 0)}
    assert "gie.gamma" in violations[0].message
    assert GRAPH in violations[0].message


def test_ar001_python_file_in_a_nested_directory_counts(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/gamma/deep/er/module.py": "VALUE = 1\n"})
    assert found(ca.check(root, use_git=False)) == {("AR001", "gie/gamma/", 0)}


def test_ar001_directory_without_python_files_is_not_a_package(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/data/README.txt": "synthetic fixture\n"})
    write(root, "gie/alpha/__pycache__/engine.cpython-311.pyc", "not python source")
    write(root, "gie/cache_only/__pycache__/stale.py", "VALUE = 1\n")
    assert ca.check(root, use_git=False) == []


def test_ar001_declaring_the_package_clears_the_violation(tmp_path: Path) -> None:
    document = fixture_graph()
    document["packages"]["gie.gamma"] = {"session": "S03", "may_import": ["gie.core"]}
    document["sessions"]["S03"]["owns"].append("gie/gamma/")
    root = make_tree(tmp_path, document=document, files={"gie/gamma/__init__.py": "import gie.core\n"})
    assert ca.check(root, use_git=False) == []


def test_ar001_module_directly_below_gie_belongs_to_no_package(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/helpers.py": "import gie.beta.model\n"})
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR001", "gie/helpers.py", 0)}
    assert "gie.helpers" in violations[0].message


# ============================================================================== AR002
FORBIDDEN_IMPORTS: list[tuple[str, int]] = [
    ("import gie.beta.model\n", 1),
    ("import gie.beta.model as model\n", 1),
    ("from gie.beta import model\n", 1),
    ("from gie.beta.model import Thing\n", 1),
    ("from gie import beta\n", 1),
    ("from ..beta import model\n", 1),
    ("from .. import beta\n", 1),
    ("import gie.core.clock, gie.beta.model\n", 1),
    ("def load():\n    import gie.beta.model\n\n    return gie.beta.model\n", 2),
    ("from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from gie.beta.model import Thing\n", 4),
    ("try:\n    from gie.beta import model\nexcept ImportError:\n    model = None\n", 2),
    (
        "class Lazy:\n    def get(self):\n        from gie.beta.model import Thing\n\n        return Thing\n",
        3,
    ),
    ("import importlib\n\nmodel = importlib.import_module('gie.beta.model')\n", 3),
    ("from importlib import import_module as load\n\nmodel = load('gie.beta.model')\n", 3),
    ("model = __import__('gie.beta.model')\n", 1),
]


@pytest.mark.requirement("COM-06", "S00-DEL-03")
@pytest.mark.parametrize(("snippet", "line"), FORBIDDEN_IMPORTS)
def test_ar002_subsystem_may_not_import_another_subsystem(tmp_path: Path, snippet: str, line: int) -> None:
    violations = check_engine(tmp_path, snippet)
    assert found(violations) == {("AR002", ENGINE, line)}
    assert "gie.alpha may not import gie.beta" in violations[0].message
    assert "gie.core" in violations[0].message  # the message says what is allowed instead
    assert "gie.contracts" in violations[0].message


ALLOWED_IMPORTS: list[str] = [
    "import gie\n",
    "import gie.core.clock\n",
    "from gie.core import clock\n",
    "from gie import core, contracts\n",
    "from gie import __version__\n",
    "from gie.contracts import records\n",
    "from gie.contracts.records import RECORD\n",
    "from . import helper\n",
    "from .helper import VALUE\n",
    "from gie.alpha import helper\n",
    "import gie.alpha.sub.deep\n",
    "from .sub import deep\n",
    "from ..core import clock\n",
    "import json\nfrom decimal import Decimal\n",
    "import numpy\nimport yaml\n",  # gie.alpha has no third_party entry: not restricted by AR003
    "model = __import__(name)\n",  # computed names are outside this rule (check_live_lock reports them)
]


@pytest.mark.requirement("COM-06")
@pytest.mark.parametrize("snippet", ALLOWED_IMPORTS)
def test_ar002_own_package_core_and_contracts_are_allowed(tmp_path: Path, snippet: str) -> None:
    assert check_engine(tmp_path, snippet) == []


@pytest.mark.requirement("COM-06")
def test_ar002_relative_import_is_resolved_against_the_package_of_the_file(tmp_path: Path) -> None:
    deep = "gie/alpha/sub/deep.py"
    root = make_tree(tmp_path, files={deep: "from .. import helper\nfrom ...beta import model\n"})
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR002", deep, 2)}
    # The same three dots one level higher leave the package tree altogether.
    violations = check_engine(tmp_path, "from ... import anything\n")
    assert found(violations) == {("AR002", ENGINE, 1)}
    assert "leaves the top-level package" in violations[0].message


@pytest.mark.requirement("COM-06")
def test_ar002_core_may_import_nothing_and_contracts_only_core(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        files={
            "gie/core/errors.py": "from gie.contracts import records\n",
            "gie/contracts/records.py": "import gie.alpha.helper\nfrom gie.core import errors\n",
        },
    )
    violations = ca.check(root, use_git=False)
    assert found(violations) == {
        ("AR002", "gie/core/errors.py", 1),
        ("AR002", "gie/contracts/records.py", 1),
    }
    by_path = {violation.path: violation.message for violation in violations}
    assert "gie.core may not import gie.contracts" in by_path["gie/core/errors.py"]
    assert "allowed: itself" in by_path["gie/core/errors.py"]
    assert "gie.contracts may not import gie.alpha" in by_path["gie/contracts/records.py"]


@pytest.mark.requirement("COM-06")
def test_ar002_wildcard_allows_the_composition_root_everything(tmp_path: Path) -> None:
    app = "gie/app/__init__.py"
    wiring = "import gie.alpha.engine\nimport gie.beta.model\nfrom gie import contracts, core\n"
    document = fixture_graph()
    root = make_tree(tmp_path, document=document, files={app: wiring})
    assert ca.check(root, use_git=False) == []
    document["packages"]["gie.app"]["may_import"] = ["gie.core"]
    write_graph(root, document)
    assert found(ca.check(root, use_git=False)) == {("AR002", app, 1), ("AR002", app, 2), ("AR002", app, 3)}


@pytest.mark.requirement("COM-06")
def test_ar002_may_import_can_name_a_single_module_of_a_package(tmp_path: Path) -> None:
    document = fixture_graph()
    document["packages"]["gie.alpha"]["may_import"] = ["gie.core", "gie.contracts.records"]
    files = {ENGINE: "from gie.contracts import records\nfrom gie.contracts import other\n"}
    files["gie/contracts/other.py"] = "VALUE = 2\n"
    root = make_tree(tmp_path, document=document, files=files)
    assert found(ca.check(root, use_git=False)) == {("AR002", ENGINE, 2)}


@pytest.mark.requirement("COM-06")
def test_ar002_root_package_and_star_import(tmp_path: Path) -> None:
    violations = check_engine(tmp_path, "from gie import *\n")
    assert found(violations) == {("AR002", ENGINE, 1)}
    assert "star import" in violations[0].message
    root = make_tree(
        tmp_path, files={ENGINE: BASE_FILES[ENGINE], "gie/__init__.py": "from gie import core\n"}
    )
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR002", "gie/__init__.py", 1)}
    assert "gie may not import gie.core" in violations[0].message


@pytest.mark.requirement("COM-06")
def test_ar002_dashboard_tree_is_checked_only_when_it_is_declared(tmp_path: Path) -> None:
    view = "dashboard/gie/view.py"
    files = {
        "dashboard/gie/__init__.py": "",
        view: "from gie.contracts import records\nimport gie.beta.model\nfrom . import widgets\n",
        "dashboard/gie/widgets.py": "import gie.core.errors\n",
    }
    root = make_tree(tmp_path, files=files)
    assert ca.check(root, use_git=False) == []  # no 'dashboard.gie' entry: the tree is not this rule's
    document = fixture_graph()
    document["packages"]["dashboard.gie"] = {
        "session": "S11",
        "may_import": ["gie.core", "gie.contracts"],
        "read_only": True,
    }
    write_graph(root, document)
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR002", view, 2)}
    assert "dashboard.gie may not import gie.beta" in violations[0].message
    # A subsystem may not reach into the dashboard either.
    write(root, ENGINE, "from dashboard.gie import widgets\n")
    assert ("AR002", ENGINE, 1) in found(ca.check(root, use_git=False))


# ============================================================================== AR003
THIRD_PARTY_IMPORTS: list[tuple[str, int, str]] = [
    ("import numpy\n", 1, "numpy"),
    ("import numpy.random as npr\n", 1, "numpy"),
    ("from requests import get\n", 1, "requests"),
    ("import json\nimport yaml\n", 2, "yaml"),
    ("def later():\n    import pandas\n\n    return pandas\n", 2, "pandas"),
    ("from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import openai\n", 4, "openai"),
    ("import importlib\n\nclient = importlib.import_module('anthropic')\n", 3, "anthropic"),
    ("import tools.ci\n", 1, "tools"),
    ("from tests.gie import samples\n", 1, "tests"),
]


@pytest.mark.requirement("G03-05", "S00-DEL-03")
@pytest.mark.parametrize(("snippet", "line", "module"), THIRD_PARTY_IMPORTS)
def test_ar003_foundation_packages_are_standard_library_only(
    tmp_path: Path, snippet: str, line: int, module: str
) -> None:
    for target in ("gie/core/errors.py", "gie/contracts/records.py"):
        root = make_tree(tmp_path / target.split("/")[1], files={target: snippet})
        violations = ca.check(root, use_git=False)
        assert found(violations) == {("AR003", target, line)}
        assert f"may not import {module}" in violations[0].message
        assert "standard library" in violations[0].message


@pytest.mark.requirement("G03-05")
def test_ar003_standard_library_is_always_allowed(tmp_path: Path) -> None:
    standard = (
        "from __future__ import annotations\n\n"
        "import datetime as _dt\nimport hashlib\nimport os.path\nimport tomllib\n"
        "from collections.abc import Mapping\nfrom decimal import Decimal\nfrom zoneinfo import ZoneInfo\n"
    )
    root = make_tree(tmp_path, files={"gie/core/errors.py": standard})
    assert ca.check(root, use_git=False) == []


@pytest.mark.requirement("G03-05")
def test_ar003_applies_only_to_packages_with_an_entry_and_honours_the_allow_list(tmp_path: Path) -> None:
    imports = "import jsonschema\nimport numpy as np\n"
    document = fixture_graph()
    document["third_party"]["gie.contracts"] = ["jsonschema"]
    root = make_tree(
        tmp_path,
        document=document,
        files={"gie/contracts/records.py": imports, "gie/beta/model.py": imports},
    )
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR003", "gie/contracts/records.py", 2)}
    assert "jsonschema" in violations[0].message  # the message shows the allow-list of the package
    del document["third_party"]
    write_graph(root, document)
    assert ca.check(root, use_git=False) == []


# ============================================================================== AR004
WALL_CLOCK_SNIPPETS: list[tuple[str, set[int]]] = [
    ("import time\n\nstamp = time.time()\n", {3}),
    ("import time\n\nstamp = time.time_ns()\n", {3}),
    ("import time\n\ntime.sleep(0.5)\n", {3}),
    ("import time\n\nparts = time.localtime()\n", {3}),
    ("import time\n\nparts = time.gmtime()\n", {3}),
    ("import time\n\ntext = time.ctime()\n", {3}),
    ("import time as _time\n\nstamp = _time.time()\n", {3}),
    ("from time import time\n\nstamp = time()\n", {1, 3}),
    ("from time import sleep as nap\n\nnap(1)\n", {1, 3}),
    ("from time import *\n\nsleep(1)\n", {3}),
    ("import datetime\n\nstamp = datetime.datetime.now()\n", {3}),
    ("import datetime\n\nstamp = datetime.datetime.now(tz=datetime.timezone.utc)\n", {3}),
    ("import datetime as dt\n\nstamp = dt.datetime.utcnow()\n", {3}),
    ("from datetime import datetime\n\nstamp = datetime.now()\n", {3}),
    ("from datetime import datetime as moment\n\nstamp = moment.today()\n", {3}),
    ("from datetime import date\n\nday = date.today()\n", {3}),
    ("import datetime\n\nday = datetime.date.today()\n", {3}),
    ("import threading\n\ntimer = threading.Timer(1.0, print)\n", {3}),
    ("from threading import Timer\n\ntimer = Timer(1.0, print)\n", {1, 3}),
    ("import sched\n\nqueue = sched.scheduler()\n", {3}),
    ("import asyncio\n\n\nasync def nap() -> None:\n    await asyncio.sleep(1)\n", {5}),
    ("import signal\n\nsignal.alarm(5)\n", {3}),
    ("import time\n\nwall_source = time.time_ns\n", {3}),  # a reference without a call counts
    ("import datetime\n\nnow = datetime.datetime.now\nstamp = now()\n", {3, 4}),  # the alias is followed
    ("import time\n\nticker = time\nstamp = ticker.time()\n", {4}),
    ("import time\n\nstamp = getattr(time, 'time')()\n", {3}),
    ("def late() -> float:\n    import time\n\n    return time.time()\n", {4}),
]


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize(("snippet", "lines"), WALL_CLOCK_SNIPPETS)
def test_ar004_wall_clock_access_outside_the_clock_module(
    tmp_path: Path, snippet: str, lines: set[int]
) -> None:
    violations = check_engine(tmp_path, snippet)
    assert found(violations) == {("AR004", ENGINE, line) for line in lines}
    assert all("gie.core.clock" in violation.message for violation in violations)


DETERMINISTIC_TIME_SNIPPETS: list[str] = [
    "import time\n\nstarted = time.perf_counter_ns()\n",
    "import time\n\nstarted = time.perf_counter()\n",
    "import time\n\nstarted = time.monotonic_ns()\n",
    "import time\n\nstarted = time.monotonic()\n",
    "from time import perf_counter_ns\n\nstarted = perf_counter_ns()\n",
    "import datetime\n\nmoment = datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)\n",
    "from datetime import datetime, timezone\n\nmoment = datetime(2026, 1, 2, tzinfo=timezone.utc)\n",
    "import threading\n\nlock = threading.Lock()\n",
    "def read(clock):\n    return clock.now_ns()\n",
    "def read(time):\n    return time.time()\n",  # a parameter called time is not the module
    "class Feed:\n    def sleep(self) -> None:\n        self.time = 0\n",
]


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-21")
@pytest.mark.parametrize("snippet", DETERMINISTIC_TIME_SNIPPETS)
def test_ar004_latency_measurement_and_injected_time_are_allowed(tmp_path: Path, snippet: str) -> None:
    assert check_engine(tmp_path, snippet) == []


@pytest.mark.requirement("S00-ASG-19")
def test_ar004_only_the_clock_module_is_exempt(tmp_path: Path) -> None:
    reads = "import time\n\n\ndef wall_ns() -> int:\n    return time.time_ns()\n"
    root = make_tree(tmp_path, files={"gie/core/clock.py": reads, "gie/core/timeutil.py": reads})
    violations = ca.check(root, use_git=False)
    assert found(violations) == {("AR004", "gie/core/timeutil.py", 5)}
    # Another file called clock.py is not the clock module.
    write(root, "gie/core/timeutil.py", "VALUE = 1\n")
    write(root, "gie/alpha/clock.py", reads)
    assert found(ca.check(root, use_git=False)) == {("AR004", "gie/alpha/clock.py", 5)}


# ============================================================================== AR005
RANDOMNESS_SNIPPETS: list[tuple[str, set[int]]] = [
    ("import uuid\n\nvalue = uuid.uuid4()\n", {3}),
    ("import uuid\n\nvalue = uuid.uuid1()\n", {3}),
    ("from uuid import uuid4\n\nvalue = uuid4()\n", {1, 3}),
    ("import os\n\nvalue = os.urandom(8)\n", {3}),
    ("from os import urandom\n", {1}),
    ("import secrets\n\nvalue = secrets.token_hex(8)\n", {1, 3}),
    ("from secrets import token_bytes\n", {1}),
    ("import random\n\nvalue = random.random()\n", {3}),
    ("import random\n\nvalue = random.randint(1, 6)\n", {3}),
    ("import random\n\nvalue = random.choice([1, 2])\n", {3}),
    ("import random\n\nitems = [1, 2]\nrandom.shuffle(items)\n", {4}),
    ("import random\n\nrandom.seed(42)\n", {3}),  # seeding the hidden generator is still shared state
    ("from random import choice\n\nvalue = choice([1, 2])\n", {1, 3}),
    ("import random\n\ngenerator = random.Random()\n", {3}),
    ("import random\n\ngenerator = random.Random(None)\n", {3}),
    ("import random\n\ngenerator = random.SystemRandom()\n", {3}),
    ("from random import Random\n\ngenerator = Random()\n", {3}),
    ("import numpy as np\n\nvalues = np.random.rand(3)\n", {3}),
    ("import numpy as np\n\nnp.random.seed(1)\n", {3}),
    ("import numpy as np\n\ngenerator = np.random.default_rng()\n", {3}),
    ("import numpy\n\nvalue = numpy.random.normal()\n", {3}),
    ("from numpy import random as npr\n\nvalue = npr.uniform()\n", {3}),
    ("from numpy.random import default_rng\n\ngenerator = default_rng()\n", {3}),
    ("from numpy.random import randint\n", {1}),
    ("value = hash('abc')\n", {1}),
    ("def bucket(key: object) -> int:\n    return hash(key) % 16\n", {2}),
    ("ordered = sorted(['b', 'a'], key=hash)\n", {1}),
    ("import builtins\n\nvalue = builtins.hash('abc')\n", {3}),
    ("import uuid\n\nvalue = getattr(uuid, 'uuid4')()\n", {3}),
]


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize(("snippet", "lines"), RANDOMNESS_SNIPPETS)
def test_ar005_non_deterministic_identity_and_randomness(
    tmp_path: Path, snippet: str, lines: set[int]
) -> None:
    violations = check_engine(tmp_path, snippet)
    assert found(violations) == {("AR005", ENGINE, line) for line in lines}


SEEDED_SNIPPETS: list[str] = [
    "import random\n\ngenerator = random.Random(42)\nvalue = generator.random()\n",
    "import random\n\n\ndef make(seed: int) -> random.Random:\n    return random.Random(seed)\n",
    "import random\n\n\ndef draw(generator: random.Random) -> float:\n    return generator.random()\n",
    "import random\n\ngenerator = random.Random(x=7)\n",
    "from random import Random\n\ngenerator = Random(7)\n",
    "import numpy as np\n\ngenerator = np.random.default_rng(42)\nvalue = generator.normal()\n",
    "import numpy as np\n\n\ndef draw(rng: np.random.Generator) -> float:\n    return rng.normal()\n",
    "from numpy.random import default_rng\n\ngenerator = default_rng(seed=3)\n",
    "import uuid\n\nvalue = uuid.uuid5(uuid.NAMESPACE_DNS, 'fixture')\n",
    "value = id(object())\n",
    "class Key:\n    def __hash__(self) -> int:\n        return hash((1, 2))\n",
    "class Key:\n    def __hash__(self) -> int:\n        return 7\n",
    "import hashlib\n\ndigest = hashlib.sha256(b'fixture').hexdigest()\n",
    "def name(hash):\n    return hash.hexdigest()\n",  # a local value called hash, never called
    "import os\n\nseparator = os.sep\n",
]


@pytest.mark.requirement("S00-ASG-22")
@pytest.mark.parametrize("snippet", SEEDED_SNIPPETS)
def test_ar005_seeded_generators_and_stable_identity_are_allowed(tmp_path: Path, snippet: str) -> None:
    assert check_engine(tmp_path, snippet) == []


@pytest.mark.requirement("S00-ASG-22")
def test_ar005_hash_message_explains_the_per_process_salt(tmp_path: Path) -> None:
    violations = check_engine(tmp_path, "value = hash('abc')\n")
    assert "PYTHONHASHSEED" in violations[0].message
    assert "content_hash" in violations[0].message
    # hash() next to a __hash__ method is still reported: only the method body is the protocol.
    snippet = (
        "class Key:\n    def __hash__(self) -> int:\n        return hash(1)\n\n"
        "    def bucket(self) -> int:\n        return hash(self)\n"
    )
    assert found(check_engine(tmp_path, snippet)) == {("AR005", ENGINE, 6)}


@pytest.mark.requirement("S00-ASG-19", "S00-ASG-22")
def test_ar004_and_ar005_apply_below_gie_only(tmp_path: Path) -> None:
    # The rule text says "anywhere under gie/": the dashboard may show the time of day.
    document = fixture_graph()
    document["packages"]["dashboard.gie"] = {"session": "S11", "may_import": ["gie.core", "gie.contracts"]}
    view = "import time\nimport uuid\n\nshown = time.time()\ntab = uuid.uuid4()\n"
    root = make_tree(tmp_path, document=document, files={"dashboard/gie/view.py": view})
    assert ca.check(root, use_git=False) == []
    write(root, "gie/__init__.py", view)  # the root package of gie is below gie/
    assert found(ca.check(root, use_git=False)) == {
        ("AR004", "gie/__init__.py", 4),
        ("AR005", "gie/__init__.py", 5),
    }


# ============================================================================== AR006
Mutation = Callable[[dict[str, Any]], None]


def _set(path: str, value: object) -> Mutation:
    """A mutation that sets ``a.b.c`` of the graph to *value*."""

    def apply(document: dict[str, Any]) -> None:
        *parents, last = path.split("/")
        target: Any = document
        for key in parents:
            target = target[key]
        target[last] = value

    return apply


def _delete(path: str) -> Mutation:
    def apply(document: dict[str, Any]) -> None:
        *parents, last = path.split("/")
        target: Any = document
        for key in parents:
            target = target[key]
        del target[last]

    return apply


def _protect(path: str) -> Mutation:
    """A mutation that lists *path* (which may contain slashes) as a protected path."""

    def apply(document: dict[str, Any]) -> None:
        document["protected_paths"]["paths"][path] = "2" * 40

    return apply


def _both(*mutations: Mutation) -> Mutation:
    def apply(document: dict[str, Any]) -> None:
        for mutation in mutations:
            mutation(document)

    return apply


INVALID_GRAPHS: list[tuple[str, Mutation, str]] = [
    ("version-2", _set("schema_version", 2), "schema_version must be 1"),
    ("version-text", _set("schema_version", "1"), "schema_version must be 1"),
    ("version-true", _set("schema_version", True), "schema_version must be 1"),
    ("version-missing", _delete("schema_version"), "schema_version must be 1"),
    ("session-missing", _delete("sessions/S07"), "session S07 is missing"),
    ("session-not-a-mapping", _set("sessions/S07", ["S00"]), "session S07 must be a mapping"),
    ("no-wave", _delete("sessions/S04/wave"), "session S04 has no 'wave'"),
    ("no-depends-on", _delete("sessions/S04/depends_on"), "session S04 has no 'depends_on'"),
    ("no-contract-only", _delete("sessions/S04/contract_only"), "session S04 has no 'contract_only'"),
    ("no-optional", _delete("sessions/S04/optional"), "session S04 has no 'optional'"),
    ("no-owns", _delete("sessions/S04/owns"), "session S04 has no 'owns'"),
    ("wave-text", _set("sessions/S04/wave", "three"), "wave must be a positive integer"),
    ("wave-zero", _set("sessions/S04/wave", 0), "wave must be a positive integer"),
    ("depends-on-text", _set("sessions/S04/depends_on", "S00"), "'depends_on' must be a list of strings"),
    ("unknown-dependency", _set("sessions/S04/depends_on", ["S00", "S99"]), "depends_on names S99"),
    ("unknown-contract-only", _set("sessions/S04/contract_only", ["S42"]), "contract_only names S42"),
    ("unknown-optional", _set("sessions/S04/optional", ["S77"]), "optional names S77"),
    ("self-dependency", _set("sessions/S04/depends_on", ["S04"]), "cycle: S04 -> S04"),
    (
        "two-cycle",
        _both(_set("sessions/S04/depends_on", ["S05"]), _set("sessions/S05/depends_on", ["S04"])),
        "cycle: S04 -> S05 -> S04",
    ),
    ("later-wave", _set("sessions/S01/depends_on", ["S00", "S09"]), "S01 (wave 2) depends on S09"),
    (
        "package-session-unknown",
        _set("packages/gie.alpha/session", "S99"),
        "package gie.alpha: session 'S99'",
    ),
    ("package-session-missing", _delete("packages/gie.alpha/session"), "package gie.alpha: session None"),
    ("package-not-a-mapping", _set("packages/gie.alpha", "S01"), "package gie.alpha must be"),
    ("may-import-missing", _delete("packages/gie.alpha/may_import"), "'may_import' must be a list"),
    ("may-import-unknown", _set("packages/gie.alpha/may_import", ["gie.cor"]), "may_import names gie.cor"),
    ("packages-missing", _delete("packages"), "'packages' must be a mapping"),
    ("sessions-missing", _delete("sessions"), "'sessions' must be a mapping"),
    ("overlap-same-path", _set("sessions/S05/owns", ["gie/alpha/"]), "owned paths overlap"),
    ("overlap-nested", _set("sessions/S05/owns", ["gie/alpha/sub/"]), "contains gie/alpha/sub/"),
    ("overlap-parent", _set("sessions/S05/owns", ["gie/"]), "owned paths overlap: gie/ of session S05"),
    (
        "overlap-file-in-directory",
        _set("sessions/S05/owns", ["gie/core/clock.py"]),
        "contains gie/core/clock.py",
    ),
    ("owned-path-absolute", _set("sessions/S05/owns", ["/etc/"]), "is not a relative POSIX path"),
    ("owned-path-parent", _set("sessions/S05/owns", ["../elsewhere/"]), "is not a relative POSIX path"),
    ("owned-path-backslash", _set("sessions/S05/owns", ["gie\\gamma\\"]), "is not a relative POSIX path"),
    ("third-party-undeclared", _set("third_party/gie.nowhere", []), "third_party names gie.nowhere"),
    (
        "third-party-not-a-list",
        _set("third_party/gie.core", "none"),
        "third_party of gie.core must be a list",
    ),
    (
        "package-directory-other-owner",
        _set("packages/gie.beta/session", "S01"),
        "directory gie/beta/ is owned by session S02",
    ),
    (
        "package-directory-unowned",
        _set("packages/gie.gamma", {"session": "S03", "may_import": []}),
        "directory gie/gamma/ is owned by no session",
    ),
    (
        "protected-path-owned",
        _protect("gie/core/clock.py"),
        "protected path gie/core/clock.py is also owned",
    ),
    ("protected-malformed", _set("protected_paths", ["Start"]), "'protected_paths' must be a mapping"),
]


@pytest.mark.requirement("S00-DEL-03", "S00-ASG-09")
@pytest.mark.parametrize(
    ("mutation", "expected"), [case[1:] for case in INVALID_GRAPHS], ids=[case[0] for case in INVALID_GRAPHS]
)
def test_ar006_invalid_dependency_graph_is_reported(
    tmp_path: Path, mutation: Mutation, expected: str
) -> None:
    document = fixture_graph()
    mutation(document)
    root = make_tree(tmp_path, document=document)
    reported = of_rule(ca.check(root, use_git=False), "AR006")
    assert reported, "the invalid graph was accepted"
    assert all(violation.path == GRAPH for violation in reported)
    assert any(expected in violation.message for violation in reported), [v.message for v in reported]


@pytest.mark.requirement("S00-DEL-03", "M-DIR-03", "M-DIR-04", "M-DIR-05")
def test_ar006_dependency_within_the_same_or_an_earlier_wave_is_fine(tmp_path: Path) -> None:
    document = fixture_graph()
    document["sessions"]["S02"]["depends_on"] = ["S00", "S01"]  # wave 2 on wave 2
    document["sessions"]["S09"]["depends_on"] = ["S00", "S04", "S10"]  # wave 4 on waves 3 and 4
    document["sessions"]["S01"]["contract_only"] = ["S09"]  # only depends_on is ordered by waves
    root = make_tree(tmp_path, document=document)
    assert ca.check(root, use_git=False) == []


@pytest.mark.requirement("S00-DEL-03")
def test_ar006_a_cycle_is_reported_once_and_points_at_the_session(tmp_path: Path) -> None:
    document = fixture_graph()
    document["sessions"]["S03"]["depends_on"] = ["S04"]
    document["sessions"]["S04"]["depends_on"] = ["S05"]
    document["sessions"]["S05"]["depends_on"] = ["S03"]
    root = make_tree(tmp_path, document=document)
    cycles = [v for v in of_rule(ca.check(root, use_git=False), "AR006") if "cycle" in v.message]
    assert [violation.message for violation in cycles] == ["depends_on has a cycle: S03 -> S04 -> S05 -> S03"]
    lines = (root / GRAPH).read_text(encoding="utf-8").splitlines()
    session_line = lines.index("  S03:") + 1
    assert session_line < cycles[0].line <= session_line + 12  # inside the block of session S03
    assert "depends_on" in lines[cycles[0].line - 1]


@pytest.mark.requirement("S00-ASG-09")
def test_ar006_similar_names_and_nested_paths_of_one_session_do_not_overlap(tmp_path: Path) -> None:
    document = fixture_graph()
    document["sessions"]["S05"]["owns"] = ["gie/alpha_extra/", "gie/alphabet.py", "tests/gie/alpha_more/"]
    document["sessions"]["S00"]["owns"] += ["tools/audit/", "tools/audit/README.md"]
    root = make_tree(tmp_path, document=document)
    assert ca.check(root, use_git=False) == []


@pytest.mark.requirement("S00-DEL-03")
def test_ar006_duplicate_keys_are_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    text = (root / GRAPH).read_text(encoding="utf-8")
    write(root, GRAPH, text + "schema_version: 1\n")
    reported = of_rule(ca.check(root, use_git=False), "AR006")
    assert [violation.message for violation in reported] == ["duplicate key 'schema_version'"]
    assert reported[0].line == len(text.splitlines()) + 1


@pytest.mark.requirement("S00-DEL-03")
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "cannot be read"),
        ("sessions: [unclosed\n", "not valid YAML"),
        ("- just\n- a list\n", "must be a mapping"),
        ("", "must be a mapping"),
    ],
)
def test_ar006_unusable_graph_fails_closed(tmp_path: Path, content: str | None, expected: str) -> None:
    root = make_tree(tmp_path, files={ENGINE: "import time\n\nstamp = time.time()\nimport gie.beta\n"})
    if content is None:
        (root / GRAPH).unlink()
    else:
        write(root, GRAPH, content)
    violations = ca.check(root, use_git=False)
    reported = of_rule(violations, "AR006")
    assert len(reported) == 1
    assert expected in reported[0].message
    assert "were not evaluated" in reported[0].message
    # The rules that do not need the graph still run; the others are not guessed.
    assert found(violations) - {("AR006", GRAPH, reported[0].line)} == {("AR004", ENGINE, 3)}


# ============================================================================== AR007
@pytest.mark.requirement("S00-ASG-09", "S00-ASG-08", "M-AUTH-10")
def test_ar007_every_tracked_file_needs_an_owner_or_protection(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    tracked = [
        *BASE_FILES,
        GRAPH,
        "Start",  # protected
        "tools/audit/README.md",  # nested entries of one session
        "tests/gie/alpha/test_engine.py",
        "reviews/session_07/review.pdf",
        "orphan.txt",
        "gie/gamma/data.csv",
        "tests/conftest.py",
        "gie/alphabet.py",  # 'gie/alpha/' is a path prefix, not a string prefix
    ]
    report = ca.scan(root, tracked=tracked)
    assert found(list(report.violations)) == {
        ("AR007", "orphan.txt", 0),
        ("AR007", "gie/gamma/data.csv", 0),
        ("AR007", "tests/conftest.py", 0),
        ("AR007", "gie/alphabet.py", 0),
    }
    assert report.tracked_files == len(tracked)
    assert report.notes == ()
    assert all(GRAPH in violation.message for violation in report.violations)


@pytest.mark.requirement("S00-ASG-09")
def test_ar007_entry_without_trailing_slash_is_one_exact_file(tmp_path: Path) -> None:
    document = fixture_graph()
    document["sessions"]["S05"]["owns"] = ["docs", "README.md"]
    root = make_tree(tmp_path, document=document)
    violations = ca.check(root, tracked=["README.md", "docs", "docs/ARCHITECTURE.md", "README.md.bak"])
    assert found(violations) == {("AR007", "docs/ARCHITECTURE.md", 0), ("AR007", "README.md.bak", 0)}


@pytest.mark.requirement("S00-ASG-09", "S00-ASG-08")
def test_ar007_reads_the_file_list_from_git(tmp_path: Path, git_available: None) -> None:
    root = make_tree(tmp_path / "repository")
    git(root, "init", "--quiet")
    write(root, "orphan.txt", "SYNTHETIC FIXTURE: tracked, owned by nobody\n")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "synthetic fixture commit")
    write(root, "untracked.txt", "SYNTHETIC FIXTURE: not tracked, so not this rule's business\n")
    report = ca.scan(root)
    assert found(list(report.violations)) == {("AR007", "orphan.txt", 0)}
    assert report.tracked_files == len(BASE_FILES) + 2  # the base files, the graph and the orphan
    assert report.notes == ()
    git(root, "rm", "--quiet", "--cached", "orphan.txt")
    assert ca.check(root) == []


@pytest.mark.requirement("S00-ASG-09")
def test_ar007_is_skipped_with_a_note_when_git_cannot_answer(tmp_path: Path, git_available: None) -> None:
    root = make_tree(tmp_path / "plain")
    write(root, "orphan.txt", "SYNTHETIC FIXTURE\n")
    report = ca.scan(root)  # a directory that is not a git work tree
    assert report.violations == ()
    assert report.tracked_files is None
    assert len(report.notes) == 1
    assert "AR007" in report.notes[0]
    assert "not inside a git work tree" in report.notes[0]
    # A directory below the top level of a work tree is not that repository's root either.
    outer = tmp_path / "outer"
    inner = make_tree(outer / "nested")
    git(outer, "init", "--quiet")
    git(outer, "add", "--all")
    git(outer, "commit", "--quiet", "-m", "synthetic fixture commit")
    report = ca.scan(inner)
    assert report.violations == ()
    assert "not the top level" in report.notes[0]


# ============================================================================== AR008
CALL_ARGS = "now_ns=2, reference_ns=1, max_age_ns=5"
LOOK_AHEAD_SNIPPETS: list[tuple[str, set[int]]] = [
    (f"from gie.core.freshness import is_fresh\n\nok = is_fresh({CALL_ARGS})\n", {1, 3}),
    ("from gie.core import freshness\n\nage = freshness.age_ns(now_ns=2, reference_ns=1)\n", {3}),
    (f"import gie.core.freshness\n\ngie.core.freshness.require_fresh({CALL_ARGS}, what='tick')\n", {3}),
    ("import gie.core.freshness as fr\n\ncheck = fr.is_fresh\n", {3}),  # a reference without a call
    (f"from gie.core.freshness import *\n\nok = is_fresh({CALL_ARGS})\n", {3}),
    (f"from gie.core.freshness import require_fresh as guard\n\nguard({CALL_ARGS}, what='x')\n", {1, 3}),
    ("from gie.core import freshness\n\ncheck = getattr(freshness, 'age_ns')\n", {3}),
    ("from gie.core import freshness\n\nchecker = freshness\nok = checker.is_fresh\n", {4}),
    ("from ..core.freshness import age_ns\n", {1}),  # relative import, resolved against gie.alpha
    ("from ..core import freshness as fr\n\nfr.require_fresh\n", {3}),
    # a future tolerance other than the literal 0, in any call
    (
        "from gie.core import freshness\n\n"
        "freshness.is_fresh_now(clock, reference_ns=1, max_age_ns=5, future_tolerance_ns=1)\n",
        {3},
    ),
    ("SKEW = 0\n\ncheck(clock, future_tolerance_ns=SKEW)\n", {3}),
    ("check(clock, future_tolerance_ns=False)\n", {1}),
    ("check(clock, future_tolerance_ns=0.0)\n", {1}),
    ("check(clock, future_tolerance_ns=-0)\n", {1}),
    ("check(clock, future_tolerance_ns=10**9)\n", {1}),
]


@pytest.mark.requirement("S00-ASG-23", "G03-12", "G06-04")
@pytest.mark.parametrize(("snippet", "lines"), LOOK_AHEAD_SNIPPETS)
def test_ar008_plain_freshness_helpers_and_future_tolerance_are_reported(
    tmp_path: Path, snippet: str, lines: set[int]
) -> None:
    violations = check_engine(tmp_path, snippet)
    assert found(violations) == {("AR008", ENGINE, line) for line in lines}
    assert all("clock of the run" in violation.message for violation in violations)


CLOCKED_FRESHNESS_SNIPPETS: list[str] = [
    (
        "from gie.core.freshness import is_expired_now, is_fresh_now, require_fresh_now\n"
        "from gie.core.freshness import require_not_expired_now\n\n\n"
        "def judge(clock, tick, plan):\n"
        "    is_fresh_now(clock, reference_ns=tick, max_age_ns=5)\n"
        "    require_fresh_now(clock, reference_ns=tick, max_age_ns=5, what='tick', future_tolerance_ns=0)\n"
        "    is_expired_now(clock, valid_until_ns=plan)\n"
        "    return require_not_expired_now(clock, valid_until_ns=plan, what='plan')\n"
    ),
    "from gie.core.freshness import MAX_FUTURE_TOLERANCE_NS\n\nLIMIT = MAX_FUTURE_TOLERANCE_NS\n",
    "def is_fresh() -> bool:\n    return True\n\n\nok = is_fresh()\n",  # a local function of that name
    "def judge(record):\n    return record.is_fresh() and record.age_ns\n",  # attributes of other objects
    "from gie.alpha import helper\n\nok = helper.is_fresh\n",
]


@pytest.mark.requirement("S00-ASG-23", "G03-12")
@pytest.mark.parametrize("snippet", CLOCKED_FRESHNESS_SNIPPETS)
def test_ar008_clocked_helpers_and_other_names_are_allowed(tmp_path: Path, snippet: str) -> None:
    assert check_engine(tmp_path, snippet) == []


@pytest.mark.requirement("S00-ASG-23", "G03-12")
def test_ar008_applies_below_gie_outside_gie_core_only(tmp_path: Path) -> None:
    plain = f"from gie.core.freshness import is_fresh\n\nok = is_fresh({CALL_ARGS}, future_tolerance_ns=1)\n"
    document = fixture_graph()
    document["packages"]["dashboard.gie"] = {"session": "S11", "may_import": ["gie.core", "gie.contracts"]}
    files = {"gie/core/timeutil.py": plain, "gie/contracts/records.py": plain, "dashboard/gie/view.py": plain}
    root = make_tree(tmp_path, document=document, files=files)
    # gie/core defines the helpers; the dashboard is not below gie/; gie/contracts is not exempt.
    assert found(ca.check(root, use_git=False)) == {
        ("AR008", "gie/contracts/records.py", 1),
        ("AR008", "gie/contracts/records.py", 3),
    }


@pytest.mark.requirement("S00-ASG-23", "G03-12", "G06-04")
def test_ar008_real_repository_passes_the_rule() -> None:
    report = ca.scan(REPO_ROOT)
    assert report.python_files > 0
    assert [v.render() for v in report.violations if v.rule == "AR008"] == []


# ============================================================================== AR009
VIEW = "dashboard/gie/view.py"


def read_only_tree(root: Path, view: str, *, read_only: object = True) -> Path:
    """SYNTHETIC FIXTURE: the base tree plus a declared dashboard package with *view* as one module."""
    document = fixture_graph()
    document["packages"]["dashboard.gie"] = {
        "session": "S11",
        "may_import": ["gie.core", "gie.contracts"],
        "read_only": read_only,
    }
    return make_tree(root, document=document, files={"dashboard/gie/__init__.py": "", VIEW: view})


READ_ONLY_SNIPPETS: list[tuple[str, set[int]]] = [
    ("from gie.contracts.interfaces import RecordSink\n", {1}),
    ("from gie.contracts.guarded import GuardedExecutionAdapter\n", {1}),
    ("from gie.contracts.interfaces import ExecutionAdapter, OrderGateway, ProtectionPort\n", {1}),
    ("from gie.core.routing_lock import RoutingGuard, register_simulated_adapter\n", {1}),
    ("from gie.contracts import journal\n\nsink = journal.MemoryJournal()\n", {3}),
    ("import gie.contracts.journal as store\n\nkind = store.FileJournal\n", {3}),
    ("from gie.contracts import journal\n\nkind = getattr(journal, 'MemoryJournal')\n", {3}),
    (
        "from gie.contracts import interfaces\n\n\ndef show(port: interfaces.ProtectionPort) -> None:\n"
        "    return None\n",
        {4},
    ),
    (
        "def act(view, bus, db, ui, plan, old, new, name):\n"
        "    view.submit(plan)\n"
        "    view.cancel(plan)\n"
        "    view.replace(plan)\n"
        "    bus.publish(plan)\n"
        "    db.commit()\n"
        "    ui.tighten_stop(plan)\n"
        "    ui.exit(plan)\n"
        "    getattr(view, 'submit')(plan)\n"
        "    name.replace(old, new)\n",
        set(range(2, 11)),
    ),
    ("import os\n\nos.exit(0)\n", {3}),  # only sys.exit is provably harmless
    ("def leave(sys):\n    sys.exit(1)\n", {2}),  # a parameter called sys is not the module
]


@pytest.mark.requirement("S11-ASG-16", "COM-06")
@pytest.mark.parametrize(("snippet", "lines"), READ_ONLY_SNIPPETS)
def test_ar009_write_capability_in_a_read_only_package_is_reported(
    tmp_path: Path, snippet: str, lines: set[int]
) -> None:
    violations = ca.check(read_only_tree(tmp_path, snippet), use_git=False)
    assert found(violations) == {("AR009", VIEW, line) for line in lines}
    assert all("read-only" in violation.message for violation in violations)


READ_ONLY_ALLOWED: list[str] = [
    "from gie.contracts.journal import JournalReader\nfrom gie.contracts.interfaces import RecordSource\n",
    "from gie.contracts import records\n\nshown = records.RECORD\n",
    "import sys\n\nsys.exit(0)\n",
    (
        "def tidy(name, old, new):\n"
        "    first = 'a-b'.replace(old, new)\n"
        "    second = name.replace('-', '_')\n"
        "    third = name.replace('-', '_', 1)\n"
        "    fourth = f'{name}!'.replace(old, new)\n"
        "    fifth = str.replace(name, old, new)\n"
        "    return first, second, third, fourth, fifth\n"
    ),
    "def show(view, event):\n    view.on_submit(event)\n    return view.submit_label, view.cancelled\n",
]


@pytest.mark.requirement("S11-ASG-16")
@pytest.mark.parametrize("snippet", READ_ONLY_ALLOWED)
def test_ar009_reading_records_is_allowed(tmp_path: Path, snippet: str) -> None:
    assert ca.check(read_only_tree(tmp_path, snippet), use_git=False) == []


WRITER = "from gie.contracts.journal import MemoryJournal\n\n\ndef act(view, plan):\n    view.submit(plan)\n"


@pytest.mark.requirement("S11-ASG-16")
def test_ar009_applies_to_read_only_packages_only(tmp_path: Path) -> None:
    root = read_only_tree(tmp_path / "declared-false", WRITER, read_only=False)
    assert ca.check(root, use_git=False) == []
    assert check_engine(tmp_path / "subsystem", WRITER) == []  # gie.alpha is not read-only
    document = fixture_graph()
    document["packages"]["gie.beta"]["read_only"] = True  # any package can be declared read-only
    root = make_tree(tmp_path / "beta", document=document, files={"gie/beta/model.py": WRITER})
    assert found(ca.check(root, use_git=False)) == {
        ("AR009", "gie/beta/model.py", 1),
        ("AR009", "gie/beta/model.py", 5),
    }


@pytest.mark.requirement("S11-ASG-16", "S00-DEL-03")
@pytest.mark.parametrize("value", ["yes", 1, None, [True]])
def test_ar009_unreadable_read_only_value_fails_closed(tmp_path: Path, value: object) -> None:
    violations = ca.check(read_only_tree(tmp_path, WRITER, read_only=value), use_git=False)
    assert ("AR006", GRAPH) in {(violation.rule, violation.path) for violation in violations}
    assert any("'read_only' must be true or false" in violation.message for violation in violations)
    assert {("AR009", VIEW, 1), ("AR009", VIEW, 5)} <= found(violations)


@pytest.mark.requirement("S11-ASG-16")
def test_ar009_real_repository_declares_the_dashboard_read_only_and_passes() -> None:
    document = yaml.safe_load((REPO_ROOT / GRAPH).read_text(encoding="utf-8"))
    assert document["packages"]["dashboard.gie"]["read_only"] is True
    report = ca.scan(REPO_ROOT)
    assert report.python_files > 0
    assert [v.render() for v in report.violations if v.rule == "AR009"] == []


# ============================================================================== command line
def test_cli_reports_one_line_per_violation_and_exit_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_tree(tmp_path)
    assert ca.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[-1].startswith("check_architecture: OK - ")
    assert f"{len(BASE_FILES)} Python files scanned" in lines[-1]
    assert lines[0].startswith("check_architecture: note: AR007")  # explicit note: not a git work tree

    write(root, ENGINE, "import gie.beta.model\nimport time\n\nstamp = time.time()\n")
    assert ca.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    violation_lines = [
        line for line in captured.out.splitlines() if not line.startswith("check_architecture:")
    ]
    assert len(violation_lines) == 2
    assert violation_lines[0].startswith(f"{ENGINE}:1: AR002 ")
    assert violation_lines[1].startswith(f"{ENGINE}:4: AR004 ")
    assert "OK" not in captured.out
    assert "FAILED - 2 violation(s)" in captured.err


def test_cli_json_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree(tmp_path, files={ENGINE: "import uuid\n\nvalue = uuid.uuid4()\n"})
    assert ca.main(["--root", str(root), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["tool"] == "tools/check_architecture.py"
    assert document["violation_count"] == 1
    assert document["violations"] == [
        {"rule": "AR005", "path": ENGINE, "line": 3, "message": document["violations"][0]["message"]}
    ]
    assert sorted(document["rules"]) == [f"AR00{number}" for number in range(10)]
    assert document["python_files_scanned"] == len(BASE_FILES)
    assert document["tracked_files_checked"] is None
    assert len(document["notes"]) == 1


def test_cli_rejects_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ca.main(["--root", str(tmp_path / "missing")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_module_entry_point_runs_as_documented(tmp_path: Path) -> None:
    root = make_tree(tmp_path, files={"gie/core/errors.py": "import requests\n"})
    completed = subprocess.run(
        [sys.executable, "-m", "tools.check_architecture", "--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "gie/core/errors.py:1: AR003 " in completed.stdout


# ============================================================================== the real repository
@pytest.mark.requirement("S00-DEL-03", "COM-06", "G03-05", "S00-ASG-19", "S00-ASG-22")
def test_real_repository_passes_the_graph_import_and_determinism_rules() -> None:
    report = ca.scan(REPO_ROOT)
    assert report.python_files > 0, "nothing was scanned: the check would be vacuous"
    assert [v.render() for v in report.violations if v.rule != "AR007"] == []


@pytest.mark.requirement("S00-ASG-09", "S00-ASG-08", "M-AUTH-10")
def test_real_repository_every_tracked_file_has_an_owner() -> None:
    report = ca.scan(REPO_ROOT)
    if report.tracked_files is None:
        pytest.skip(f"BLOCKED: the file list of git is not available here - {report.notes}")
    assert report.tracked_files > 0
    assert [v.render() for v in report.violations if v.rule == "AR007"] == []
