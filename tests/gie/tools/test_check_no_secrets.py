"""Tests for ``tools/check_no_secrets.py`` - no credentials in the repository (SC000-SC006).

Requirement: M-AUTH-15 ("Do not expose credentials"); ``CLAUDE.md`` rule 6.

The expected behaviour is taken from the rule texts in the module documentation of the tool - not
from the implementation.  The most important property is tested for every rule: the output names
the rule, the file, the line and the name a value is assigned to, and **never the value**.

SYNTHETIC FIXTURE: nothing in this module is a credential.  Every "secret" is an invented value
that is assembled at run time from short harmless pieces (see :func:`joined`), so that this file
itself contains no text a secret scanner could report - the scan of the real repository at the end
of the module includes this very file.  All files are written below pytest's ``tmp_path``.
"""

from __future__ import annotations

import json
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest

from tools import check_no_secrets as cs

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.requirement("M-AUTH-15")

ALLOWLIST = cs.ALLOWLIST_PATH

Found = set[tuple[str, str, int]]


# ============================================================================== synthetic fixtures
def joined(*pieces: str) -> str:
    """SYNTHETIC FIXTURE: an invented value put together at run time; it protects nothing."""
    return "".join(pieces)


#: SYNTHETIC FIXTURE: looks like a typed-in credential (21 characters, no placeholder word).
INVENTED = joined("hunter2", "-", "Zx9", "-", "synthetic")
#: SYNTHETIC FIXTURE: a second invented value, to tell findings apart.
INVENTED_TOO = joined("tr0ub4dor", "+", "Qm7", "+", "fixture")
#: SYNTHETIC FIXTURE: invented user and host of a connection string.
INVENTED_USER = joined("svc", "_", "gie", "_", "fixture")
INVENTED_HOST = joined("db", ".", "fixture", ".", "invalid")


def invented_token(length: int = 40) -> str:
    """SYNTHETIC FIXTURE: a reproducible token that looks random (a fixed arithmetic walk, no secret)."""
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits + "+/"
    return "".join(alphabet[(index * 37 + 11) % len(alphabet)] for index in range(length))


def key_header(kind: str) -> str:
    """SYNTHETIC FIXTURE: the first line of a key file of *kind*, assembled so it is not in this file."""
    return joined("-" * 5, "BEGIN ", kind, "-" * 5)


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def scan_one(root: Path, relative: str, content: str | bytes) -> list[cs.Violation]:
    """Write one SYNTHETIC FIXTURE file into an otherwise empty tree and scan the tree (without git)."""
    write(root, relative, content)
    return cs.check(root, use_git=False)


def found(violations: list[cs.Violation]) -> Found:
    return {(violation.rule, violation.path, violation.line) for violation in violations}


def everything_printed(violations: list[cs.Violation]) -> str:
    return "\n".join(
        [*(violation.render() for violation in violations), json.dumps([v.message for v in violations])]
    )


def git(cwd: Path, *arguments: str) -> None:
    """Run git inside a temporary fixture repository (never in the real one)."""
    assert REPO_ROOT not in (cwd, *cwd.parents), "fixture git commands must stay below tmp_path"
    subprocess.run(
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
            *arguments,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def git_available() -> None:
    if shutil.which("git") is None:
        pytest.skip("BLOCKED: no git executable on this machine - temporary repositories cannot be built")


def test_the_fixture_values_are_what_the_tests_assume() -> None:
    token = invented_token()
    assert len(token) == 40
    assert cs.shannon_entropy(token) > cs.ENTROPY_THRESHOLD
    assert not cs.is_word_run(token)
    assert not cs.is_placeholder(INVENTED)
    assert not cs.is_placeholder(INVENTED_TOO)
    assert len(INVENTED) >= cs.MINIMUM_VALUE_LENGTH
    assert key_header("RSA PRIVATE KEY").startswith("-----BEGIN RSA")


# ============================================================================== SC001
@pytest.mark.parametrize(
    "kind",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
    ],
)
def test_sc001_private_key_header(tmp_path: Path, kind: str) -> None:
    body = invented_token(28)  # shorter than a reportable token: only the header is found
    content = f"Deployment notes (synthetic fixture).\n\n{key_header(kind)}\n{body}\n"
    for relative in ("notes/deploy.txt", "module.py"):
        violations = scan_one(tmp_path / relative.replace("/", "_"), relative, content)
        assert found(violations) == {("SC001", relative, 3)}
        assert body not in everything_printed(violations)


@pytest.mark.parametrize("kind", ["PUBLIC KEY", "CERTIFICATE", "RSA PUBLIC KEY", "CERTIFICATE REQUEST"])
def test_sc001_public_material_is_not_a_private_key(tmp_path: Path, kind: str) -> None:
    assert scan_one(tmp_path, "notes/public.txt", f"{key_header(kind)}\nsynthetic fixture\n") == []


