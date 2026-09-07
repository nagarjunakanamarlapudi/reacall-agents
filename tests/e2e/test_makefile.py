from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_make_help_lists_the_supported_project_workflows() -> None:
    result = _make("help")

    assert result.returncode == 0
    for target in (
        "setup",
        "data-validate",
        "demo",
        "ui",
        "ui-stdio",
        "mcp-smoke",
        "eval",
        "notebooks",
        "diagrams",
        "test",
        "lint",
        "security",
        "verify",
    ):
        assert target in result.stdout


def test_make_ui_dry_run_uses_configurable_runtime_and_port() -> None:
    result = _make(
        "-n",
        "ui",
        "RUNTIME_DIR=.test-runtime",
        "PORT=8765",
    )

    assert result.returncode == 0
    assert 'RECALLOPS_RUNTIME_DIR=".test-runtime"' in result.stdout
    assert "streamlit run src/recallops/ui/app.py" in result.stdout
    assert '--server.port "8765"' in result.stdout


def test_make_ui_stdio_dry_run_selects_stdio_transport() -> None:
    result = _make("-n", "ui-stdio")

    assert result.returncode == 0
    assert "RECALLOPS_MCP_TRANSPORT=stdio" in result.stdout


def test_make_verify_dry_run_covers_submission_gates() -> None:
    result = _make("-n", "verify")

    assert result.returncode == 0
    for command in (
        "recallops data-validate",
        "ruff format --check",
        "pytest -q",
        "build_notebooks.py",
        "render_diagrams.sh --verify",
        "test_mcp_servers.py",
        "recallops eval",
        "pip-audit",
        "npm audit --omit=dev",
    ):
        assert command in result.stdout
