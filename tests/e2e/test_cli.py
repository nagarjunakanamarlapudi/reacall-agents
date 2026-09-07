from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from recallops.cli import main
from recallops.paths import DATA_DIR, EvaluationArtifactPaths


def copy_verified_artifacts(destination: Path) -> tuple[EvaluationArtifactPaths, Path]:
    for name in (
        "report.json",
        "retrieval_report.json",
        "orchestration_report.json",
        "scorecard.json",
        "scenarios.json",
        "retrieval_cases.json",
        "orchestration_cases.json",
    ):
        shutil.copyfile(DATA_DIR / "evals" / name, destination / name)
    return (
        EvaluationArtifactPaths(
            safety_report=destination / "report.json",
            retrieval_report=destination / "retrieval_report.json",
            orchestration_report=destination / "orchestration_report.json",
        ),
        destination / "scorecard.json",
    )


def test_eval_command_summarizes_committed_report_schema(capsys, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "gate_passed": True,
                "results": [
                    {"id": "R01", "safety_critical": True, "passed": True},
                    {"id": "R02", "safety_critical": True, "passed": True},
                ],
            }
        )
    )

    assert main(["eval", "--report", str(report_path)]) == 0
    output = capsys.readouterr().out
    assert "Evaluation scenarios: 2" in output
    assert "Safety-critical: 2/2 passed" in output


def test_eval_command_fails_when_report_gate_is_not_passed(capsys, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "gate_passed": False,
                "results": [{"id": "R01", "safety_critical": True, "passed": True}],
            }
        )
    )

    assert main(["eval", "--report", str(report_path)]) == 1
    assert "Evaluation gate: FAILED" in capsys.readouterr().out


def test_eval_retrieval_validates_and_summarizes_verified_report(capsys, tmp_path: Path) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)

    assert main(["eval-retrieval", "--report", str(paths.retrieval_report)]) == 0
    output = capsys.readouterr().out
    assert "Retrieval cases: 96" in output
    assert "Persisted results: 576" in output
    assert "Agentic RAG Recall@5:" in output
    assert "Report SHA-256:" in output
    assert "Retrieval gate: PASSED" in output


def test_eval_retrieval_fails_closed_on_unverified_report(capsys, tmp_path: Path) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)
    paths.retrieval_report.write_text("{}")

    assert main(["eval-retrieval", "--report", str(paths.retrieval_report)]) == 1
    output = capsys.readouterr().out
    assert "Retrieval evaluation: UNVERIFIED" in output
    assert "PASSED" not in output


def test_eval_retrieval_run_atomically_builds_a_verified_report(capsys, tmp_path: Path) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)
    paths.retrieval_report.unlink()

    assert (
        main(
            [
                "eval-retrieval",
                "--run",
                "--cases",
                str(paths.retrieval_corpus),
                "--report",
                str(paths.retrieval_report),
            ]
        )
        == 0
    )
    assert paths.retrieval_report.is_file()
    assert "Retrieval gate: PASSED" in capsys.readouterr().out


def test_eval_retrieval_failed_run_preserves_previous_report(capsys, tmp_path: Path) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)
    previous = paths.retrieval_report.read_bytes()
    paths.retrieval_corpus.write_text("{}")

    assert (
        main(
            [
                "eval-retrieval",
                "--run",
                "--cases",
                str(paths.retrieval_corpus),
                "--report",
                str(paths.retrieval_report),
            ]
        )
        == 1
    )
    assert paths.retrieval_report.read_bytes() == previous
    assert "Retrieval evaluation: UNVERIFIED" in capsys.readouterr().out