# ============================================================================== SC002: text forms
TEXT_FORMS: list[tuple[str, str, int, str]] = [
    ("settings.ini", 'password = "{value}"\n', 1, "password"),
    ("settings.ini", "[database]\npassword = {value}\n", 2, "password"),
    ("settings.toml", 'api_key = "{value}"\n', 1, "api_key"),
    ("config.yaml", "database:\n  password: {value}\n", 2, "password"),
    ("config.yaml", "database:\n  password: {value}  # rotate soon\n", 2, "password"),
    ("config.yml", "accounts:\n  - token: '{value}'\n", 2, "token"),
    ("compose.yaml", "environment: {{DB_PASSWORD: {value}, RETRIES: 3}}\n", 1, "DB_PASSWORD"),
    ("data.json", '{{"client_secret": "{value}"}}\n', 1, "client_secret"),
    ("data.json", '{{\n  "user": "fixture",\n  "apiKey": "{value}"\n}}\n', 3, "apiKey"),
    ("headers.json", '{{"Authorization": "Bearer {value}"}}\n', 1, "Authorization"),
    ("service.env.txt", "ACCESS_KEY={value}\n", 1, "ACCESS_KEY"),
    ("run.sh", "export AUTH_TOKEN={value}\n", 1, "AUTH_TOKEN"),
    ("run.sh", "MT5_PASSWD={value} ./start.sh\n", 1, "MT5_PASSWD"),
    ("app.cfg", "pwd={value}\n", 1, "pwd"),
    ("notes.md", "Start it with `--api-key={value}` on the paper machine.\n", 1, "api-key"),
    ("notes.md", "Line one.\nSecretKey = {value}\n", 2, "SecretKey"),
    ("Start", "echo setup\nTOKEN={value}\n", 2, "TOKEN"),
    # the three forms whose syntax ends the value without white space
    ("compose.yaml", "environment: {{RETRIES: 3, API_TOKEN: {value}}}\n", 1, "API_TOKEN"),
    ("compose.yaml", "services:\n  app: {{image: gie, secret: '{value}', replicas: 1}}\n", 2, "secret"),
    ("run.sh", "  export GIE_AUTH={value} ./start.sh --paper\n", 1, "GIE_AUTH"),
    ("notes.md", "Run `start --client-secret={value}.`\n", 1, "client-secret"),
    ("notes.txt", 'start --auth-token="{value}" --paper\n', 1, "auth-token"),
]


@pytest.mark.parametrize(("relative", "template", "line", "name"), TEXT_FORMS)
def test_sc002_name_with_a_literal_value_in_text(
    tmp_path: Path, relative: str, template: str, line: int, name: str
) -> None:
    violations = scan_one(tmp_path, relative, template.format(value=INVENTED))
    assert found(violations) == {("SC002", relative, line)}
    assert f"'{name}'" in violations[0].message  # the name is shown ...
    assert INVENTED not in everything_printed(violations)  # ... the value never is


NOT_A_LITERAL_CREDENTIAL: list[tuple[str, str]] = [
    # obvious placeholders and references to the environment
    ("config.yaml", "password: FAKE-NOT-A-SECRET\n"),
    ("config.yaml", "password: example-only-value\n"),
    ("config.yaml", "password: <your-password>\n"),
    ("config.yaml", "password: ${DB_PASSWORD}\n"),
    ("config.yaml", "password: $DB_PASSWORD\n"),
    ("config.yaml", "password: '{{ vault.db_password }}'\n"),
    ("config.yaml", "password: changeme\n"),
    ("config.yaml", "password: REDACTED\n"),
    ("config.yaml", "api_key: PLACEHOLDER_UNTIL_ISSUED\n"),
    ("config.yaml", "password: None\ntoken: null\nsecret: ''\napi_key:\n"),
    ("config.yaml", "password: '********'\n"),
    ("data.json", '{"password": null, "token": "", "api_key": "<api-key>"}\n'),
    ("run.sh", 'export TOKEN="$(cat /run/token-file)"\n'),
    # too short to be a credential
    ("config.yaml", "pwd: abc12\n"),
    # prose and Markdown, not assignments
    ("notes.md", "Password: remember to rotate it every quarter.\n"),
    ("notes.md", "Keep the token: it identifies the run in the journal.\n"),
    ("notes.md", "| password | secret | token |\n|---|---|---|\n"),
    ("notes.md", "The `password=` keyword must never carry a literal.\n"),
    ("notes.txt", "token:abcdefghijkl is not a YAML mapping (no space after the colon)\n"),
    # values that are not strings in a typed format
    ("limits.json", '{"max_tokens": 1000000, "token_ttl_seconds": 86400.5, "use_auth": false}\n'),
    ("limits.toml", "token_limit = 1000000\nauth_required = true\n"),
    # expressions and collections
    ("config.yaml", "tokens: [alpha, beta, gamma]\nauth: !vault |\n"),
    ("script.sh", "TOKEN=$(fetch_token --from vault)\n"),
    # names that only contain the letters
    ("meta.yaml", "author: Coordinator\nauthority: project-owner\nauthors: several-people\n"),
    ("meta.yaml", "path: specifications/acceptance_tests.yaml\ndescription: the password policy document\n"),
    ("meta.yaml", "keyboard: qwertz-layout\nmonkey: capuchin-fixture\n"),
    # the flow-mapping, shell-prefix and flag forms with placeholders, expressions and prose
    ("compose.yaml", "environment: {DB_PASSWORD: ${DB_PASSWORD}, RETRIES: 3}\n"),
    ("compose.yaml", "environment: {DB_PASSWORD: some longer words, RETRIES: 3}\n"),
    ("compose.yaml", "environment: {RETRIES: 3, TOKEN_TTL: 86400}\n"),
    ("run.sh", "MT5_PASSWD=$MT5_PASSWD ./start.sh\n"),
    ("run.sh", "MT5_PASSWD=$(read-secret mt5) ./start.sh\n"),
    ("notes.md", "Then MT5_PASSWD=abcdefghij is typed by the owner, never written down.\n"),
    ("notes.md", "Start it with `--api-key=<issued-key>` on the paper machine.\n"),
    ("notes.md", "Start it with `--api-key=$GIE_API_KEY` or `--token=${GIE_TOKEN}`.\n"),
    ("notes.md", "Start it with `--api-key=abc12` (too short to be a credential).\n"),
    ("notes.md", "Pass --gie-evidence=out.json and --author=coordinator-fixture.\n"),
    ("limits.json", '{"args": "--token-limit=1000000"}\n'),
    # comparisons and paths
    ("notes.txt", "if token == expected_value then continue\n"),
    ("notes.txt", "see https://example.org/login?next=/account for the form\n"),
]


