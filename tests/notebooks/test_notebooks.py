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


def test_exactly_seven_numbered_notebooks_exist() -> None:
    actual = sorted(path.name for path in NOTEBOOKS.glob("*.ipynb"))
    assert actual == EXPECTED


def test_evaluation_notebook_is_self_contained_and_emits_ablation_markers() -> None:
    notebook = nbformat.read(NOTEBOOKS / "07_evaluation_ablation_and_orchestration.ipynb", 4)
    source = _sources(notebook)
    assert "import recallops" not in source
    assert "RRF uplift" in source
    assert "single-agent" in source
    assert "fixed-specialists" in source
    assert "96 retrieval" in source
    assert "24 orchestration" in source
    assert "zero rewrite uplift" in source
    assert "in-sample" in source
    assert "tamper rejection" in source
    assert "deterministic safety" in source


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
    for score in ("Score 1", "Score 2", "Score 3", "Score 4", "Score 5"):
        assert score in rubric
    assert "deterministic-only" in rubric
    assert "must not approve" in rubric
    assert "must not close" in rubric


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
