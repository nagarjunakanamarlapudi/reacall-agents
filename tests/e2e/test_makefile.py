from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make(*arguments: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", "-f", str(ROOT / "Makefile"), *arguments],
        cwd=cwd,
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
        "eval-fast",
        "eval-safety",
        "eval-retrieval",
        "eval-orchestration",
        "eval",
        "eval-summary",
        "eval-model",
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


def test_make_eval_expands_all_offline_suites() -> None:
    result = _make("-n", "eval")

    assert result.returncode == 0
    assert "runtime_executor" in result.stdout
    assert "retrieval_benchmark" in result.stdout
    assert "orchestration_benchmark" in result.stdout
    assert "eval-scorecard" in result.stdout


def test_make_eval_model_is_explicit_opt_in_and_read_only() -> None:
    missing = _make("-n", "eval-model")
    configured = _make("-n", "eval-model", "LIVE_MODEL_ADAPTER=recallops_live:factory")

    assert missing.returncode == 0
    assert "LIVE_MODEL_ADAPTER is required" in missing.stdout
    assert "exit 2" in missing.stdout
    assert configured.returncode == 0
    assert "recallops_live:factory" in configured.stdout
    assert "eval-orchestration --run" in configured.stdout
    for operation in (
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ):
        assert operation not in configured.stdout


def test_make_eval_summary_is_cwd_independent(tmp_path: Path) -> None:
    result = _make("eval-summary", cwd=tmp_path)

    assert result.returncode == 0
    assert "Offline evaluation gate: PASSED" in result.stdout
