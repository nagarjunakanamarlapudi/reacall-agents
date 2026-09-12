"""Contract and execution tests for the seven Week 3 teaching notebooks."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOKS = ROOT / "notebooks"
EXPECTED = [
    "01_langgraph_state_planning.ipynb",
    "02_mcp_boundaries.ipynb",
    "03_middleware_recovery.ipynb",
    "04_supervisor_specialists.ipynb",
    "05_durable_hitl.ipynb",
    "06_end_to_end_evaluation.ipynb",
    "07_evaluation_ablation_and_orchestration.ipynb",
]
MARKER = "EDUCATIONAL — SELF-CONTAINED"
TOPIC_TERMS = {
    EXPECTED[0]: ("StateGraph", "conditional", "plan"),
    EXPECTED[1]: ("tool", "resource", "schema", "discovery"),
    EXPECTED[2]: ("middleware", "model", "recovery", "retry"),
    EXPECTED[3]: ("supervisor", "specialist", "Deep Agents", "write_todos"),
    EXPECTED[4]: ("interrupt", "resume", "idempot", "human"),
    EXPECTED[5]: ("investigation", "evaluation", "reconciliation", "assert"),
    EXPECTED[6]: ("RRF", "single-agent", "fixed-specialists", "tamper"),
}
FORBIDDEN_SOURCE = re.compile(
    r"(?:%|!)pip\s+install|conda\s+install|\brecallops\b|\bhttpx\b|"
    r"\baiohttp\b|\bwebsockets\b|\brequests\b|\burllib\b|\bsocket\b|"
    r"https?://|Path\s*\(|open\s*\(",
    re.IGNORECASE,
)


def _sources(notebook: dict) -> str:
    return "\n".join(cell.get("source", "") for cell in notebook.cells)


def _execute_code_cells(notebook: dict) -> dict:
    namespace: dict = {"__name__": "notebook_contract"}
    for cell in notebook.cells:
        if cell.cell_type == "code":
            exec(cell.source, namespace)
    return namespace


def test_exactly_seven_numbered_notebooks_exist() -> None:
    actual = sorted(path.name for path in NOTEBOOKS.glob("*.ipynb"))
    assert actual == EXPECTED


def test_evaluation_notebook_derives_ranking_trajectory_and_ablation_results() -> None:
    notebook = nbformat.read(NOTEBOOKS / "07_evaluation_ablation_and_orchestration.ipynb", 4)
    source = _sources(notebook)
    assert "import recallops" not in source
    assert "os.environ" not in source and "getenv" not in source
    namespace = _execute_code_cells(notebook)
    rankings = {
        tuple(namespace["bm25_ranking"]),
        tuple(namespace["dense_ranking"]),
        tuple(namespace["rrf_ranking"]),
        tuple(namespace["reranked_ranking"]),
    }
    assert len(rankings) == 4
    assert namespace["bm25_scores"]["A"] > namespace["bm25_scores"]["B"]
    assert namespace["agentic_summary"] == {
        "queries": 2,
        "hops": 2,
        "reads": 2,
        "stop": "evidence_gap_after_rewrite",
    }
    assert namespace["comparison_deltas"]["fusion_recall_at_5"] > 0
    assert namespace["comparison_deltas"]["rerank_ndcg_at_5"] > 0
    assert namespace["comparison_deltas"]["agentic_minus_rerank_recall_at_5"] < 0
    assert namespace["comparison_deltas"]["agentic_minus_rerank_ndcg_at_5"] < 0
    assert namespace["comparison_deltas"]["rewrite_uplift"] == 0
    assert namespace["trajectory_metrics"]["single-agent"]["task_success_rate"] == 1.0
    assert namespace["trajectory_metrics"]["fixed-specialists"]["delegation_accuracy"] == 1.0
    assert namespace["trajectory_metrics"]["fixed-specialists"]["specialist_count"] == 4
    assert namespace["trajectory_metrics"]["fixed-specialists"]["order_accuracy"] == 1.0
    assert namespace["trajectory_metrics"]["fixed-specialists"]["duplicate_tool_call_ratio"] == 0.0
    assert namespace["optional_live_status"] == {
        "status": "not_run_missing_credentials",
        "excluded_from_offline_gates": True,
    }
    assert namespace["tamper_rejected"]


def test_evaluation_trajectory_metrics_reject_missing_delegation_and_early_verifier() -> None:
    notebook = nbformat.read(NOTEBOOKS / "07_evaluation_ablation_and_orchestration.ipynb", 4)
    namespace = _execute_code_cells(notebook)
    derive_trajectory = namespace["derive_trajectory"]
    without_delegation = [
        event for event in namespace["specialist_events"] if event["kind"] != "delegate"
    ]
    missing_one_delegation = [
        event
        for event in namespace["specialist_events"]
        if not (event["kind"] == "delegate" and event["task"] == "trace")
    ]
    early_verifier = [
        namespace["specialist_events"][0],
        namespace["specialist_events"][-1],
        *namespace["specialist_events"][1:-1],
    ]
    assert derive_trajectory(without_delegation, True)["delegation_accuracy"] == 0.0
    assert derive_trajectory(missing_one_delegation, True)["delegation_accuracy"] == 0.0
    assert derive_trajectory(early_verifier, True)["order_accuracy"] == 0.0


def test_evaluation_notebook_executes_from_a_clean_temporary_cwd() -> None:
    notebook = nbformat.read(NOTEBOOKS / "07_evaluation_ablation_and_orchestration.ipynb", 4)
    with tempfile.TemporaryDirectory() as temporary:
        NotebookClient(
            notebook,
            timeout=120,
            kernel_name=os.environ.get("RECALLOPS_NOTEBOOK_KERNEL", "python3"),
            resources={"metadata": {"path": temporary}},
        ).execute()
    outputs = "\n".join(
        output.get("text", "")
        for cell in notebook.cells
        if cell.cell_type == "code"
        for output in cell.get("outputs", [])
    )
    assert "BM25-like rankings" in outputs
    assert "agentic trace" in outputs
    assert "digest-bound" in outputs


def test_evaluation_rubric_has_anchored_human_scores_and_authority_limits() -> None:
    rubric = (ROOT / "docs" / "EVALUATION_RUBRIC.md").read_text(encoding="utf-8")
    for dimension in (
        "Correctness and citation alignment",
        "Completeness",
        "Uncertainty and abstention",
        "Actionability",
        "Clarity",
    ):
        assert dimension in rubric
        section = rubric.split(f"### {dimension}", 1)[1].split("###", 1)[0]
        assert all(f"**Score {score}:**" in section for score in range(1, 6))
    assert "deterministic-only" in rubric
    assert "must not approve" in rubric
    assert "must not close" in rubric
    assert "| Dimension | Evidence IDs | Score (1–5) | Rationale |" in rubric
    assert "Rubric and prompt digest" in rubric
    assert "anonymous randomized A/B" in rubric
    assert "multiple independent, repeated judgments" in rubric
    assert "score distributions" in rubric
    assert "variance" in rubric
    assert "Adjudication" in rubric


def test_builder_is_byte_deterministic_in_separate_directories() -> None:
    sys.path.insert(0, str(ROOT))
    from scripts import build_notebooks

    with tempfile.TemporaryDirectory() as temporary:
        first = Path(temporary) / "first"
        second = Path(temporary) / "second"
        build_notebooks.build_notebooks(first)
        build_notebooks.build_notebooks(second)
        for name in EXPECTED:
            assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_hitl_notebook_uses_sqlite_and_rebuilds_the_checkpointer() -> None:
    source = _sources(nbformat.read(str(NOTEBOOKS / EXPECTED[4]), as_version=4))
    assert "SqliteSaver" in source
    assert "MemorySaver" not in source
    assert "rebuild" in source.lower()
    assert "same thread_id" in source.lower()


def test_each_notebook_is_self_contained_and_teaches_its_topic() -> None:
    for name in EXPECTED:
        notebook = nbformat.read(str(NOTEBOOKS / name), as_version=4)
        source = _sources(notebook)
        assert source.count(MARKER) >= 2, name
        assert "assert " in source or "assert(" in source, name
        assert "print(" in source or "display(" in source, name
        assert not FORBIDDEN_SOURCE.search(source), name
        assert all(term.lower() in source.lower() for term in TOPIC_TERMS[name]), name


def test_notebooks_have_executable_code_and_no_empty_placeholder_cells() -> None:
    for name in EXPECTED:
        notebook = nbformat.read(str(NOTEBOOKS / name), as_version=4)
        code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
        assert len(code_cells) >= 3, name
        assert all(cell.source.strip() for cell in code_cells), name


def test_notebooks_explain_the_live_authority_boundary_without_claiming_model_execution() -> None:
    for name in EXPECTED:
        source = _sources(nbformat.read(str(NOTEBOOKS / name), as_version=4))
        for term in (
            "OpenAI",
            "write_todos",
            "safe typed claims",
            "independent source verifier",
            "make ui-openai",
            "make eval-model",
            "No live model was called",
        ):
            assert term in source, (name, term)


def test_all_notebooks_execute_offline_and_emit_markers() -> None:
    for name in EXPECTED:
        notebook = nbformat.read(str(NOTEBOOKS / name), as_version=4)
        kernel_name = os.environ.get("RECALLOPS_NOTEBOOK_KERNEL", "python3")
        NotebookClient(notebook, timeout=120, kernel_name=kernel_name).execute()
        outputs = "\n".join(
            output.get("text", "")
            for cell in notebook.cells
            if cell.cell_type == "code"
            for output in cell.get("outputs", [])
        )
        assert outputs.count(MARKER) >= 2, name
        assert "ASSERTION PASSED" in outputs, name
