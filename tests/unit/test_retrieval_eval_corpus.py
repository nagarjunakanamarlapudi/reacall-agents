from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from recallops.evaluation.digests import canonical_json_bytes
from recallops.evaluation.retrieval_schema import (
    RetrievalCase,
    RetrievalEvalCorpus,
    load_retrieval_cases,
)
from recallops.paths import DATA_DIR, RepositoryPaths
from recallops.retrieval.corpus import KnowledgeCorpus

EXPECTED_COUNTS = {
    "exact_identifier": 24,
    "semantic_product_hazard": 16,
    "lineage": 16,
    "reconciliation": 16,
    "cross_source": 8,
    "difficult_rewrite": 8,
    "abstention_adversarial": 8,
}
CASE_PATH = DATA_DIR / "evals" / "retrieval_cases.json"


def _payload() -> dict[str, object]:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def _write_payload(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "retrieval-cases.json"
    path.write_bytes(canonical_json_bytes(payload))
    return path


def test_retrieval_corpus_has_exact_balance_and_known_documents() -> None:
    corpus = load_retrieval_cases(CASE_PATH)

    assert Counter(case.family for case in corpus.cases) == EXPECTED_COUNTS
    assert len(corpus.cases) == 96
    known = {doc.citation_id for doc in KnowledgeCorpus.load().documents}
    cited = {
        doc_id
        for case in corpus.cases
        for doc_id in (
            *case.judgments,
            *case.required_document_ids,
            *case.prohibited_document_ids,
        )
    }
    assert cited <= known


def test_retrieval_corpus_is_immutable_and_bound_to_canonical_knowledge() -> None:
    corpus = load_retrieval_cases(CASE_PATH)

    assert isinstance(corpus, RetrievalEvalCorpus)
    assert corpus.knowledge_corpus_sha256 == KnowledgeCorpus.load().manifest.corpus_sha256
    with pytest.raises(ValidationError, match="frozen"):
        corpus.cases[0].question = "mutated"  # type: ignore[misc]


def test_loader_rejects_duplicate_case_id(tmp_path: Path) -> None:
    payload = _payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    duplicate = dict(cases[1])
    duplicate["id"] = cases[0]["id"]
    cases[1] = duplicate

    with pytest.raises(ValueError, match="duplicate retrieval case IDs"):
        load_retrieval_cases(_write_payload(tmp_path, payload))


def test_loader_rejects_unknown_document_id(tmp_path: Path) -> None:
    payload = _payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    mutated = dict(cases[0])
    mutated["judgments"] = {"UNKNOWN-DOCUMENT": {"relevance": 3}}
    mutated["required_document_ids"] = ["UNKNOWN-DOCUMENT"]
    mutated["prohibited_document_ids"] = []
    cases[0] = mutated

    with pytest.raises(ValueError, match="unknown cited document IDs"):
        load_retrieval_cases(_write_payload(tmp_path, payload))


def test_case_rejects_source_route_contradiction() -> None:
    case = load_retrieval_cases(CASE_PATH).cases[0]

    with pytest.raises(ValidationError, match="expected route excludes required evidence source"):
        RetrievalCase.model_validate(
            case.model_dump(mode="json") | {"expected_route": ["official"]}
        )


def test_case_rejects_prohibited_required_overlap() -> None:
    case = load_retrieval_cases(CASE_PATH).cases[0]

    with pytest.raises(ValidationError, match="required and prohibited document IDs overlap"):
        RetrievalCase.model_validate(
            case.model_dump(mode="json")
            | {"prohibited_document_ids": [case.required_document_ids[0]]}
        )


def test_case_rejects_missing_rationale() -> None:
    case = load_retrieval_cases(CASE_PATH).cases[0]

    with pytest.raises(ValidationError, match="rationale"):
        RetrievalCase.model_validate(case.model_dump(mode="json") | {"rationale": "   "})


def test_loader_rejects_wrong_family_count(tmp_path: Path) -> None:
    payload = _payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    mutated = dict(cases[0])
    mutated["family"] = "lineage"
    cases[0] = mutated

    with pytest.raises(ValueError, match="retrieval family counts must equal"):
        load_retrieval_cases(_write_payload(tmp_path, payload))


def test_loader_rejects_knowledge_digest_mutation(tmp_path: Path) -> None:
    payload = _payload()
    payload["knowledge_corpus_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="knowledge corpus digest mismatch"):
        load_retrieval_cases(_write_payload(tmp_path, payload))


def test_loader_rejects_noncanonical_json(tmp_path: Path) -> None:
    path = tmp_path / "retrieval-cases.json"
    path.write_text(json.dumps(_payload(), indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical JSON"):
        load_retrieval_cases(path)


def test_cases_have_independent_facts_and_bounded_read_only_budgets() -> None:
    corpus = load_retrieval_cases(CASE_PATH)

    assert all(case.rationale.strip() for case in corpus.cases)
    assert all(1 <= case.top_k <= 20 for case in corpus.cases)
    assert all(1 <= case.max_queries <= 4 for case in corpus.cases)
    assert all(1 <= case.max_hops <= 2 for case in corpus.cases)
    assert all(1 <= case.max_reads <= 8 for case in corpus.cases)
    answerable = [case for case in corpus.cases if not case.unanswerable]
    assert all(case.required_document_ids and case.required_facts for case in answerable)
    assert all(
        any(fact.casefold() not in case.question.casefold() for fact in case.required_facts)
        for case in answerable
    )


def test_repository_paths_expose_retrieval_artifacts(tmp_path: Path) -> None:
    paths = RepositoryPaths(tmp_path)

    assert paths.retrieval_evaluation_corpus == tmp_path / "data/evals/retrieval_cases.json"
    assert paths.retrieval_evaluation_report == tmp_path / "data/evals/retrieval_report.json"
