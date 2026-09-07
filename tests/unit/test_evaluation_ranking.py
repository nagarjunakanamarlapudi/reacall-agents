import hashlib
import math

import pytest

from recallops.evaluation.digests import canonical_sha256, verify_sha256
from recallops.evaluation.ranking import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_ranking_metrics_match_hand_calculated_example() -> None:
    relevance = {"A": 3, "B": 2, "C": 0, "D": 1}
    ranked = ["C", "B", "X", "A", "D"]
    assert recall_at_k(relevance, ranked, 3) == pytest.approx(1 / 3)
    assert precision_at_k(relevance, ranked, 3) == pytest.approx(1 / 3)
    assert reciprocal_rank(relevance, ranked) == pytest.approx(1 / 2)
    expected_dcg = (2**2 - 1) / math.log2(3)
    ideal_dcg = 7 + 3 / math.log2(3) + 1 / math.log2(4)
    assert ndcg_at_k(relevance, ranked, 3) == pytest.approx(expected_dcg / ideal_dcg)


def test_canonical_digest_is_key_order_independent() -> None:
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_empty_relevance_returns_zero_metrics() -> None:
    assert recall_at_k({}, ["A"], 1) == 0.0
    assert precision_at_k({}, ["A"], 1) == 0.0
    assert reciprocal_rank({}, ["A"]) == 0.0
    assert ndcg_at_k({}, ["A"], 1) == 0.0


@pytest.mark.parametrize("metric", [recall_at_k, precision_at_k, ndcg_at_k])
def test_ranking_metrics_reject_invalid_k(metric) -> None:
    with pytest.raises(ValueError):
        metric({"A": 1}, ["A"], 0)
    with pytest.raises(ValueError):
        metric({"A": 1}, ["A"], -1)


def test_reciprocal_rank_rejects_duplicate_ranked_ids() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank({"A": 1}, ["A", "A"])


def test_all_ranking_metrics_reject_duplicate_ranked_ids() -> None:
    for metric in (recall_at_k, precision_at_k, ndcg_at_k):
        with pytest.raises(ValueError):
            metric({"A": 1}, ["A", "A"], 2)


@pytest.mark.parametrize("relevance", [{"A": -1}, {"A": 1.5}, {"A": True}])
def test_ranking_metrics_reject_invalid_relevance_grades(relevance) -> None:
    with pytest.raises(ValueError):
        recall_at_k(relevance, ["A"], 1)


def test_digest_verification_uses_expected_sha256() -> None:
    value = {"b": 2, "a": 1}
    expected = hashlib.sha256(b'{"a":1,"b":2}\n').hexdigest()
    assert canonical_sha256(value) == expected
    verify_sha256(value, expected)
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_sha256(value, "0" * 64)


def test_canonical_digest_rejects_non_json_values() -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_sha256({"value": object()})


def test_ndcg_remains_finite_for_large_integer_grades() -> None:
    assert ndcg_at_k({"A": 10_000}, ["A"], 1) == 1.0


def test_ndcg_perfect_multi_document_grade_1023_ranking_is_one() -> None:
    relevance = {"A": 1023, "B": 1023, "C": 1023, "D": 1023}
    assert ndcg_at_k(relevance, ["A", "B", "C", "D"], 4) == pytest.approx(1.0)


@pytest.mark.parametrize("expected", ["é" + "0" * 63, "g" * 64, "0" * 63, "0" * 65])
def test_digest_verification_rejects_malformed_expected_digest(expected: str) -> None:
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_sha256({"a": 1}, expected)
