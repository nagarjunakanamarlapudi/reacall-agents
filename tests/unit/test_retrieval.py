import hashlib
import json
from time import perf_counter

import pytest
from pydantic import ValidationError

from recallops.retrieval.corpus import (
    KnowledgeCorpus,
    build_knowledge_artifacts,
)
from recallops.retrieval.hybrid import HybridIndex, reciprocal_rank_fusion
from recallops.retrieval.models import HybridSearchRequest


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def corpus() -> KnowledgeCorpus:
    return KnowledgeCorpus.load()


@pytest.fixture(scope="module")
def index(corpus: KnowledgeCorpus) -> HybridIndex:
    return HybridIndex(corpus.documents)


def test_corpus_indexes_every_scaled_record_and_every_frozen_public_row(
    corpus: KnowledgeCorpus,
) -> None:
    assert corpus.manifest.document_count == 1175
    assert corpus.manifest.source_counts == {"official": 10, "synthetic": 1165}
    assert corpus.manifest.record_type_counts["openfda_recall"] == 5
    assert corpus.manifest.record_type_counts["policy"] == 5
    assert corpus.manifest.record_type_counts["product"] == 48
    assert corpus.manifest.record_type_counts["lot"] == 144
    assert corpus.manifest.record_type_counts["event"] == 577
    assert corpus.manifest.record_type_counts["inventory_position"] == 216
    assert corpus.manifest.record_type_counts["supplier_shipment"] == 144
    assert corpus.manifest.record_type_counts["facility_acknowledgement"] == 18


def test_corpus_preserves_resolvable_citations_hashes_and_source_boundaries(
    corpus: KnowledgeCorpus,
) -> None:
    for document in corpus.documents:
        assert corpus.resolve(document.citation_id) == document
        assert document.content_hash == _sha256(document.text)
        assert document.source_url
        if document.source_class == "official":
            assert document.origin in {"OFFICIAL_OPENFDA_SNAPSHOT", "OFFICIAL_POLICY_REFERENCE"}
            assert document.audience_label.startswith("OFFICIAL")
        else:
            assert document.origin == "SYNTHETIC_RETAILER_DIGITAL_TWIN"
            assert document.audience_label == "SYNTHETIC — ACADEMIC DEMO"


def test_knowledge_artifact_builder_is_byte_deterministic_and_matches_committed_manifest(
    corpus: KnowledgeCorpus,
) -> None:
    first = build_knowledge_artifacts()
    second = build_knowledge_artifacts()

    assert first == second
    assert json.loads(first["manifest.json"])["corpus_sha256"] == corpus.manifest.corpus_sha256
    assert (
        first["policy_corpus.json"]
        == (corpus.data_dir / "knowledge" / "policy_corpus.json").read_bytes()
    )
    assert first["manifest.json"] == (corpus.data_dir / "knowledge" / "manifest.json").read_bytes()


@pytest.mark.parametrize(
    ("query", "source_filter", "record_type", "record_id"),
    [
        ("H-1230-2026", "official", "openfda_recall", "H-1230-2026"),
        ("080000000042", "synthetic", "product", "P-BG-042"),
        ("LOT-BG-042-03", "synthetic", "lot", "LOT-BG-042-03"),
    ],
)
def test_exact_identifiers_rank_their_lexical_record_first(
    index: HybridIndex,
    query: str,
    source_filter: str,
    record_type: str,
    record_id: str,
) -> None:
    response = index.search(
        HybridSearchRequest(
            query=query,
            top_k=5,
            source_filter=source_filter,
            intent="operational" if source_filter == "synthetic" else "regulatory",
        )
    )

    assert response.results[0].document.record_type == record_type
    assert response.results[0].document.record_id == record_id
    assert response.results[0].sparse_rank == 1
    assert "exact identifier" in " ".join(response.results[0].explanation)


def test_local_lsa_dense_retrieval_contributes_a_paraphrase_absent_from_sparse_top_k(
    index: HybridIndex,
) -> None:
    query = "How should a business follow an item through handoffs?"
    sparse = index.sparse_search(query, top_k=3, source_filter="official")
    dense = index.dense_search(query, top_k=10, source_filter="official")
    response = index.search(
        HybridSearchRequest(
            query=query,
            top_k=10,
            source_filter="official",
            intent="regulatory",
        )
    )
    citation_id = "FDA-TRACEABILITY-CONCEPTS"

    assert citation_id not in {item.citation_id for item in sparse}
    assert citation_id in {item.citation_id for item in dense}
    contribution = next(
        item for item in response.results if item.document.citation_id == citation_id
    )
    assert contribution.sparse_rank is None
    assert contribution.dense_rank is not None
    assert contribution.dense_score > 0
    assert response.index.dense_method == "local_tfidf_truncated_svd_lsa"


