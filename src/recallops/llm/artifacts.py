"""Bounded structural claims: model prose never crosses the durable boundary.

The pinned-notice policy requires exact source extraction. Text whose equality
matters is represented by a source field and a digest; displays resolve the
accepted value from source records. Communication wording is application-owned.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from recallops.agents.deep_supervisor import SupervisorResponse
from recallops.agents.specialists import (
    ContainmentProposal,
    ProductLotAssessment,
    RecallIntelligence,
    TraceabilityAssessment,
)
from recallops.paths import DATA_DIR

ROLES = (
    "recall-intelligence",
    "product-lot-matching",
    "traceability-reconciliation",
    "containment-communications",
)
ROLE_MODELS = dict(
    zip(
        ROLES,
        (RecallIntelligence, ProductLotAssessment, TraceabilityAssessment, ContainmentProposal),
        strict=True,
    )
)
Role = Literal[
    "recall-intelligence",
    "product-lot-matching",
    "traceability-reconciliation",
    "containment-communications",
]
ReadName = Literal[
    "search_recalls",
    "get_recall",
    "get_product_metadata",
    "find_candidate_products",
    "match_lots",
    "trace_forward",
    "trace_backward",
    "get_inventory",
    "get_sales",
    "reconcile_units",
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_digest(value: Any) -> str:
    def plain(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, Mapping):
            return {key: plain(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(val) for val in item]
        return item

    return hashlib.sha256(
        (
            json.dumps(
                plain(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode()
    ).hexdigest()


def _identifier(value: Any) -> str:
    if (
        type(value) is not str
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.?/-]{0,127}", value)
        or re.search(
            r"(?i)((sk|pk)[-_]|bearer|api[-_]?key|password|secret|credential|token|authorization|AKIA|ASIA|AIza|gh[pousr]_|github_pat_|xox[baprs]-|eyJ)",
            value,
        )
    ):
        raise ValueError("invalid evidence identifier")
    return value


Identifier = Annotated[str, BeforeValidator(_identifier)]
Identifiers = Annotated[tuple[Identifier, ...], Field(max_length=512)]


class SafeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )

    @field_validator("schema_version", "executed", mode="before", check_fields=False)
    @classmethod
    def exact_literals(cls, value, info):
        if info.field_name == "schema_version" and (type(value) is not int or value != 1):
            raise ValueError("schema version must be exact")
        if info.field_name == "executed" and value is not False:
            raise ValueError("execution must be exactly false")
        return value

    @field_validator("*", mode="after")
    @classmethod
    def freeze_mappings(cls, value):
        return MappingProxyType(dict(value)) if isinstance(value, Mapping) else value

    @field_serializer("*", when_used="json")
    def serialize_mappings(self, value):
        return dict(value) if isinstance(value, Mapping) else value

    @model_validator(mode="after")
    def bounded_unique(self):
        def walk(value):
            if isinstance(value, Mapping):
                if len(value) > 512:
                    raise ValueError("claim mapping exceeds budget")
                for entry in value.values():
                    walk(entry)
            elif isinstance(value, (list, tuple)):
                if len(value) > 512:
                    raise ValueError("claim collection exceeds budget")
                if all(isinstance(entry, str) for entry in value) and len(set(value)) != len(value):
                    raise ValueError("duplicate claim values")
                for entry in value:
                    walk(entry)

        walk(self.__dict__)
        return self


class SourceTextClaim(SafeModel):
    citation_id: Identifier
    field_path: Annotated[
        str,
        Field(
            pattern=r"^(predicate\.(product_terms|geography|hazard)(\.[0-9]{1,3})?|official_products\.[0-9]{1,3}\.description|classification|status)$"
        ),
    ]
    value_digest: Digest


class GapClaim(SafeModel):
    code: Literal[
        "unaccounted_units",
        "missing_receiving",
        "missing_reconciliation",
        "unverified_reconciliation",
        "unclassified",
        "retrieval_gap",
        "ambiguous_lot",
    ]
    lot_id: Identifier | None = None
    units: Annotated[int, Field(ge=0, le=1_000_000_000)] | None = None
    digest: Digest | None = None


class RetrievalEntry(SafeModel):
    citation_id: Identifier
    source_class: Literal["official", "synthetic"]
    origin: Literal[
        "OFFICIAL_OPENFDA_SNAPSHOT", "OFFICIAL_POLICY_REFERENCE", "SYNTHETIC_RETAILER_DIGITAL_TWIN"
    ]
    record_id: Identifier
    record_type: Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
    source_url: Annotated[str, Field(min_length=1, max_length=1024)]
    content_hash: Digest
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    truncated: bool


class RetrievalContext(SafeModel):
    entries: Annotated[tuple[RetrievalEntry, ...], Field(max_length=8)]
    source_digest: Digest
    coverage_satisfied: bool
    stop_reason: Literal[
        "coverage_satisfied",
        "coverage_satisfied_after_rewrite",
        "evidence_gap_after_rewrite",
        "budget_exhausted",
        "progress_stalled",
        "empty_query",
    ]
    rewrite_used: bool
    evidence_gaps: Annotated[tuple[GapClaim, ...], Field(max_length=32)]
    digest: Digest

    @model_validator(mode="after")
    def bound_context(self):
        payload = self.model_dump(mode="json", exclude={"digest"})
        if canonical_digest(payload) != self.digest:
            raise ValueError("retrieval digest mismatch")
        if sum(len(row.text) for row in self.entries) > 24_576:
            raise ValueError("retrieval aggregate budget exceeded")
        if len({row.citation_id for row in self.entries}) != len(self.entries):
            raise ValueError("duplicate retrieval citations")
        return self


class LiveInvestigationRequest(SafeModel):
    schema_version: Literal[1] = 1
    case_id: Identifier
    thread_id: Identifier
    investigation_case_version: Annotated[int, Field(ge=0)]
    recall_number: Identifier
    question: Annotated[str, Field(min_length=1, max_length=8192)]
    scope_lot_ids: Annotated[Identifiers, Field(max_length=64)]
    source_digest: Digest
    context: RetrievalContext
    run_id: Digest
    request_digest: Digest

    @model_validator(mode="after")
    def bound_request(self):
        payload = self.model_dump(mode="json", exclude={"run_id", "request_digest"})
        digest = canonical_digest(payload)
        if self.source_digest != self.context.source_digest or self.request_digest != digest:
            raise ValueError("request binding mismatch")
        if self.run_id != canonical_digest({"request": digest, "schema": 1}):
            raise ValueError("run binding mismatch")
        return self


class PredicateClaims(SafeModel):
    product_terms: tuple[SourceTextClaim, ...]
    upcs: Identifiers
    plant_codes: Identifiers
    julian_start: Annotated[int, Field(ge=1, le=366)]
    julian_end: Annotated[int, Field(ge=1, le=366)]
    geography: tuple[SourceTextClaim, ...]
    hazard: SourceTextClaim


class OfficialProductClaim(SafeModel):
    item_number: Annotated[int, Field(ge=1, le=128)]
    description: SourceTextClaim
    upc: Identifier | None


class RecallClaims(SafeModel):
    recall_number: Identifier
    predicate: PredicateClaims
    official_products: tuple[OfficialProductClaim, ...]
    classification: SourceTextClaim
    status: SourceTextClaim
    source_provenance: Literal["OFFICIAL_OPENFDA_SNAPSHOT", "LIVE_OPENFDA"]
    citations: Identifiers
    evidence_gaps: tuple[GapClaim, ...]


class MatchClaim(SafeModel):
    product_id: Identifier
    lot_id: Identifier
    classification: Literal["exact", "probable", "ambiguous", "rejected"]
    product_score: Annotated[float, Field(ge=0, le=1)]
    product_classification: Literal["exact", "probable", "rejected"]
    matched_fields: tuple[Literal["upc", "plant_code", "julian_date"], ...]
    requires_human_review: bool
    evidence_ids: Identifiers


class MatchingClaims(SafeModel):
    decisions: tuple[MatchClaim, ...]
    confirmed_lot_ids: Identifiers
    ambiguous_lot_ids: Identifiers

    @model_validator(mode="after")
    def unique_candidates(self):
        if len({row.lot_id for row in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate candidate claims")
        return self


class CoverageClaim(SafeModel):
    lot_id: Identifier
    facility_ids: Identifiers
    event_ids: Identifiers
    forward_event_ids: Identifiers
    backward_event_ids: Identifiers
    inventory_evidence_ids: Identifiers
    reconciliation_evidence_ids: Identifiers
    facility_evidence: Mapping[Identifier, Identifiers]
    unaccounted_units: Annotated[int, Field(ge=0, le=1_000_000_000)]
    complete: bool


Quantity = Annotated[int, Field(ge=0, le=1_000_000_000)]
Component = Literal[
    "received", "on_hand", "quarantined", "sold", "returned", "disposed", "unaccounted"
]


class ReconciliationClaim(SafeModel):
    lot_id: Identifier
    received: Quantity
    on_hand: Quantity
    quarantined: Quantity
    sold: Quantity
    returned: Quantity
    disposed: Quantity
    unaccounted: Quantity
    evidence_ids: Identifiers
    component_evidence: Mapping[Component, Identifiers]
    verified: bool


class TraceabilityClaims(SafeModel):
    lot_ids: Identifiers
    affected_facilities: Identifiers
    coverage: tuple[CoverageClaim, ...]
    forward_traces: Mapping[Identifier, Identifiers]
    backward_traces: Mapping[Identifier, Identifiers]
    reconciliations: tuple[ReconciliationClaim, ...]
    evidence_ids: Identifiers
    evidence_gaps: tuple[GapClaim, ...]


class ActionClaim(SafeModel):
    action_id: Identifier
    action_type: Literal[
        "create_case",
        "apply_inventory_hold",
        "create_facility_tasks",
        "record_acknowledgment",
        "record_disposition",
        "close_case",
    ]
    case_id: Identifier
    target_ids: Identifiers
    evidence_ids: Identifiers
    evidence_by_target: Mapping[Identifier, Identifiers]
    expected_case_version: Annotated[int, Field(ge=0)]


class CommunicationClaim(SafeModel):
    audience: Literal["facility", "food_safety_manager"]
    target_ids: Identifiers
    evidence_by_target: Mapping[Identifier, Identifiers]
    evidence_ids: Identifiers


class ContainmentClaims(SafeModel):
    proposed_actions: tuple[ActionClaim, ...]
    communication_drafts: tuple[CommunicationClaim, ...]
    all_cited_evidence_ids: Identifiers
    executed: Literal[False]


class SpecialistClaims(SafeModel):
    schema_version: Literal[1] = 1
    request_digest: Digest
    context_digest: Digest
    source_digest: Digest
    run_id: Digest
    completed_roles: tuple[Role, ...]
    recall: RecallClaims
    matching: MatchingClaims
    traceability: TraceabilityClaims
    containment: ContainmentClaims


class ReadEvidenceReceipt(SafeModel):
    name: ReadName
    status: Literal["completed", "failed"]
    input_digest: Digest
    result_digest: Digest
    source_digest: Digest
    duration_ms: Annotated[float, Field(ge=0)]


class VerificationViolation(SafeModel):
    code: Literal[
        "binding_mismatch",
        "source_unavailable",
        "receipt_mismatch",
        "recall_mismatch",
        "matching_mismatch",
        "traceability_mismatch",
        "containment_mismatch",
        "incomplete_roles",
        "unsupported_gap",
        "invalid_claims",
    ]
    path: Literal[
        "binding",
        "source",
        "receipts",
        "recall",
        "matching",
        "traceability",
        "containment",
        "roles",
        "claims",
    ]


class VerificationResult(SafeModel):
    schema_version: Literal[1] = 1
    verifier: Literal["independent-source-verifier-v1"] = "independent-source-verifier-v1"
    investigation_source: Literal["openai", "deterministic", "deterministic_fallback"] = "openai"
    run_id: Digest
    request_digest: Digest
    claims_digest: Digest
    context_digest: Digest
    source_digest: Digest
    passed: bool
    criteria: tuple[
        Literal["binding", "receipts", "recall", "matching", "traceability", "containment"], ...
    ]
    violations: tuple[VerificationViolation, ...]
    evidence_gaps: tuple[GapClaim, ...]

    @model_validator(mode="after")
    def honest_outcome(self):
        if self.passed != (not self.violations and len(self.criteria) == 6):
            raise ValueError("verification outcome contradicts criteria")
        return self


class LiveExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    kind: Literal["model", "tool"]
    name: Identifier
    status: Literal["completed", "failed"]
    duration_ms: Annotated[float, Field(ge=0)]


def _immutable_sequence(value):
    if type(value) not in {list, tuple}:
        raise ValueError("execution sequence must be a plain list or tuple")
    return tuple(value)


class LiveReasoningSummary(BaseModel):
    """Bounded execution telemetry; evidence acceptance is reported separately."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    provider: Literal["openai"] = "openai"
    model: Identifier
    status: Literal["completed", "failed"]
    plan: Annotated[tuple[Role, ...], BeforeValidator(_immutable_sequence)] = Field(
        default=(), max_length=4
    )
    specialist_sequence: Annotated[tuple[Role, ...], BeforeValidator(_immutable_sequence)] = Field(
        default=(), max_length=4
    )
    read_tool_sequence: Annotated[tuple[ReadName, ...], BeforeValidator(_immutable_sequence)] = (
        Field(default=(), max_length=128)
    )
    response_summary: SupervisorResponse | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    duration_ms: Annotated[float, Field(ge=0)]
    fallback_used: bool = False
    error_category: (
        Literal[
            "authentication",
            "rate_limit",
            "timeout",
            "invalid_response",
            "provider_error",
            "budget_exceeded",
        ]
        | None
    ) = None
    events: Annotated[tuple[LiveExecutionEvent, ...], BeforeValidator(_immutable_sequence)] = Field(
        default=(), max_length=256
    )

    @model_validator(mode="after")
    def known_events(self):
        tools = {
            "write_todos",
            "task",
            "ls",
            "read_file",
            "search_recalls",
            "get_recall",
            "get_product_metadata",
            "find_candidate_products",
            "match_lots",
            "trace_forward",
            "trace_backward",
            "get_inventory",
            "get_sales",
            "reconcile_units",
        }
        if any(
            (row.kind == "model" and row.name != self.model)
            or (row.kind == "tool" and row.name not in tools)
            for row in self.events
        ):
            raise ValueError("unknown execution event")
        return self