def test_eval_retrieval_unexpected_runner_failure_is_bounded(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    from recallops.evaluation import retrieval_benchmark

    paths, _ = copy_verified_artifacts(tmp_path)
    previous = paths.retrieval_report.read_bytes()

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private runner failure")

    monkeypatch.setattr(retrieval_benchmark, "run_retrieval_benchmark", fail)

    assert (
        main(
            [
                "eval-retrieval",
                "--run",
                "--cases",
                str(paths.retrieval_corpus),
                "--report",
                str(paths.retrieval_report),
            ]
        )
        == 1
    )
    assert paths.retrieval_report.read_bytes() == previous
    assert "Retrieval evaluation: UNVERIFIED" in capsys.readouterr().out


def test_eval_orchestration_validates_and_summarizes_verified_report(
    capsys, tmp_path: Path
) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)

    assert main(["eval-orchestration", "--report", str(paths.orchestration_report)]) == 0
    output = capsys.readouterr().out
    assert "Orchestration cases: 24" in output
    assert "Persisted offline results: 48" in output
    assert "Optional live: not_run_missing_credentials (excluded from offline gate)" in output
    assert "Report SHA-256:" in output
    assert "Orchestration gate: PASSED" in output


def test_eval_orchestration_run_atomically_builds_a_verified_report(capsys, tmp_path: Path) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)
    paths.orchestration_report.unlink()

    assert (
        main(
            [
                "eval-orchestration",
                "--run",
                "--cases",
                str(paths.orchestration_corpus),
                "--report",
                str(paths.orchestration_report),
            ]
        )
        == 0
    )
    assert paths.orchestration_report.is_file()
    assert "Orchestration gate: PASSED" in capsys.readouterr().out