@pytest.mark.parametrize(("relative", "content"), NOT_A_LITERAL_CREDENTIAL)
def test_sc002_placeholders_prose_and_non_literals_are_not_reported(
    tmp_path: Path, relative: str, content: str
) -> None:
    assert scan_one(tmp_path, relative, content) == []


# ============================================================================== SC002: Python forms
PYTHON_FORMS: list[tuple[str, int, str]] = [
    ('password = "{value}"\n', 1, "password"),
    ("password = '{value}'\n", 1, "password"),
    ('class Settings:\n    api_key: str = "{value}"\n', 2, "api_key"),
    ('settings = {{"user": "fixture", "client_secret": "{value}"}}\n', 1, "client_secret"),
    ('connection = connect(user="fixture", token="{value}")\n', 1, "token"),
    ('def connect(user, passwd="{value}"):\n    return user\n', 1, "passwd"),
    ('def connect(user, *, auth_token="{value}"):\n    return user\n', 1, "auth_token"),
    ('session.password = "{value}"\n', 1, "password"),
    ('config["access_key"] = "{value}"\n', 1, "access_key"),
    ('user, password = "fixture", "{value}"\n', 1, "password"),
    ('SECRET_KEY = b"{value}"\n', 1, "SECRET_KEY"),
    ('if (token := "{value}"):\n    pass\n', 1, "token"),
    ('DEFAULTS = dict(\n    retries=3,\n    apikey="{value}",\n)\n', 3, "apikey"),
]


@pytest.mark.parametrize(("template", "line", "name"), PYTHON_FORMS)
def test_sc002_name_with_a_literal_value_in_python(
    tmp_path: Path, template: str, line: int, name: str
) -> None:
    violations = scan_one(tmp_path, "gie/alpha/module.py", template.format(value=INVENTED))
    assert found(violations) == {("SC002", "gie/alpha/module.py", line)}
    assert f"'{name}'" in violations[0].message
    assert INVENTED not in everything_printed(violations)


PYTHON_NOT_REPORTED: list[str] = [
    "password = read_password()\n",
    'token = os.environ["GIE_TOKEN"]\n',
    'token = os.getenv("GIE_TOKEN")\n',
    "password: str | None = None\n",
    "token = tokens[0]\n",
    'password = "FAKE-NOT-A-SECRET"\n',
    'api_key = "<api-key>"\n',
    'password = ""\n',
    'pwd = "abc12"\n',
    'password = f"{prefix}{suffix}"\n',
    "max_tokens = 1000000\n",
    'author = "Coordinator of the fixture"\n',
    'report(author="Coordinator of the fixture", authority="project owner")\n',
    'description = "the password policy document"\n',
    'TOKEN_PATTERN = re.compile("[a-z]+")\n',
    "def connect(user, password=None, token=DEFAULT):\n    return user\n",
    'labels = {"kind": "password-reset-request"}\n',
]


@pytest.mark.parametrize("snippet", PYTHON_NOT_REPORTED)
def test_sc002_python_expressions_and_placeholders_are_not_reported(tmp_path: Path, snippet: str) -> None:
    assert scan_one(tmp_path, "gie/alpha/module.py", snippet) == []


