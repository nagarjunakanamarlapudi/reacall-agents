"""Contract and execution tests for the six Week 3 teaching notebooks."""

from __future__ import annotations

import os
import re
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
]
MARKER = "EDUCATIONAL — SELF-CONTAINED"
TOPIC_TERMS = {
    EXPECTED[0]: ("StateGraph", "conditional", "plan"),
    EXPECTED[1]: ("tool", "resource", "schema", "discovery"),
    EXPECTED[2]: ("middleware", "model", "recovery", "retry"),
    EXPECTED[3]: ("supervisor", "specialist", "Deep Agents", "write_todos"),
    EXPECTED[4]: ("interrupt", "resume", "idempot", "human"),
    EXPECTED[5]: ("investigation", "evaluation", "reconciliation", "assert"),
}
FORBIDDEN_SOURCE = re.compile(
    r"(?:%|!)pip\s+install|conda\s+install|\brecallops\b|\brequests\b|"
    r"\burllib\b|\bsocket\b|https?://|Path\s*\(|open\s*\(",
    re.IGNORECASE,
)


def _sources(notebook: dict) -> str:
    return "\n".join(cell.get("source", "") for cell in notebook.cells)


def test_exactly_six_numbered_notebooks_exist() -> None:
    actual = sorted(path.name for path in NOTEBOOKS.glob("*.ipynb"))
    assert actual == EXPECTED


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
