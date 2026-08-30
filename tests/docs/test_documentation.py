"""Documentation contract for the RecallOps submission set.

Run with: python3 -m unittest discover -s tests/docs -p 'test_*.py'
"""

from pathlib import Path
import json
import re
import subprocess
import unittest
from typing import Optional
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"

REQUIRED_DOCUMENTS = (
    ".node-version",
    "package.json",
    "package-lock.json",
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
    "docs/demo_contract.json",
    "docs/images/README.md",
    "scripts/render_diagrams.sh",
    "scripts/mermaid-config.json",
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
        "Deterministic default planner",
        "Verification / Critic",
        "StateGraph",
    ),
    "04_mcp_tool_safety": (
        "Recall Registry MCP",
        "Traceability MCP",
        "Recall Operations MCP",
        "Approval required",
        "BLOCKED",
        "Same idempotency key replay",
        "Conflicting duplicate",
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
        "Human Review",
        "Open — closure blocked",
    ),
}


class DocumentationContractTests(unittest.TestCase):
    def assert_demo_artifact_contract(self, path: Path, contract: dict, text: Optional[str] = None) -> None:
        artifact = contract["artifact_contract"][path.name]
        content = text if text is not None else path.read_text(encoding="utf-8")
        last_position = -1
        for stamp in contract["timeline"]:
            self.assertEqual(content.count(stamp), 1, f"{path.name} must contain {stamp} exactly once")
            position = content.index(stamp)
            self.assertGreater(position, last_position, f"{path.name} must preserve timeline order")
            last_position = position
        for value in artifact["commands"] + artifact["required_terms"]:
            self.assertIn(value, content, f"{path.name} is missing contract value {value!r}")

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
            source = (IMAGES / f"{name}.mmd").read_text(encoding="utf-8")
            svg = (IMAGES / f"{name}.svg").read_text(encoding="utf-8")
            root = ElementTree.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"), f"{name}.svg is not an SVG root")
            for label in labels:
                self.assertIn(label, source, f"{name}.mmd must include {label!r}")
                self.assertIn(label, svg, f"{name}.svg must preserve {label!r}")

    def test_mcp_diagram_has_only_one_approved_operations_path(self) -> None:
        diagram = (IMAGES / "04_mcp_tool_safety.mmd").read_text(encoding="utf-8")
        self.assertIn('AG -. "unauthorized direct-write attempt" .-> BLOCKED', diagram)
        self.assertNotRegex(diagram, r"AG\s*[-.] .*OM")
        self.assertIn('GR["Approved graph execution node"] --> AP', diagram)
        self.assertIn('AP --> OM["Recall Operations MCP', diagram)
        self.assertIn("Same idempotency key replay", diagram)
        self.assertIn("Conflicting duplicate", diagram)

    def test_orchestration_shows_deterministic_and_live_branches(self) -> None:
        diagram = (IMAGES / "03_orchestration.mmd").read_text(encoding="utf-8")
        self.assertIn("Deterministic default planner", diagram)
        self.assertIn("Optional live Deep Agent supervisor", diagram)
        self.assertIn("DS --> RI", diagram)
        self.assertIn("DA --> RI", diagram)
        self.assertIn("Verification / Critic<br/>outside supervisor context", diagram)

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

    def test_demo_documents_are_derived_from_one_machine_readable_contract(self) -> None:
        contract = json.loads((DOCS / "demo_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(
            contract["runtime_integration_status"],
            "approved contract pending Task 11 runtime integration",
        )
        document_paths = (
            DOCS / "DEMO_WALKTHROUGH.md",
            DOCS / "SUBMISSION_DOCUMENT.md",
            IMAGES / "07_demo_story.mmd",
        )
        for path in document_paths:
            self.assert_demo_artifact_contract(path, contract)

    def test_demo_artifact_contract_rejects_one_missing_timestamp(self) -> None:
        contract = json.loads((DOCS / "demo_contract.json").read_text(encoding="utf-8"))
        path = DOCS / "DEMO_WALKTHROUGH.md"
        omitted_timestamp = "03:10"
        fixture = path.read_text(encoding="utf-8").replace(omitted_timestamp, "", 1)
        with self.assertRaises(AssertionError):
            self.assert_demo_artifact_contract(path, contract, fixture)

    def test_renderer_is_pinned_and_double_render_is_stable(self) -> None:
        renderer = (ROOT / "scripts/render_diagrams.sh").read_text(encoding="utf-8")
        self.assertIn('NODE_EXPECTED_VERSION="v24.15.0"', renderer)
        self.assertIn('NPM_EXPECTED_VERSION="11.12.1"', renderer)
        self.assertIn('MERMAID_CLI_VERSION="11.12.0"', renderer)
        self.assertIn("mermaid-config.json", renderer)
        self.assertIn("node_modules/.bin/mmdc", renderer)
        self.assertNotIn("npx", renderer)
        result = subprocess.run(
            ["./scripts/render_diagrams.sh", "--verify"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Stable double-render verified", result.stdout)
        self.assertIn("Committed SVGs match fresh render", result.stdout)

    def test_toolchain_files_are_exactly_locked(self) -> None:
        self.assertEqual((ROOT / ".node-version").read_text(encoding="utf-8").strip(), "24.15.0")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["engines"]["node"], "24.15.0")
        self.assertEqual(package["packageManager"], "npm@11.12.1")
        self.assertEqual(package["devDependencies"]["@mermaid-js/mermaid-cli"], "11.12.0")
        self.assertEqual(lock["packages"]["node_modules/@mermaid-js/mermaid-cli"]["version"], "11.12.0")

    def test_submission_handout_and_ai_log_are_honest_and_presenter_ready(self) -> None:
        handout = (DOCS / "SUBMISSION_DOCUMENT.md").read_text(encoding="utf-8")
        for required in (
            "Project overview",
            "Datasets used",
            "Vibe-coding prompts and briefs",
            "Iterations tried",
            "Learnings and observations",
            "00:00",
            "04:20",
        ):
            self.assertIn(required, handout)

        coding_log = (DOCS / "AI_CODING_LOG.md").read_text(encoding="utf-8")
        self.assertIn("Codex", coding_log)
        self.assertIn("Claude Code", coding_log)
        self.assertIn("Grok", coding_log)
        self.assertIn("author-reported pending Task 11 verification", coding_log)
        for text in (handout, coding_log):
            self.assertIn("Codex (GPT-5 family; exact host alias not surfaced to this task)", text)
            self.assertIn("gpt-5.6-terra", text)
            self.assertIn("gpt-5.6-luna", text)

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
