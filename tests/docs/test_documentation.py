"""Documentation contract for the RecallOps submission set.

Run with: python3 -m unittest discover -s tests/docs -p 'test_*.py'
"""

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path
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
    "docs/BUSINESS_DOMAIN.md",
    "docs/ARCHITECTURE.md",
    "docs/DATA_SOURCES.md",
    "docs/MCP_AND_TOOLS.md",
    "docs/MIDDLEWARE_AND_HITL.md",
    "docs/OPERATIONS.md",
    "docs/EVALUATION.md",
    "docs/VERIFICATION.md",
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
        "Agentic RAG",
        "BM25 + LSA",
        "Human action review",
        "Execution confirmation",
    ),
    "03_orchestration": (
        "Deep Agent supervisor",
        "Deterministic default planner",
        "Verification / Critic",
        "StateGraph",
        "create_case v0→v1",
        "apply_inventory_hold v1→v2",
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
        "Execution confirmation",
        "one write / version",
    ),
    "07_demo_story": (
        "H-1230-2026",
        "Human Review",
        "Open — closure blocked",
        "create_case v0→v1",
        "apply_inventory_hold v1→v2",
    ),
    "08_business_recall_lifecycle": (
        "SYNTHETIC — ACADEMIC DEMO",
        "FDA / regulator",
        "Recall coordinator",
        "Supplier",
        "DC / store",
        "Food-safety manager",
        "Consumers",
        "First human action review",
        "Separate execution confirmation",
        "Exact proposed action + case version",
        "Operation receipt",
        "disposition evidence",
        "Unit reconciliation",
        "Every facility acknowledgement",
        "Explicit closure request",
        "Deterministic closure gate evaluation",
        "Second human closure review",
        "Keep open / escalate",
        "Internal closure blocked",
        "FDA termination is separate",
    ),
    "09_domain_evidence_model": (
        "SYNTHETIC — ACADEMIC DEMO",
        "Official public recall record",
        "RecallOps proposed predicate — human verified",
        "Product match",
        "Lot match",
        "Lineage event",
        "Inventory position",
        "Facility",
        "Proposed action",
        "Approval",
        "Execution confirmation",
        "Operation receipt",
        "Internal closure decision",
    ),
}

POLISHED_VISUALS = (
    "recallops-data-boundary.png",
    "recallops-system-architecture.png",
    "recallops-five-minute-demo.png",
)


