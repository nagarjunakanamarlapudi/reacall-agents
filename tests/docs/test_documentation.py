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
        "Read-only evaluation plane",
        "digest-bound scorecard",
        "No Operations MCP credentials",
    ),
    "03_orchestration": (
        "Deep Agent supervisor",
        "Deterministic default planner",
        "Verification / Critic",
        "StateGraph",
        "create_case v0→v1",
        "apply_inventory_hold v1→v2",
        "Typed trajectory capture",
        "Orchestration comparison",
        "excluded from action authority",
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
        "Safety evaluation",
        "Retrieval ablation",
        "Orchestration comparison",
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
    "10_evaluation_architecture": (
        "AUTHORED + LABELLED + DIGEST-BOUND",
        "OFFLINE EVALUATION DATA",
        "not official source evidence",
        "21 safety scenarios",
        "R01–R21",
        "96 retrieval cases",
        "24 orchestration cases",
        "Deterministic safety suite",
        "Six retrieval ablations",
        "Two orchestration profiles",
        "digest-bound scorecard",
        "Optional live Deep Agents run",
        "Optional human / model judge",
        "excluded from deterministic authority",
        "No Operations MCP credentials",
        "No SQLite writes",
    ),
}

POLISHED_VISUALS = (
    "recallops-data-boundary.png",
    "recallops-system-architecture.png",
    "recallops-five-minute-demo.png",
)

SYSTEM_ARCHITECTURE_EVALUATION_ROOTS = ("EVALUATION", "EV", "SUITES", "SCORE", "EVALSAFE")
ORCHESTRATION_EVALUATION_ROOTS = ("TC", "OE", "ER")
WRITE_AUTHORITY_NODES = ("GN", "W", "OM", "OP", "OPERATIONS", "SQLITE_WRITE")


