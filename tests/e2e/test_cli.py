from __future__ import annotations

import json

from recallops.cli import main


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
