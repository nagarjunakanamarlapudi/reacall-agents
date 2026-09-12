"""Explicit OpenAI opt-in using the same read-only reasoning service as the UI."""

import json

from recallops.evaluation.orchestration_benchmark import (
    LIVE_READ_TOOLS,
    LiveProgram,
    LiveRunnerFactory,
    LiveUsage,
    _capture_reasoning_summary,
)
from recallops.llm import LLMSettings
from recallops.llm.live_reasoning import LiveReasoningService


def build_openai_live_factory(settings: LLMSettings) -> LiveRunnerFactory:
    """Construct lazily; provider construction and credentials remain in the shared service."""
    if settings.mode != "openai":
        raise ValueError("RECALLOPS_MODEL_MODE must be openai for live evaluation")

    async def invoke(inputs, capture):
        # Pass investigation inputs only, never the corpus expectations or grading oracle.
        question = "Investigate this read-only evaluation scope: " + json.dumps(
            inputs.model_dump(mode="json"), sort_keys=True
        )
        summary = await LiveReasoningService(settings).run(question, transport="direct")
        _capture_reasoning_summary(capture, summary)
        return LiveUsage(tokens=summary.total_tokens)

    return LiveRunnerFactory(
        provider=settings.provider,
        model=settings.model,
        factory=lambda: LiveProgram(invoke=invoke, exposed_tool_names=LIVE_READ_TOOLS),
    )
