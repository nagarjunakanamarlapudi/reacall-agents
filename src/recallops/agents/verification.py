"""Independent source checks gate every live claim before actionable state exists."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from recallops.agents.specialists import (
    assess_product_lots,
    assess_traceability,
    draft_containment,
    investigate_recall,
)
from recallops.llm.artifacts import (
    ROLES,
    LiveInvestigationRequest,
    ReadEvidenceReceipt,
    SpecialistClaims,
    VerificationResult,
    VerificationViolation,
    canonical_digest,
    project_specialist_claims,
    source_revision,
)
from recallops.models import CandidateProduct, LotMatch, RecallRecord
from recallops.paths import DATA_DIR
from recallops.services.recall_registry import RecallRegistryService
from recallops.services.traceability import TraceabilityService


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class TrustedEvidence:
    """Ephemeral independent records and comparison oracles; never model-supplied."""

    source_digest: str
    request_digest: str
    artifacts: dict[str, Any]
    projection: dict[str, Any]
    required_receipts: tuple[ReadEvidenceReceipt, ...]


@dataclass(frozen=True)
class VerifiedInvestigation:
    result: VerificationResult
    projection: dict[str, Any] | None


async def resolve_trusted_evidence(
    request: LiveInvestigationRequest, *, gateway=None
) -> TrustedEvidence:
    """Read the independently determined scope, never a model-selected evidence universe."""
    revision = source_revision()
    if revision != request.source_digest:
        raise ValueError("source revision mismatch")
    registry = RecallRegistryService(data_dir=DATA_DIR, source_mode="snapshot")
    trace = TraceabilityService(data_dir=DATA_DIR, source_mode="snapshot")
    receipts = []

    async def read(name, payload):
        target = gateway if gateway is not None else registry if name == "get_recall" else trace
        started = perf_counter()
        response = getattr(target, name)(**payload)
        if inspect.isawaitable(response):
            response = await response
        result = _json(response)
        if name in {"find_candidate_products", "match_lots"}:
            model = CandidateProduct if name == "find_candidate_products" else LotMatch
            result = [_json(model.model_validate(row)) for row in result]
        receipts.append(
            ReadEvidenceReceipt(
                name=name,
                status="completed",
                input_digest=canonical_digest(payload),
                result_digest=canonical_digest(result),
                source_digest=revision,
                duration_ms=(perf_counter() - started) * 1000,
            )
        )
        return result

    recall = RecallRecord.model_validate(
        await read("get_recall", {"recall_number": request.recall_number})
    )
    # These pure domain policies are comparison oracles. No returned model claim is
    # replaced with an oracle result on mismatch or on a missing role.
    intelligence = investigate_recall(recall)
    products = await read("find_candidate_products", {"predicate": intelligence.predicate})
    all_lots = await read("match_lots", {"predicate": intelligence.predicate})
    scope = set(request.scope_lot_ids)
    if scope - {row["lot_id"] for row in all_lots}:
        raise ValueError("scope unavailable")
    lots = [row for row in all_lots if not scope or row["lot_id"] in scope]
    matching = assess_product_lots(
        predicate=intelligence.predicate, candidate_products=products, candidate_lots=lots
    )
    relevant = [*matching.confirmed_lot_ids, *matching.ambiguous_lot_ids]
    events, inventory, reconciliations = [], [], []
    forward, backward = {}, {}
    for lot in relevant:
        lot_forward = await read("trace_forward", {"lot_id": lot})
        lot_backward = await read("trace_backward", {"lot_id": lot})
        events.extend(lot_forward)
        inventory.extend(await read("get_inventory", {"lot_id": lot}))
        reconciliations.append(await read("reconcile_units", {"lot_id": lot}))
        forward[lot] = [row["event_id"] for row in lot_forward]
        backward[lot] = [row["event_id"] for row in lot_backward]
    tracing = assess_traceability(
        lot_ids=relevant,
        events=events,
        inventory_positions=inventory,
        reconciliations=reconciliations,
    )
    if tracing.forward_traces != forward or tracing.backward_traces != backward:
        raise ValueError("source trace order mismatch")
    containment = draft_containment(
        case_id=request.case_id,
        expected_case_version=request.investigation_case_version,
        matching=matching,
        traceability=tracing,
    )
    artifacts = dict(
        zip(
            ROLES,
            [_json(item) for item in (intelligence, matching, tracing, containment)],
            strict=True,
        )
    )
    by_lot = {
        row.lot_id: list(
            dict.fromkeys(
                [*row.event_ids, *row.inventory_evidence_ids, *row.reconciliation_evidence_ids]
            )
        )
        for row in tracing.coverage
    }
    by_facility = {}
    for row in tracing.coverage:
        if row.lot_id not in matching.confirmed_lot_ids:
            continue
        for facility, ids in row.facility_evidence.items():
            by_facility.setdefault(facility, []).extend([*ids, *row.reconciliation_evidence_ids])
    by_facility = {key: list(dict.fromkeys(value)) for key, value in sorted(by_facility.items())}
    projection = {
        "recall": _json(recall),
        "recall_predicate": _json(intelligence.predicate),
        "candidate_products": products,
        "candidate_lots": lots,
        "match_decisions": [_json(row) for row in matching.decisions],
        "confirmed_lot_ids": matching.confirmed_lot_ids,
        "ambiguous_lot_ids": matching.ambiguous_lot_ids,
        "trace_events": events,
        "inventory_positions": inventory,
        "reconciliations": reconciliations,
        "forward_traces": forward,
        "backward_traces": backward,
        "required_facilities": sorted(by_facility),
        "evidence_by_lot": by_lot,
        "evidence_by_facility": by_facility,
        "evidence_gaps": tracing.evidence_gaps,
        "specialist_outputs": artifacts,
        "specialist_execution_order": list(ROLES),
        "official_evidence": {
            "provenance": recall.provenance,
            "recall_number": recall.recall_number,
            "citations": intelligence.citations,
            "source_url": recall.source_url,
            "sha256": recall.sha256,
        },
        "synthetic_evidence": {
            "origin": "SYNTHETIC_RETAILER_DIGITAL_TWIN",
            "lot_ids": relevant,
            "facility_ids": sorted(by_facility),
            "evidence_ids": list(dict.fromkeys(item for ids in by_lot.values() for item in ids)),
        },
    }
    if source_revision() != revision:
        raise ValueError("source changed during verification")
    return TrustedEvidence(revision, request.request_digest, artifacts, projection, tuple(receipts))


def verify_live_investigation(
    request: LiveInvestigationRequest,
    claims: SpecialistClaims,
    trusted_evidence: TrustedEvidence,
    *,
    receipts=(),
) -> VerifiedInvestigation:
    """Compare the original safe claim object, then release source values only on pass."""
    violations, criteria = [], []

    def violation(code, path):
        violations.append(VerificationViolation(code=code, path=path))

    try:
        request = LiveInvestigationRequest.model_validate_json(request.model_dump_json())
        claims = SpecialistClaims.model_validate_json(claims.model_dump_json())
        if (
            claims.request_digest != request.request_digest
            or claims.run_id != request.run_id
            or claims.context_digest != request.context.digest
            or claims.source_digest != request.source_digest
            or trusted_evidence.source_digest != request.source_digest
            or trusted_evidence.request_digest != request.request_digest
        ):
            raise ValueError("binding mismatch")
        criteria.append("binding")
    except ValueError:
        violation("binding_mismatch", "binding")
    if tuple(claims.completed_roles) != ROLES:
        violation("incomplete_roles", "roles")
    try:
        validated_receipts = tuple(
            ReadEvidenceReceipt.model_validate_json(row.model_dump_json()) for row in receipts
        )
        actual = {
            (row.name, row.input_digest, row.result_digest, row.source_digest)
            for row in validated_receipts
            if row.status == "completed"
        }
        required = {
            (row.name, row.input_digest, row.result_digest, row.source_digest)
            for row in trusted_evidence.required_receipts
        }
        if (
            not required
            or not required <= actual
            or any(row.status != "completed" for row in validated_receipts)
        ):
            raise ValueError("missing source read")
        criteria.append("receipts")
    except ValueError:
        violation("receipt_mismatch", "receipts")
    try:
        expected = project_specialist_claims(request, trusted_evidence.artifacts)
        for role in ("recall", "matching", "traceability", "containment"):
            supplied = getattr(claims, role).model_dump(mode="json")
            wanted = getattr(expected, role).model_dump(mode="json")
            if role == "containment":
                # Draft identifiers have no runtime authority. All target, type,
                # version and citation claims still require exact source support.
                for packet in (supplied, wanted):
                    for action in packet["proposed_actions"]:
                        action.pop("action_id")
            if supplied != wanted or (role == "matching" and not claims.matching.confirmed_lot_ids):
                violation(f"{role}_mismatch", role)
            else:
                criteria.append(role)
    except ValueError:
        violation("source_unavailable", "source")
    gaps = (*claims.recall.evidence_gaps, *claims.traceability.evidence_gaps)
    if any(row.code == "unclassified" for row in gaps):
        violation("unsupported_gap", "claims")
    result = VerificationResult(
        run_id=request.run_id,
        request_digest=request.request_digest,
        claims_digest=canonical_digest(claims),
        context_digest=request.context.digest,
        source_digest=request.source_digest,
        passed=not violations,
        criteria=tuple(criteria),
        violations=tuple(violations),
        evidence_gaps=tuple(gaps),
    )
    return VerifiedInvestigation(result, trusted_evidence.projection if result.passed else None)