def test_eval_orchestration_live_error_never_changes_offline_exit_status(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    from recallops.evaluation.orchestration_benchmark import LiveRunnerFactory

    paths, _ = copy_verified_artifacts(tmp_path)
    module = ModuleType("test_live_adapter")
    module.adapter = LiveRunnerFactory(
        provider="test",
        model="failing-model",
        factory=lambda: (_ for _ in ()).throw(ValueError("credentials unavailable")),
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert (
        main(
            [
                "eval-orchestration",
                "--run",
                "--live-adapter",
                "test_live_adapter:adapter",
                "--cases",
                str(paths.orchestration_corpus),
                "--report",
                str(paths.orchestration_report),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Optional live: error (excluded from offline gate)" in output
    assert "Optional live error: factory_error" in output
    assert "Orchestration gate: PASSED" in output


def test_eval_orchestration_reports_missing_live_adapter_without_traceback(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)
    module = ModuleType("missing_credentials_adapter")

    def unavailable():
        raise RuntimeError("provider credentials are missing")

    module.adapter = unavailable
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert (
        main(
            [
                "eval-orchestration",
                "--run",
                "--live-adapter",
                "missing_credentials_adapter:adapter",
                "--cases",
                str(paths.orchestration_corpus),
                "--report",
                str(paths.orchestration_report),
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "Orchestration evaluation: UNVERIFIED" in output
    assert "Traceback" not in output


@pytest.mark.parametrize(
    "adapter",
    (
        "module-only",
        "module:attribute:extra",
        "module with space:attribute",
        "module:`attribute`",
        "module:$(attribute)",
        "module:hyphen-name",
    ),
)
def test_eval_orchestration_rejects_malformed_live_adapter_syntax(
    adapter: str, capsys, tmp_path: Path
) -> None:
    paths, _ = copy_verified_artifacts(tmp_path)

    assert (
        main(
            [
                "eval-orchestration",
                "--run",
                "--live-adapter",
                adapter,
                "--cases",
                str(paths.orchestration_corpus),
                "--report",
                str(paths.orchestration_report),
            ]
        )
        == 1
    )
    assert "live adapter must use MODULE:ATTRIBUTE syntax" in capsys.readouterr().out


def test_empty_evaluation_error_is_bounded_and_cleans_temporary_file(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    from recallops.evaluation import orchestration_benchmark

    paths, _ = copy_verified_artifacts(tmp_path)
    previous = paths.orchestration_report.read_bytes()

    async def fail(*_args, **_kwargs):
        raise ValueError()

    monkeypatch.setattr(orchestration_benchmark, "run_orchestration_benchmark", fail)

    assert (
        main(
            [
                "eval-orchestration",
                "--run",
                "--cases",
                str(paths.orchestration_corpus),
                "--report",
                str(paths.orchestration_report),
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "Orchestration evaluation: UNVERIFIED" in output
    assert "Traceback" not in output
    assert paths.orchestration_report.read_bytes() == previous
    assert not tuple(tmp_path.glob(".orchestration_report.json.*.tmp"))


def test_eval_scorecard_validates_and_summarizes_all_suites(capsys, tmp_path: Path) -> None:
    _, scorecard = copy_verified_artifacts(tmp_path)

    assert main(["eval-scorecard", "--scorecard", str(scorecard)]) == 0
    output = capsys.readouterr().out
    assert "safety: 21 cases · 21 results · PASSED" in output
    assert "retrieval: 96 cases · 576 results · PASSED" in output
    assert "orchestration: 24 cases · 48 results · PASSED" in output
    assert "Optional live: not_run_missing_credentials (excluded from offline gate)" in output
    assert "Scorecard SHA-256:" in output
    assert "Offline evaluation gate: PASSED" in output


def test_eval_scorecard_summary_fails_on_stale_artifact(capsys, tmp_path: Path) -> None:
    paths, scorecard = copy_verified_artifacts(tmp_path)
    paths.retrieval_report.write_text("{}")

    assert main(["eval-scorecard", "--scorecard", str(scorecard)]) == 1
    output = capsys.readouterr().out
    assert "Combined evaluation: UNVERIFIED" in output
    assert "PASSED" not in output


def test_data_validate_command_reports_real_dataset_counts(capsys) -> None:
    assert main(["data-validate"]) == 0
    output = capsys.readouterr().out
    assert "DATA VALID: H-1230-2026" in output
    assert "48 products · 144 lots · 577 events" in output
    assert "SYNTHETIC — ACADEMIC DEMO" in output


def test_demo_command_prints_exact_copy_paste_landmarks(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path / "runtime"))
    assert main(["demo", "--recall-number", "H-1230-2026"]) == 0
    output = capsys.readouterr().out
    for text in (
        "RecallOps Command Center",
        "OFFICIAL — openFDA snapshot",
        "SYNTHETIC — ACADEMIC DEMO",
        "Agentic RAG: BM25 sparse + LSA dense → RRF → deterministic rerank → critic",
        "Review required",
        "Decision=approve",
        "Simulate approved actions",
        "Simulated action recorded",
        "Open — closure blocked",
    ):
        assert text in output


def test_mcp_config_is_machine_readable_and_has_three_servers(capsys) -> None:
    assert main(["mcp-config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["mcpServers"]) == {
        "recall-registry",
        "traceability",
        "operations",
    }
    assert all(server["command"] == "uv" for server in payload["mcpServers"].values())


def test_unknown_demo_recall_fails_safely_without_traceback(capsys) -> None:
    assert main(["demo", "--recall-number", "NOT-A-RECALL"]) == 2
    captured = capsys.readouterr()
    assert "Recall 'NOT-A-RECALL' is unavailable from the configured registry." in captured.err
    assert "Traceback" not in captured.err


def test_demo_command_is_reentrant_and_deterministic(capsys, monkeypatch) -> None:
    monkeypatch.delenv("RECALLOPS_RUNTIME_DIR", raising=False)
    assert main(["demo", "--recall-number", "H-1230-2026"]) == 0
    first = capsys.readouterr().out
    assert main(["demo", "--recall-number", "H-1230-2026"]) == 0
    second = capsys.readouterr().out
    assert second == first


def test_demo_rejects_unknown_mcp_transport(capsys, monkeypatch) -> None:
    monkeypatch.setenv("RECALLOPS_MCP_TRANSPORT", "pretend")
    assert main(["demo", "--recall-number", "H-1230-2026"]) == 2
    captured = capsys.readouterr()
    assert "direct" in captured.err
    assert "stdio" in captured.err
    assert "Traceback" not in captured.err
