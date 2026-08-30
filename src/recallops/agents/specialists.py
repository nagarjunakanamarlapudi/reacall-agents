"""Typed, deterministic specialists for the offline RecallOps reasoning plane."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from recallops.models import (
    CandidateProduct,
    LotMatch,
    MatchClassification,
    ProposedAction,
    Provenance,
    RecallPredicate,
    RecallRecord,
    Reconciliation,
    TraceEvent,
)

SYNTHETIC_LABEL = "SYNTHETIC — ACADEMIC DEMO"


class RecallIntelligence(BaseModel):
    recall_number: str
    predicate: RecallPredicate
    classification: str
    status: str
    source_provenance: Provenance
    citations: list[str] = Field(min_length=1)
    evidence_gaps: list[str] = Field(default_factory=list)


class MatchDecision(BaseModel):
    product_id: str
    lot_id: str
    classification: MatchClassification
    matched_fields: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    requires_human_review: bool = False
    evidence_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def ambiguity_requires_review(self) -> MatchDecision:
        if self.classification == "ambiguous" and not self.requires_human_review:
            raise ValueError("ambiguous match requires human review")
        if self.classification != "ambiguous" and self.requires_human_review:
            raise ValueError("only ambiguous matches require match review")
        return self


class ProductLotAssessment(BaseModel):
    decisions: list[MatchDecision]
    confirmed_lot_ids: list[str]
    ambiguous_lot_ids: list[str]

    @model_validator(mode="after")
    def summaries_match_decisions(self) -> ProductLotAssessment:
        confirmed = [
            item.lot_id for item in self.decisions if item.classification in {"exact", "probable"}
        ]
        ambiguous = [item.lot_id for item in self.decisions if item.classification == "ambiguous"]
        if self.confirmed_lot_ids != confirmed:
            raise ValueError("confirmed lot summary does not match decisions")
        if self.ambiguous_lot_ids != ambiguous:
            raise ValueError("ambiguous lot summary does not match decisions")
        return self


class LotTraceCoverage(BaseModel):
    lot_id: str
    facility_ids: list[str]
    event_ids: list[str]
    reconciliation_evidence_ids: list[str]
    unaccounted_units: int = Field(ge=0)
    complete: bool


class TraceabilityAssessment(BaseModel):
    lot_ids: list[str]
    affected_facilities: list[str]
    coverage: list[LotTraceCoverage]
    reconciliations: list[Reconciliation]
    evidence_ids: list[str]
    evidence_gaps: list[str]

    @model_validator(mode="after")
    def evidence_is_declared(self) -> TraceabilityAssessment:
        known = set(self.evidence_ids)
        referenced = {
            evidence_id
            for item in self.coverage
            for evidence_id in (*item.event_ids, *item.reconciliation_evidence_ids)
        }
        if not referenced <= known:
            raise ValueError("coverage cites unknown evidence")
        return self


class CommunicationDraft(BaseModel):
    audience: Literal["facility", "food_safety_manager"]
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def synthetic_context_is_visible(self) -> CommunicationDraft:
        if SYNTHETIC_LABEL not in self.body:
            raise ValueError("communication must retain the synthetic demo label")
        return self


class ContainmentProposal(BaseModel):
    proposed_actions: list[ProposedAction]
    communication_drafts: list[CommunicationDraft]
    all_cited_evidence_ids: list[str]
    executed: Literal[False] = False

    @model_validator(mode="after")
    def citations_stay_in_quarantined_context(self) -> ContainmentProposal:
        allowed = set(self.all_cited_evidence_ids)
        cited = {
            evidence_id for draft in self.communication_drafts for evidence_id in draft.evidence_ids
        } | {evidence_id for action in self.proposed_actions for evidence_id in action.evidence_ids}
        if not cited <= allowed:
            raise ValueError("draft cites unknown evidence")
        return self


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _extract_primary_upc(description: str) -> str:
    scoped = re.search(
        r"Kroger,\s*Large 12 Eggs.*?UPC\s+([\d\s-]+?)\.",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fallback = re.search(r"UPC\s+([\d\s-]+?)\.", description, flags=re.IGNORECASE)
    match = scoped or fallback
    if match is None:
        raise ValueError("recall lacks a parseable UPC")
    return _digits(match.group(1))


def investigate_recall(recall: RecallRecord) -> RecallIntelligence:
    """Extract the pinned product predicate without consulting synthetic operations."""
    payload = recall.payload
    description = str(payload.get("product_description", ""))
    code_info = str(payload.get("code_info", ""))
    plant_match = re.search(r"Codes\s+([^\s]+)\s+or\s+([^\s]+)", code_info, re.IGNORECASE)
    date_match = re.search(r"Julian Date between\s+(\d{1,3})\s+and\s+(\d{1,3})", code_info)
    gaps: list[str] = []
    if plant_match is None:
        gaps.append("Plant-code scope is absent from authoritative recall evidence")
    if date_match is None:
        gaps.append("Julian-date scope is absent from authoritative recall evidence")
    if gaps:
        raise ValueError("; ".join(gaps))

    geography = [
        item.strip()
        for item in str(payload.get("distribution_pattern", "")).split(",")
        if item.strip()
    ]
    predicate = RecallPredicate(
        product_terms=["Grade A", "Large", "Eggs"],
        upcs=[_extract_primary_upc(description)],
        plant_codes=[plant_match.group(1), plant_match.group(2)],
        julian_start=int(date_match.group(1)),
        julian_end=int(date_match.group(2)),
        geography=geography,
        hazard=str(payload.get("reason_for_recall", "Unknown hazard")),
    )
    return RecallIntelligence(
        recall_number=recall.recall_number,
        predicate=predicate,
        classification=str(payload.get("classification", "Unknown")),
        status=str(payload.get("status", "Unknown")),
        source_provenance=recall.provenance,
        citations=[f"openfda:{recall.recall_number}"],
        evidence_gaps=[],
    )


def _derive_lot_classification(
    lot: LotMatch, product: CandidateProduct, predicate: RecallPredicate
) -> MatchClassification:
    within_window = predicate.julian_start <= lot.julian_date <= predicate.julian_end
    exact_plant = lot.plant_code in predicate.plant_codes
    ambiguous_plant = (
        lot.plant_code.endswith("?") and lot.plant_code.rstrip("?") in predicate.plant_codes
    )
    if not within_window or not product.score:
        return "rejected"
    if ambiguous_plant:
        return "ambiguous"
    if exact_plant and product.classification == "exact":
        return "exact"
    if exact_plant:
        return "probable"
    return "rejected"


def _match_rationale(
    lot: LotMatch, product: CandidateProduct, predicate: RecallPredicate
) -> tuple[list[str], str]:
    within_window = predicate.julian_start <= lot.julian_date <= predicate.julian_end
    exact_plant = lot.plant_code in predicate.plant_codes
    ambiguous_plant = (
        lot.plant_code.endswith("?") and lot.plant_code.rstrip("?") in predicate.plant_codes
    )
    fields: list[str] = []
    reasons: list[str] = []
    if product.classification == "exact":
        fields.append("upc")
        reasons.append("UPC exact")
    elif product.classification == "probable":
        reasons.append("UPC one-digit difference")
    else:
        reasons.append("UPC does not match")
    if exact_plant:
        fields.append("plant_code")
        reasons.append("plant code exact")
    elif ambiguous_plant:
        reasons.append(f"plant code {lot.plant_code} uncertain")
    else:
        reasons.append(f"plant code {lot.plant_code} outside recall scope")
    if within_window:
        fields.append("julian_date")
        reasons.append(
            f"Julian date {lot.julian_date} within {predicate.julian_start}–{predicate.julian_end}"
        )
    else:
        reasons.append(
            f"Julian date {lot.julian_date} outside {predicate.julian_start}–{predicate.julian_end}"
        )
    return fields, "; ".join(reasons)


def assess_product_lots(
    *,
    predicate: RecallPredicate,
    candidate_products: Sequence[CandidateProduct | dict],
    candidate_lots: Sequence[LotMatch | dict],
) -> ProductLotAssessment:
    """Validate and explain product/lot classifications from read-only observations."""
    products = [CandidateProduct.model_validate(item) for item in candidate_products]
    lots = [LotMatch.model_validate(item) for item in candidate_lots]
    product_by_id = {item.product_id: item for item in products}
    decisions: list[MatchDecision] = []
    for lot in lots:
        product = product_by_id.get(lot.product_id)
        if product is None:
            raise ValueError(f"lot {lot.lot_id} references unknown candidate product")
        derived = _derive_lot_classification(lot, product, predicate)
        if lot.classification != derived:
            raise ValueError(f"lot {lot.lot_id} classification conflicts with predicate evidence")
        fields, rationale = _match_rationale(lot, product, predicate)
        decisions.append(
            MatchDecision(
                product_id=lot.product_id,
                lot_id=lot.lot_id,
                classification=derived,
                matched_fields=fields,
                rationale=rationale,
                requires_human_review=derived == "ambiguous",
                evidence_ids=[lot.product_id, lot.lot_id],
            )
        )
    return ProductLotAssessment(
        decisions=decisions,
        confirmed_lot_ids=[
            item.lot_id for item in decisions if item.classification in {"exact", "probable"}
        ],
        ambiguous_lot_ids=[item.lot_id for item in decisions if item.classification == "ambiguous"],
    )


def assess_traceability(
    *,
    lot_ids: Sequence[str],
    events: Sequence[TraceEvent | dict],
    reconciliations: Sequence[Reconciliation | dict],
) -> TraceabilityAssessment:
    """Assess lineage, facility coverage, and reconciliation inside delegated lot scope."""
    delegated_lots = list(dict.fromkeys(lot_ids))
    allowed_lots = set(delegated_lots)
    typed_events = [TraceEvent.model_validate(event) for event in events]
    typed_reconciliations = [Reconciliation.model_validate(item) for item in reconciliations]
    event_ids = [event.event_id for event in typed_events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("duplicate trace event identifiers")
    for event in typed_events:
        if event.lot_id not in allowed_lots:
            raise ValueError(f"event {event.event_id} is outside delegated lot scope")
    reconciliation_by_lot: dict[str, Reconciliation] = {}
    for reconciliation in typed_reconciliations:
        if reconciliation.lot_id not in allowed_lots:
            raise ValueError(
                f"reconciliation {reconciliation.lot_id} is outside delegated lot scope"
            )
        if reconciliation.lot_id in reconciliation_by_lot:
            raise ValueError(f"duplicate reconciliation for {reconciliation.lot_id}")
        reconciliation_by_lot[reconciliation.lot_id] = reconciliation

    known_events = set(event_ids)
    gaps: list[str] = []
    coverage: list[LotTraceCoverage] = []
    all_evidence: list[str] = []
    all_facilities: set[str] = set()
    for lot_id in delegated_lots:
        lot_events = [event for event in typed_events if event.lot_id == lot_id]
        lot_event_ids = [event.event_id for event in lot_events]
        facilities = sorted(
            {
                facility
                for event in lot_events
                for facility in (event.from_facility, event.to_facility)
                if facility
            }
        )
        all_facilities.update(facilities)
        lot_gaps: list[str] = []
        if not any(event.event_type == "receiving" for event in lot_events):
            lot_gaps.append(f"{lot_id}: missing receiving event")
        for event in lot_events:
            if event.parent_event_id and event.parent_event_id not in known_events:
                lot_gaps.append(
                    f"{lot_id}: event {event.event_id} missing parent {event.parent_event_id}"
                )
        reconciliation = reconciliation_by_lot.get(lot_id)
        if reconciliation is None:
            lot_gaps.append(f"{lot_id}: missing reconciliation")
            reconciliation_evidence: list[str] = []
            unaccounted = 0
        else:
            reconciliation_evidence = reconciliation.evidence_ids
            unaccounted = reconciliation.unaccounted
            if unaccounted:
                lot_gaps.append(f"{lot_id}: {unaccounted} unaccounted units")
            if not reconciliation.verified:
                lot_gaps.append(f"{lot_id}: reconciliation evidence is unverified")
        gaps.extend(lot_gaps)
        coverage.append(
            LotTraceCoverage(
                lot_id=lot_id,
                facility_ids=facilities,
                event_ids=lot_event_ids,
                reconciliation_evidence_ids=reconciliation_evidence,
                unaccounted_units=unaccounted,
                complete=not lot_gaps,
            )
        )
        all_evidence.extend(lot_event_ids)
        all_evidence.extend(reconciliation_evidence)
    return TraceabilityAssessment(
        lot_ids=delegated_lots,
        affected_facilities=sorted(all_facilities),
        coverage=coverage,
        reconciliations=[
            reconciliation_by_lot[lot_id]
            for lot_id in delegated_lots
            if lot_id in reconciliation_by_lot
        ],
        evidence_ids=list(dict.fromkeys(all_evidence)),
        evidence_gaps=list(dict.fromkeys(gaps)),
    )


def draft_containment(
    *,
    case_id: str,
    expected_case_version: int,
    matching: ProductLotAssessment,
    traceability: TraceabilityAssessment,
) -> ContainmentProposal:
    """Draft evidence-backed actions; execution remains an approval-gated graph concern."""
    if not traceability.evidence_ids:
        raise ValueError("containment requires trace evidence")
    coverage_by_lot = {item.lot_id: item for item in traceability.coverage}

    def evidence_for_lots(lot_ids: Sequence[str]) -> list[str]:
        return list(
            dict.fromkeys(
                evidence_id
                for lot_id in lot_ids
                for evidence_id in (
                    coverage_by_lot.get(lot_id).event_ids
                    if coverage_by_lot.get(lot_id) is not None
                    else []
                )
            )
        )

    confirmed_evidence = evidence_for_lots(matching.confirmed_lot_ids)
    if matching.confirmed_lot_ids and not confirmed_evidence:
        raise ValueError("confirmed lots lack trace evidence")
    facility_evidence = list(traceability.evidence_ids[:8])
    proposed_actions: list[ProposedAction] = []
    if matching.confirmed_lot_ids:
        proposed_actions.append(
            ProposedAction(
                action_id=f"{case_id}-hold-v{expected_case_version}",
                action_type="apply_inventory_hold",
                case_id=case_id,
                target_ids=matching.confirmed_lot_ids,
                rationale="Hold only confirmed exact/probable lots pending human authorization.",
                evidence_ids=confirmed_evidence,
                expected_case_version=expected_case_version,
            )
        )
    if traceability.affected_facilities:
        proposed_actions.append(
            ProposedAction(
                action_id=f"{case_id}-facility-tasks-v{expected_case_version}",
                action_type="create_facility_tasks",
                case_id=case_id,
                target_ids=traceability.affected_facilities,
                rationale="Request inventory verification and acknowledgement at traced facilities.",
                evidence_ids=facility_evidence,
                expected_case_version=expected_case_version,
            )
        )

    review_evidence = list(traceability.evidence_ids[:4])
    drafts = [
        CommunicationDraft(
            audience="facility",
            subject="Recall inventory verification request",
            body=(
                f"{SYNTHETIC_LABEL}. Verify traced inventory for case {case_id}; do not act "
                "outside the approved task and report discrepancies."
            ),
            evidence_ids=review_evidence,
        ),
        CommunicationDraft(
            audience="food_safety_manager",
            subject="Containment proposal ready for human review",
            body=(
                f"{SYNTHETIC_LABEL}. Review confirmed holds, ambiguous matches, facility coverage, "
                "and reconciliation gaps before authorizing any simulated write."
            ),
            evidence_ids=review_evidence,
        ),
    ]
    cited = list(
        dict.fromkeys(
            evidence_id
            for identifiers in (
                *(action.evidence_ids for action in proposed_actions),
                *(draft.evidence_ids for draft in drafts),
            )
            for evidence_id in identifiers
        )
    )
    return ContainmentProposal(
        proposed_actions=proposed_actions,
        communication_drafts=drafts,
        all_cited_evidence_ids=cited,
        executed=False,
    )