class LiveInvestigationResult(SafeModel):
    status: Literal["success", "semantic_failure", "execution_failure"]
    summary: LiveReasoningSummary
    request_digest: Digest
    context_digest: Digest
    source_digest: Digest
    run_id: Digest
    claims: SpecialistClaims | None = None
    claims_digest: Digest | None = None
    receipts: Annotated[tuple[ReadEvidenceReceipt, ...], Field(max_length=128)] = ()
    failure_category: Literal["invalid_claims", "provider_execution"] | None = None

    @model_validator(mode="after")
    def exact_success(self):
        if self.status == "success":
            if (
                self.claims is None
                or self.failure_category is not None
                or self.summary.status != "completed"
            ):
                raise ValueError("live success requires complete claims")
            if self.claims_digest != canonical_digest(self.claims):
                raise ValueError("claims digest mismatch")
            if any(
                getattr(self, field) != getattr(self.claims, field)
                for field in ("request_digest", "context_digest", "source_digest", "run_id")
            ):
                raise ValueError("result binding mismatch")
        elif self.claims is not None or self.claims_digest is not None or self.receipts:
            raise ValueError("failed run must discard partial evidence")
        elif self.failure_category is None:
            raise ValueError("failed run requires category")
        elif self.summary.status != "failed" or self.failure_category != (
            "provider_execution" if self.status == "execution_failure" else "invalid_claims"
        ):
            raise ValueError("failed run contradicts its execution summary")
        return self