def test_python_is_read_through_the_syntax_tree_not_line_by_line(tmp_path: Path) -> None:
    # A value in a comment is not an assignment of the program: SC002 does not see it (documented
    # limit) - but a long random token in a comment is still text and SC006 does.
    commented = f'# password = "{INVENTED}"\nVALUE = 1\n'
    assert scan_one(tmp_path / "a", "module.py", commented) == []
    token = invented_token()
    violations = scan_one(tmp_path / "b", "module.py", f"# issued: {token}\nVALUE = 1\n")
    assert found(violations) == {("SC006", "module.py", 1)}
    # A Python file that cannot be parsed is not skipped: it is read like any other text.
    broken = f'def broken(:\n    pass\npassword = "{INVENTED}"\n'
    assert found(scan_one(tmp_path / "c", "module.py", broken)) == {("SC002", "module.py", 3)}


def test_sc002_text_inside_a_value_is_never_printed_as_a_name(tmp_path: Path) -> None:
    fragment = joined("Zq", "token", "Xv7")
    line = f"SETTING={fragment}={INVENTED}\n"  # the middle part is a value, not a name
    violations = scan_one(tmp_path, "service.env.txt", line)
    assert violations == []
    long_name = "x" * 60 + "_password"
    violations = scan_one(tmp_path, "service.env.txt", f"{long_name}={INVENTED}\n")
    assert found(violations) == {("SC002", "service.env.txt", 1)}
    assert long_name not in violations[0].message  # an over-long name is cut
    assert "x" * 48 + "..." in violations[0].message
    assert INVENTED not in everything_printed(violations)


# ============================================================================== SC003
@pytest.mark.parametrize("scheme", ["postgres", "https", "mysql+pymysql", "amqp", "ftp"])
@pytest.mark.parametrize("relative", ["config/database.yaml", "gie/alpha/module.py", "docs/SETUP.md"])
def test_sc003_url_with_embedded_credentials(tmp_path: Path, scheme: str, relative: str) -> None:
    url = f"{scheme}://{INVENTED_USER}:{INVENTED}@{INVENTED_HOST}:5432/gie"
    content = (
        f"# synthetic fixture\nDATABASE = '{url}'\n"
        if relative.endswith(".py")
        else f"first line\nurl -> {url}\n"
    )
    violations = scan_one(tmp_path, relative, content)
    assert found(violations) == {("SC003", relative, 2)}
    assert f"'{scheme}'" in violations[0].message
    printed = everything_printed(violations)
    for hidden in (INVENTED, INVENTED_USER, INVENTED_HOST):
        assert hidden not in printed


@pytest.mark.parametrize(
    "url",
    [
        "postgres://{user}:<password>@{host}/gie",
        "postgres://{user}:${{DB_PASSWORD}}@{host}/gie",
        "postgres://{user}:{{password}}@{host}/gie",
        "postgres://{user}:%s@{host}/gie",
        "postgres://{user}:REDACTED@{host}/gie",
        "postgres://{user}@{host}/gie",
        "https://{host}:8443/path@fixture",
        "https://{host}/a/b?c=d",
        "git@{host}:owner/repository.git",
        "mailto:{user}@{host}",
    ],
)
def test_sc003_placeholders_and_urls_without_a_password_are_not_reported(tmp_path: Path, url: str) -> None:
    content = "connect to " + url.format(user=INVENTED_USER, host=INVENTED_HOST) + " (synthetic fixture)\n"
    assert scan_one(tmp_path, "docs/SETUP.md", content) == []


# ============================================================================== SC004
@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        ".env.production",
        "deploy/.env",
        "certs/server.pem",
        "certs/Server.PEM",
        "certs/private.key",
        "certs/bundle.pfx",
        "certs/store.p12",
        "home/id_rsa",
        "home/id_rsa.pub",
        "home/id_rsa_backup",
        "vault/passwords.kdbx",
    ],
)
def test_sc004_file_that_must_never_be_committed(tmp_path: Path, relative: str) -> None:
    violations = scan_one(tmp_path, relative, "SYNTHETIC FIXTURE: harmless content\n")
    assert found(violations) == {("SC004", relative, 0)}
    # The name is enough: a binary file of that name is reported as well.
    violations = scan_one(tmp_path / "binary", relative, b"\x00\x01\x02 synthetic fixture")
    assert found(violations) == {("SC004", relative, 0)}


@pytest.mark.parametrize(
    "relative",
    ["environment.md", "docs/env.txt", ".envrc", "key.txt", "keys/README.md", "keyboard.py", "my_id_rsa.txt"],
)
def test_sc004_similar_names_are_not_reported(tmp_path: Path, relative: str) -> None:
    assert scan_one(tmp_path, relative, "SYNTHETIC FIXTURE: harmless content\n") == []


# ============================================================================== SC005
BROKER_LOGINS: list[tuple[str, int, str]] = [
    (
        'import MetaTrader5 as mt5\n\nmt5.initialize(login=1234567, password="{value}", server="Demo")\n',
        3,
        "initialize",
    ),
    ('terminal.login(1234567, password="{value}")\n', 1, "login"),
    ('login(\n    account,\n    password="{value}",\n)\n', 3, "login"),
    ('ok = broker.session.login(password="{value}")\n', 1, "login"),
]


