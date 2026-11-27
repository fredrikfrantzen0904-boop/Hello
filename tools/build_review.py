"""Build the Session 00 foundation review PDF from recorded evidence.

``python -m tools.build_review --evidence-dir reviews/session_00/evidence --out FILE.pdf``

Every number, status and table in the PDF is read from a file: the registers
(``specifications/*.yaml``), the logs (``docs/*.md``), the contract declarations
and the evidence written by ``tools/ci.py`` / ``tools/benchmark_foundation.py``.
Nothing is typed into the report by hand except explanatory prose, so the PDF
can be checked against the machine-readable evidence next to it (M-AUTH-17).
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import gie.contracts
from gie.contracts.registry import all_contracts

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION = "S00"
INK = colors.HexColor("#1b2a41")
ACCENT = colors.HexColor("#8a6d1d")
GRID = colors.HexColor("#b8bec8")
SHADE = colors.HexColor("#eef1f5")
STATUS_COLOURS = {
    "verified": colors.HexColor("#1e6b3a"),
    "passing": colors.HexColor("#1e6b3a"),
    "passed": colors.HexColor("#1e6b3a"),
    "APPROVED": colors.HexColor("#1e6b3a"),
    "RESOLVED": colors.HexColor("#1e6b3a"),
    "implemented": colors.HexColor("#3b5b92"),
    "partial": colors.HexColor("#9a6700"),
    "in_progress": colors.HexColor("#9a6700"),
    "PROPOSED": colors.HexColor("#9a6700"),
    "OPEN": colors.HexColor("#9a6700"),
    "blocked": colors.HexColor("#a12d2d"),
    "failed": colors.HexColor("#a12d2d"),
    "failing": colors.HexColor("#a12d2d"),
    "not_started": colors.HexColor("#5c6470"),
    "planned": colors.HexColor("#5c6470"),
}

_styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_styles["BodyText"], fontName="Helvetica", fontSize=9, leading=12.5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.5, leading=9.5)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=7.5, leading=9.3)
CELL_BOLD = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
MONO = ParagraphStyle("mono", parent=CELL, fontName="Courier", fontSize=7, leading=8.6)
H1 = ParagraphStyle(
    "h1",
    parent=BODY,
    fontName="Helvetica-Bold",
    fontSize=15,
    leading=19,
    textColor=INK,
    spaceBefore=4,
    spaceAfter=7,
)
H2 = ParagraphStyle(
    "h2",
    parent=BODY,
    fontName="Helvetica-Bold",
    fontSize=11,
    leading=14,
    textColor=INK,
    spaceBefore=9,
    spaceAfter=4,
)
H3 = ParagraphStyle(
    "h3",
    parent=BODY,
    fontName="Helvetica-Bold",
    fontSize=9.5,
    leading=12,
    textColor=ACCENT,
    spaceBefore=6,
    spaceAfter=2,
)
TITLE = ParagraphStyle(
    "title",
    parent=BODY,
    fontName="Helvetica-Bold",
    fontSize=24,
    leading=29,
    textColor=INK,
    alignment=TA_CENTER,
)
SUBTITLE = ParagraphStyle(
    "subtitle", parent=BODY, fontSize=12, leading=16, alignment=TA_CENTER, textColor=ACCENT
)
CENTER = ParagraphStyle("center", parent=BODY, alignment=TA_CENTER)
PAGE_WIDTH = A4[0] - 36 * mm


# --------------------------------------------------------------------------- helpers
def p(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(text, style)


def esc(text: object) -> str:
    return escape(str(text))


def status_text(status: str) -> str:
    colour = STATUS_COLOURS.get(status)
    if colour is None:
        return f"<b>{esc(status)}</b>"
    return f'<font color="#{colour.hexval()[2:]}"><b>{esc(status)}</b></font>'


def table(
    rows: list[list[Any]], widths: list[float], header: bool = True, style: ParagraphStyle = CELL
) -> Table:
    data: list[list[Any]] = []
    for index, row in enumerate(rows):
        cell_style = CELL_BOLD if header and index == 0 else style
        data.append(
            [cell if isinstance(cell, Flowable) else Paragraph(str(cell), cell_style) for cell in row]
        )
    total = sum(widths)
    scaled = [width / total * PAGE_WIDTH for width in widths]
    result = Table(data, colWidths=scaled, repeatRows=1 if header else 0)
    commands: list[tuple[Any, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.3, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), SHADE))
    result.setStyle(TableStyle(commands))
    return result


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def load_yaml(relative: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
    return loaded


def parse_log(relative: str) -> list[dict[str, str]]:
    """Entries of a markdown log: ``## X-nnn - Title`` followed by ``- Key: value`` lines and prose."""
    path = REPO_ROOT / relative
    if not path.exists():
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^## ([A-Z]{1,2}-[0-9]{2,5}) - (.+)$", line)
        if match:
            if current is not None:
                current["body"] = "\n".join(body).strip()
                entries.append(current)
            current = {"id": match[1], "title": match[2].strip()}
            body = []
            continue
        if current is None:
            continue
        if line.startswith("# ") or line.strip() == "---":
            continue
        field = re.match(r"^- ([A-Za-z ]+): (.+)$", line)
        if field and field[1].strip().lower() in ("status", "date", "requirements", "decided by", "raised"):
            current[field[1].strip().lower()] = field[2].strip()
        else:
            body.append(line)
    if current is not None:
        current["body"] = "\n".join(body).strip()
        entries.append(current)
    return entries


