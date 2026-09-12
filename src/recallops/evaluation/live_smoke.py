"""One bounded OpenAI investigation, separate from the 24-case benchmark contract."""

from pathlib import Path
from time import perf_counter

from recallops.agents.deep_supervisor import SUPERVISOR_PROMPT
from recallops.agents.verification import resolve_trusted_evidence, verify_live_investigation
from recallops.llm import LLMSettings
from recallops.llm.artifacts import ROLES, build_live_request, canonical_digest
from recallops.llm.live_reasoning import LiveReasoningService
from recallops.paths import DATA_DIR
from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway


async def run_live_smoke(settings: LLMSettings, output_path: Path) -> dict:
    """Measure exactly one case; never rewrite a benchmark or manufacture quality scores."""
    import json

    if settings.mode != "openai":
        raise ValueError("live smoke requires OpenAI mode")
    # Reserve the output before spending; existing evidence is never overwritten.
    with output_path.open("x") as output:
        started = perf_counter()
        question = "Identify affected lots, trace facilities, and contain H-1230-2026."
        retriever = AgenticRetriever(ClosedRetrievalGateway.direct(data_dir=DATA_DIR))
        rag = await retriever.resume(
            retriever.start(question, authoritative_facts={"recall_number": "H-1230-2026"})
        )
        request = build_live_request(
            case_id="CASE-LIVE",
            thread_id="EVAL-SMOKE-LIVE",
            case_version=0,
            recall_number="H-1230-2026",
            question=question,
            scope_lot_ids=["LOT-EXACT-170", "LOT-PROBABLE-160", "LOT-AMBIG-175", "LOT-REJECT-190"],
            rag_result=rag.model_dump(mode="json"),
        )
        result = await LiveReasoningService(settings).run(request, transport="direct")
        verified = None
        if result.status == "success":
            try:
                verified = verify_live_investigation(
                    request,
                    result.claims,
                    await resolve_trusted_evidence(request),
                    receipts=result.receipts,
                )
            except Exception:
                pass  # Source errors cannot earn verification credit or expose exception prose.
        summary = result.summary
        passed = bool(verified and verified.result.passed)
        report = {
            "schema_version": "1.0",
            "evaluation_kind": "one_case_live_smoke",
            "excluded_from_offline_gate": True,
            "executed_case_count": 1,
            "recall_number": request.recall_number,
            "provider": summary.provider,
            "model_sha256": canonical_digest(settings.model),
            "prompt_sha256": canonical_digest(SUPERVISOR_PROMPT),
            "source_sha256": request.source_digest,
            "request_sha256": result.request_digest,
            "claims_sha256": canonical_digest(result.claims.model_dump(mode="json"))
            if result.claims
            else None,
            "provider_status": summary.status,
            "result_status": result.status,
            "error_category": summary.error_category,
            "verification_passed": passed,
            "plan_observed": tuple(summary.plan) == ROLES,
            "ordered_specialists": tuple(summary.specialist_sequence) == ROLES,
            "specialist_count": len(summary.specialist_sequence),
            "read_receipt_count": len(result.receipts),
            "model_call_count": sum(event.kind == "model" for event in summary.events),
            "tokens": summary.total_tokens,
            "cost": None,
            "duration_ms": (perf_counter() - started) * 1000,
            "passed": passed
            and tuple(summary.plan) == ROLES
            and tuple(summary.specialist_sequence) == ROLES,
        }
        report["report_sha256"] = canonical_digest(report)
        output.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report