def source_revision() -> str:
    """Resolve the same validated frozen files for RAG, MCP reads, and verification."""
    from recallops.data.loaders import load_demo_dataset, load_recall_snapshot
    from recallops.retrieval.corpus import KnowledgeCorpus

    return canonical_digest(
        {
            "recall": load_recall_snapshot(data_dir=DATA_DIR),
            "dataset": load_demo_dataset(DATA_DIR),
            "corpus": KnowledgeCorpus.load(data_dir=DATA_DIR).manifest.corpus_sha256,
        }
    )


def build_live_request(
    *, case_id, thread_id, case_version, recall_number, question, scope_lot_ids, rag_result
) -> LiveInvestigationRequest:
    from recallops.retrieval.corpus import KnowledgeCorpus

    revision = source_revision()
    corpus = KnowledgeCorpus.load(data_dir=DATA_DIR)
    retained_ids = set()
    for hit in rag_result["evidence"]:
        doc = hit["document"]
        identifier = doc["citation_id"]
        if identifier in retained_ids or doc != corpus.resolve(identifier).model_dump(mode="json"):
            raise ValueError("retrieval document differs from bound source")
        retained_ids.add(identifier)
    entries = []
    remaining = 24_576
    for hit in rag_result["evidence"][:8]:
        doc = hit["document"]
        text = doc["text"][: min(4096, remaining)]
        if not text:
            break
        remaining -= len(text)
        entries.append(
            {
                key: doc[key]
                for key in (
                    "citation_id",
                    "source_class",
                    "origin",
                    "record_id",
                    "record_type",
                    "source_url",
                    "content_hash",
                )
            }
            | {"text": text, "truncated": len(text) < len(doc["text"])}
        )
    context = {
        "entries": entries,
        "source_digest": revision,
        "coverage_satisfied": rag_result["coverage_satisfied"],
        "stop_reason": rag_result["stop_reason"],
        "rewrite_used": rag_result["rewrite_used"],
        "evidence_gaps": [
            {"code": "retrieval_gap", "digest": canonical_digest(gap)}
            for gap in rag_result["evidence_gaps"][:32]
        ],
    }
    context["digest"] = canonical_digest(context)
    # Include default nulls in the canonical representation before binding.
    provisional = {
        "schema_version": 1,
        "case_id": case_id,
        "thread_id": thread_id,
        "investigation_case_version": case_version,
        "recall_number": recall_number,
        "question": question,
        "scope_lot_ids": scope_lot_ids,
        "source_digest": revision,
        "context": context,
    }
    # Gap nulls are explicitly represented so JSON round trips retain the digest.
    for gap in context["evidence_gaps"]:
        gap.update(lot_id=None, units=None)
    context["digest"] = canonical_digest({k: v for k, v in context.items() if k != "digest"})
    digest = canonical_digest(provisional)
    return LiveInvestigationRequest.model_validate_json(
        json.dumps(
            provisional
            | {
                "request_digest": digest,
                "run_id": canonical_digest({"request": digest, "schema": 1}),
            }
        )
    )