def first_paragraph(text: str, limit: int = 600) -> str:
    paragraph = text.strip().split("\n\n")[0].replace("\n", " ")
    paragraph = re.sub(r"\*\*(.+?)\*\*", r"\1", paragraph)
    paragraph = paragraph.replace("`", "")
    return paragraph if len(paragraph) <= limit else paragraph[: limit - 3] + "..."


# --------------------------------------------------------------------------- diagrams
def _arrow(drawing: Drawing, x1: float, y1: float, x2: float, y2: float, dashed: bool = False) -> None:
    line = Line(x1, y1, x2, y2, strokeColor=INK, strokeWidth=0.6)
    if dashed:
        line.strokeDashArray = [2, 2]
        line.strokeColor = colors.HexColor("#7a8290")
    drawing.add(line)
    angle = math.atan2(y2 - y1, x2 - x1)
    size = 4.0
    left = (x2 - size * math.cos(angle - 0.4), y2 - size * math.sin(angle - 0.4))
    right = (x2 - size * math.cos(angle + 0.4), y2 - size * math.sin(angle + 0.4))
    drawing.add(
        Polygon(
            [x2, y2, left[0], left[1], right[0], right[1]],
            fillColor=line.strokeColor,
            strokeColor=line.strokeColor,
            strokeWidth=0.3,
        )
    )


def dependency_diagram(graph: dict[str, Any]) -> Drawing:
    sessions: dict[str, Any] = graph["sessions"]
    width, height = PAGE_WIDTH, 250.0
    drawing = Drawing(width, height)
    waves: dict[int, list[str]] = {}
    for sid, info in sessions.items():
        waves.setdefault(int(info["wave"]), []).append(sid)
    box_w, box_h = 104.0, 30.0
    column_gap = (width - box_w * len(waves)) / max(len(waves) - 1, 1)
    position: dict[str, tuple[float, float]] = {}
    for column, wave in enumerate(sorted(waves)):
        members = sorted(waves[wave])
        x = column * (box_w + column_gap)
        slot = (height - 24) / len(members)
        drawing.add(
            String(
                x + box_w / 2,
                height - 10,
                f"Wave {wave}",
                fontName="Helvetica-Bold",
                fontSize=8,
                fillColor=ACCENT,
                textAnchor="middle",
            )
        )
        for row, sid in enumerate(members):
            y = height - 24 - (row + 0.5) * slot - box_h / 2
            position[sid] = (x, y)
    for sid, info in sessions.items():
        x, y = position[sid]
        for dep in info.get("depends_on", []):
            dx, dy = position[dep]
            _arrow(drawing, dx + box_w, dy + box_h / 2, x, y + box_h / 2)
        for dep in info.get("contract_only", []):
            if sid == "S11":
                continue  # the dashboard reads the contracts of all sessions; stated in the legend
            dx, dy = position[dep]
            if dx < x:
                _arrow(drawing, dx + box_w, dy + box_h / 2, x, y + box_h / 2, dashed=True)
            else:
                _arrow(
                    drawing,
                    dx + box_w / 2,
                    dy + (box_h if dy < y else 0),
                    x + box_w / 2,
                    y + (0 if dy < y else box_h),
                    dashed=True,
                )
    for sid, info in sessions.items():
        x, y = position[sid]
        delivered = sid == SESSION
        drawing.add(
            Rect(
                x,
                y,
                box_w,
                box_h,
                rx=3,
                ry=3,
                fillColor=colors.HexColor("#dfe9df") if delivered else colors.white,
                strokeColor=INK,
                strokeWidth=0.8,
            )
        )
        drawing.add(
            String(
                x + box_w / 2,
                y + box_h - 11,
                f"{sid[1:]}  {info['name']}",
                fontName="Helvetica-Bold",
                fontSize=7,
                fillColor=INK,
                textAnchor="middle",
            )
        )
        label = "this review" if delivered else "not started"
        drawing.add(
            String(
                x + box_w / 2,
                y + 6,
                label,
                fontName="Helvetica",
                fontSize=6.5,
                fillColor=colors.HexColor("#5c6470"),
                textAnchor="middle",
            )
        )
    return drawing


def flow_diagram() -> Drawing:
    names = ["Tick", "Evidence", "Candidate", "TradePlan", "RiskDecision", "OrderEvent", "Position"]
    owners = ["01", "02 / 03", "04", "05", "08", "07", "07"]
    width, height = PAGE_WIDTH, 62.0
    drawing = Drawing(width, height)
    box_w = (width - 6 * 12) / 7
    for index, (name, owner) in enumerate(zip(names, owners, strict=True)):
        x = index * (box_w + 12)
        drawing.add(Rect(x, 22, box_w, 28, rx=3, ry=3, fillColor=SHADE, strokeColor=INK, strokeWidth=0.8))
        drawing.add(
            String(
                x + box_w / 2,
                38,
                name,
                fontName="Helvetica-Bold",
                fontSize=7.5,
                fillColor=INK,
                textAnchor="middle",
            )
        )
        drawing.add(
            String(
                x + box_w / 2,
                27,
                f"session {owner}",
                fontName="Helvetica",
                fontSize=6.5,
                fillColor=colors.HexColor("#5c6470"),
                textAnchor="middle",
            )
        )
        if index:
            _arrow(drawing, x - 12, 36, x, 36)
    drawing.add(
        String(
            0,
            8,
            "ModelVersion (09) is referenced by Candidate and TradePlan.  Clock (00) is injected into every component.  Reflex (06) reads Tick, OrderEvent, Position.",
            fontName="Helvetica",
            fontSize=6.5,
            fillColor=colors.HexColor("#5c6470"),
        )
    )
    return drawing


