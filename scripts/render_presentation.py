"""Render presentation diagrams with fixed geometry and local Arial/DejaVu fonts."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1920, 1080
INK = "#17233d"
MUTED = "#536279"
PURPLE = "#7047c6"
BLUE = "#2563a6"
GREEN = "#187457"
ORANGE = "#b35c20"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    raise RuntimeError("Install Arial or DejaVu Sans to render presentation diagrams")


def canvas(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f4f6fb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 12), fill=PURPLE)
    draw.text((64, 42), "RECALLOPS  /  EVIDENCE BEFORE ACTION", font=font(23, True), fill=PURPLE)
    draw.text((64, 87), title, font=font(49, True), fill=INK)
    draw.text((66, 157), subtitle, font=font(25), fill=MUTED)
    return image, draw


def card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    title: str,
    lines: tuple[str, ...],
    color: str,
) -> None:
    draw.rounded_rectangle(
        (x, y, x + w, y + h), radius=18, fill="white", outline="#d5ddeb", width=2
    )
    draw.rounded_rectangle((x, y, x + 8, y + h), radius=4, fill=color)
    draw.text((x + 25, y + 18), label, font=font(20, True), fill=color)
    draw.text((x + 25, y + 52), title, font=font(27, True), fill=INK)
    for index, line in enumerate(lines):
        if draw.textlength(line, font=font(22)) > w - 50:
            raise ValueError(f"Presentation line exceeds card: {line}")
        draw.text((x + 25, y + 99 + index * 31), line, font=font(22), fill=MUTED)


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int]) -> None:
    draw.line((start, end), fill=MUTED, width=4)
    x, y = end
    if start[1] == y:
        draw.polygon(((x, y), (x - 12, y - 8), (x - 12, y + 8)), fill=MUTED)
    else:
        draw.polygon(((x, y), (x - 8, y - 12), (x + 8, y - 12)), fill=MUTED)


def architecture(output: Path) -> None:
    image, draw = canvas(
        "OpenAI reasons. Evidence earns authority.",
        "Official openFDA H-1230-2026 + separate Northstar synthetic data • sealed read-only MCP",
    )
    top = (
        (
            "01  CONTEXT",
            "Bounded agentic RAG",
            (
                "Sparse + dense fusion / rerank",
                "Policy-based critique / rewrite",
                "Citations + hard read budgets",
            ),
            BLUE,
        ),
        (
            "02  OPENAI LLM",
            "Observed planning",
            (
                "write_todos: four fixed roles",
                "Case, scope and source binding",
                "Safe plan projection in the UI",
            ),
            PURPLE,
        ),
        (
            "03  OPENAI LLM",
            "Deep Agents supervisor",
            (
                "One delegation at a time",
                "Validated prerequisite claims",
                "No inner checkpoint persistence",
            ),
            PURPLE,
        ),
    )
    for index, (label, title, lines, color) in enumerate(top):
        card(draw, 64 + index * 610, 220, 572, 219, label, title, lines, color)
        if index < 2:
            arrow(draw, (641 + index * 610, 327), (666 + index * 610, 327))
    draw.text(
        (64, 468),
        "04  FOUR SEQUENTIAL, CONTEXT-BOUND LLM SPECIALISTS",
        font=font(22, True),
        fill=PURPLE,
    )
    specialists = (
        ("1", "Regulatory intake", ("Official scope + citations", "Sealed Registry reads")),
        ("2", "Product / lot matching", ("Classify candidate lots", "Sealed Traceability reads")),
        (
            "3",
            "Trace / reconciliation",
            ("Lineage + inventory + units", "Sealed Traceability reads"),
        ),
        ("4", "Containment draft", ("Validated prior claims", "No MCP tools; no execution")),
    )
    for index, (label, title, lines) in enumerate(specialists):
        card(draw, 64 + index * 456, 513, 423, 181, label, title, lines, PURPLE)
        if index < 3:
            arrow(draw, (492 + index * 456, 605), (512 + index * 456, 605))
    draw.text(
        (64, 721),
        "05  STRUCTURED SYNTHESIS → SAFE TYPED CLAIMS  /  UNVERIFIED UNTIL SOURCE CHECK",
        font=font(22, True),
        fill=BLUE,
    )
    bottom = (
        (
            "06  INDEPENDENT CHECK",
            "Source verifier",
            ("Re-read sources; verify receipts", "Reject unsupported claims"),
            GREEN,
        ),
        (
            "07  HITL CONTROL PLANE",
            "Two human gates",
            ("Action review → execution confirm", "Exact version + digest + grant"),
            ORANGE,
        ),
        (
            "08  APPROVED GRAPH NODE",
            "Simulated Operations",
            ("One write / version → receipt", "Fresh review or closure blocked"),
            BLUE,
        ),
    )
    for index, (label, title, lines, color) in enumerate(bottom):
        card(draw, 64 + index * 610, 764, 572, 187, label, title, lines, color)
        if index < 2:
            arrow(draw, (641 + index * 610, 858), (666 + index * 610, 858))
    draw.text(
        (64, 987),
        "LangGraph owns durable state, approval and closure. Agents have no Operations tools.",
        font=font(25, True),
        fill=INK,
    )
    draw.text(
        (64, 1030),
        "Read-only evaluation is separate: deterministic gates + additional measured live lane. Live metrics unavailable until measured.",
        font=font(21),
        fill=MUTED,
    )
    image.save(output / "recallops-system-architecture.png")


def demo(output: Path) -> None:
    image, draw = canvas(
        "A recall investigation you can inspect.",
        "H-1230-2026 • SYNTHETIC — ACADEMIC DEMO • timed presentation 4:55; live latency varies",
    )
    items = (
        (
            "00:00  READY",
            "Open the case",
            ("Reasoning mode: OpenAI · model", "Check ready; separate data origins"),
            BLUE,
        ),
        (
            "00:35  INVESTIGATE",
            "Show actual model work",
            ("RAG → write_todos → supervisor", "Four sequential LLM specialists"),
            PURPLE,
        ),
        (
            "01:20  VERIFY",
            "Evidence before review",
            ("Safe claims → source verifier", "Reconciliation gaps stay visible"),
            GREEN,
        ),
        (
            "02:00  REVIEW",
            "Approve exact action",
            ("create_case • version 0 + digest", "First HITL gate writes zero"),
            ORANGE,
        ),
        (
            "02:35  CONFIRM",
            "Simulate approved action",
            ("Second gate → Operations MCP", "create_case receipt • v0 → v1"),
            ORANGE,
        ),
        (
            "03:05–03:40  REPEAT",
            "Fresh review + confirm",
            ("New action, approval and key", "Inventory hold receipt • v1 → v2"),
            ORANGE,
        ),
        (
            "04:00  MEASURE",
            "Show both eval lanes",
            ("Safety / retrieval / orchestration", "Live metrics only when measured"),
            BLUE,
        ),
        (
            "04:45  CLOSE?",
            "Open — closure blocked",
            ("Ambiguity + quantity gap remain", "Internal close ≠ FDA termination"),
            GREEN,
        ),
    )
    for index, (label, title, lines, color) in enumerate(items):
        col, row = index % 4, index // 4
        card(draw, 64 + col * 456, 243 + row * 298, 423, 244, label, title, lines, color)
    draw.rounded_rectangle((64, 862, 1856, 1029), radius=18, fill="#e8e2f4")
    draw.text(
        (91, 885),
        "Presenter proof: plan • read-only MCP trail • safe claims • verifier • two HITL gates • receipts",
        font=font(26, True),
        fill=INK,
    )
    draw.text(
        (91, 935),
        "Ready validates configuration. A completed plan, read trail and accepted verifier demonstrate a live investigation.",
        font=font(24),
        fill=MUTED,
    )
    draw.text(
        (91, 976),
        "make ui-openai  /  make eval-model     No raw prompts, credentials or chain-of-thought in recordings.",
        font=font(24),
        fill=MUTED,
    )
    image.save(output / "recallops-five-minute-demo.png")


def main() -> None:
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    architecture(output)
    demo(output)


if __name__ == "__main__":
    main()