@pytest.mark.parametrize(("template", "line", "call"), BROKER_LOGINS)
def test_sc005_broker_login_with_a_literal_password(
    tmp_path: Path, template: str, line: int, call: str
) -> None:
    violations = scan_one(tmp_path, "gie/market_data/session.py", template.format(value=INVENTED))
    # One finding per literal: the more specific rule replaces SC002 for this keyword.
    assert found(violations) == {("SC005", "gie/market_data/session.py", line)}
    assert f"{call}()" in violations[0].message
    assert INVENTED not in everything_printed(violations)


def test_sc005_has_no_minimum_length_but_knows_placeholders(tmp_path: Path) -> None:
    module = "gie/market_data/session.py"
    short = joined("a", "b", "1")  # three characters: too short for SC002, still a literal password
    assert found(scan_one(tmp_path / "a", module, f'terminal.login(password="{short}")\n')) == {
        ("SC005", module, 1)
    }
    harmless = (
        "terminal.login(password=password)\n"
        'terminal.login(password=os.environ["MT5_PASSWORD"])\n'
        'terminal.login(password="FAKE-NOT-A-SECRET")\n'
        'terminal.login(password="")\n'
        'terminal.initialize(path="C:/terminal/terminal64.exe", portable=True)\n'
        'render(password="abc")\n'
    )
    assert scan_one(tmp_path / "b", module, harmless) == []


# ============================================================================== SC006
@pytest.mark.parametrize(
    "relative", ["evidence/run.log", "gie/alpha/module.py", "data/export.json", "docs/NOTES.md"]
)
def test_sc006_long_high_entropy_token(tmp_path: Path, relative: str) -> None:
    token = invented_token()
    violations = scan_one(tmp_path, relative, f"synthetic fixture\nissued = '{token}'\n")
    assert found(violations) == {("SC006", relative, 2)}
    assert "40 characters" in violations[0].message
    assert token not in everything_printed(violations)
    assert token[:8] not in everything_printed(violations)


def test_sc006_exclusions_of_the_rule_text(tmp_path: Path) -> None:
    token = invented_token()
    digits = "0123456789abcdef"
    harmless = "\n".join(
        [
            f"sha256 {digits * 4}",  # hexadecimal only: hashes and GIE identifiers
            f"commit {(digits * 3)[:40].upper()}",
            f"short {invented_token(31)}",  # below 32 characters
            f"flat {'ab' * 24}",  # long, but nearly no entropy
            f"uuid {digits[:8]}-{digits[:4]}-{digits[4:8]}-{digits[8:12]}-{digits[:12]}",
        ]
    )
    assert scan_one(tmp_path / "a", "evidence/hashes.txt", harmless + "\n") == []
    # Files below these prefixes are outside SC006 (the other rules still apply there).
    for exempt in ("specifications/source/NOTES.md", "gie/contracts/schemas/Fixture-1.0.0.schema.json"):
        root = tmp_path / exempt.split("/")[1]
        assert scan_one(root, exempt, f"value {token}\n") == []
        violations = scan_one(root, exempt, f'value {token}\npassword = "{INVENTED}"\n')
        assert found(violations) == {("SC002", exempt, 2)}


WORD_RUNS: list[str] = [
    "specifications/source/OWNER_DIRECTIVE_2026-11-27",
    "future_tolerance_ns=MAX_FUTURE_TOLERANCE_NS",
    "/tmp/work-0/-home-user-Project/a1a30857-a87b-5b9c-a837-27f407c26e59/scratchpad/rev_s101112_x7/confirm",
    "reviews/session_00/evidence/RiskDecision-1/approved_reduced_defensive",
    "test_GuardedExecutionAdapter_refuses_Unregistered_adapter_classes_v2",
]


@pytest.mark.parametrize("run", WORD_RUNS)
def test_sc006_paths_and_identifiers_made_of_words_are_not_tokens(tmp_path: Path, run: str) -> None:
    assert len(run) >= cs.MINIMUM_RUN_LENGTH
    assert cs.is_word_run(run)
    assert scan_one(tmp_path, "docs/NOTES.md", f"see {run} for details\n") == []