# --------------------------------------------------------------------------- sections
def session_requirements(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for requirement in matrix["requirements"]:
        contributions = [c for c in requirement.get("contributions", []) if c.get("session") == SESSION]
        if requirement["id"].startswith("S00-") or contributions:
            selected.append({**requirement, "s00": contributions[0] if contributions else None})
    return selected


def requirement_rows(requirements: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["id", "requirement", "status", "what Session 00 delivered / why not", "evidence"]
    ]
    for requirement in requirements:
        contribution = requirement["s00"] or {}
        status = contribution.get("status", requirement["status"])
        evidence = "<br/>".join(esc(item) for item in contribution.get("evidence", [])[:4])
        blockers = ", ".join(contribution.get("blockers", []))
        summary = esc(contribution.get("summary", requirement.get("notes", "")))
        if blockers:
            summary += f" <b>[{esc(blockers)}]</b>"
        rows.append(
            [
                esc(requirement["id"]),
                esc(requirement["text"]),
                status_text(status),
                summary,
                Paragraph(evidence, MONO),
            ]
        )
    return rows


def contract_section(story: list[Any]) -> None:
    order = [
        "Tick",
        "Evidence",
        "Candidate",
        "TradePlan",
        "RiskDecision",
        "OrderEvent",
        "Position",
        "ModelVersion",
        "Clock",
    ]
    contracts = all_contracts()
    for name in order:
        record_type = contracts[name]
        doc = " ".join((record_type.__doc__ or "").split())
        story.append(p(f"{esc(name)} {esc(record_type.VERSION)}", H3))
        story.append(p(esc(doc), SMALL))
        rows: list[list[Any]] = [["field", "type", "req.", "unit", "meaning"]]
        for spec in record_type.FIELD_SPECS:
            rows.append(
                [
                    Paragraph(esc(spec.name), MONO),
                    esc(spec.ftype.describe()),
                    "yes" if spec.required else "no",
                    esc(spec.unit or ""),
                    esc(spec.doc),
                ]
            )
        story.append(table(rows, [22, 24, 5, 15, 46]))
        if record_type.ALL_INVARIANTS:
            story.append(Spacer(1, 2))
            invariant_rows: list[list[Any]] = [
                ["invariant", "rule (a record violating it cannot be constructed or decoded)"]
            ]
            invariant_rows += [[esc(rule.id), esc(rule.text)] for rule in record_type.ALL_INVARIANTS]
            story.append(table(invariant_rows, [12, 100]))
        story.append(Spacer(1, 5))


def build(evidence_dir: Path, out: Path, reviewed_commit: str) -> None:
    matrix = load_yaml("specifications/requirements_matrix.yaml")
    acceptance = load_yaml("specifications/acceptance_tests.yaml")
    graph = load_yaml("specifications/dependency_graph.yaml")
    decisions = parse_log("docs/DECISION_LOG.md")
    changes = parse_log("docs/CHANGE_REQUESTS.md")
    ci = load_json(evidence_dir / "ci_results.json")
    tests = load_json(evidence_dir / "test_results.json")
    coverage = load_json(evidence_dir / "coverage.json")
    bench = load_json(evidence_dir / "benchmark.json")
    manifest = load_json(evidence_dir / "file_manifest.json")
    independent = load_json(evidence_dir / "independent_review.json")
    requirements = session_requirements(matrix)
    counts: dict[str, int] = {}
    for requirement in requirements:
        status = (requirement["s00"] or {}).get("status", requirement["status"])
        counts[status] = counts.get(status, 0) + 1
    total_counts: dict[str, int] = {}
    for requirement in matrix["requirements"]:
        total_counts[requirement["status"]] = total_counts.get(requirement["status"], 0) + 1
    s00_acceptance = [entry for entry in acceptance["acceptance_tests"] if entry["session"] == SESSION]
    blockers = [entry for entry in changes if entry["id"].startswith("B-")]
    change_requests = [entry for entry in changes if entry["id"].startswith("CR-")]
    open_items = [entry for entry in decisions if entry["id"].startswith("U-")]
    decision_entries = [entry for entry in decisions if entry["id"].startswith("D-")]

    story: list[Any] = []
    # ------------------------------------------------------------------ title
    story += [Spacer(1, 45 * mm), p("Gold Intelligence Engine", TITLE), Spacer(1, 4 * mm)]
    story += [
        p("Session 00 - Project Coordinator", SUBTITLE),
        p("Foundation review package", SUBTITLE),
        Spacer(1, 14 * mm),
    ]
    overall = ci["overall"] if ci else "no CI evidence found"
    title_rows = [
        ["Submitted for", "Owner approval at the architectural review gate (independent review: ChatGPT)"],
        ["Review status", "UNVERIFIED - awaiting owner decision (approved / conditional / rejected)"],
        ["Commit reviewed", f"<font face='Courier'>{esc(reviewed_commit)}</font>"],
        ["Branch", "claude/gold-intelligence-engine-l9uw10"],
        ["Contract set", f"{esc(gie.contracts.CONTRACT_SET_VERSION)} (PROPOSED)"],
        ["Integration CI on that commit", esc(overall)],
        ["Scope delivered", "Session 00 only. Sessions 01-12 are not started."],
        ["Trading", "Paper / replay research only. Real order routing is locked. No live trading."],
    ]
    story.append(table(title_rows, [30, 82], header=False, style=BODY))
    story += [
        Spacer(1, 16 * mm),
        p(
            "Every figure in this document is generated from the machine-readable evidence in <font face='Courier'>reviews/session_00/evidence/</font> and from the registers in the repository. Performance figures are micro-benchmarks of software primitives on synthetic fixtures; no trading performance is claimed anywhere in this package.",
            CENTER,
        ),
        PageBreak(),
    ]

    # ------------------------------------------------------------------ 1 summary
    story.append(p("1. Summary for the review gate", H1))
    story.append(
        p(
            "<b>What Session 00 was asked to do</b> (brief 00): audit the existing system without modifying production; establish repository, worktrees, ownership and the dependency graph; define nine versioned shared contracts with units, timestamps, IDs, freshness, error semantics and event ordering; create CLAUDE.md, the decision log, the requirements matrix and the integration CI; pass the acceptance tests (contract round-trip and compatibility, replay clock, paper-only routing lock, baseline regression, no production changes)."
        )
    )
    story.append(Spacer(1, 3))
    summary_rows: list[list[Any]] = [["Session 00 requirement status", "count"]]
    for status in (
        "verified",
        "implemented",
        "partial",
        "in_progress",
        "blocked",
        "not_started",
        "not_applicable",
    ):
        if counts.get(status):
            summary_rows.append([status_text(status), str(counts[status])])
    summary_rows.append(["<b>total requirements tracked for Session 00</b>", f"<b>{len(requirements)}</b>"])
    story.append(table(summary_rows, [80, 20]))
    story.append(Spacer(1, 4))
    story.append(
        p(
            f"The whole project tracks <b>{len(matrix['requirements'])}</b> atomised requirements; status over all sessions: "
            + ", ".join(f"{esc(k)} {v}" for k, v in sorted(total_counts.items()))
            + "."
        )
    )
    story.append(p("Not delivered - stated explicitly", H2))
    if blockers:
        for entry in blockers:
            story.append(
                p(
                    f"<b>{esc(entry['id'])} - {esc(entry['title'])}</b> ({status_text(entry.get('status', '?'))}). {esc(first_paragraph(entry['body'], 900))}"
                )
            )
    blocked = [
        r
        for r in requirements
        if (r["s00"] or {}).get("status", r["status"]) in ("blocked", "partial", "in_progress", "not_started")
    ]
    if blocked:
        story.append(Spacer(1, 3))
        rows = [["id", "requirement", "status", "reason"]]
        for requirement in blocked:
            contribution = requirement["s00"] or {}
            rows.append(
                [
                    esc(requirement["id"]),
                    esc(requirement["text"]),
                    status_text(contribution.get("status", requirement["status"])),
                    esc(contribution.get("summary", "")),
                ]
            )
        story.append(table(rows, [13, 37, 11, 51]))
    story.append(p("Decision requested from the owner", H2))
    story.append(
        p(
            "1. Approve, conditionally approve or reject the shared contracts (section 4, appendix A) and the architecture decisions D-002 .. D-018 (section 8).<br/>2. Choose how the existing system will be made accessible so that the audit and the baseline freeze can be completed (blocker B-001, open item U-01).<br/>3. Answer the open items U-02 .. U-13 (section 9), or delegate them to the sessions that need them.<br/>4. Authorise - or not - the start of wave 2 (sessions 01, 02, 07, 08). Nothing beyond Session 00 has been started."
        )
    )
    story.append(PageBreak())

    # ------------------------------------------------------------------ 2 architecture
    story.append(p("2. Architecture report", H1))
    story.append(
        p(
            "Full text: <font face='Courier'>docs/ARCHITECTURE.md</font>. GIE observes several markets and trades only XAUUSD. Work is split into three timing layers (master G03): <b>reflex</b> on every tick (protection only, never waits for a model, the network or the dashboard), <b>tactical</b> on every tick or second (plans, risk gate, orders) and <b>strategic</b> on completed candles and in the background (evidence, models). Subsystems exchange immutable, versioned records and never import each other."
        )
    )
    story.append(p("Data flow", H2))
    story.append(flow_diagram())
    story.append(p("Design principles enforced by the foundation", H2))
    principle_rows = [
        ["principle", "how it is enforced", "where"],
        [
            "Immutable facts",
            "frozen records; a change is a new revision / next sequence number",
            "gie/contracts",
        ],
        [
            "Valid by construction",
            "field rules and cross-field invariants checked on construction and decoding",
            "gie/contracts/_spec.py",
        ],
        [
            "Deterministic",
            "canonical JSON (RFC 8785 with exact integers), content-derived ids, injected time, total order",
            "gie/core",
        ],
        [
            "Causal (no look-ahead)",
            "ordering by availability time; causality watermark as_of_time_ns <= decision_time_ns; future data is an error",
            "gie/core/ordering.py, freshness.py",
        ],
        [
            "Fail closed",
            "stale, expired, invalid or unknown input can never create risk; reducing risk stays allowed",
            "gie/core/errors.py, freshness.py",
        ],
        [
            "Labelled data",
            "every record names run, mode and data origin; fixtures are SYNTHETIC_FIXTURE",
            "gie/contracts/common.py",
        ],
        [
            "No live trading",
            "four-layer routing lock (vocabulary, constant, guard, static scan)",
            "gie/core/routing_lock.py, tools/check_live_lock.py",
        ],
        [
            "Parallel work without collisions",
            "import rules: subsystem -> core + contracts only; composition root owned by the coordinator",
            "tools/check_architecture.py",
        ],
    ]
    story.append(table(principle_rows, [22, 58, 32]))
    story.append(p("Routing lock", H2))
    story.append(
        p(
            '"Real routing" is any order message leaving the process towards a broker, terminal or exchange - demo accounts included (decision D-011, to be confirmed: U-06). (1) <b>Vocabulary</b>: RunMode = {REPLAY, PAPER}, ExecutionVenue = {SIMULATED}; a live order cannot be constructed or decoded. (2) <b>Constant</b>: LIVE_ROUTING_PERMITTED is the literal False, read from no environment variable, file or flag. (3) <b>Guard</b>: RoutingGuard.authorize admits only registered simulated adapter types. (4) <b>Static scan</b> in CI for broker order-entry calls, broker SDK imports and tampering. Honest limit: Python cannot stop a person who edits the lock module itself; such an edit is caught by the scan, the tests and review. There is no configuration that unlocks it.'
        )
    )
    story.append(p("Repository layout and ownership", H2))
    owner_rows: list[list[Any]] = [["session", "name", "wave", "depends on", "contracts only", "owned paths"]]
    for sid, info in graph["sessions"].items():
        owner_rows.append(
            [
                sid,
                esc(info["name"]),
                str(info["wave"]),
                ", ".join(info["depends_on"]) or "-",
                ", ".join(info["contract_only"]) or "-",
                Paragraph(esc(", ".join(info["owns"])), MONO),
            ]
        )
    story.append(table(owner_rows, [7, 20, 5, 14, 16, 50]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 3 dependency graph
    story.append(p("3. Dependency graph", H1))
    story.append(
        p(
            "Source of truth: <font face='Courier'>specifications/dependency_graph.yaml</font>, verified in CI by <font face='Courier'>tools/check_architecture.py</font>. Solid arrows: the dependency's review package must be approved before integration. Dashed arrows: only the shared contracts of that session are needed. Session 11 (dashboard) reads the contracts of all sessions (arrows omitted). Sessions 10 and 12 start scaffolding early, as the master specification allows."
        )
    )
    story.append(dependency_diagram(graph))
    story.append(p("Python import rules", H2))
    import_rows: list[list[Any]] = [
        ["package", "owner", "may import (besides the standard library and itself)"]
    ]
    for name, info in graph["packages"].items():
        import_rows.append(
            [
                Paragraph(esc(name), MONO),
                info["session"],
                esc(", ".join(info["may_import"]) or "nothing (standard library only)"),
            ]
        )
    story.append(table(import_rows, [30, 10, 72]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 4 contracts
    story.append(p("4. Shared contracts", H1))
    story.append(
        p(
            "Normative rules: <font face='Courier'>specifications/SHARED_CONTRACTS.md</font>. Machine-readable schemas (JSON Schema 2020-12, generated from the code and locked by SHA-256): <font face='Courier'>gie/contracts/schemas/</font>. Complete field tables: appendix A."
        )
    )
    rules_rows = [
        ["topic", "rule"],
        [
            "Units",
            "instants: int64 nanoseconds since the Unix epoch, UTC; prices, volumes, money and risk fractions: exact decimals (strings on the wire); statistics: finite doubles; unknown is null, never 0; fractions not percent; R multiples are net of costs.",
        ],
        [
            "Timestamps",
            "event time (at the source), receive time (availability at GIE), decision time (record created); causality watermark as_of_time_ns; all time from an injected Clock; trading day = Europe/Prague calendar date.",
        ],
        [
            "IDs",
            "prefix + 128-bit hex of SHA-256 over documented identity parts; deterministic, recomputable, verified by the record; market events de-duplicate by market_event_id; orders are idempotent by client_order_id.",
        ],
        [
            "Freshness",
            "usable iff now < valid_until; fresh iff 0 <= age < max_age; data from the future raises FutureDataError; limits are research parameters.",
        ],
        [
            "Error semantics",
            "typed errors with stable codes; fail closed; invalid input is never repaired; unknown vocabulary is refused; normal rejections are records with reason codes, not errors.",
        ],
        [
            "Event ordering",
            "per-stream sequence numbers (duplicate / gap / conflict detection); total order (availability time, stream rank, stream, seq); late ticks are flagged, not reordered.",
        ],
        [
            "Versioning",
            "semantic version per contract; PATCH = documentation, MINOR = new optional field or enum value, MAJOR = anything else; readers refuse another MAJOR, ignore unknown fields only from a newer MINOR, always refuse unknown enum values; CI enforces the rules on the locked schemas.",
        ],
    ]
    story.append(table(rules_rows, [16, 96]))
    story.append(p("The nine contracts", H2))
    overview_rows: list[list[Any]] = [["contract", "version", "fields", "invariants", "purpose"]]
    for name in (
        "Tick",
        "Evidence",
        "Candidate",
        "TradePlan",
        "RiskDecision",
        "OrderEvent",
        "Position",
        "ModelVersion",
        "Clock",
    ):
        record_type = all_contracts()[name]
        purpose = " ".join((record_type.__doc__ or "").split()).split(". ")[0]
        overview_rows.append(
            [
                esc(name),
                esc(record_type.VERSION),
                str(len(record_type.FIELD_SPECS)),
                str(len(record_type.ALL_INVARIANTS)),
                esc(purpose),
            ]
        )
    story.append(table(overview_rows, [16, 9, 8, 10, 69]))
    story.append(p("Safety rules that are structural", H2))
    story.append(
        p(
            "A record that violates one of these cannot be constructed or decoded: no live mode or real venue exists; only XAUUSD can be planned, ordered or held by GIE; every plan has a hard stop on the losing side; every entry order carries its plan, its risk approval and an attached protective stop; a rejection grants nothing; emergency halt never approves; at or below the daily profit floor nothing is approved; a stop is never wider than the initial stop and never removed; a position is never increased; the executable net P&L peak never decreases; missing evidence has null scores and inactive evidence zero scores; a model becomes ACTIVE only after SHADOW and with a recorded owner approval."
        )
    )
    story.append(p("Provisional elements", H2))
    story.append(
        p(
            "Because the six engines could not be inspected (B-001), engine-specific vocabularies are open strings or small provisional enumerations (EvidenceStage, MarketSession, TickFlag, ModelKind, reason codes). Additional shared types (Candle, InstrumentSpec, AccountSnapshot, broker calendar) are expected to be proposed by sessions 01, 07 and 08 (change request CR-003)."
        )
    )
    story.append(PageBreak())

    # ------------------------------------------------------------------ 5 baseline audit
    story.append(p("5. Baseline audit", H1))
    audit_text = (REPO_ROOT / "docs" / "BASELINE_AUDIT.md").read_text(encoding="utf-8")
    verdict = re.search(r"## Verdict\n\n(.+?)\n\n## ", audit_text, re.S)
    story.append(p(esc(first_paragraph(verdict[1] if verdict else "", 1200))))
    story.append(
        p(
            "Full report: <font face='Courier'>docs/BASELINE_AUDIT.md</font>. What was examined: only the Git repository attached to the session, which contained a single 203-byte file (<font face='Courier'>Start</font>, generic git bootstrap commands, no trading code) at commit 35e32cff. That file is a protected path and is verified byte-identical on every CI run; branch <font face='Courier'>main</font> was not modified."
        )
    )
    audit_rows = [
        ["inventory item (brief 00)", "state", "what exists instead"],
        [
            "languages, processes, data, tests, hardware, broker interfaces of D:\\ftmo_eurusd",
            status_text("blocked"),
            "read-only audit tool for the owner to run: tools/audit/gie_baseline_audit.py (standard library only, no network, no file contents, no credentials; tested to leave the audited tree byte-identical)",
        ],
        [
            "baseline regression (GCE-B, Round 1, Round 2, Cavemen trade sets)",
            status_text("blocked"),
            "freeze / verify harness tools/baseline_freeze.py, self-tested on a labelled synthetic fixture; the real test reports BLOCKED, it does not pass",
        ],
        [
            "figures quoted in master G09",
            "unverified claims",
            "recorded in specifications/baselines/claims.yaml for reconciliation in sessions 02 and 10; nothing treats them as verified",
        ],
    ]
    story.append(table(audit_rows, [34, 12, 66]))
    story.append(
        p(
            "Proposed way forward (owner to choose, U-01): (1) the owner runs the audit tool and returns the output folder - recommended; (2) the machine is connected through Claude Remote Control; (3) a copy of the code without credentials is provided. After access: complete the inventory, freeze the baselines, re-run the regression test, revisit decisions D-003 and D-014, and submit an addendum."
        )
    )

    # ------------------------------------------------------------------ 6 evidence
    story.append(p("6. Test evidence", H1))
    if ci:
        story.append(
            p(
                f"Integration CI (<font face='Courier'>python -m tools.ci</font>) on commit <font face='Courier'>{esc(ci.get('commit', ''))[:12]}</font>, working tree clean: {esc(ci.get('working_tree_clean'))}; Python {esc(ci.get('python'))}; {esc(ci.get('platform'))}. Overall: <b>{esc(ci['overall'])}</b>."
            )
        )
        step_rows: list[list[Any]] = [["step", "command", "result", "exit", "seconds"]]
        for step in ci["steps"]:
            note = f"<br/>{esc(step['note'])}" if step.get("note") else ""
            step_rows.append(
                [
                    esc(step["name"]) + note,
                    Paragraph(esc(step["command"]), MONO),
                    status_text(step["status"]),
                    str(step["exit_code"]),
                    f"{step['duration_s']:.1f}",
                ]
            )
        story.append(table(step_rows, [30, 54, 10, 6, 9]))
    else:
        story.append(p("<b>No CI evidence file was found.</b>"))
    if tests:
        story.append(p("Test results", H2))
        story.append(
            p(
                "Outcome counts over all collected tests: "
                + ", ".join(f"<b>{esc(k)}</b> {v}" for k, v in tests["counts"].items())
                + f". Total {len(tests['tests'])} tests in {tests['duration_s']:.1f} s."
            )
        )
        not_passed = [t for t in tests["tests"] if t["outcome"] != "passed"]
        if not_passed:
            rows = [["test", "outcome", "reason"]]
            for item in not_passed:
                rows.append(
                    [
                        Paragraph(esc(item["nodeid"]), MONO),
                        status_text(item["outcome"]),
                        esc(item["detail"][:300]),
                    ]
                )
            story.append(p("Tests that did not pass (failed, skipped or blocked) - listed explicitly:", BODY))
            story.append(table(rows, [50, 10, 52]))
        else:
            story.append(p("No test failed and no test was skipped."))
    story.append(p("Acceptance tests of Session 00", H2))
    acceptance_rows: list[list[Any]] = [
        ["id", "acceptance test", "status", "tests passed / failed / skipped", "blockers / notes"]
    ]
    for entry in s00_acceptance:
        evidence = entry.get("evidence") or {}
        counts_text = (
            f"{evidence.get('passed', 0)} / {evidence.get('failed', 0)} / {evidence.get('skipped', 0)}"
            if evidence
            else "-"
        )
        acceptance_rows.append(
            [
                esc(entry["id"]),
                esc(entry["title"]),
                status_text(entry["status"]),
                counts_text,
                esc(
                    ", ".join(entry.get("blockers", []))
                    + (" " + entry.get("notes", "") if entry.get("notes") else "")
                ),
            ]
        )
    story.append(table(acceptance_rows, [16, 26, 11, 20, 39]))
    if coverage:
        totals = coverage.get("totals", {})
        story.append(p("Coverage", H2))
        story.append(
            p(
                f"Statement and branch coverage of the <font face='Courier'>gie</font> package measured by the same run: <b>{totals.get('percent_covered', 0):.1f} %</b> ({totals.get('covered_lines', 0)} of {totals.get('num_statements', 0)} statements, {totals.get('covered_branches', 0)} of {totals.get('num_branches', 0)} branches). Coverage is a measure of what the tests execute, not of correctness."
            )
        )
    if independent:
        story.append(p("Independent review inside the session", H2))
        story.append(p(esc(independent.get("summary", ""))))
        rows = [
            [
                "source",
                "findings",
                "accepted and fixed",
                "recorded as open item / change request",
                "not accepted (with reason)",
            ]
        ]
        for row in independent.get("rounds", []):
            rows.append(
                [
                    esc(row["source"]),
                    str(row["findings"]),
                    str(row["fixed"]),
                    str(row["recorded"]),
                    str(row["declined"]),
                ]
            )
        story.append(table(rows, [40, 12, 18, 24, 18]))
        story.append(
            p(
                "The individual findings and their disposition are in <font face='Courier'>reviews/session_00/evidence/independent_review.json</font>. This internal review does not replace the supervisor's independent review.",
                SMALL,
            )
        )
    story.append(PageBreak())

    # ------------------------------------------------------------------ 7 performance
    story.append(p("7. Measured performance", H1))
    if bench:
        machine = bench["machine"]
        story.append(
            p(
                f"Micro-benchmarks of the foundation primitives ({bench['iterations_per_operation']} iterations per operation, single thread, {esc(machine['implementation'])} {esc(machine['python'])}, {esc(machine['platform'])}, {machine['cpu_count']} CPUs). <b>{esc(bench['label'])}.</b> These numbers say nothing about the owner's hardware, about complete tick processing or about trading results; the specification's latency targets are proposed research parameters and are quoted for reference only: "
                + ", ".join(
                    f"{esc(k)} = {v['value_ns'] / 1e6:g} ms ({esc(v['requirement'])})"
                    for k, v in bench["proposed_targets_for_reference"].items()
                )
                + "."
            )
        )
        rows = [["operation", "p50 (us)", "p95 (us)", "p99 (us)", "max (us)"]]
        for row in bench["results"]:
            rows.append(
                [
                    esc(row["name"]),
                    f"{row['p50_ns'] / 1000:.1f}",
                    f"{row['p95_ns'] / 1000:.1f}",
                    f"{row['p99_ns'] / 1000:.1f}",
                    f"{row['max_ns'] / 1000:.1f}",
                ]
            )
        story.append(table(rows, [52, 15, 15, 15, 15]))
        story.append(
            p(
                "Percentiles are nearest-rank values over all samples. The maximum is dominated by scheduler and garbage-collection pauses of a shared container and is reported as measured (no guarantee of zero latency, G03-13).",
                SMALL,
            )
        )
    else:
        story.append(p("<b>No benchmark evidence file was found.</b>"))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 8 decisions
    story.append(p("8. Decisions", H1))
    story.append(
        p(
            "Full text: <font face='Courier'>docs/DECISION_LOG.md</font>. PROPOSED decisions were made by the coordinator and are not binding until the owner approves them."
        )
    )
    rows = [["id", "decision", "status", "requirements", "summary"]]
    for entry in decision_entries:
        rows.append(
            [
                esc(entry["id"]),
                esc(entry["title"]),
                status_text(entry.get("status", "?")),
                esc(entry.get("requirements", "")),
                esc(first_paragraph(entry["body"], 420)),
            ]
        )
    story.append(table(rows, [8, 26, 12, 20, 46]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 9 unresolved
    story.append(p("9. Unresolved decisions, blockers and change requests", H1))
    story.append(p("Open items for the owner", H2))
    rows = [["id", "question", "status", "requirements", "detail"]]
    for entry in open_items:
        rows.append(
            [
                esc(entry["id"]),
                esc(entry["title"]),
                status_text(entry.get("status", "?")),
                esc(entry.get("requirements", "")),
                esc(first_paragraph(entry["body"], 520)),
            ]
        )
    story.append(table(rows, [7, 25, 9, 19, 52]))
    story.append(p("Blockers and change requests", H2))
    rows = [["id", "title", "status", "requirements", "summary"]]
    for entry in blockers + change_requests:
        rows.append(
            [
                esc(entry["id"]),
                esc(entry["title"]),
                status_text(entry.get("status", "?")),
                esc(entry.get("requirements", "")),
                esc(first_paragraph(entry["body"], 520)),
            ]
        )
    story.append(table(rows, [8, 26, 10, 22, 46]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 10 worktrees
    story.append(p("10. Worktree instructions", H1))
    story.append(
        p(
            "Full text: <font face='Courier'>docs/WORKTREES.md</font>. Only the development branch <font face='Courier'>claude/gold-intelligence-engine-l9uw10</font> is pushed; <font face='Courier'>main</font> is never touched; no pull request is opened unless the owner asks. From wave 2 on each session works in its own local branch and worktree (<font face='Courier'>gie/sNN-name</font>), edits only the paths it owns, ends with a review package and stops. The coordinator merges a session branch (<font face='Courier'>--no-ff</font>) only after the owner approved that review package, and re-runs the full CI on the result."
        )
    )
    for line in (
        "git worktree add -b gie/s01-market-data ../gie-s01 claude/gold-intelligence-engine-l9uw10",
        'cd ../gie-s01 && python -m pip install -e ".[dev]" && python -m tools.ci',
        "git merge --no-ff gie/s01-market-data   # coordinator, after owner approval",
        "git worktree remove ../gie-s01",
    ):
        story.append(p(esc(line), MONO))
    story.append(
        p(
            "The production tree <font face='Courier'>D:\\ftmo_eurusd</font> is not a worktree of this repository and is never written to by anything in this project."
        )
    )

    # ------------------------------------------------------------------ 11 requirement table
    story.append(p("11. Requirement-by-requirement status for Session 00", H1))
    story.append(
        p(
            'Generated from <font face=\'Courier\'>specifications/requirements_matrix.yaml</font>. "verified" means implemented and backed by the listed evidence; "partial" means the foundation part is delivered and the remainder belongs to a later session; "blocked" names its blocker. The complete matrix of all sessions is <font face=\'Courier\'>specifications/REQUIREMENTS_MATRIX.md</font>.'
        )
    )
    story.append(table(requirement_rows(requirements), [11, 27, 9, 38, 27]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ 12 evidence index
    story.append(p("12. Evidence index and reproduction", H1))
    story.append(p("To reproduce every result of this package from a clean checkout of the reviewed commit:"))
    for line in (
        f"git checkout {reviewed_commit}",
        'python -m pip install -e ".[dev]"',
        "python -m tools.ci --evidence-dir out            # all gates; writes ci_results.json, test_results.json, junit.xml, coverage.json",
        "python -m tools.benchmark_foundation --out out/benchmark.json",
        "python -m tools.build_review --evidence-dir out --out review.pdf --commit <commit>",
    ):
        story.append(p(esc(line), MONO))
    if manifest:
        story.append(p("Files of the reviewed commit (SHA-256)", H2))
        rows = [["path", "bytes", "sha256"]]
        for item in manifest["files"]:
            rows.append(
                [Paragraph(esc(item["path"]), MONO), str(item["size"]), Paragraph(esc(item["sha256"]), MONO)]
            )
        story.append(table(rows, [44, 8, 60]))
    story.append(PageBreak())

    # ------------------------------------------------------------------ appendix
    story.append(p("Appendix A. Contract field reference", H1))
    story.append(
        p(
            "Generated from the contract declarations in <font face='Courier'>gie/contracts/</font> (the same source as the JSON Schemas and <font face='Courier'>specifications/contracts/FIELDS.md</font>)."
        )
    )
    contract_section(story)

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#5c6470"))
        canvas.drawString(
            18 * mm,
            10 * mm,
            f"GIE | Session 00 foundation review | commit {reviewed_commit[:12]} | Paper / research only - no live trading",
        )
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{document.page}")
        canvas.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=17 * mm,
        title="GIE Session 00 - Foundation review",
        author="GIE project coordinator (Claude)",
        subject="Paper / research only",
        invariant=1,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Session 00 foundation review PDF.")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--commit", required=True, help="commit hash the evidence was produced on")
    args = parser.parse_args(argv)
    build(args.evidence_dir, args.out, args.commit)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
