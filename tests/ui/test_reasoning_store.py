import json
import sqlite3

import pytest

from recallops.llm.live_reasoning import LiveReasoningSummary
from recallops.ui.reasoning_store import ReasoningStore


@pytest.mark.parametrize(
    "mutation",
    [
        {"model": "sk-canary-private-credential"},
        {"model": "Bearer canary-private-credential"},
        {"model": "api_key=canary-private-credential"},
        {"plan": ["sk-canary-private-credential"]},
        {"specialist_sequence": ["sk-canary-private-credential"]},
        {"read_tool_sequence": ["sk-canary-private-credential"]},
        {"read_tool_sequence": ["create_case"]},
        {"provider": "sk-canary-private-credential"},
        {
            "events": [
                {
                    "kind": "tool",
                    "name": "sk-canary-private-credential",
                    "status": "completed",
                    "duration_ms": 1,
                }
            ]
        },
        {
            "events": [
                {"kind": "tool", "name": "create_case", "status": "completed", "duration_ms": 1}
            ]
        },
        {
            "events": [
                {
                    "kind": "model",
                    "name": "sk-canary-private-credential",
                    "status": "completed",
                    "duration_ms": 1,
                }
            ]
        },
        {
            "events": [
                {
                    "kind": "model",
                    "name": "different-model",
                    "status": "completed",
                    "duration_ms": 1,
                }
            ]
        },
        {"status": "sk-canary-private-credential"},
        {"error_category": "sk-canary-private-credential"},
        {"total_tokens": -1},
        {"duration_ms": float("inf")},
    ],
)
def test_corrupt_persisted_metadata_is_rejected_without_echoing_content(tmp_path, mutation):
    """Catch permissive summary schemas admitting arbitrary stored strings into case state."""
    store = ReasoningStore(tmp_path / "reasoning.sqlite3")
    store.claim("thread-1")
    payload = LiveReasoningSummary(model="gpt-4.1", status="completed", duration_ms=10).model_dump(
        mode="json"
    )
    payload.update(mutation)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE reasoning_summaries SET summary_json = ?", (json.dumps(payload),)
        )
    with pytest.raises(RuntimeError, match="Reasoning telemetry unavailable") as error:
        store.get("thread-1")
    assert "canary-private-credential" not in str(error.value)


def test_malformed_summary_json_has_a_constant_safe_error(tmp_path):
    store = ReasoningStore(tmp_path / "reasoning.sqlite3")
    store.claim("thread-1")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE reasoning_summaries SET summary_json = ?", ('{"sk-canary-private-credential":',)
        )
    with pytest.raises(RuntimeError, match="Reasoning telemetry unavailable") as error:
        store.get("thread-1")
    assert "canary-private-credential" not in str(error.value)


@pytest.mark.parametrize(
    "canary",
    [
        "sk-proj-0123456789abcdef0123456789abcdef",
        "sk" + "_live_0123456789abcdef0123456789abcdef",
        "gho_0123456789abcdef0123456789abcdef",
        "AIzaSy0123456789abcdef0123456789abcdef",
    ],
)
def test_model_identifier_rejects_credential_prefixes_without_helpful_secret_words(
    tmp_path, canary
):
    store = ReasoningStore(tmp_path / "reasoning.sqlite3")
    store.claim("thread-1")
    payload = LiveReasoningSummary(
        model=canary, status="completed", duration_ms=1
    ).model_dump_json()
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE reasoning_summaries SET summary_json = ?", (payload,))
    with pytest.raises(RuntimeError, match="Reasoning telemetry unavailable") as error:
        store.get("thread-1")
    assert canary not in str(error.value)


def test_actual_unreadable_database_returns_only_a_safe_error(tmp_path):
    path = tmp_path / "reasoning.sqlite3"
    path.write_bytes(b"not sqlite: sk-canary-private-credential")
    with pytest.raises(RuntimeError, match="Reasoning telemetry unavailable") as error:
        ReasoningStore(path).get("thread-1")
    assert "canary-private-credential" not in str(error.value)


def test_summary_store_claim_is_durable_and_never_repeats_an_unfinished_call(tmp_path):
    """Catch duplicate claims across adapters and loss of the sanitized result."""
    from recallops.ui.reasoning_store import ReasoningStore

    path = tmp_path / "reasoning.sqlite3"
    first = ReasoningStore(path)
    assert first.claim("thread-1") is True
    restarted = ReasoningStore(path)
    assert restarted.claim("thread-1") is False
    with pytest.raises(RuntimeError, match="in progress|interrupted"):
        restarted.get("thread-1")
    summary = LiveReasoningSummary(model="test-model", status="completed", duration_ms=10)
    first.finish("thread-1", summary)
    assert restarted.get("thread-1").model_dump(mode="json") == summary.model_dump(mode="json")
    assert restarted.get("other-thread") is None
    assert restarted.claim("thread-1") is False