def _shape(value, schema, defs, *, depth=0):
    """Enforce recursive output types/extras before permissive legacy parsers run."""
    if depth > 24:
        raise ValueError("model response nesting exceeds budget")
    if "$ref" in schema:
        return _shape(value, defs[schema["$ref"].split("/")[-1]], defs, depth=depth + 1)
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                _shape(value, branch, defs, depth=depth + 1)
                return
            except ValueError:
                pass
        raise ValueError("model response union type mismatch")
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if kind in types and type(value) is not types[kind]:
        raise ValueError("model response type mismatch")
    if kind == "number" and (type(value) not in {float, int} or not math.isfinite(value)):
        raise ValueError("model response number must be finite")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ValueError("model response literal mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("model response enum mismatch")
    if kind == "string" and len(value) > 8192:
        raise ValueError("model response text exceeds budget")
    if kind == "object":
        if len(value) > 512:
            raise ValueError("model response object exceeds budget")
        properties = schema.get("properties")
        if properties is not None:
            if set(value) - set(properties):
                raise ValueError("model response has unknown fields")
            # Even legacy model defaults must be explicit in a live artifact;
            # missing evidence or executed=False cannot be silently filled in.
            if set(properties) - set(value):
                raise ValueError("model response missing fields")
            for key, item in value.items():
                _shape(item, properties[key], defs, depth=depth + 1)
        else:
            for key, item in value.items():
                _identifier(key)
                _shape(item, schema.get("additionalProperties", {}), defs, depth=depth + 1)
    if kind == "array":
        if len(value) > 512:
            raise ValueError("model response collection exceeds budget")
        for item in value:
            _shape(item, schema.get("items", {}), defs, depth=depth + 1)


def validate_artifact_shape(role: str, raw: Any) -> None:
    if role not in ROLE_MODELS:
        raise ValueError("unknown model role")
    schema = ROLE_MODELS[role].model_json_schema()
    _shape(raw, schema, schema.get("$defs", {}))
    if len(json.dumps(raw, allow_nan=False)) > 262_144:
        raise ValueError("model response aggregate budget exceeded")


def _gaps(values):
    projected = []
    for gap in values:
        match = re.fullmatch(r"(LOT-[A-Z0-9?-]+): ([0-9]+) unaccounted units", gap)
        if match:
            projected.append(
                {"code": "unaccounted_units", "lot_id": match[1], "units": int(match[2])}
            )
        else:
            projected.append({"code": "unclassified", "digest": canonical_digest(gap)})
    return projected


def project_specialist_claims(
    request: LiveInvestigationRequest, artifacts: dict
) -> SpecialistClaims:
    """Do not repair any factual label, quantity, target, citation, or omission."""
    if set(artifacts) != set(ROLES):
        raise ValueError("incomplete model roles")
    for role, raw in artifacts.items():
        validate_artifact_shape(role, raw)
    recall, matching, trace, containment = (artifacts[role] for role in ROLES)
    citation = f"openfda:{request.recall_number}"

    def text_claim(path, value):
        return {
            "citation_id": citation,
            "field_path": path,
            "value_digest": canonical_digest(" ".join(value.split())),
        }

    pred = recall["predicate"]
    recall_claim = {
        **recall,
        "predicate": {
            **pred,
            "product_terms": [
                text_claim(f"predicate.product_terms.{i}", value)
                for i, value in enumerate(pred["product_terms"])
            ],
            "geography": [
                text_claim(f"predicate.geography.{i}", value)
                for i, value in enumerate(pred["geography"])
            ],
            "hazard": text_claim("predicate.hazard", pred["hazard"]),
        },
        "official_products": [
            {
                **row,
                "description": text_claim(f"official_products.{i}.description", row["description"]),
            }
            for i, row in enumerate(recall["official_products"])
        ],
        "classification": text_claim("classification", recall["classification"]),
        "status": text_claim("status", recall["status"]),
        "evidence_gaps": _gaps(recall["evidence_gaps"]),
    }
    actions = []
    for action in containment["proposed_actions"]:
        if (
            action["case_id"] != request.case_id
            or action["expected_case_version"] != request.investigation_case_version
        ):
            raise ValueError("model action binding mismatch")
        actions.append({key: value for key, value in action.items() if key != "rationale"})
    payload = {
        "request_digest": request.request_digest,
        "context_digest": request.context.digest,
        "source_digest": request.source_digest,
        "run_id": request.run_id,
        "completed_roles": list(ROLES),
        "recall": recall_claim,
        "matching": {
            **matching,
            "decisions": [
                {key: val for key, val in row.items() if key != "rationale"}
                for row in matching["decisions"]
            ],
        },
        "traceability": {**trace, "evidence_gaps": _gaps(trace["evidence_gaps"])},
        "containment": {
            **containment,
            "proposed_actions": actions,
            "communication_drafts": [
                {key: val for key, val in row.items() if key not in {"subject", "body"}}
                for row in containment["communication_drafts"]
            ],
        },
    }
    return SpecialistClaims.model_validate_json(json.dumps(payload, allow_nan=False))