_EDGE_OPERATOR = re.compile(
    r"(?<![-.=~<>])(?:"
    r'--\s+(?:"[^"\n]*"|\'[^\'\n]*\'|.*?)\s+--+[-ox>]'
    r'|[<ox]?==(?!=|>)(?:"[^"\n]*"|\'[^\'\n]*\'|[^=\n]+?)==+[=ox>]'
    r'|-\.\s+(?:"[^"\n]*"|\'[^\'\n]*\'|.*?)\s+\.+-[ox>]?'
    r"|[<ox]?--+[-ox>]"
    r"|[<ox]?==+[=ox>]"
    r"|[<ox]?-?\.+-[ox>]?"
    r"|~~+"
    r")(?:\s*\|[^|\n]*\|)?\s*(?![-.=~<>])"
)
_CONNECTOR_CANDIDATE = re.compile(r"[<ox]?[-.=~]{2,}[<>=ox-]*")
_QUOTED_TEXT = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_SUBGRAPH_DECLARATION = re.compile(r"^\s*subgraph\s+([A-Za-z_][A-Za-z0-9_]*)")
_SHAPED_NODE_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*(?=@\{|\[|\(|\{|>)"
)


def _mermaid_node_id(segment: str) -> str | None:
    """Return the node adjacent to an edge in the Mermaid subset used here."""
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", segment)
    return match.group(1) if match else None


def _mermaid_directions(operator: str) -> tuple[bool, bool]:
    """Return forward/reverse direction flags for one validated operator."""
    without_pipe_label = re.sub(r"\s*\|[^|\n]*\|\s*$", "", operator).strip()
    if re.fullmatch(r"~~+", without_pipe_label):
        return False, False
    forward = without_pipe_label[-1:] in {">", "o", "x"}
    reverse = without_pipe_label[:1] in {"<", "o", "x"}
    if not forward and not reverse:
        return True, True
    return forward, reverse


def _validate_mermaid_operators(line: str, operators: list[re.Match[str]]) -> None:
    """Reject any connector-shaped token outside the supported Mermaid grammar."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("%%", "classDef ", "style ", "linkStyle ")):
        return
    masked = list(line)
    for operator in operators:
        masked[operator.start() : operator.end()] = " " * (operator.end() - operator.start())
    remainder = "".join(masked)
    remainder = _QUOTED_TEXT.sub(lambda match: " " * len(match.group()), remainder)
    if operators and (composition := re.search(r"[;&]", remainder)) is not None:
        raise AssertionError(f"unsupported Mermaid composition {composition.group()!r}")
    candidate = _CONNECTOR_CANDIDATE.search(remainder)
    if candidate is not None:
        raise AssertionError(f"unsupported Mermaid connector {candidate.group()!r}")


def mermaid_directed_edges(source: str) -> set[tuple[str, str]]:
    """Extract every validated directed edge and fail closed on unknown operators."""
    edges: set[tuple[str, str]] = set()
    for line in source.splitlines():
        operators = list(_EDGE_OPERATOR.finditer(line))
        _validate_mermaid_operators(line, operators)
        for index, operator in enumerate(operators):
            left = operators[index - 1].end() if index else 0
            right = operators[index + 1].start() if index + 1 < len(operators) else len(line)
            source_id = _mermaid_node_id(line[left : operator.start()])
            target_id = _mermaid_node_id(line[operator.end() : right])
            forward, reverse = _mermaid_directions(operator.group())
            if (forward or reverse) and (source_id is None or target_id is None):
                raise AssertionError(
                    f"could not resolve nodes around Mermaid connector {operator.group()!r}"
                )
            if forward and source_id is not None and target_id is not None:
                edges.add((source_id, target_id))
            if reverse and source_id is not None and target_id is not None:
                edges.add((target_id, source_id))
    return edges


def mermaid_declared_ids(source: str) -> set[str]:
    """Inventory explicit/implicit Mermaid node IDs and subgraph IDs."""
    declared: set[str] = set()
    for line in source.splitlines():
        if subgraph := _SUBGRAPH_DECLARATION.match(line):
            declared.add(subgraph.group(1))

        without_quoted_text = _QUOTED_TEXT.sub(lambda match: " " * len(match.group()), line)
        declared.update(_SHAPED_NODE_DECLARATION.findall(without_quoted_text))

        operators = list(_EDGE_OPERATOR.finditer(line))
        _validate_mermaid_operators(line, operators)
        for index, operator in enumerate(operators):
            left = operators[index - 1].end() if index else 0
            right = operators[index + 1].start() if index + 1 < len(operators) else len(line)
            for segment in (
                line[left : operator.start()],
                line[operator.end() : right],
            ):
                if node_id := _mermaid_node_id(segment):
                    declared.add(node_id)
    return declared


def mermaid_directed_path(
    edges: set[tuple[str, str]], start: str, target: str
) -> tuple[str, ...] | None:
    """Return one deterministic directed path, if reachable."""
    adjacency: dict[str, set[str]] = {}
    for source, destination in edges:
        adjacency.setdefault(source, set()).add(destination)
    pending = [start]
    paths: dict[str, tuple[str, ...]] = {start: (start,)}
    while pending:
        current = pending.pop(0)
        for destination in sorted(adjacency.get(current, set())):
            if destination in paths:
                continue
            path = (*paths[current], destination)
            if destination == target:
                return path
            paths[destination] = path
            pending.append(destination)
    return None


class DocumentationContractTests(unittest.TestCase):
    def assert_no_mermaid_path(
        self,
        diagram: str,
        sources: tuple[str, ...],
        targets: tuple[str, ...],
    ) -> None:
        edges = mermaid_directed_edges(diagram)
        for source in sources:
            for target in targets:
                path = mermaid_directed_path(edges, source, target)
                self.assertIsNone(
                    path,
                    f"{source} reaches {target} through {' -> '.join(path or ())}",
                )

    def assert_mermaid_sets_isolated(
        self,
        diagram: str,
        first: tuple[str, ...],
        second: tuple[str, ...],
    ) -> None:
        self.assert_no_mermaid_path(diagram, first, second)
        self.assert_no_mermaid_path(diagram, second, first)

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

    def test_supporting_diagram_inventory_is_exactly_ten_source_svg_pairs(self) -> None:
        expected = set(DIAGRAM_LABELS)
        sources = {path.stem for path in IMAGES.glob("*.mmd")}
        rendered = {path.stem for path in IMAGES.glob("*.svg")}
        self.assertEqual(sources, expected)
        self.assertEqual(rendered, expected)

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

    def test_evaluation_architecture_separates_deterministic_authority_from_advice(self) -> None:
        diagram = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        declared_ids = mermaid_declared_ids(diagram)
        evaluation_ids = tuple(sorted(declared_ids.difference(WRITE_AUTHORITY_NODES)))
        self.assertEqual(set(evaluation_ids), declared_ids.difference(WRITE_AUTHORITY_NODES))
        self.assertTrue(
            {"CORPORA", "RUNNERS", "MEASURES", "AUTHORITY", "OPTIONAL", "OUTPUTS"}.issubset(
                evaluation_ids
            )
        )
        for edge in (
            "SAFETY --> SAFETY_RUN",
            "RETRIEVAL --> RETRIEVAL_RUN",
            "ORCHESTRATION --> ORCHESTRATION_RUN",
            "SAFETY_GATE --> SCORECARD",
            "RETRIEVAL_METRICS --> SCORECARD",
            "ORCHESTRATION_METRICS --> SCORECARD",
            "SCORECARD --> UI",
            "SCORECARD --> DEMO",
            "SCORECARD --> CI",
        ):
            self.assertIn(edge, diagram)
        self.assertIn("LIVE -. advisory observation .-> ADVISORY", diagram)
        self.assertIn("JUDGE -. presentation feedback .-> ADVISORY", diagram)
        self.assert_no_mermaid_path(
            diagram,
            ("LIVE", "JUDGE", "ADVISORY"),
            ("SCORECARD",),
        )
        self.assert_mermaid_sets_isolated(
            diagram,
            evaluation_ids,
            WRITE_AUTHORITY_NODES,
        )

    def test_mermaid_declared_id_inventory_covers_nodes_subgraphs_and_inline_nodes(self) -> None:
        fixture = """\
flowchart LR
  subgraph GROUP["GHOST[not a node]"]
    A["Alpha"] --> B{"Beta"} --> C(("Gamma"))
  end
  C --> D["Delta"]
"""
        self.assertEqual(mermaid_declared_ids(fixture), {"GROUP", "A", "B", "C", "D"})

    def test_mermaid_directed_edge_extractor_handles_supported_labels_and_chains(self) -> None:
        fixture = """\
flowchart LR
  EVAL["Evaluation"] -->|verified report| MID["Middle"] --> SCORE["Score"]
  SAFE["semicolon ; and ampersand & are label text"] -->|compare ; & cite| CLEAN["Clean"]
  SCORE -- "labelled solid" --> VIEW["View"]
  LIVE -. advisory observation .-> ADVISORY --> UI
  VIEW -.-> END
  THICK ==> ARCHIVE
  LABELLED == audit copy ==> REPORT
  CIRCLE --o REVIEW
  CROSS --x BLOCK
  OM <--> JUDGE
  OPEN --- PEER
  DOTTED -. visible .- NOTE
  THICK_OPEN === VAULT
  LAYOUT ~~~ ONLY
"""
        self.assertEqual(
            mermaid_directed_edges(fixture),
            {
                ("EVAL", "MID"),
                ("MID", "SCORE"),
                ("SAFE", "CLEAN"),
                ("SCORE", "VIEW"),
                ("LIVE", "ADVISORY"),
                ("ADVISORY", "UI"),
                ("VIEW", "END"),
                ("THICK", "ARCHIVE"),
                ("LABELLED", "REPORT"),
                ("CIRCLE", "REVIEW"),
                ("CROSS", "BLOCK"),
                ("OM", "JUDGE"),
                ("JUDGE", "OM"),
                ("OPEN", "PEER"),
                ("PEER", "OPEN"),
                ("DOTTED", "NOTE"),
                ("NOTE", "DOTTED"),
                ("THICK_OPEN", "VAULT"),
                ("VAULT", "THICK_OPEN"),
            },
        )

    def test_mermaid_directed_edge_extractor_rejects_unknown_connector_syntax(self) -> None:
        with self.assertRaisesRegex(AssertionError, "unsupported Mermaid connector"):
            mermaid_directed_edges("flowchart LR\n  JUDGE ~~> OM\n")

    def test_authority_path_guard_rejects_ambiguous_edge_composition(self) -> None:
        diagram = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        for mutation in (
            "X --> Y; JUDGE ==> OM",
            "X & JUDGE ==> OM",
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(AssertionError, "unsupported Mermaid composition"):
                    self.assert_no_mermaid_path(
                        f"{diagram}\n{mutation}\n",
                        ("JUDGE",),
                        ("GN", "W", "OM", "OP"),
                    )

    def test_full_authority_guard_covers_subgraphs_open_edges_and_reverse_paths(self) -> None:
        evaluation = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        architecture = (IMAGES / "02_system_architecture.mmd").read_text(encoding="utf-8")
        evaluation_roots = tuple(
            sorted(mermaid_declared_ids(evaluation).difference(WRITE_AUTHORITY_NODES))
        )

        mutations = (
            (
                evaluation,
                evaluation_roots,
                "OPTIONAL --> OM",
                r"OPTIONAL.*OM",
            ),
            (
                architecture,
                SYSTEM_ARCHITECTURE_EVALUATION_ROOTS,
                "EVALUATION --> OM",
                r"EVALUATION.*OM",
            ),
            (
                evaluation,
                evaluation_roots,
                "JUDGE --- OM",
                r"JUDGE.*OM",
            ),
            (
                evaluation,
                evaluation_roots,
                "OM --> JUDGE",
                r"OM.*JUDGE",
            ),
        )
        for diagram, roots, mutation, error in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(AssertionError, error):
                    self.assert_mermaid_sets_isolated(
                        f"{diagram}\n{mutation}\n",
                        roots,
                        WRITE_AUTHORITY_NODES,
                    )

        self.assert_mermaid_sets_isolated(
            f"{evaluation}\nOPTIONAL ~~~ OM\n",
            evaluation_roots,
            WRITE_AUTHORITY_NODES,
        )
        self.assert_mermaid_sets_isolated(
            f"{architecture}\nEVALUATION ~~~ OM\n",
            SYSTEM_ARCHITECTURE_EVALUATION_ROOTS,
            WRITE_AUTHORITY_NODES,
        )

    def test_full_authority_guard_covers_every_evaluation_node_and_runtime_plane(self) -> None:
        evaluation = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        architecture = (IMAGES / "02_system_architecture.mmd").read_text(encoding="utf-8")
        orchestration = (IMAGES / "03_orchestration.mmd").read_text(encoding="utf-8")
        evaluation_roots = tuple(
            sorted(mermaid_declared_ids(evaluation).difference(WRITE_AUTHORITY_NODES))
        )

        mutations = (
            (evaluation, evaluation_roots, "SAFETY --> GN", r"SAFETY.*GN"),
            (evaluation, evaluation_roots, "SAFETY_RUN --> OM", r"SAFETY_RUN.*OM"),
            (
                evaluation,
                evaluation_roots,
                "RETRIEVAL_METRICS --> OP",
                r"RETRIEVAL_METRICS.*OP",
            ),
            (
                evaluation,
                evaluation_roots,
                "ORCHESTRATION_RUN==label==>OM",
                r"ORCHESTRATION_RUN.*OM",
            ),
            (
                architecture,
                SYSTEM_ARCHITECTURE_EVALUATION_ROOTS,
                "EV --> W",
                r"EV.*W",
            ),
            (orchestration, ORCHESTRATION_EVALUATION_ROOTS, "ER --> OM", r"ER.*OM"),
            (orchestration, ORCHESTRATION_EVALUATION_ROOTS, "OM --> ER", r"OM.*ER"),
        )
        for diagram, roots, mutation, error in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(AssertionError, error):
                    self.assert_mermaid_sets_isolated(
                        f"{diagram}\n{mutation}\n",
                        roots,
                        WRITE_AUTHORITY_NODES,
                    )

        with self.assertRaisesRegex(AssertionError, r"NEW_EVAL.*OM"):
            mutation = 'NEW_EVAL["new evaluation node"] --> OM'
            mutated = f"{evaluation}\n{mutation}\n"
            self.assert_mermaid_sets_isolated(
                mutated,
                tuple(sorted(mermaid_declared_ids(mutated).difference(WRITE_AUTHORITY_NODES))),
                WRITE_AUTHORITY_NODES,
            )

    def test_authority_path_guard_rejects_direct_labelled_and_transitive_leaks(self) -> None:
        diagram = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        writes = ("GN", "W", "OM", "OP")

        with self.assertRaisesRegex(AssertionError, r"ADVISORY.*OM"):
            self.assert_no_mermaid_path(
                diagram + '\nADVISORY -- "forged approval" --> OM["Operations MCP"]\n',
                ("ADVISORY",),
                writes,
            )

        with self.assertRaisesRegex(AssertionError, r"SCORECARD.*OP"):
            self.assert_no_mermaid_path(
                diagram
                + '\nSCORECARD -. bad bridge .-> LEAK["bridge"] --> OP["Operations SQLite"]\n',
                ("SCORECARD",),
                writes,
            )

        directed_mutations = (
            "JUDGE ==> OM",
            "JUDGE == write ==> OM",
            "JUDGE --o OM",
            "JUDGE --x OM",
            "OM <--> JUDGE",
        )
        for mutation in directed_mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(AssertionError, r"JUDGE.*OM"):
                    self.assert_no_mermaid_path(
                        f"{diagram}\n{mutation}\n",
                        ("JUDGE",),
                        writes,
                    )

    def test_evaluation_corpora_are_authored_audit_data_not_official_evidence(self) -> None:
        diagram = (IMAGES / "10_evaluation_architecture.mmd").read_text(encoding="utf-8")
        normalized = diagram.replace("<br/>", " ")
        self.assertIn("AUTHORED + LABELLED + DIGEST-BOUND OFFLINE EVALUATION DATA", normalized)
        self.assertIn("not official source evidence", diagram)
        for node in ("SAFETY", "RETRIEVAL", "ORCHESTRATION"):
            declaration = next(
                line.strip() for line in diagram.splitlines() if line.strip().startswith(f"{node}[")
            )
            self.assertTrue(declaration.endswith(":::evaldata"), declaration)
            self.assertNotIn(":::official", declaration)

    def test_evaluation_architecture_is_legible_at_markdown_width(self) -> None:
        root = ElementTree.parse(IMAGES / "10_evaluation_architecture.svg").getroot()
        _, _, width, height = (float(value) for value in root.attrib["viewBox"].split())
        self.assertLessEqual(width, 1400, "evaluation SVG is too wide for a 700px Markdown column")
        self.assertLessEqual(height / width, 2, "evaluation SVG is too tall to scan as one system")

    def test_runtime_diagrams_keep_evaluation_read_only_and_before_closure(self) -> None:
        architecture = (IMAGES / "02_system_architecture.mmd").read_text(encoding="utf-8")
        self.assertIn("G -. read-only traces .-> EV", architecture)
        self.assertIn("RAG -. read-only retrieval report .-> EV", architecture)
        self.assert_mermaid_sets_isolated(
            architecture,
            SYSTEM_ARCHITECTURE_EVALUATION_ROOTS,
            WRITE_AUTHORITY_NODES,
        )

        orchestration = (IMAGES / "03_orchestration.mmd").read_text(encoding="utf-8")
        self.assertIn("VC -. read-only typed events .-> TC", orchestration)
        self.assertIn("TC --> OE", orchestration)
        self.assert_mermaid_sets_isolated(
            orchestration,
            ORCHESTRATION_EVALUATION_ROOTS,
            WRITE_AUTHORITY_NODES,
        )

        demo = (IMAGES / "07_demo_story.mmd").read_text(encoding="utf-8")
        self.assertIn("H --> J --> K --> I", demo)
        self.assertLess(
            demo.index('H["04:10 Audit & Evaluation'), demo.index('J["Retrieval ablation')
        )
        self.assertLess(
            demo.index('J["Retrieval ablation'), demo.index('K["Orchestration comparison')
        )
        self.assertLess(demo.index('K["Orchestration comparison'), demo.index('I["04:35'))

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
        self.assertIn("Stable double-render verified for 10 diagrams", result.stdout)
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
