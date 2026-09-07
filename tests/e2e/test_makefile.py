from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make(
    *arguments: str,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", "-f", str(ROOT / "Makefile"), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _copy_eval_dir(destination: Path) -> Path:
    destination.mkdir()
    source = ROOT / "data" / "evals"
    for name in (
        "report.json",
        "retrieval_report.json",
        "orchestration_report.json",
        "scorecard.json",
        "scenarios.json",
        "retrieval_cases.json",
        "orchestration_cases.json",
    ):
        shutil.copyfile(source / name, destination / name)
    return destination


def _live_error_environment(tmp_path: Path) -> tuple[dict[str, str], str]:
    module = tmp_path / "make_live_adapter.py"
    module.write_text(
        "\n".join(
            (
                "from recallops.evaluation.orchestration_benchmark import LiveRunnerFactory",
                "def unavailable():",
                "    raise ValueError('provider credentials unavailable')",
                "adapter = LiveRunnerFactory(provider='test', model='test-model', factory=unavailable)",
            )
        )
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(tmp_path), env.get("PYTHONPATH", "")) if item
    )
    return env, "make_live_adapter:adapter"


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
    assert "recallops_live:factory" not in configured.stdout
    assert "${LIVE_MODEL_ADAPTER}" in configured.stdout
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


def test_make_eval_summary_refreshes_a_scorecard_older_than_suite_reports(
    tmp_path: Path,
) -> None:
    eval_dir = _copy_eval_dir(tmp_path / "evals")
    scorecard_path = eval_dir / "scorecard.json"
    forged = json.loads(scorecard_path.read_bytes())
    forged["artifact_digests"]["retrieval_report"] = "0" * 64
    scorecard_path.write_text(json.dumps(forged))

    os.utime(scorecard_path, (1, 1))
    for report_name in (
        "report.json",
        "retrieval_report.json",
        "orchestration_report.json",
    ):
        os.utime(eval_dir / report_name, (2, 2))

    result = _make("eval-summary", f"EVAL_DIR={eval_dir}")

    assert result.returncode == 0
    assert "Offline evaluation gate: PASSED" in result.stdout
    refreshed = json.loads(scorecard_path.read_bytes())
    assert refreshed["artifact_digests"]["retrieval_report"] != "0" * 64


def test_make_eval_model_stops_on_missing_adapter_without_changing_artifacts(
    tmp_path: Path,
) -> None:
    eval_dir = _copy_eval_dir(tmp_path / "evals")
    before_report = (eval_dir / "orchestration_report.json").read_bytes()
    before_scorecard = (eval_dir / "scorecard.json").read_bytes()

    result = _make(
        "eval-model",
        f"EVAL_DIR={eval_dir}",
        "LIVE_MODEL_ADAPTER=module_that_does_not_exist:adapter",
    )

    assert result.returncode != 0
    assert "Orchestration evaluation: UNVERIFIED" in result.stdout
    assert "Offline evaluation gate" not in result.stdout
    assert (eval_dir / "orchestration_report.json").read_bytes() == before_report
    assert (eval_dir / "scorecard.json").read_bytes() == before_scorecard


def test_make_eval_model_stops_on_wrong_adapter_type_without_changing_artifacts(
    tmp_path: Path,
) -> None:
    eval_dir = _copy_eval_dir(tmp_path / "evals")
    before_report = (eval_dir / "orchestration_report.json").read_bytes()
    before_scorecard = (eval_dir / "scorecard.json").read_bytes()

    result = _make(
        "eval-model",
        f"EVAL_DIR={eval_dir}",
        "LIVE_MODEL_ADAPTER=os:path",
    )

    assert result.returncode != 0
    assert "LiveRunnerFactory" in result.stdout
    assert "Offline evaluation gate" not in result.stdout
    assert (eval_dir / "orchestration_report.json").read_bytes() == before_report
    assert (eval_dir / "scorecard.json").read_bytes() == before_scorecard


def test_make_eval_model_stops_when_scorecard_build_fails(tmp_path: Path) -> None:
    eval_dir = _copy_eval_dir(tmp_path / "evals")
    before_scorecard = (eval_dir / "scorecard.json").read_bytes()
    (eval_dir / "report.json").unlink()
    env, adapter = _live_error_environment(tmp_path)

    result = _make(
        "eval-model",
        f"EVAL_DIR={eval_dir}",
        f"LIVE_MODEL_ADAPTER={adapter}",
        env=env,
    )

    assert result.returncode != 0
    assert "Offline evaluation gate" not in result.stdout
    assert (eval_dir / "scorecard.json").read_bytes() == before_scorecard


def test_make_eval_model_keeps_valid_live_error_outside_offline_exit(tmp_path: Path) -> None:
    eval_dir = _copy_eval_dir(tmp_path / "evals")
    env, adapter = _live_error_environment(tmp_path)

    result = _make(
        "eval-model",
        f"EVAL_DIR={eval_dir}",
        f"LIVE_MODEL_ADAPTER={adapter}",
        env=env,
    )

    assert result.returncode == 0
    assert "Optional live: error (excluded from offline gate)" in result.stdout
    assert "Offline evaluation gate: PASSED" in result.stdout
    scorecard = json.loads((eval_dir / "scorecard.json").read_bytes())
    assert scorecard["offline_gate_passed"] is True
    assert scorecard["optional_live_status"]["status"] == "error"


def test_make_eval_model_never_executes_adapter_shell_syntax(tmp_path: Path) -> None:
    for index, adapter in enumerate(
        (
            f"`touch {tmp_path / 'backtick-marker'}`",
            f"$(touch {tmp_path / 'substitution-marker'})",
        )
    ):
        eval_dir = _copy_eval_dir(tmp_path / f"evals-{index}")
        result = _make(
            "eval-model",
            f"EVAL_DIR={eval_dir}",
            f"LIVE_MODEL_ADAPTER={adapter}",
        )

        assert result.returncode != 0
        assert "live adapter must use MODULE:ATTRIBUTE syntax" in result.stdout
    assert not (tmp_path / "backtick-marker").exists()
    assert not (tmp_path / "substitution-marker").exists()
