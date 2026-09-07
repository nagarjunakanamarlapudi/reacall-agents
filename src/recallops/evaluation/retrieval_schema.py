"""Typed contracts and strict loading for the labelled retrieval benchmark."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from recallops.evaluation.digests import canonical_json_bytes
from recallops.paths import DATA_DIR
from recallops.retrieval.corpus import KnowledgeCorpus

RetrievalFamily = Literal[
    "exact_identifier",
    "semantic_product_hazard",
    "lineage",
    "reconciliation",
    "cross_source",
    "difficult_rewrite",
    "abstention_adversarial",
]
RetrievalRoute = Literal["official", "synthetic"]
RetrievalIntent = Literal["regulatory", "operational", "mixed"]
RetrievalConfigurationName = Literal[
    "sparse_bm25",
    "dense_lsa",
    "naive_hybrid",
    "rrf_fusion",
    "rrf_plus_rerank",
    "agentic_rag",
]

EXPECTED_RETRIEVAL_FAMILY_COUNTS: dict[str, int] = {
    "exact_identifier": 24,
    "semantic_product_hazard": 16,
    "lineage": 16,
    "reconciliation": 16,
    "cross_source": 8,
    "difficult_rewrite": 8,
    "abstention_adversarial": 8,
}


def _freeze_mapping[Key, Value](
    value: Mapping[Key, Value],
) -> Mapping[Key, Value]:
    """Copy a validated mapping behind a read-only runtime view."""

    return MappingProxyType(dict(value))


def _strict_finite_float(value: Any) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("value must be a strict finite float")
    return value


StrictFiniteFloat = Annotated[float, BeforeValidator(_strict_finite_float)]


class RetrievalJudgment(BaseModel):
    """Independent graded relevance label for one corpus document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relevance: StrictInt = Field(ge=0, le=3)


def _source_from_citation(citation_id: str) -> RetrievalRoute | None:
    if citation_id.startswith("NORTHSTAR-"):
        return "synthetic"
    if citation_id.startswith(("OPENFDA-", "FDA-", "GS1-")):
        return "official"
    return None