class DocumentationContractTests(unittest.TestCase):
    def assert_demo_artifact_contract(
        self, path: Path, contract: dict, text: str | None = None
    ) -> None:
        artifact = contract["artifact_contract"][path.name]
        content = text if text is not None else path.read_text(encoding="utf-8")
        last_position = -1
        for stamp in contract["timeline"]:
            self.assertEqual(
                content.count(stamp), 1, f"{path.name} must contain {stamp} exactly once"
            )
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
        missing += [
            f"docs/images/{name}" for name in POLISHED_VISUALS if not (IMAGES / name).is_file()
        ]
        self.assertEqual(missing, [], f"missing promised documentation artifacts: {missing}")

    def test_operations_cwd_independent_cli_examples_select_the_project(self) -> None:
        operations = (DOCS / "OPERATIONS.md").read_text(encoding="utf-8")
        for command in (
            "eval",
            "eval-retrieval",
            "eval-orchestration",
            "eval-scorecard",
        ):
            self.assertIn(
                f"uv run --project /absolute/repository/path recallops {command}",
                operations,
            )

    def test_polished_visuals_are_primary_in_submission_entrypoints(self) -> None:
        expected = {
            ROOT / "README.md": (
                "docs/images/recallops-data-boundary.png",
                "docs/images/recallops-system-architecture.png",
                "docs/images/recallops-five-minute-demo.png",
            ),
            DOCS / "BUSINESS_DOMAIN.md": ("images/recallops-data-boundary.png",),
            DOCS / "ARCHITECTURE.md": ("images/recallops-system-architecture.png",),
            DOCS / "DEMO_WALKTHROUGH.md": ("images/recallops-five-minute-demo.png",),
            DOCS / "SUBMISSION_DOCUMENT.md": (
                "images/recallops-data-boundary.png",
                "images/recallops-system-architecture.png",
                "images/recallops-five-minute-demo.png",
            ),
            DOCS / "SUBMISSION_CHECKLIST.md": (
                "images/recallops-data-boundary.png",
                "images/recallops-system-architecture.png",
                "images/recallops-five-minute-demo.png",
            ),
        }
        for path, targets in expected.items():
            content = path.read_text(encoding="utf-8")
            for target in targets:
                self.assertIn(target, content, f"{path.name} must feature {target}")

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

    def test_business_orientation_is_linked_from_every_submission_entrypoint(self) -> None:
        expected_targets = {
            ROOT / "README.md": (
                "docs/BUSINESS_DOMAIN.md",
                "docs/images/08_business_recall_lifecycle.svg",
                "docs/images/09_domain_evidence_model.svg",
            ),
            ROOT / "PROPOSAL.md": (
                "docs/BUSINESS_DOMAIN.md",
                "docs/images/08_business_recall_lifecycle.svg",
                "docs/images/09_domain_evidence_model.svg",
            ),
            DOCS / "ARCHITECTURE.md": (
                "BUSINESS_DOMAIN.md",
                "images/08_business_recall_lifecycle.svg",
                "images/09_domain_evidence_model.svg",
            ),
            DOCS / "DATA_SOURCES.md": (
                "BUSINESS_DOMAIN.md",
                "images/08_business_recall_lifecycle.svg",
                "images/09_domain_evidence_model.svg",
            ),
            DOCS / "SUBMISSION_DOCUMENT.md": (
                "BUSINESS_DOMAIN.md",
                "images/08_business_recall_lifecycle.svg",
                "images/09_domain_evidence_model.svg",
            ),
            DOCS / "DEMO_WALKTHROUGH.md": (
                "BUSINESS_DOMAIN.md",
                "images/08_business_recall_lifecycle.svg",
                "images/09_domain_evidence_model.svg",
            ),
        }
        for source, targets in expected_targets.items():
            content = source.read_text(encoding="utf-8")
            for target in targets:
                self.assertIn(f"]({target})", content, f"{source.name} must link {target}")
                self.assertTrue((source.parent / target).resolve().is_file())

    def test_business_diagrams_do_not_mix_in_implementation_architecture(self) -> None:
        for name in ("08_business_recall_lifecycle", "09_domain_evidence_model"):
            diagram = (IMAGES / f"{name}.mmd").read_text(encoding="utf-8")
            self.assertNotRegex(diagram, r"(?i)langgraph|deep agent|\bmcp\b")

    def test_business_lifecycle_has_distinct_action_and_closure_authorizations(self) -> None:
        diagram = (IMAGES / "08_business_recall_lifecycle.mmd").read_text(encoding="utf-8")
        ordered_nodes = (
            'ACTION_REVIEW["Food-safety manager<br/>First human action review',
            'ACTION_GATE{"Approve the exact proposed action<br/>at this case version?',
            'EXEC_CONFIRM["Food-safety manager<br/>Separate execution confirmation',
            'RECORD["DC / store<br/>Record authorized simulated action"]',
            'EVIDENCE["Operation receipt • disposition evidence',
            'CLOSURE_REQUEST["Recall coordinator<br/>Explicit closure request',
            'GATE_EVAL{"Deterministic closure gate evaluation',
            'CLOSURE_REVIEW["Food-safety manager<br/>Second human closure review"]',
            'CLOSURE_GATE{"Authorize internal retailer closure?"}',
        )
        positions = [diagram.index(node) for node in ordered_nodes]
        self.assertEqual(positions, sorted(positions))
        for edge in (
            "ACTION_REVIEW --> ACTION_GATE",
            "ACTION_GATE -->|approve exact action| EXEC_CONFIRM",
            "EXEC_CONFIRM -->|confirm execution| RECORD",
            "RECORD --> EVIDENCE",
            "EVIDENCE --> CLOSURE_REQUEST",
            "CLOSURE_REQUEST --> GATE_EVAL",
            "GATE_EVAL -->|all deterministic gates pass| CLOSURE_REVIEW",
            "CLOSURE_REVIEW --> CLOSURE_GATE",
            "CLOSURE_GATE -->|close| CLOSED",
            "CLOSURE_GATE -->|keep open / escalate| OPEN",
        ):
            self.assertIn(edge, diagram)
        self.assertNotRegex(diagram, r"(?:NOTICE|COORD|TRACE|ACTION_REVIEW)\s*-->\s*RECORD")

    def test_business_diagrams_label_simulation_and_predicate_class_explicitly(self) -> None:
        for name in ("08_business_recall_lifecycle", "09_domain_evidence_model"):
            source = (IMAGES / f"{name}.mmd").read_text(encoding="utf-8")
            svg = (IMAGES / f"{name}.svg").read_text(encoding="utf-8")
            self.assertIn("SYNTHETIC — ACADEMIC DEMO", source)
            self.assertIn("SYNTHETIC — ACADEMIC DEMO", svg)
            boundary_line = next(
                line.strip()
                for line in source.splitlines()
                if line.strip().startswith("DEMO_NOTE[")
            )
            self.assertTrue(boundary_line.endswith(":::synthetic"))

        evidence_model = (IMAGES / "09_domain_evidence_model.mmd").read_text(encoding="utf-8")
        predicate_line = next(
            line.strip() for line in evidence_model.splitlines() if line.strip().startswith("PRED[")
        )
        self.assertIn("RecallOps proposed predicate — human verified", predicate_line)
        self.assertTrue(predicate_line.endswith(":::review"))
        self.assertNotIn(":::official", predicate_line)
        self.assertIn("PUBLIC -->|scope evidence for derivation| DEMO_NOTE", evidence_model)
        self.assertIn("DEMO_NOTE --> DRAFT", evidence_model)
        self.assertIn("DRAFT -->|human review and verification| PRED", evidence_model)

    def test_business_lifecycle_is_readable_in_a_markdown_column(self) -> None:
        root = ElementTree.parse(IMAGES / "08_business_recall_lifecycle.svg").getroot()
        _, _, width, height = (float(value) for value in root.attrib["viewBox"].split())
        self.assertLessEqual(width, 850, "lifecycle SVG is too wide for a 700px Markdown column")
        self.assertLessEqual(
            height / width, 4, "lifecycle SVG is too tall to scan as one lifecycle"
        )

    def test_business_lifecycle_cluster_titles_clear_their_first_nodes(self) -> None:
        root = ElementTree.parse(IMAGES / "08_business_recall_lifecycle.svg").getroot()

        def translated_y(element: ElementTree.Element) -> float:
            match = re.fullmatch(
                r"translate\([^,]+,\s*([^)]+)\)",
                element.attrib["transform"],
            )
            self.assertIsNotNone(match, f"unexpected transform: {element.attrib['transform']}")
            return float(match.group(1))

        for cluster_id, first_node_name in (
            ("my-svg-STAGE1", "TRACE"),
            ("my-svg-STAGE2", "CLOSURE_REQUEST"),
        ):
            cluster = next(element for element in root.iter() if element.get("id") == cluster_id)
            label = next(element for element in cluster if element.get("class") == "cluster-label")
            label_box = next(element for element in label if element.tag.endswith("foreignObject"))
            label_bottom = translated_y(label) + float(label_box.attrib["height"])

            first_node = next(
                element
                for element in root.iter()
                if element.get("id", "").startswith(f"my-svg-flowchart-{first_node_name}-")
            )
            node_box = next(
                element
                for element in first_node.iter()
                if "label-container" in element.get("class", "").split()
            )
            node_top = translated_y(first_node) + float(node_box.attrib["y"])

            self.assertGreaterEqual(
                node_top - label_bottom,
                10,
                f"{cluster_id} title needs visible clearance above {first_node_name}",
            )

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
            "execution confirmation",
            "create_case",
            "apply_inventory_hold",
        ):
            self.assertIn(required, demo, f"demo walkthrough must include {required!r}")

    def test_demo_documents_are_derived_from_one_machine_readable_contract(self) -> None:
        contract = json.loads((DOCS / "demo_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(
            contract["runtime_integration_status"],
            "implemented durable runtime and five-view UI",
        )
        document_paths = (
            DOCS / "DEMO_WALKTHROUGH.md",
            DOCS / "SUBMISSION_DOCUMENT.md",
            IMAGES / "07_demo_story.mmd",
        )
        for path in document_paths:
            self.assert_demo_artifact_contract(path, contract)

    def test_demo_contract_matches_durable_runtime_and_data_artifacts(self) -> None:
        contract = json.loads((DOCS / "demo_contract.json").read_text(encoding="utf-8"))
        synthetic = json.loads(
            (ROOT / "data/synthetic/northstar_demo/manifest.json").read_text(encoding="utf-8")
        )
        knowledge = json.loads((ROOT / "data/knowledge/manifest.json").read_text(encoding="utf-8"))
        public = json.loads((ROOT / "data/public/H-1230-2026.json").read_text(encoding="utf-8"))

        self.assertLessEqual(contract["duration_seconds"], 285)
        self.assertEqual(
            contract["action_cycles"],
            [
                {
                    "action": "create_case",
                    "from_version": 0,
                    "to_version": 1,
                },
                {
                    "action": "apply_inventory_hold",
                    "from_version": 1,
                    "to_version": 2,
                },
            ],
        )
        self.assertEqual(
            contract["failure_recovery"],
            {
                "scenario": "lost write response → same-key replay",
                "arm_button": "Run failure fixture",
                "initial_button": "Simulate approved actions",
                "recovery_button": "Recover recorded outcome (same key)",
                "expected_logical_receipts": 1,
                "expected_version_increments": 1,
            },
        )
        self.assertEqual(contract["data"]["synthetic_record_counts"], synthetic["record_counts"])
        self.assertEqual(contract["data"]["knowledge_document_count"], knowledge["document_count"])
        self.assertEqual(contract["data"]["public_snapshot_records"], len(public["results"]))
        self.assertFalse(contract["dependencies"]["general_web_search"])
        self.assertFalse(contract["dependencies"]["you_com"])
        self.assertEqual(contract["dependencies"]["live_public_host_allowlist"], ["api.fda.gov"])

    def test_data_source_register_contains_current_trust_anchor_digests(self) -> None:
        source_register = (DOCS / "DATA_SOURCES.md").read_text(encoding="utf-8")
        for relative_path in (
            "data/public/H-1230-2026.json",
            "data/public/H-1230-2026.metadata.json",
            "data/synthetic/northstar_demo/dataset.json",
            "data/synthetic/northstar_demo/manifest.json",
        ):
            digest = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
            self.assertIn(digest, source_register, f"missing current digest for {relative_path}")
        knowledge = json.loads((ROOT / "data/knowledge/manifest.json").read_text(encoding="utf-8"))
        self.assertIn(knowledge["corpus_sha256"], source_register)

    def test_demo_artifact_contract_rejects_one_missing_timestamp(self) -> None:
        contract = json.loads((DOCS / "demo_contract.json").read_text(encoding="utf-8"))
        path = DOCS / "DEMO_WALKTHROUGH.md"
        omitted_timestamp = "03:40"
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
        self.assertEqual(
            lock["packages"]["node_modules/@mermaid-js/mermaid-cli"]["version"], "11.12.0"
        )

    def test_submission_handout_and_ai_log_are_honest_and_presenter_ready(self) -> None:
        handout = (DOCS / "SUBMISSION_DOCUMENT.md").read_text(encoding="utf-8")
        for required in (
            "Project overview",
            "Datasets used",
            "Vibe-coding prompts and briefs",
            "Iterations tried",
            "Learnings and observations",
            "00:00",
            "04:35",
        ):
            self.assertIn(required, handout)

        coding_log = (DOCS / "AI_CODING_LOG.md").read_text(encoding="utf-8")
        self.assertIn("Codex", coding_log)
        self.assertIn("Claude Code", coding_log)
        self.assertIn("Grok", coding_log)
        self.assertIn("no invocation evidence", coding_log)
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
