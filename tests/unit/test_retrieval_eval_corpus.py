from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from recallops.evaluation.digests import canonical_json_bytes
from recallops.evaluation.retrieval_schema import (
    RetrievalCase,
    RetrievalCaseResult,
    RetrievalConfigurationMetrics,
    RetrievalConfigurationResult,
    RetrievalEvalCorpus,
    RetrievalEvalGates,
    RetrievalJudgment,
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
    with pytest.raises(TypeError):
        corpus.cases[0].judgments["MUTATED"] = RetrievalJudgment(relevance=3)  # type: ignore[index]


def _zero_metrics() -> RetrievalConfigurationMetrics:
    return RetrievalConfigurationMetrics(
        case_count=0,
        recall_at_1=0.0,
        recall_at_3=0.0,
        recall_at_5=0.0,
        precision_at_5=0.0,
        mean_reciprocal_rank=0.0,
        ndcg_at_5=0.0,
        citation_precision=0.0,
        required_fact_coverage=0.0,
        route_accuracy=0.0,
        abstention_accuracy=0.0,
        provenance_label_accuracy=0.0,
        budget_compliance=0.0,
        prohibited_hit_count=0,
        unsupported_answer_count=0,
        latency_p50_ms=0.0,
        latency_p95_ms=0.0,
    )


def _zero_gates() -> RetrievalEvalGates:
    return RetrievalEvalGates(
        route_accuracy=0.0,
        abstention_accuracy=0.0,
        provenance_label_accuracy=0.0,
        budget_compliance=0.0,
        prohibited_hit_count=0,
        unsupported_answer_count=0,
        agentic_recall_at_5=0.0,
        agentic_ndcg_at_5=0.0,
        fusion_recall_delta=0.0,
        rerank_ndcg_delta=0.0,
    )


@pytest.mark.parametrize("invalid", [0, True, float("nan"), float("inf"), -float("inf")])
def test_configuration_metrics_reject_non_exact_or_nonfinite_floats(
    invalid: object,
) -> None:
    payload = _zero_metrics().model_dump(mode="python")
    payload["recall_at_1"] = invalid

    with pytest.raises(ValidationError, match="strict finite float"):
        RetrievalConfigurationMetrics.model_validate(payload)


@pytest.mark.parametrize("invalid", [0, True, float("nan"), float("inf"), -float("inf")])
def test_evaluation_gates_reject_non_exact_or_nonfinite_floats(invalid: object) -> None:
    payload = _zero_gates().model_dump(mode="python")
    payload["route_accuracy"] = invalid

    with pytest.raises(ValidationError, match="strict finite float"):
        RetrievalEvalGates.model_validate(payload)


def test_exact_float_metrics_and_gates_round_trip_through_json() -> None:
    metrics = _zero_metrics().model_copy(update={"recall_at_1": 0.25})
    gates = _zero_gates().model_copy(update={"route_accuracy": 1.0})

    restored_metrics = RetrievalConfigurationMetrics.model_validate_json(
        metrics.model_dump_json()
    )
    restored_gates = RetrievalEvalGates.model_validate_json(gates.model_dump_json())

    assert restored_metrics == metrics
    assert type(restored_metrics.recall_at_1) is float
    assert restored_gates == gates
    assert type(restored_gates.route_accuracy) is float


def test_result_mapping_fields_are_deeply_immutable_and_serializable() -> None:
    default_result = RetrievalCaseResult(
        case_id="RET-001",
        family="exact_identifier",
    )
    result = RetrievalCaseResult(
        case_id="RET-001",
        family="exact_identifier",
        metric_contributions={"hit": True},
    )
    metrics = _zero_metrics()
    configuration = RetrievalConfigurationResult(
        name="sparse_bm25",
        results=(result,),
        metrics=metrics,
        family_metrics={"exact_identifier": metrics},
    )

    with pytest.raises(TypeError):
        default_result.metric_contributions["hit"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        result.metric_contributions["hit"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        configuration.family_metrics["lineage"] = metrics  # type: ignore[index]
    assert result.model_dump(mode="json")["metric_contributions"] == {"hit": True}
    assert configuration.model_dump(mode="json")["family_metrics"] == {
        "exact_identifier": metrics.model_dump(mode="json")
    }


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


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("unanswerable", "false"),
        ("rewrite_allowed", 0),
        ("top_k", "5"),
        ("question", 123),
    ],
)
def test_loader_rejects_canonical_but_coercible_case_scalars(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    payload = _payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    mutated = dict(cases[0])
    mutated[field] = invalid_value
    cases[0] = mutated

    with pytest.raises(ValidationError):
        load_retrieval_cases(_write_payload(tmp_path, payload))


def test_loader_rejects_string_relevance_grade(tmp_path: Path) -> None:
    payload = _payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    mutated = dict(cases[0])
    judgments = dict(mutated["judgments"])
    document_id = next(iter(judgments))
    judgments[document_id] = {"relevance": "3"}
    mutated["judgments"] = judgments
    cases[0] = mutated

    with pytest.raises(ValidationError):
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


def test_synthetic_acknowledgement_facts_match_source_booleans() -> None:
    corpus = load_retrieval_cases(CASE_PATH)
    dataset = json.loads(
        (DATA_DIR / "synthetic/northstar_demo/dataset.json").read_text(encoding="utf-8")
    )
    acknowledgements = {
        row["facility_id"]: row["acknowledged"]
        for row in dataset["facility_acknowledgements"]
    }
    checked = 0
    prefix = "NORTHSTAR-FACILITY_ACKNOWLEDGEMENTS-"
    for case in corpus.cases:
        facts = {
            fact.split("=", 1)[0]: fact.split("=", 1)[1]
            for fact in case.required_facts
            if "=" in fact
        }
        if "synthetic_acknowledged" not in facts:
            continue
        acknowledgement_ids = [
            document_id
            for document_id in case.required_document_ids
            if document_id.startswith(prefix)
        ]
        assert len(acknowledgement_ids) == 1
        facility_id = acknowledgement_ids[0].removeprefix(prefix)
        assert facts["synthetic_acknowledged"] == str(
            acknowledgements[facility_id]
        ).lower()
        checked += 1
    assert checked == 4


def test_repository_paths_expose_retrieval_artifacts(tmp_path: Path) -> None:
    paths = RepositoryPaths(tmp_path)

    assert paths.retrieval_evaluation_corpus == tmp_path / "data/evals/retrieval_cases.json"
    assert paths.retrieval_evaluation_report == tmp_path / "data/evals/retrieval_report.json"