def test_rrf_preserves_both_component_ranks_scores_and_stable_ties() -> None:
    fused = reciprocal_rank_fusion(
        sparse=[("A", 7.5), ("B", 4.0)],
        dense=[("B", 0.8), ("A", 0.4)],
        rank_constant=60,
    )

    assert [item.citation_id for item in fused] == ["A", "B"]
    assert fused[0].model_dump() == {
        "citation_id": "A",
        "sparse_rank": 1,
        "sparse_score": 7.5,
        "dense_rank": 2,
        "dense_score": 0.4,
        "rrf_score": pytest.approx(1 / 61 + 1 / 62),
    }
    assert fused[1].sparse_rank == 2
    assert fused[1].dense_rank == 1


def test_reranker_promotes_intended_source_without_hiding_origin(index: HybridIndex) -> None:
    query = "recall response evidence facility"
    regulatory = index.search(
        HybridSearchRequest(query=query, top_k=6, source_filter="all", intent="regulatory")
    )
    operational = index.search(
        HybridSearchRequest(query=query, top_k=6, source_filter="all", intent="operational")
    )

    assert regulatory.results[0].document.source_class == "official"
    assert operational.results[0].document.source_class == "synthetic"
    assert all(result.document.origin for result in regulatory.results + operational.results)
    assert "regulatory source boost" in " ".join(regulatory.results[0].explanation)
    assert "operational source boost" in " ".join(operational.results[0].explanation)


def test_non_anchor_scaled_records_are_retrievable(index: HybridIndex) -> None:
    for query, expected in (
        ("Northstar Soft Cheese SKU 042", "P-BG-042"),
        ("LOT-BG-042-03 plant BG-P-02", "LOT-BG-042-03"),
        ("SHIP-LOT-BG-043-03", "SHIP-LOT-BG-043-03"),
        ("STORE-16 acknowledgement", "STORE-16"),
    ):
        response = index.search(
            HybridSearchRequest(
                query=query,
                top_k=8,
                source_filter="synthetic",
                intent="operational",
            )
        )
        assert expected in {item.document.record_id for item in response.results}


def test_source_filters_and_mixed_source_search_are_exact(index: HybridIndex) -> None:
    official = index.search(
        HybridSearchRequest(query="recall shipment", top_k=8, source_filter="official")
    )
    synthetic = index.search(
        HybridSearchRequest(query="recall shipment", top_k=8, source_filter="synthetic")
    )
    mixed = index.search(
        HybridSearchRequest(query="recall shipment", top_k=12, source_filter="all")
    )

    assert {item.document.source_class for item in official.results} == {"official"}
    assert {item.document.source_class for item in synthetic.results} == {"synthetic"}
    assert {item.document.source_class for item in mixed.results} == {"official", "synthetic"}


def test_two_indexes_return_identical_results_and_metadata(corpus: KnowledgeCorpus) -> None:
    request = HybridSearchRequest(
        query="trace LOT-BG-042-03 from supplier to store",
        top_k=10,
        source_filter="all",
        intent="mixed",
    )
    first = HybridIndex(corpus.documents).search(request).model_dump(mode="json")
    second = HybridIndex(corpus.documents).search(request).model_dump(mode="json")

    assert first == second
    assert first["index"]["corpus_sha256"] == corpus.manifest.corpus_sha256


def test_diversity_adjustment_is_visible_in_score_and_explanation(index: HybridIndex) -> None:
    response = index.search(
        HybridSearchRequest(
            query="recall product hazard",
            top_k=8,
            source_filter="official",
            intent="regulatory",
        )
    )
    repeated_type = next(
        item
        for item in response.results[1:]
        if item.document.record_type == response.results[0].document.record_type
    )

    assert "diversity penalty" in " ".join(repeated_type.explanation)
    assert repeated_type.rerank_score < max(
        item.rerank_score
        for item in response.results
        if item.document.record_type == repeated_type.document.record_type
    )


@pytest.mark.parametrize("top_k", [0, 21, True, 1.5, "5"])
def test_search_request_rejects_invalid_bounds(top_k: object) -> None:
    with pytest.raises(ValidationError):
        HybridSearchRequest(query="recall", top_k=top_k)


@pytest.mark.parametrize("query", ["", "   "])
def test_search_request_rejects_blank_query(query: str) -> None:
    with pytest.raises(ValidationError):
        HybridSearchRequest(query=query)


def test_scaled_local_index_build_and_queries_stay_within_generous_budget(
    corpus: KnowledgeCorpus,
) -> None:
    started = perf_counter()
    index = HybridIndex(corpus.documents)
    build_seconds = perf_counter() - started
    started = perf_counter()
    for query in (
        "H-1230-2026",
        "Possible Salmonella Enteritidis",
        "LOT-BG-042-03",
        "080000000042",
        "STORE-16 acknowledgement",
        "traceability lot handoffs",
        "Class I recall",
        "unaccounted units",
    ):
        index.search(HybridSearchRequest(query=query, top_k=8))
    query_seconds = perf_counter() - started

    assert build_seconds < 15
    assert query_seconds < 5
