"""Explicit OpenAI opt-in using the same read-only reasoning service as the UI."""

from recallops.agents.verification import resolve_trusted_evidence, verify_live_investigation
from recallops.evaluation.orchestration_benchmark import (
    LIVE_READ_TOOLS,
    LiveProgram,
    LiveRunnerFactory,
    LiveUsage,
    _capture_live_investigation,
)
from recallops.llm import LLMSettings
from recallops.llm.artifacts import build_live_request
from recallops.llm.live_reasoning import LiveReasoningService
from recallops.paths import DATA_DIR
from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway


def build_openai_live_factory(settings: LLMSettings) -> LiveRunnerFactory:
    """Construct lazily; provider construction and credentials remain in the shared service."""
    if settings.mode != "openai":
        raise ValueError("RECALLOPS_MODEL_MODE must be openai for live evaluation")

    async def invoke(inputs, capture):
        # Pass investigation inputs only, never the corpus expectations or grading oracle.
        retriever = AgenticRetriever(ClosedRetrievalGateway.direct(data_dir=DATA_DIR))
        rag = await retriever.resume(
            retriever.start(
                inputs.question, authoritative_facts={"recall_number": inputs.recall_number}
            )
        )
        request = build_live_request(
            case_id=inputs.id,
            thread_id=f"EVAL-{inputs.id}",
            case_version=0,
            recall_number=inputs.recall_number,
            question=inputs.question,
            scope_lot_ids=list(inputs.lot_ids),
            rag_result=rag.model_dump(mode="json"),
        )
        result = await LiveReasoningService(settings).run(request, transport="direct")
        accepted = None
        if result.status == "success":
            try:
                accepted = verify_live_investigation(
                    request,
                    result.claims,
                    await resolve_trusted_evidence(request),
                    receipts=result.receipts,
                )
            except Exception:
                pass  # No source support means no evidence or completion credit.
        _capture_live_investigation(capture, result, accepted)
        return LiveUsage(tokens=result.summary.total_tokens)

    return LiveRunnerFactory(
        provider=settings.provider,
        model=settings.model,
        factory=lambda: LiveProgram(invoke=invoke, exposed_tool_names=LIVE_READ_TOOLS),
    )
