"""Deterministic ranking metrics for offline evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def _validate_relevance(relevance: Mapping[str, int]) -> None:
    if not isinstance(relevance, Mapping):
        raise ValueError("relevance must be a mapping")
    for document_id, grade in relevance.items():
        if not isinstance(document_id, str):
            raise ValueError("relevance IDs must be strings")
        if isinstance(grade, bool) or not isinstance(grade, int) or grade < 0:
            raise ValueError("relevance grades must be non-negative integers")


def _validate_ranking(relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int) -> None:
    _validate_relevance(relevance)
    if not isinstance(ranked_ids, Sequence) or isinstance(ranked_ids, (str, bytes)):
        raise ValueError("ranked_ids must be a sequence")
    if any(not isinstance(document_id, str) for document_id in ranked_ids):
        raise ValueError("ranked IDs must be strings")
    if len(set(ranked_ids)) != len(ranked_ids):
        raise ValueError("ranked IDs must be unique")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")


def recall_at_k(relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int) -> float:
    _validate_ranking(relevance, ranked_ids, k)
    relevant = {document_id for document_id, grade in relevance.items() if grade > 0}
    if not relevant:
        return 0.0
    result = len(relevant & set(ranked_ids[:k])) / len(relevant)
    return float(result) if math.isfinite(result) else 0.0


def precision_at_k(relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int) -> float:
    _validate_ranking(relevance, ranked_ids, k)
    result = sum(relevance.get(document_id, 0) > 0 for document_id in ranked_ids[:k]) / k
    return float(result) if math.isfinite(result) else 0.0


def reciprocal_rank(relevance: Mapping[str, int], ranked_ids: Sequence[str]) -> float:
    _validate_ranking(relevance, ranked_ids, max(1, len(ranked_ids)))
    for rank, document_id in enumerate(ranked_ids, start=1):
        if relevance.get(document_id, 0) > 0:
            return 1.0 / rank
    return 0.0


def _dcg(grades: Sequence[int]) -> float:
    return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades))


def _scaled_dcg(grades: Sequence[int], scale: int) -> float:
    """Compute DCG divided by ``2**scale`` without integer/float overflow."""
    scale_factor = math.ldexp(1.0, -scale)
    return sum(
        (math.ldexp(1.0, grade - scale) - scale_factor) / math.log2(rank + 2)
        for rank, grade in enumerate(grades)
    )


def ndcg_at_k(relevance: Mapping[str, int], ranked_ids: Sequence[str], k: int) -> float:
    _validate_ranking(relevance, ranked_ids, k)
    observed = [relevance.get(document_id, 0) for document_id in ranked_ids[:k]]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    if not ideal:
        return 0.0
    ideal_dcg = _dcg(ideal) if ideal and ideal[0] < 1024 else _scaled_dcg(ideal, ideal[0])
    if ideal_dcg == 0.0:
        return 0.0
    observed_dcg = _dcg(observed) if ideal[0] < 1024 else _scaled_dcg(observed, ideal[0])
    result = observed_dcg / ideal_dcg
    return float(result) if math.isfinite(result) else 0.0
