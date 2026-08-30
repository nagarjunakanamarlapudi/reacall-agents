"""Documentation contract for the RecallOps submission set.

Run with: python3 -m unittest discover -s tests/docs -p 'test_*.py'
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"

REQUIRED_DOCUMENTS = (
    "README.md",
    "PROPOSAL.md",
    "docs/ARCHITECTURE.md",
    "docs/DATA_SOURCES.md",
    "docs/MCP_AND_TOOLS.md",
    "docs/MIDDLEWARE_AND_HITL.md",
    "docs/OPERATIONS.md",
    "docs/EVALUATION.md",
    "docs/DEMO_WALKTHROUGH.md",
    "docs/SUBMISSION_CHECKLIST.md",
    "docs/BACKLOG.md",
    "docs/SUBMISSION_DOCUMENT.md",
    "docs/AI_CODING_LOG.md",
    "docs/CURRICULUM_COVERAGE.md",
    "docs/images/README.md",
    "scripts/render_diagrams.sh",
)

DIAGRAM_LABELS = {
    "01_data_provenance": (
        "official openFDA H-1230-2026",
        "SYNTHETIC — ACADEMIC DEMO",
        "Northstar Grocers",
    ),
    "02_system_architecture": (
        "No A2A",
        "No direct agent writes",
        "LangGraph",
    ),
    "03_orchestration": (
        "Deep Agent supervisor",
        "Verification / Critic",
        "StateGraph",
    ),
    "04_mcp_tool_safety": (
        "Recall Registry MCP",
        "Traceability MCP",
        "Recall Operations MCP",
        "Approval required",
    ),
    "05_middleware_lifecycle": (
        "Case context",
        "Approval guard",
        "Trace recorder",
    ),
    "06_hitl_closure": (
        "interrupt()",
        "Command(resume=...)",
        "Closure blocked",
    ),
    "07_demo_story": (
        "H-1230-2026",
        "Human review",
        "Open case",
    ),
}


class DocumentationContractTests(unittest.TestCase):
    def test_promised_artifacts_exist(self) -> None:
        missing = [path for path in REQUIRED_DOCUMENTS if not (ROOT / path).is_file()]
        missing += [
            f"docs/images/{name}.{extension}"
            for name in DIAGRAM_LABELS
            for extension in ("mmd", "svg")
            if not (IMAGES / f"{name}.{extension}").is_file()
        ]
        self.assertEqual(missing, [], f"missing promised documentation artifacts: {missing}")

    def test_diagrams_preserve_scope_critical_labels(self) -> None:
        for name, labels in DIAGRAM_LABELS.items():
            text = (IMAGES / f"{name}.mmd").read_text(encoding="utf-8")
            for label in labels:
                self.assertIn(label, text, f"{name}.mmd must include {label!r}")

    def test_docs_do_not_contain_placeholder_language(self) -> None:
        files = [ROOT / name for name in REQUIRED_DOCUMENTS if name.endswith(".md")]
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            if re.search(r"\b(?:TODO|TBD)\b", text, flags=re.IGNORECASE):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"placeholder language found in: {offenders}")

    def test_demo_uses_pinned_case_and_explicit_review_inputs(self) -> None:
        demo = (DOCS / "DEMO_WALKTHROUGH.md").read_text(encoding="utf-8")
        for required in (
            "H-1230-2026",
            "recallops demo",
            "approve",
            "Food-safety manager",
            "SYNTHETIC — ACADEMIC DEMO",
            "final integration confirmation",
        ):
            self.assertIn(required, demo, f"demo walkthrough must include {required!r}")

    def test_submission_handout_and_ai_log_are_honest_and_presenter_ready(self) -> None:
        handout = (DOCS / "SUBMISSION_DOCUMENT.md").read_text(encoding="utf-8")
        for required in (
            "Project overview",
            "Datasets used",
            "Vibe-coding prompts and briefs",
            "Iterations tried",
            "Learnings and observations",
            "00:00",
            "04:30",
        ):
            self.assertIn(required, handout)

        coding_log = (DOCS / "AI_CODING_LOG.md").read_text(encoding="utf-8")
        self.assertIn("Codex", coding_log)
        self.assertIn("Claude Code", coding_log)
        self.assertIn("Grok", coding_log)

    def test_curriculum_coverage_maps_every_week_three_topic(self) -> None:
        coverage = (DOCS / "CURRICULUM_COVERAGE.md").read_text(encoding="utf-8")
        for topic in (
            "Agent loop",
            "Planning",
            "State and checkpoints",
            "Memory",
            "HITL",
            "Supervisor",
            "MCP",
            "Middleware",
            "Failures and recovery",
            "Cost, latency, and reliability",
            "Observability and evaluation",
            "A2A",
        ):
            self.assertIn(topic, coverage)


if __name__ == "__main__":
    unittest.main()
