"""Typed contracts for provenance-preserving hybrid evidence retrieval."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

SourceClass = Literal["official", "synthetic"]
SourceFilter = Literal["official", "synthetic", "all"]
RetrievalIntent = Literal["auto", "regulatory", "operational", "mixed"]
KnowledgeOrigin = Literal[
    "OFFICIAL_OPENFDA_SNAPSHOT",
    "OFFICIAL_POLICY_REFERENCE",
    "SYNTHETIC_RETAILER_DIGITAL_TWIN",
]


class KnowledgeDocument(BaseModel):
    """One immutable, independently citable corpus record."""

    model_config = ConfigDict(frozen=True)

    citation_id: str = Field(min_length=1)
    source_class: SourceClass
    origin: KnowledgeOrigin
    audience_label: str = Field(min_length=1)
    record_type: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "citation_id",
        "audience_label",
        "record_type",
        "record_id",
        "title",
        "text",
        "source_url",
    )
    @classmethod
    def strip_nonblank_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("knowledge document strings must be nonblank")
        return normalized

    @model_validator(mode="after")
    def validate_boundary_and_hash(self) -> KnowledgeDocument:
        expected_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_hash != expected_hash:
            raise ValueError("knowledge document content hash mismatch")
        if self.source_class == "official" and self.origin not in {
            "OFFICIAL_OPENFDA_SNAPSHOT",
            "OFFICIAL_POLICY_REFERENCE",
        }:
            raise ValueError("official document has a non-official origin")
        if self.source_class == "synthetic" and self.origin != ("SYNTHETIC_RETAILER_DIGITAL_TWIN"):
            raise ValueError("synthetic document has a non-synthetic origin")
        return self


class KnowledgeManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_name: Literal["recallops.hybrid-knowledge-corpus"]
    schema_version: Literal["1.0.0"]
    generated_at: datetime
    document_count: int = Field(ge=1)
    source_counts: dict[str, int]
    record_type_counts: dict[str, int]
    source_checksums: dict[str, str]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HybridSearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    top_k: StrictInt = Field(default=8, ge=1, le=20)
    source_filter: SourceFilter = "all"
    intent: RetrievalIntent = "auto"
    record_types: tuple[str, ...] = ()

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must be nonblank")
        return normalized

    @field_validator("record_types")
    @classmethod
    def normalize_record_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("record types must be nonblank")
        return tuple(dict.fromkeys(normalized))


class ComponentHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_id: str
    score: float
    term_contributions: dict[str, float] = Field(default_factory=dict)


class FusionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_id: str
    sparse_rank: int | None = Field(default=None, ge=1)
    sparse_score: float | None = None
    dense_rank: int | None = Field(default=None, ge=1)
    dense_score: float | None = None
    rrf_score: float = Field(ge=0)


class HybridSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: KnowledgeDocument
    sparse_rank: int | None = Field(default=None, ge=1)
    sparse_score: float | None = None
    dense_rank: int | None = Field(default=None, ge=1)
    dense_score: float | None = None
    rrf_score: float = Field(ge=0)
    rerank_score: float
    matched_terms: dict[str, float] = Field(default_factory=dict)
    explanation: tuple[str, ...]


class IndexMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_count: int = Field(ge=1)
    vocabulary_size: int = Field(ge=1)
    lsa_dimensions: int = Field(ge=1)
    dense_method: Literal["local_tfidf_truncated_svd_lsa"]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HybridSearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    source_filter: SourceFilter
    intent: RetrievalIntent
    results: tuple[HybridSearchResult, ...]
    index: IndexMetadata