def test_sc006_word_run_exemption_does_not_hide_a_token(tmp_path: Path) -> None:
    # The first two runs above are beyond the entropy threshold: without the exemption they
    # would be findings, so the exemption is what the previous test proves.
    assert cs.shannon_entropy(WORD_RUNS[0]) > cs.ENTROPY_THRESHOLD
    assert cs.shannon_entropy(WORD_RUNS[1]) > cs.ENTROPY_THRESHOLD
    token = invented_token()
    # One piece that is not a word, a number or hexadecimal makes the whole run a candidate again.
    for run in (f"reviews/session_00/evidence/{token}", f"api_key={token}", f"{token}_backup_copy"):
        assert not cs.is_word_run(run)
        violations = scan_one(tmp_path, "evidence/run.log", f"synthetic fixture: {run}\n")
        assert "SC006" in {violation.rule for violation in violations}
    # A run without separators is never exempt, whatever it is made of.
    lower = joined(*(string.ascii_lowercase[(index * 7 + 3) % 26] for index in range(52)))
    assert not cs.is_word_run(lower)
    assert cs.shannon_entropy(lower) > cs.ENTROPY_THRESHOLD
    assert found(scan_one(tmp_path / "plain", "evidence/run.log", f"{lower}\n")) == {
        ("SC006", "evidence/run.log", 1)
    }


# ============================================================================== binary files and PDF
def test_binary_files_are_skipped_but_counted(tmp_path: Path) -> None:
    secret_text = f'password = "{INVENTED}"\n{invented_token()}\n'.encode()
    root = tmp_path / "tree"
    write(root, "data/blob.bin", b"\x00\x01\x02" + secret_text)
    write(root, "data/late_nul.txt", secret_text + b"x" * cs.BINARY_PROBE_BYTES + b"\x00")
    report = cs.scan(root, use_git=False)
    # A NUL byte within the first 8 KiB makes a file binary; one that comes later does not.
    assert found(list(report.violations)) == {
        ("SC002", "data/late_nul.txt", 1),
        ("SC006", "data/late_nul.txt", 2),
    }
    assert report.files == 2
    assert report.content_skipped == 1


def test_utf16_text_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    content = f'first line\npassword = "{INVENTED}"\n'.encode("utf-16")
    assert b"\x00" in content[: cs.BINARY_PROBE_BYTES]
    assert found(scan_one(tmp_path, "notes/windows.ini", content)) == {("SC002", "notes/windows.ini", 2)}


def synthetic_pdf(readable: str, encoded: str) -> bytes:
    """SYNTHETIC FIXTURE: just enough PDF structure for the scanner - not a valid document."""
    return (
        b"%PDF-1.4\n"
        b"%\xe2\xe3\xcf\xd3\x00\n"
        b"1 0 obj\n"
        b"<< /Length 99 >>\n"
        b"stream\n" + readable.encode("latin-1") + b"\nendstream\nendobj\n"
        b"2 0 obj\n"
        b"<< /Length 99 /Filter [ /ASCII85Decode /FlateDecode ] >>\n"
        b"stream\n" + encoded.encode("latin-1") + b"\nendstream\nendobj\n"
        b"%%EOF\n"
    )


def test_pdf_is_scanned_as_raw_bytes_except_encoded_streams(tmp_path: Path) -> None:
    token = invented_token()
    readable = f'password = "{INVENTED}"'
    encoded = f"{token}\npwd={INVENTED_TOO}"
    document = synthetic_pdf(readable, encoded)
    assert b"\x00" in document[: cs.BINARY_PROBE_BYTES]  # binary - and scanned nevertheless
    violations = scan_one(tmp_path / "a", "reviews/session_00/review.pdf", document)
    assert found(violations) == {("SC002", "reviews/session_00/review.pdf", 6)}
    assert INVENTED not in everything_printed(violations)
    # The same bytes in a stream without /Filter are readable text and are reported.
    unfiltered = document.replace(b" /Filter [ /ASCII85Decode /FlateDecode ]", b"")
    violations = scan_one(tmp_path / "b", "reviews/session_00/review.pdf", unfiltered)
    assert {violation.rule for violation in violations} == {"SC002", "SC006"}
    assert len(violations) == 3
    # The owner's specification PDFs are not opened at all.
    root = tmp_path / "c"
    write(root, "specifications/source/MASTER.pdf", unfiltered)
    report = cs.scan(root, use_git=False)
    assert report.violations == ()
    assert report.content_skipped == 1
    write(root, "specifications/source/notes.txt", f'password = "{INVENTED}"\n')  # only *.pdf is unopened
    assert found(cs.check(root, use_git=False)) == {("SC002", "specifications/source/notes.txt", 1)}


# ============================================================================== which files
def test_git_lists_tracked_and_untracked_files_but_not_ignored_ones(
    tmp_path: Path, git_available: None
) -> None:
    root = tmp_path / "repository"
    secret_line = f"password: {INVENTED}\n"
    write(root, ".gitignore", "ignored/\n.env\n")
    write(root, "tracked.yaml", secret_line)
    git(root, "init", "--quiet")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "SYNTHETIC FIXTURE")
    write(root, "untracked.yaml", secret_line)
    write(root, "ignored/local.yaml", secret_line)
    write(root, ".env", secret_line)
    report = cs.scan(root)
    assert found(list(report.violations)) == {("SC002", "tracked.yaml", 1), ("SC002", "untracked.yaml", 1)}
    assert report.files == 3  # .gitignore, tracked.yaml, untracked.yaml
    assert report.notes == ()
    # An ignored file that was added by force is tracked - and reported by name and by content.
    git(root, "add", "--force", ".env")
    assert found(cs.check(root)) == {
        ("SC002", "tracked.yaml", 1),
        ("SC002", "untracked.yaml", 1),
        ("SC004", ".env", 0),
        ("SC002", ".env", 1),
    }
    # A tracked file that was deleted from the working tree is listed by git and simply has no content.
    (root / "tracked.yaml").unlink()
    assert ("SC002", "tracked.yaml", 1) not in found(cs.check(root))


