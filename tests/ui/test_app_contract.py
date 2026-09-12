import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from recallops.llm.live_reasoning import LiveReasoningService, LiveReasoningSummary

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


def test_corrupt_telemetry_is_visible_without_blocking_case_or_leaking_into_session(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RECALLOPS_UI_MODE", "durable")
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run()
    original_pending = app.session_state["ui_case"]["pending_interrupt"]
    thread_id = app.session_state["ui_case"]["thread_id"]
    with sqlite3.connect(tmp_path / "reasoning.sqlite3") as connection:
        connection.execute(
            "INSERT INTO reasoning_summaries VALUES (?, ?)",
            (thread_id, '{"sk-canary-private-credential":'),
        )
    app.button(key="run_investigation_button").click().run()
    assert not app.exception
    case = app.session_state["ui_case"]
    assert case["pending_interrupt"] == original_pending
    assert case["llm_status"] == "unavailable"
    assert "canary-private-credential" not in json.dumps(case)
    assert any("reasoning telemetry unavailable" in item.value.lower() for item in app.warning)
    assert (
        next(item.value for item in app.metric if item.label == "Reasoning mode") == "Unavailable"
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_openai_product_readiness_live_cards_and_visible_fallback(monkeypatch, tmp_path, status):
    """Catch missing settings injection, live telemetry, or failure labels in the rendered app."""
    monkeypatch.setenv("RECALLOPS_UI_MODE", "durable")
    monkeypatch.setenv("RECALLOPS_MODEL_MODE", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path))
    calls = []
    roles = [
        "recall-intelligence",
        "product-lot-matching",
        "traceability-reconciliation",
        "containment-communications",
    ]

    async def run(self, question, *, transport):
        calls.append(question)
        return LiveReasoningSummary(
            model=self.settings.model,
            status=status,
            duration_ms=125,
            plan=roles,
            specialist_sequence=roles if status == "completed" else [],
            fallback_used=status == "failed",
            error_category="timeout" if status == "failed" else None,
            total_tokens=50,
            input_tokens=30,
            output_tokens=20,
            events=[
                {"kind": "tool", "name": "get_recall", "status": "completed", "duration_ms": 10}
            ],
        )

    monkeypatch.setattr(LiveReasoningService, "run", run)
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert (
        next(item.value for item in app.metric if item.label == "Reasoning mode")
        == "OpenAI · test-model"
    )
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    assert any("ready" in item.value.lower() for item in app.caption)
    app.button(key="run_investigation_button").click().run()
    assert not app.exception
    text = "\n".join(item.value for item in (*app.markdown, *app.caption))
    assert all(role in text for role in roles)
    assert "not verified" in text.lower()
    assert next(item.value for item in app.metric if item.label == "Total tokens") == "50"
    assert any("get_recall" in item.value.to_string() for item in app.dataframe)
    if status == "failed":
        assert any("deterministic fallback" in item.value.lower() for item in app.warning)
    app.run()
    app.button(key="run_investigation_button").click().run()
    assert len(calls) == 1
