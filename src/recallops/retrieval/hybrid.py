"""Deterministic BM25-style and local TF-IDF/SVD LSA hybrid retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from recallops.retrieval.models import (
    ComponentHit,
    FusionRecord,
    HybridSearchRequest,
    HybridSearchResponse,
    HybridSearchResult,
    IndexMetadata,
    KnowledgeDocument,
    RetrievalIntent,
    SourceFilter,
)

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-./][A-Za-z0-9?]+)*")
IDENTIFIER_PATTERN = re.compile(
    r"\b(?:H-\d{4}-\d{4}|LOT-[A-Z0-9?-]+|SHIP-[A-Z0-9?-]+|STORE-[A-Z0-9?-]+|"
    r"DC-[A-Z0-9?-]+|P-[A-Z0-9?-]+|\d{8,14})\b",
    flags=re.IGNORECASE,
)
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "between",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


def _round(value: float) -> float:
    return round(float(value), 12)


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_PATTERN.findall(value.casefold()):
        if raw not in STOP_WORDS:
            tokens.append(raw)
        if any(separator in raw for separator in "-./"):
            compact = "".join(character for character in raw if character.isalnum())
            if compact:
                tokens.append(compact)
            tokens.extend(
                part
                for part in re.split(r"[-./]", raw)
                if part and part not in STOP_WORDS and len(part) > 1
            )
    return tokens


def _search_text(document: KnowledgeDocument) -> str:
    metadata = json.dumps(
        document.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return f"{document.title} {document.record_id} {document.text} {metadata}"


def reciprocal_rank_fusion(
    *,
    sparse: Sequence[tuple[str, float]],
    dense: Sequence[tuple[str, float]],
    rank_constant: int = 60,
    sparse_weight: float = 1.0,
    dense_weight: float = 1.0,
) -> tuple[FusionRecord, ...]:
    if isinstance(rank_constant, bool) or not isinstance(rank_constant, int) or rank_constant <= 0:
        raise ValueError("rank_constant must be a positive integer")
    if any(
        type(weight) is not float or not math.isfinite(weight) or weight <= 0
        for weight in (sparse_weight, dense_weight)
    ):
        raise ValueError("RRF weights must be strict positive finite floats")
    for component_name, ranked in (("sparse", sparse), ("dense", dense)):
        for citation_id, score in ranked:
            if type(score) is not float or not math.isfinite(score):
                raise ValueError(
                    f"{component_name} score for {citation_id!r} must be a strict finite float"
                )
    rows: dict[str, dict[str, float | int | None]] = defaultdict(
        lambda: {
            "sparse_rank": None,
            "sparse_score": None,
            "dense_rank": None,
            "dense_score": None,
            "rrf_score": 0.0,
        }
    )
    for kind, ranked in (("sparse", sparse), ("dense", dense)):
        seen: set[str] = set()
        for rank, (citation_id, score) in enumerate(ranked, start=1):
            if citation_id in seen:
                raise ValueError(f"duplicate {kind} citation ID {citation_id!r}")
            seen.add(citation_id)
            rows[citation_id][f"{kind}_rank"] = rank
            rows[citation_id][f"{kind}_score"] = _round(score)
            weight = sparse_weight if kind == "sparse" else dense_weight
            rows[citation_id]["rrf_score"] = float(rows[citation_id]["rrf_score"]) + weight / (
                rank_constant + rank
            )
    fused = [
        FusionRecord(
            citation_id=citation_id,
            sparse_rank=row["sparse_rank"],
            sparse_score=row["sparse_score"],
            dense_rank=row["dense_rank"],
            dense_score=row["dense_score"],
            rrf_score=_round(float(row["rrf_score"])),
        )
        for citation_id, row in rows.items()
    ]
    return tuple(
        sorted(
            fused,
            key=lambda item: (
                -item.rrf_score,
                item.sparse_rank if item.sparse_rank is not None else math.inf,
                item.dense_rank if item.dense_rank is not None else math.inf,
                item.citation_id,
            ),
        )
    )


@dataclass(frozen=True)
class _Reranked:
    fusion: FusionRecord
    document: KnowledgeDocument
    score: float
    explanation: tuple[str, ...]


class HybridIndex:
    """In-memory deterministic index; dense vectors are local LSA, not neural embeddings."""

    def __init__(self, documents: Iterable[KnowledgeDocument]) -> None:
        self.documents = tuple(sorted(documents, key=lambda item: item.citation_id))
        if not self.documents:
            raise ValueError("hybrid index requires at least one document")
        if len({item.citation_id for item in self.documents}) != len(self.documents):
            raise ValueError("hybrid index citation IDs must be unique")
        self._by_citation = {item.citation_id: item for item in self.documents}
        self._texts = tuple(_search_text(item) for item in self.documents)
        self._tokenized = tuple(_tokens(text) for text in self._texts)
        self._term_frequencies = tuple(Counter(tokens) for tokens in self._tokenized)
        self._average_length = sum(map(len, self._tokenized)) / len(self._tokenized)
        self._document_frequency = Counter(
            term for tokens in self._tokenized for term in set(tokens)
        )
        self._vectorizer = TfidfVectorizer(
            lowercase=True,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
            norm="l2",
            max_features=20000,
        )
        tfidf = self._vectorizer.fit_transform(self._texts)
        dimensions = min(64, tfidf.shape[0] - 1, tfidf.shape[1] - 1)
        if dimensions < 1:
            raise ValueError("hybrid index needs enough documents and terms for LSA")
        self._svd = TruncatedSVD(
            n_components=dimensions,
            algorithm="randomized",
            n_iter=7,
            random_state=20260830,
        )
        self._dense_documents = normalize(self._svd.fit_transform(tfidf), norm="l2")
        corpus_payload = [item.model_dump(mode="json") for item in self.documents]
        corpus_sha256 = hashlib.sha256(
            (
                json.dumps(corpus_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        self.metadata = IndexMetadata(
            document_count=len(self.documents),
            vocabulary_size=len(self._vectorizer.vocabulary_),
            lsa_dimensions=dimensions,
            dense_method="local_tfidf_truncated_svd_lsa",
            corpus_sha256=corpus_sha256,
        )

    def _eligible(self, source_filter: SourceFilter, record_types: tuple[str, ...]) -> list[int]:
        return [
            index
            for index, document in enumerate(self.documents)
            if (source_filter == "all" or document.source_class == source_filter)
            and (not record_types or document.record_type in record_types)
        ]

    def sparse_search(
        self,
        query: str,
        *,
        top_k: int,
        source_filter: SourceFilter = "all",
        record_types: tuple[str, ...] = (),
    ) -> tuple[ComponentHit, ...]:
        query_terms = Counter(_tokens(query))
        eligible = self._eligible(source_filter, record_types)
        scores: list[ComponentHit] = []
        total_documents = len(self.documents)
        for index in eligible:
            frequencies = self._term_frequencies[index]
            length = len(self._tokenized[index])
            contributions: dict[str, float] = {}
            for term, query_frequency in query_terms.items():
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency[term]
                inverse_frequency = math.log(
                    1 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length / self._average_length)
                contribution = inverse_frequency * (frequency * 2.5 / denominator) * query_frequency
                contributions[term] = _round(contribution)
            score = _round(sum(contributions.values()))
            if score > 0:
                scores.append(
                    ComponentHit(
                        citation_id=self.documents[index].citation_id,
                        score=score,
                        term_contributions=dict(sorted(contributions.items())),
                    )
                )
        return tuple(sorted(scores, key=lambda item: (-item.score, item.citation_id))[:top_k])

    def dense_search(
        self,
        query: str,
        *,
        top_k: int,
        source_filter: SourceFilter = "all",
        record_types: tuple[str, ...] = (),
    ) -> tuple[ComponentHit, ...]:
        query_tfidf = self._vectorizer.transform([query])
        if query_tfidf.nnz == 0:
            return ()
        query_dense = normalize(self._svd.transform(query_tfidf), norm="l2")[0]
        similarities = np.asarray(self._dense_documents @ query_dense).reshape(-1)
        hits = [
            ComponentHit(
                citation_id=self.documents[index].citation_id,
                score=_round(max(0.0, similarities[index])),
            )
            for index in self._eligible(source_filter, record_types)
            if similarities[index] > 1e-12
        ]
        return tuple(sorted(hits, key=lambda item: (-item.score, item.citation_id))[:top_k])

    def _rerank(
        self,
        query: str,
        fused: tuple[FusionRecord, ...],
        *,
        intent: RetrievalIntent,
        signal_weight: float = 1.0,
    ) -> list[_Reranked]:
        if (
            type(signal_weight) is not float
            or not math.isfinite(signal_weight)
            or not 0 < signal_weight <= 1
        ):
            raise ValueError("rerank signal weight must be a float in (0, 1]")
        resolved_intent = self._resolve_intent(query) if intent == "auto" else intent
        identifiers = tuple(
            dict.fromkeys(match.casefold() for match in IDENTIFIER_PATTERN.findall(query))
        )
        query_folded = query.casefold()
        ranked: list[_Reranked] = []
        for item in fused:
            document = self._by_citation[item.citation_id]
            searchable = _search_text(document).casefold()
            score = item.rrf_score
            explanation = [f"RRF {item.rrf_score:.6f}"]
            if item.sparse_score is not None:
                score += min(0.15, math.log1p(item.sparse_score) * 0.025)
                explanation.append(f"BM25-style rank {item.sparse_rank}")
            if item.dense_score is not None:
                score += item.dense_score * 0.12
                explanation.append(f"local LSA rank {item.dense_rank}")
            exact_identifiers = [
                identifier for identifier in identifiers if identifier in searchable
            ]
            if exact_identifiers:
                score += 0.45 * len(exact_identifiers)
                explanation.append("exact identifier: " + ", ".join(exact_identifiers))
            if document.record_id.casefold() in query_folded:
                score += 0.8
                explanation.append("record ID is explicitly requested")
            if resolved_intent == "regulatory" and document.source_class == "official":
                score += 0.4
                explanation.append("regulatory source boost")
            elif resolved_intent == "operational" and document.source_class == "synthetic":
                score += 0.4
                explanation.append("operational source boost")
            elif resolved_intent == "mixed":
                score += 0.05
                explanation.append("mixed-source evidence candidate")
            ranked.append(
                _Reranked(
                    fusion=item,
                    document=document,
                    score=_round(item.rrf_score + signal_weight * (score - item.rrf_score)),
                    explanation=tuple(explanation),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.document.citation_id))
        return ranked

    @staticmethod
    def _resolve_intent(query: str) -> RetrievalIntent:
        terms = set(_tokens(query))
        regulatory = terms & {
            "fda",
            "class",
            "classification",
            "regulatory",
            "recall",
            "termination",
            "traceability",
        }
        operational = terms & {
            "facility",
            "inventory",
            "lot",
            "northstar",
            "shipment",
            "store",
            "supplier",
            "upc",
        }
        if regulatory and operational:
            return "mixed"
        if operational:
            return "operational"
        return "regulatory"

    def rerank_fused(
        self,
        query: str,
        fused: Sequence[FusionRecord],
        *,
        intent: RetrievalIntent,
        signal_weight: float = 1.0,
    ) -> tuple[HybridSearchResult, ...]:
        """Expose production reranking without the search diversity selector."""
        return tuple(
            HybridSearchResult(
                document=item.document,
                sparse_rank=item.fusion.sparse_rank,
                sparse_score=item.fusion.sparse_score,
                dense_rank=item.fusion.dense_rank,
                dense_score=item.fusion.dense_score,
                rrf_score=item.fusion.rrf_score,
                rerank_score=item.score,
                explanation=item.explanation,
            )
            for item in self._rerank(
                query, tuple(fused), intent=intent, signal_weight=signal_weight
            )
        )

    def search(self, request: HybridSearchRequest) -> HybridSearchResponse:
        candidate_k = min(len(self.documents), max(20, request.top_k * 4))
        sparse = self.sparse_search(
            request.query,
            top_k=candidate_k,
            source_filter=request.source_filter,
            record_types=request.record_types,
        )
        dense = self.dense_search(
            request.query,
            top_k=candidate_k,
            source_filter=request.source_filter,
            record_types=request.record_types,
        )
        fused = reciprocal_rank_fusion(
            sparse=[(item.citation_id, item.score) for item in sparse],
            dense=[(item.citation_id, item.score) for item in dense],
        )
        term_contributions = {item.citation_id: item.term_contributions for item in sparse}
        reranked = self._rerank(request.query, fused, intent=request.intent)
        selected: list[_Reranked] = []
        type_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        while reranked and len(selected) < request.top_k:
            best_index = min(
                range(len(reranked)),
                key=lambda index: (
                    -(
                        reranked[index].score
                        - type_counts[reranked[index].document.record_type] * 0.015
                        - source_counts[reranked[index].document.source_class] * 0.005
                    ),
                    reranked[index].document.citation_id,
                ),
            )
            item = reranked.pop(best_index)
            diversity_penalty = (
                type_counts[item.document.record_type] * 0.015
                + source_counts[item.document.source_class] * 0.005
            )
            if diversity_penalty:
                item = _Reranked(
                    fusion=item.fusion,
                    document=item.document,
                    score=_round(item.score - diversity_penalty),
                    explanation=(
                        *item.explanation,
                        f"diversity penalty {diversity_penalty:.3f}",
                    ),
                )
            selected.append(item)
            type_counts[item.document.record_type] += 1
            source_counts[item.document.source_class] += 1
        results = tuple(
            HybridSearchResult(
                document=item.document,
                sparse_rank=item.fusion.sparse_rank,
                sparse_score=item.fusion.sparse_score,
                dense_rank=item.fusion.dense_rank,
                dense_score=item.fusion.dense_score,
                rrf_score=item.fusion.rrf_score,
                rerank_score=item.score,
                matched_terms=term_contributions.get(item.document.citation_id, {}),
                explanation=item.explanation,
            )
            for item in selected
        )
        return HybridSearchResponse(
            query=request.query,
            source_filter=request.source_filter,
            intent=request.intent,
            results=results,
            index=self.metadata,
        )


@lru_cache(maxsize=8)
def load_local_hybrid_index(data_dir: str) -> HybridIndex:
    """Share one immutable local index per configured corpus directory."""

    from recallops.retrieval.corpus import KnowledgeCorpus

    resolved = Path(data_dir).resolve()
    return HybridIndex(KnowledgeCorpus.load(data_dir=resolved).documents)