def test_without_git_the_tree_is_walked_and_the_output_says_so(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    secret_line = f"password: {INVENTED}\n"
    write(root, "config.yaml", secret_line)
    write(root, "ignored/local.yaml", secret_line)  # there is no git to say what is ignored
    write(root, ".gitignore", "ignored/\n")
    write(root, ".git/config", secret_line)  # the repository data itself is not project content
    write(root, "pkg/__pycache__/cached.txt", secret_line)
    write(root, ".mypy_cache/cache.json", secret_line)
    for use_git in (True, False):
        report = cs.scan(root, use_git=use_git)
        assert found(list(report.violations)) == {
            ("SC002", "config.yaml", 1),
            ("SC002", "ignored/local.yaml", 1),
        }
        assert len(report.notes) == 1
        assert "does not come from git" in report.notes[0]


def test_an_explicit_file_list_replaces_the_listing(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    write(root, "one.yaml", f"password: {INVENTED}\n")
    write(root, "two.yaml", f"password: {INVENTED}\n")
    report = cs.scan(root, files=["two.yaml", "does/not/exist.yaml"])
    assert found(list(report.violations)) == {("SC002", "two.yaml", 1)}
    assert report.files == 2
    assert report.content_skipped == 1
    assert report.notes == ()


def test_symbolic_links_are_not_followed(tmp_path: Path) -> None:
    outside = write(tmp_path / "outside", "real.yaml", f"password: {INVENTED}\n")
    root = tmp_path / "tree"
    write(root, "README.md", "synthetic fixture\n")
    try:
        (root / "linked.yaml").symlink_to(outside)
    except OSError:
        pytest.skip("BLOCKED: this platform or account cannot create symbolic links")
    report = cs.scan(root, files=["README.md", "linked.yaml"])
    assert report.violations == ()
    assert report.content_skipped == 1


# ============================================================================== the allow-list
def make_findings(root: Path) -> None:
    write(root, "config.yaml", f"user: fixture\npassword: {INVENTED}\ntoken: {INVENTED_TOO}\n")
    write(root, "evidence/run.log", f"{invented_token()}\nsecond line\n{invented_token(48)}\n")


def test_allowlist_suppresses_reviewed_findings_only(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    make_findings(root)
    everything = {
        ("SC002", "config.yaml", 2),
        ("SC002", "config.yaml", 3),
        ("SC006", "evidence/run.log", 1),
        ("SC006", "evidence/run.log", 3),
    }
    assert found(cs.check(root, use_git=False)) == everything
    write(
        root,
        ALLOWLIST,
        "# SYNTHETIC FIXTURE allow-list\n"
        "\n"
        "SC002 config.yaml:2\n"
        "SC006 evidence/run.log   # the whole file for this rule\n",
    )
    report = cs.scan(root, use_git=False)
    assert found(list(report.violations)) == {("SC002", "config.yaml", 3)}
    assert report.suppressed == 3
    assert report.notes[1:] == ()  # only the note about walking the tree; every entry was used


def test_allowlist_entry_must_match_rule_path_and_line(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    make_findings(root)
    write(
        root,
        ALLOWLIST,
        "SC002 config.yaml:7\n"  # wrong line
        "SC006 config.yaml\n"  # wrong rule for that file
        "SC002 other.yaml\n"  # wrong file
        "SC006 evidence/run.log:3\n",  # matches one finding
    )
    report = cs.scan(root, use_git=False)
    assert found(list(report.violations)) == {
        ("SC002", "config.yaml", 2),
        ("SC002", "config.yaml", 3),
        ("SC006", "evidence/run.log", 1),
    }
    assert report.suppressed == 1
    unused = [note for note in report.notes if "suppresses nothing" in note]
    assert len(unused) == 3
    assert f"{ALLOWLIST}:1:" in unused[0]
    assert "'SC002 config.yaml:7'" in unused[0]


@pytest.mark.parametrize(
    "line",
    [
        "SC999 config.yaml",  # unknown rule
        "SC000 config.yaml",  # the rule about the allow-list itself cannot be allow-listed
        "SC002",  # no path
        "config.yaml:2",  # no rule
        "SC002 /etc/config.yaml",  # not relative
        "SC002 ../config.yaml",  # leaves the repository
        "SC002 config.yaml 2",  # not the documented form
        "sc002 config.yaml",  # rule ids are upper case
    ],
)
def test_sc000_allowlist_line_that_cannot_be_used_fails_closed(tmp_path: Path, line: str) -> None:
    root = tmp_path / "tree"
    make_findings(root)
    write(root, ALLOWLIST, f"# SYNTHETIC FIXTURE\n{line}\n")
    violations = cs.check(root, use_git=False)
    assert ("SC000", ALLOWLIST, 2) in found(violations)
    assert ("SC002", "config.yaml", 2) in found(violations)  # nothing is suppressed by a broken line
    # ... and SC000 cannot be suppressed either.
    write(root, ALLOWLIST, f"# SYNTHETIC FIXTURE\n{line}\nSC000 {ALLOWLIST}\n")
    assert ("SC000", ALLOWLIST, 2) in found(cs.check(root, use_git=False))


# ============================================================================== output
def test_the_value_is_never_printed_by_any_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "tree"
    token = invented_token()
    key_body = invented_token(30)
    url = f"postgres://{INVENTED_USER}:{INVENTED_TOO}@{INVENTED_HOST}/gie"
    write(root, "config.yaml", f"password: {INVENTED}\nurl: '{url}'\n")
    write(root, "certs/server.pem", f"{key_header('RSA PRIVATE KEY')}\n{key_body}\n")
    write(root, "evidence/run.log", f"issued {token}\n")
    write(root, "gie/market_data/session.py", f'terminal.login(1234567, password="{INVENTED}")\n')
    write(root, "gie/alpha/module.py", f'API_KEY = "{INVENTED_TOO}"\n')
    hidden = (INVENTED, INVENTED_TOO, INVENTED_USER, INVENTED_HOST, token, token[:10], token[-10:], key_body)

    violations = cs.check(root, use_git=False)
    assert {violation.rule for violation in violations} == {
        "SC001",
        "SC002",
        "SC003",
        "SC004",
        "SC005",
        "SC006",
    }
    for violation in violations:
        for value in hidden:
            assert value not in violation.message
            assert value not in violation.render()

    assert cs.main(["--root", str(root)]) == 1
    text_output = capsys.readouterr()
    assert cs.main(["--root", str(root), "--json"]) == 1
    json_output = capsys.readouterr()
    for printed in (text_output.out, text_output.err, json_output.out, json_output.err):
        for value in hidden:
            assert value not in printed
    # What is printed instead: rule, path, line and the name.
    assert (
        "config.yaml:1: SC002 name 'password' is given a literal value (value not shown)" in text_output.out
    )
    assert "gie/alpha/module.py:1: SC002 name 'API_KEY'" in text_output.out
    assert "certs/server.pem:0: SC004 " in text_output.out
    assert "certs/server.pem:1: SC001 " in text_output.out


def test_cli_output_and_exit_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "tree"
    write(root, "README.md", "synthetic fixture\n")
    write(root, "data/blob.bin", b"\x00\x01")
    assert cs.main(["--root", str(root)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].startswith("check_no_secrets: note: the file list does not come from git")
    assert lines[-1].startswith("check_no_secrets: OK - 2 files scanned (1 binary or unopened")
    assert captured.err == ""

    write(root, "config.yaml", f"password: {INVENTED}\n")
    assert cs.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("config.yaml:1: SC002 ")
    assert "OK" not in captured.out
    assert "FAILED - 1 violation(s)" in captured.err


def test_cli_json_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "tree"
    make_findings(root)
    write(root, ALLOWLIST, "SC006 evidence/run.log\n")
    assert cs.main(["--root", str(root), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["tool"] == "tools/check_no_secrets.py"
    assert document["files_scanned"] == 3
    assert document["findings_suppressed_by_allowlist"] == 2
    assert document["violation_count"] == 2
    assert [(v["rule"], v["path"], v["line"]) for v in document["violations"]] == [
        ("SC002", "config.yaml", 2),
        ("SC002", "config.yaml", 3),
    ]
    assert sorted(document["rules"]) == [f"SC00{number}" for number in range(7)]


def test_cli_rejects_a_root_that_is_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cs.main(["--root", str(tmp_path / "missing")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_module_entry_point_runs_as_documented(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    write(root, "config.yaml", f"password: {INVENTED}\n")
    completed = subprocess.run(
        [sys.executable, "-m", "tools.check_no_secrets", "--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "config.yaml:1: SC002 " in completed.stdout
    assert INVENTED not in completed.stdout + completed.stderr


def test_the_scan_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    make_findings(root)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert cs.main(["--root", str(root)]) == 1
    after = {
        path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


# ============================================================================== the real repository
def test_real_repository_contains_no_credentials() -> None:
    report = cs.scan(REPO_ROOT)
    assert report.files > 0, "nothing was scanned: the check would be vacuous"
    assert [violation.render() for violation in report.violations] == []


def test_this_test_module_is_part_of_the_scan_and_clean() -> None:
    relative = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    report = cs.scan(REPO_ROOT, files=[relative, "tools/check_no_secrets.py"])
    assert report.files == 2
    assert report.content_skipped == 0
    assert report.violations == ()
