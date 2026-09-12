import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from recallops.llm.live_reasoning import LiveReasoningService

APP = Path(__file__).parents[2] / "src" / "recallops" / "ui" / "app.py"


def test_corrupt_legacy_telemetry_cannot_change_new_schema_case_or_leak_into_session(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RECALLOPS_UI_MODE", "durable")
    monkeypatch.setenv("RECALLOPS_MODEL_MODE", "deterministic")
    monkeypatch.setenv("RECALLOPS_RUNTIME_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key="open_case_button").click().run()
    app.radio(key="ui_active_view").set_value("Investigation").run()
    app.button(key="run_investigation_button").click().run()
    original_pending = app.session_state["ui_case"]["pending_interrupt"]
    thread_id = app.session_state["ui_case"]["thread_id"]
    with sqlite3.connect(tmp_path / "reasoning.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE reasoning_summaries (thread_id TEXT PRIMARY KEY, summary_json TEXT)"
        )
        connection.execute(
            "INSERT INTO reasoning_summaries VALUES (?, ?)",
            (thread_id, '{"sk-canary-private-credential":'),
        )
    app.button(key="run_investigation_button").click().run()
    assert not app.exception
    case = app.session_state["ui_case"]
    assert case["pending_interrupt"] == original_pending
    assert case["llm_status"] == "not_run"
    assert "canary-private-credential" not in json.dumps(case)
    assert not any("reasoning telemetry unavailable" in item.value.lower() for item in app.warning)
    assert (
        next(item.value for item in app.metric if item.label == "Reasoning mode") == "Deterministic"
    )


@pytest.mark.parametrize("status", ["completed", "failed", "semantic_failure"])
def test_openai_product_readiness_live_cards_and_visible_fallback(
    monkeypatch, tmp_path, status, live_case
):
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

    original_run = LiveReasoningService.run

    async def run(self, request, *, transport):
        calls.append(request)
        return await original_run(self, request, transport=transport)

    import recallops.llm.live_reasoning as live

    def model(settings):
        if status == "failed":
            raise TimeoutError("PRIVATE_UI_PROVIDER_CANARY")
        raw = live_case.raw
        for action in raw["containment-communications"]["proposed_actions"]:
            action["case_id"] = calls[-1].case_id
        if status == "semantic_failure":
            raw["product-lot-matching"]["decisions"][0]["classification"] = "probable"
        return live_case.script(raw)

    monkeypatch.setattr(LiveReasoningService, "run", run)
    monkeypatch.setattr(live, "build_chat_model", model)
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert (
        next(item.value for item in app.metric if item.label == "Reasoning mode")
        == "OpenAI · test-model"
    )
    app.button(key="open_case_button").click().run()
    assert app.session_state["ui_case"]["scope_lot_ids"] == live_case.scope
    app.radio(key="ui_active_view").set_value("Investigation").run()
    assert any("ready" in item.value.lower() for item in app.caption)
    app.button(key="run_investigation_button").click().run()
    assert not app.exception
    assert list(calls[-1].scope_lot_ids) == live_case.scope
    text = "\n".join(item.value for item in (*app.markdown, *app.caption))
    assert all(role in text for role in roles)
    case = app.session_state["ui_case"]
    assert "PRIVATE_" not in json.dumps(case)
    assert next(item.value for item in app.metric if item.label == "Total tokens") == (
        "Unavailable" if status == "failed" else "125"
    )
    if status != "failed":
        assert any("get_recall" in item.value.to_string() for item in app.dataframe)
    assert case["verification"]["passed"] is (status != "semantic_failure")
    assert (case["pending_interrupt"] is not None) is (status != "semantic_failure")
    if status == "failed":
        assert any("deterministic fallback" in item.value.lower() for item in app.warning)
    else:
        assert not any("deterministic fallback" in item.value.lower() for item in app.warning)
    app.run()
    app.button(key="run_investigation_button").click().run()
    assert len(calls) == 1