class RetrievalCase(BaseModel):
    """One bounded, read-only retrieval question and its audited labels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr = Field(pattern=r"^RET-(?:0[0-9]{2}|[1-9][0-9]{2})$")
    family: RetrievalFamily
    question: StrictStr = Field(min_length=1)
    expected_route: tuple[RetrievalRoute, ...] = Field(min_length=1, max_length=2)
    judgments: Mapping[StrictStr, RetrievalJudgment]
    required_document_ids: tuple[StrictStr, ...] = ()
    prohibited_document_ids: tuple[StrictStr, ...] = ()
    required_facts: tuple[StrictStr, ...] = ()
    unanswerable: StrictBool = False
    rewrite_allowed: StrictBool = False
    expected_rewrite_intent: RetrievalIntent
    top_k: StrictInt = Field(default=5, ge=1, le=20)
    max_queries: StrictInt = Field(default=2, ge=1, le=4)
    max_hops: StrictInt = Field(default=1, ge=1, le=2)
    max_reads: StrictInt = Field(default=2, ge=1, le=8)
    rationale: StrictStr = Field(min_length=1)

    @field_validator("question", "rationale")
    @classmethod
    def strip_nonblank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question and rationale must be nonblank")
        return normalized

    @field_validator("expected_route")
    @classmethod
    def validate_route(cls, value: tuple[RetrievalRoute, ...]) -> tuple[RetrievalRoute, ...]:
        if len(value) != len(set(value)):
            raise ValueError("expected route must not contain duplicate sources")
        if value == ("synthetic", "official"):
            raise ValueError("mixed expected route must use official then synthetic order")
        return value

    @field_validator("judgments")
    @classmethod
    def freeze_judgments(
        cls, value: Mapping[str, RetrievalJudgment]
    ) -> Mapping[str, RetrievalJudgment]:
        return _freeze_mapping(value)

    @field_serializer("judgments")
    def serialize_judgments(
        self, value: Mapping[str, RetrievalJudgment]
    ) -> dict[str, RetrievalJudgment]:
        return dict(value)

    @field_validator("required_document_ids", "prohibited_document_ids", "required_facts")
    @classmethod
    def validate_unique_nonblank_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("case lists must contain nonblank strings")
        if len(normalized) != len(set(normalized)):
            raise ValueError("case lists must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_case_contract(self) -> RetrievalCase:
        required = set(self.required_document_ids)
        prohibited = set(self.prohibited_document_ids)
        if required & prohibited:
            raise ValueError("required and prohibited document IDs overlap")
        if not required <= set(self.judgments):
            raise ValueError("required document IDs must have relevance judgments")
        if not prohibited <= set(self.judgments):
            raise ValueError("prohibited document IDs must have relevance judgments")
        if any(self.judgments[item].relevance <= 0 for item in required):
            raise ValueError("required document IDs must have positive relevance")
        if any(self.judgments[item].relevance != 0 for item in prohibited):
            raise ValueError("prohibited document IDs must have zero relevance")
        if self.unanswerable:
            if required or self.required_facts:
                raise ValueError("unanswerable cases cannot require documents or facts")
        elif not required or not self.required_facts:
            raise ValueError("answerable cases require documents and facts")
        positive_ids = {
            document_id
            for document_id, judgment in self.judgments.items()
            if judgment.relevance > 0
        }
        excluded_sources = {
            source
            for document_id in positive_ids | required
            if (source := _source_from_citation(document_id)) is not None
            and source not in self.expected_route
        }
        if excluded_sources:
            raise ValueError("expected route excludes required evidence source")
        expected_intent = {
            ("official",): "regulatory",
            ("synthetic",): "operational",
            ("official", "synthetic"): "mixed",
        }.get(self.expected_route)
        if expected_intent != self.expected_rewrite_intent:
            raise ValueError("expected rewrite intent contradicts expected route")
        if self.rewrite_allowed and (self.max_queries < 2 or self.max_hops < 2):
            raise ValueError("rewrite-enabled cases require a second query and hop")
        required_reads = len(self.expected_route) * (2 if self.rewrite_allowed else 1)
        if self.max_queries < required_reads or self.max_reads < required_reads:
            raise ValueError("budgets must permit the complete source route and allowed rewrite")
        return self


class RetrievalEvalCorpus(BaseModel):
    """The immutable corpus payload committed as canonical JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    knowledge_corpus_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[RetrievalCase, ...]

    @model_validator(mode="after")
    def validate_case_matrix(self) -> RetrievalEvalCorpus:
        identifiers = [case.id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate retrieval case IDs")
        expected_ids = [f"RET-{index:03d}" for index in range(1, 97)]
        if identifiers != expected_ids:
            raise ValueError("retrieval cases must be ordered exactly RET-001 through RET-096")
        counts = Counter(case.family for case in self.cases)
        if counts != Counter(EXPECTED_RETRIEVAL_FAMILY_COUNTS):
            raise ValueError(
                f"retrieval family counts must equal {EXPECTED_RETRIEVAL_FAMILY_COUNTS}"
            )
        return self


class RetrievalCaseResult(BaseModel):
    """Persisted observable result for one case/configuration pair."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    case_id: StrictStr
    family: RetrievalFamily
    ranked_document_ids: tuple[StrictStr, ...] = ()
    route_actual: tuple[RetrievalRoute, ...] = ()
    cited_document_ids: tuple[StrictStr, ...] = ()
    initial_ranked_document_ids: tuple[StrictStr, ...] = ()
    cited_facts: tuple[StrictStr, ...] = ()
    answered: StrictBool = False
    rewrite_used: StrictBool = False
    query_count: StrictInt = Field(default=0, ge=0)
    hop_count: StrictInt = Field(default=0, ge=0)
    read_count: StrictInt = Field(default=0, ge=0)
    duration_ms: StrictInt = Field(default=0, ge=0)
    error_code: StrictStr | None = None
    evidence_gaps: tuple[StrictStr, ...] = ()
    stop_reason: Literal[
        "single_pass",
        "coverage_satisfied",
        "coverage_satisfied_after_rewrite",
        "evidence_gap_after_rewrite",
        "budget_exhausted",
        "progress_stalled",
        "empty_query",
        "execution_error",
    ] = "single_pass"
    provenance: Mapping[StrictStr, RetrievalRoute] = Field(default_factory=dict)
    metric_contributions: Mapping[StrictStr, StrictFiniteFloat | StrictInt | StrictBool] = Field(
        default_factory=dict
    )

    @field_validator("metric_contributions")
    @classmethod
    def freeze_metric_contributions(
        cls, value: Mapping[str, float | int | bool]
    ) -> Mapping[str, float | int | bool]:
        return _freeze_mapping(value)

    @field_serializer("metric_contributions")
    def serialize_metric_contributions(
        self, value: Mapping[str, float | int | bool]
    ) -> dict[str, float | int | bool]:
        return dict(value)

    @field_validator("provenance")
    @classmethod
    def freeze_provenance(cls, value: Mapping[str, RetrievalRoute]) -> Mapping[str, RetrievalRoute]:
        return _freeze_mapping(value)

    @field_serializer("provenance")
    def serialize_provenance(
        self, value: Mapping[str, RetrievalRoute]
    ) -> dict[str, RetrievalRoute]:
        return dict(value)


class RetrievalConfigurationMetrics(BaseModel):
    """Aggregate ranking, grounding, routing, budget, and rewrite measurements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_count: StrictInt = Field(ge=0)
    recall_at_1: StrictFiniteFloat = Field(ge=0, le=1)
    recall_at_3: StrictFiniteFloat = Field(ge=0, le=1)
    recall_at_5: StrictFiniteFloat = Field(ge=0, le=1)
    precision_at_5: StrictFiniteFloat = Field(ge=0, le=1)
    mean_reciprocal_rank: StrictFiniteFloat = Field(ge=0, le=1)
    ndcg_at_5: StrictFiniteFloat = Field(ge=0, le=1)
    citation_precision: StrictFiniteFloat = Field(ge=0, le=1)
    required_fact_coverage: StrictFiniteFloat = Field(ge=0, le=1)
    route_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    abstention_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    provenance_label_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    budget_compliance: StrictFiniteFloat = Field(ge=0, le=1)
    prohibited_hit_count: StrictInt = Field(ge=0)
    unsupported_answer_count: StrictInt = Field(ge=0)
    latency_p50_ms: StrictFiniteFloat = Field(ge=0)
    latency_p95_ms: StrictFiniteFloat = Field(ge=0)
    rewrite_win_count: StrictInt = Field(default=0, ge=0)
    rewrite_loss_count: StrictInt = Field(default=0, ge=0)
    rewrite_no_change_count: StrictInt = Field(default=0, ge=0)
    denominators: Mapping[StrictStr, StrictInt] = Field(default_factory=dict)

    @field_validator("denominators")
    @classmethod
    def freeze_denominators(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("metric denominators cannot be negative")
        return _freeze_mapping(value)

    @field_serializer("denominators")
    def serialize_denominators(self, value: Mapping[str, int]) -> dict[str, int]:
        return dict(value)


class RetrievalConfigurationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: RetrievalConfigurationName
    results: tuple[RetrievalCaseResult, ...]
    metrics: RetrievalConfigurationMetrics
    family_metrics: Mapping[RetrievalFamily, RetrievalConfigurationMetrics]

    @field_validator("family_metrics")
    @classmethod
    def freeze_family_metrics(
        cls, value: Mapping[RetrievalFamily, RetrievalConfigurationMetrics]
    ) -> Mapping[RetrievalFamily, RetrievalConfigurationMetrics]:
        return _freeze_mapping(value)

    @field_serializer("family_metrics")
    def serialize_family_metrics(
        self, value: Mapping[RetrievalFamily, RetrievalConfigurationMetrics]
    ) -> dict[RetrievalFamily, RetrievalConfigurationMetrics]:
        return dict(value)


class RetrievalEvalGates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    abstention_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    provenance_label_accuracy: StrictFiniteFloat = Field(ge=0, le=1)
    budget_compliance: StrictFiniteFloat = Field(ge=0, le=1)
    prohibited_hit_count: StrictInt = Field(ge=0)
    unsupported_answer_count: StrictInt = Field(ge=0)
    agentic_recall_at_5: StrictFiniteFloat = Field(ge=0, le=1)
    agentic_ndcg_at_5: StrictFiniteFloat = Field(ge=0, le=1)
    fusion_recall_delta: StrictFiniteFloat
    rerank_ndcg_delta: StrictFiniteFloat


SUPPORTED_RETRIEVAL_EXECUTION = MappingProxyType(
    {
        "rrf_sparse_weight": 0.9,
        "rrf_dense_weight": 0.1,
        "rrf_rank_constant": 1,
        "rerank_signal_weight": 0.05,
        "calibration": "in_sample_offline_synthetic",
        "calibrated_configurations": ("rrf_fusion", "rrf_plus_rerank"),
        "agentic_rrf_rank_constant": 60,
        "agentic_rrf_sparse_weight": 1.0,
        "agentic_rrf_dense_weight": 1.0,
        "agentic_rerank_signal_weight": 1.0,
    }
)


class RetrievalExecutionConfig(BaseModel):
    """The exact supported execution profile, shared by executor and validator."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    rrf_sparse_weight: StrictFiniteFloat = SUPPORTED_RETRIEVAL_EXECUTION["rrf_sparse_weight"]
    rrf_dense_weight: StrictFiniteFloat = SUPPORTED_RETRIEVAL_EXECUTION["rrf_dense_weight"]
    rrf_rank_constant: StrictInt = SUPPORTED_RETRIEVAL_EXECUTION["rrf_rank_constant"]
    rerank_signal_weight: StrictFiniteFloat = SUPPORTED_RETRIEVAL_EXECUTION["rerank_signal_weight"]
    calibration: Literal["in_sample_offline_synthetic"] = "in_sample_offline_synthetic"
    calibrated_configurations: tuple[Literal["rrf_fusion", "rrf_plus_rerank"], ...] = (
        "rrf_fusion",
        "rrf_plus_rerank",
    )
    agentic_rrf_rank_constant: StrictInt = Field(default=60, gt=0)
    agentic_rrf_sparse_weight: StrictFiniteFloat = Field(default=1.0, gt=0)
    agentic_rrf_dense_weight: StrictFiniteFloat = Field(default=1.0, gt=0)
    agentic_rerank_signal_weight: StrictFiniteFloat = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def require_supported_execution(self) -> RetrievalExecutionConfig:
        if any(getattr(self, key) != value for key, value in SUPPORTED_RETRIEVAL_EXECUTION.items()):
            raise ValueError("report execution configuration does not match the supported executor")
        return self


class RetrievalEvalReport(RetrievalExecutionConfig):
    schema_version: Literal["1.0"]
    retrieval_case_corpus_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_corpus_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: Literal["offline_deterministic"] = "offline_deterministic"
    configurations: tuple[RetrievalConfigurationResult, ...]
    gates: RetrievalEvalGates
    gate_passed: StrictBool
    report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def load_retrieval_cases(path: Path, *, data_dir: Path = DATA_DIR) -> RetrievalEvalCorpus:
    """Load a canonical case artifact and validate it against production knowledge."""

    resolved = Path(path)
    raw = resolved.read_bytes()
    try:
        decoded: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retrieval corpus is not valid UTF-8 JSON") from exc
    if raw != canonical_json_bytes(decoded):
        raise ValueError("retrieval corpus must use canonical JSON")
    corpus = RetrievalEvalCorpus.model_validate(decoded)
    knowledge = KnowledgeCorpus.load(data_dir=data_dir)
    if corpus.knowledge_corpus_sha256 != knowledge.manifest.corpus_sha256:
        raise ValueError("knowledge corpus digest mismatch")
    known = {document.citation_id for document in knowledge.documents}
    cited = {
        document_id
        for case in corpus.cases
        for document_id in (
            *case.judgments,
            *case.required_document_ids,
            *case.prohibited_document_ids,
        )
    }
    unknown = cited - known
    if unknown:
        raise ValueError(f"unknown cited document IDs: {sorted(unknown)}")
    by_id = {document.citation_id: document for document in knowledge.documents}
    contradictions = [
        (case.id, document_id)
        for case in corpus.cases
        for document_id, judgment in case.judgments.items()
        if judgment.relevance > 0 and by_id[document_id].source_class not in case.expected_route
    ]
    if contradictions:
        raise ValueError(f"source-route contradiction: {contradictions}")
    return corpus
