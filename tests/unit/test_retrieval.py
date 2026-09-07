import hashlib
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError

from recallops.retrieval.corpus import (
    TRUSTED_OPENFDA_METADATA_SHA256,
    TRUSTED_OPENFDA_SNAPSHOT_SHA256,
    TRUSTED_POLICY_CORPUS_SHA256,
    TRUSTED_SYNTHETIC_DATASET_SHA256,
    TRUSTED_SYNTHETIC_MANIFEST_SHA256,
    KnowledgeCorpus,
    KnowledgeManifest,
    _build_documents,
    build_knowledge_artifacts,
)
from recallops.retrieval.hybrid import HybridIndex, reciprocal_rank_fusion
from recallops.retrieval.models import (
    ComponentHit,
    FusionRecord,
    HybridSearchRequest,
    HybridSearchResult,
)


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


def test_openfda_corpus_documents_cite_the_five_record_capture_endpoint(
    corpus: KnowledgeCorpus,
) -> None:
    """Break caught: neighboring snapshot rows are attributed to an exact flagship query."""

    openfda = [item for item in corpus.documents if item.record_type == "openfda_recall"]

    assert len(openfda) == 5
    assert {item.source_url for item in openfda} == {
        "https://api.fda.gov/food/enforcement.json?limit=5&sort=report_date%3Adesc"
    }


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


def _write_self_consistent_attacker_manifest(data_dir: Path) -> None:
    """Model an attacker who can recompute every self-declared checksum."""

    knowledge_dir = data_dir / "knowledge"
    documents = _build_documents(data_dir, knowledge_dir)
    source_paths = {
        "policy_corpus.json": knowledge_dir / "policy_corpus.json",
        "public/H-1230-2026.json": data_dir / "public" / "H-1230-2026.json",
        "public/H-1230-2026.metadata.json": data_dir / "public" / "H-1230-2026.metadata.json",
        "synthetic/northstar_demo/dataset.json": data_dir
        / "synthetic"
        / "northstar_demo"
        / "dataset.json",
        "synthetic/northstar_demo/manifest.json": data_dir
        / "synthetic"
        / "northstar_demo"
        / "manifest.json",
    }
    corpus_bytes = (
        json.dumps(
            [item.model_dump(mode="json") for item in documents],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode()
    manifest = KnowledgeManifest(
        schema_name="recallops.hybrid-knowledge-corpus",
        schema_version="1.0.0",
        generated_at="2026-08-30T00:00:00Z",
        document_count=len(documents),
        source_counts=dict(sorted(Counter(item.source_class for item in documents).items())),
        record_type_counts=dict(sorted(Counter(item.record_type for item in documents).items())),
        source_checksums={
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in sorted(source_paths.items())
        },
        corpus_sha256=hashlib.sha256(corpus_bytes).hexdigest(),
    )
    (knowledge_dir / "manifest.json").write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "target",
    ["policy", "openfda", "openfda_metadata", "synthetic", "synthetic_manifest"],
)
def test_independent_trust_anchors_reject_self_consistent_raw_source_tampering(
    tmp_path: Path,
    target: str,
) -> None:
    """Break caught: attacker-controlled source and manifests authenticate each other."""

    data_dir = tmp_path / "data"
    shutil.copytree(Path("data"), data_dir)
    if target == "policy":
        path = data_dir / "knowledge" / "policy_corpus.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[0]["text"] += " Attacker-authored policy statement."
        payload[0]["content_hash"] = hashlib.sha256(payload[0]["text"].encode()).hexdigest()
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    elif target == "openfda":
        path = data_dir / "public" / "H-1230-2026.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["results"][0]["reason_for_recall"] = "Attacker-authored hazard."
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        metadata_path = data_dir / "public" / "H-1230-2026.metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    elif target == "openfda_metadata":
        path = data_dir / "public" / "H-1230-2026.metadata.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source_url"] = "https://attacker.invalid/fake-regulatory-source"
        path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    elif target == "synthetic":
        path = data_dir / "synthetic" / "northstar_demo" / "dataset.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["products"][0]["name"] = "Attacker-authored product"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        manifest_path = data_dir / "synthetic" / "northstar_demo" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["checksums"]["dataset.json"] = digest
        manifest["sha256"] = digest
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    else:
        path = data_dir / "synthetic" / "northstar_demo" / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    if target not in {"openfda", "openfda_metadata"}:
        _write_self_consistent_attacker_manifest(data_dir)

    with pytest.raises(ValueError, match="independent trust anchor"):
        KnowledgeCorpus.load(data_dir=data_dir)


def test_committed_manifest_source_hashes_equal_independent_reviewed_anchors(
    corpus: KnowledgeCorpus,
) -> None:
    assert corpus.manifest.source_checksums["policy_corpus.json"] == (TRUSTED_POLICY_CORPUS_SHA256)
    assert corpus.manifest.source_checksums["public/H-1230-2026.json"] == (
        TRUSTED_OPENFDA_SNAPSHOT_SHA256
    )
    assert corpus.manifest.source_checksums["public/H-1230-2026.metadata.json"] == (
        TRUSTED_OPENFDA_METADATA_SHA256
    )
    assert (
        corpus.manifest.source_checksums["synthetic/northstar_demo/dataset.json"]
        == TRUSTED_SYNTHETIC_DATASET_SHA256
    )
    assert (
        corpus.manifest.source_checksums["synthetic/northstar_demo/manifest.json"]
        == TRUSTED_SYNTHETIC_MANIFEST_SHA256
    )


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


@pytest.mark.parametrize("bad_score", [True, 1, "1.0", math.nan, math.inf, -math.inf])
def test_public_retrieval_scores_reject_non_float_or_nonfinite_values(
    corpus: KnowledgeCorpus,
    bad_score: object,
) -> None:
    """Break caught: coercible or non-JSON scores reach ordering and serialization."""

    constructors = (
        lambda: ComponentHit(citation_id="A", score=bad_score),
        lambda: ComponentHit(citation_id="A", score=1.0, term_contributions={"recall": bad_score}),
        lambda: FusionRecord(citation_id="A", rrf_score=bad_score),
        lambda: FusionRecord(citation_id="A", sparse_score=bad_score, rrf_score=0.1),
        lambda: FusionRecord(citation_id="A", dense_score=bad_score, rrf_score=0.1),
        lambda: HybridSearchResult(
            document=corpus.documents[0],
            sparse_score=bad_score,
            rrf_score=0.1,
            rerank_score=0.2,
            explanation=("test",),
        ),
        lambda: HybridSearchResult(
            document=corpus.documents[0],
            dense_score=bad_score,
            rrf_score=0.1,
            rerank_score=0.2,
            explanation=("test",),
        ),
        lambda: HybridSearchResult(
            document=corpus.documents[0],
            rrf_score=bad_score,
            rerank_score=0.2,
            explanation=("test",),
        ),
        lambda: HybridSearchResult(
            document=corpus.documents[0],
            rrf_score=0.1,
            rerank_score=bad_score,
            explanation=("test",),
        ),
        lambda: HybridSearchResult(
            document=corpus.documents[0],
            rrf_score=0.1,
            rerank_score=0.2,
            matched_terms={"recall": bad_score},
            explanation=("test",),
        ),
    )
    for constructor in constructors:
        with pytest.raises(ValidationError):
            constructor()

    with pytest.raises((TypeError, ValueError), match="finite float"):
        reciprocal_rank_fusion(sparse=[("A", bad_score)], dense=[])


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
