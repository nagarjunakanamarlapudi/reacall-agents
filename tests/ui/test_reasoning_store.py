import pytest

from recallops.llm.live_reasoning import LiveReasoningSummary


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
