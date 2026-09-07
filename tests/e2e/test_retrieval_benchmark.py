"""Offline retrieval measurements must stay complete and independently verifiable."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from recallops.paths import DATA_DIR
from recallops.retrieval.agentic import AgenticRetriever, ClosedRetrievalGateway
from recallops.retrieval.corpus import KnowledgeCorpus
from recallops.retrieval.hybrid import load_local_hybrid_index


@pytest.mark.parametrize(
    ("question", "route"),
    [
        ("What classification and plant code are recorded for lot LOT-EXACT-170?", ("synthetic",)),
        (
            "Trace movement event EV-002 for lot LOT-EXACT-170 forward: which parent receipt does it reference?",
            ("synthetic",),
        ),
        (
            "Reconcile lot LOT-EXACT-170: report received, on-hand, quarantined, sold, returned, disposed, and unexplained units.",
            ("synthetic",),
        ),
        (
            "Compare the official affected plant/date code with the plant code and Julian date recorded for LOT-PROBABLE-160.",
            ("official", "synthetic"),
        ),
        (
            "Compare the official visibility-event questions of what, when, and where with the synthetic movement event EV-005.",
            ("official", "synthetic"),
        ),
        ("Quote the confidential regulator email for recall H-9999-2099.", ("official",)),
        (
            "Certify that unknown facility DC-MARS completed every corrective action.",
            ("synthetic",),
        ),
    ],
)
def test_routing_distinguishes_operational_records_from_regulatory_policy(question, route):
    assert AgenticRetriever._plan(question)[1] == route


@pytest.mark.asyncio
async def test_direct_gateway_honors_directory_reuses_index_and_retains_seal(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    gateway = ClosedRetrievalGateway.direct(data_dir=data)
    first = load_local_hybrid_index(str(data.resolve()))
    before = load_local_hybrid_index.cache_info()
    for _ in range(2):
        response = await gateway.call("regulatory_search", "FDA recall", top_k=5, record_types=())
        assert response["index"]["corpus_sha256"] == first.metadata.corpus_sha256
    assert load_local_hybrid_index.cache_info().hits >= before.hits + 2
    with pytest.raises(TypeError, match="factory-built"):
        ClosedRetrievalGateway("direct", data_dir=data)
    with pytest.raises(AttributeError, match="sealed"):
        gateway._config = None
    changed = json.loads((data / "knowledge/manifest.json").read_text())
    changed["corpus_sha256"] = "0" * 64
    (data / "knowledge/manifest.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        await gateway.call("regulatory_search", "FDA recall", top_k=5, record_types=())


@pytest.mark.asyncio
async def test_retrieval_benchmark_runs_every_case_in_every_configuration(tmp_path: Path) -> None:
    from recallops.evaluation.retrieval_benchmark import (
        load_retrieval_report,
        run_retrieval_benchmark,
    )

    output = tmp_path / "report.json"
    report = await run_retrieval_benchmark(DATA_DIR / "evals/retrieval_cases.json", output)
    assert [item.name for item in report.configurations] == [
        "sparse_bm25",
        "dense_lsa",
        "naive_hybrid",
        "rrf_fusion",
        "rrf_plus_rerank",
        "agentic_rag",
    ]
    assert all(len(item.results) == 96 for item in report.configurations)
    assert all(item.metrics.case_count == 96 for item in report.configurations)
    assert not any(row.error_code for item in report.configurations for row in item.results)
    assert load_retrieval_report(output, DATA_DIR / "evals/retrieval_cases.json") == report
    assert report.gates.route_accuracy == 1.0
    assert report.gates.prohibited_hit_count == 0
    assert report.gate_passed
    assert report.gates.abstention_accuracy == 1.0
    assert sum(row.answered for row in report.configurations[-1].results) == 88
    assert report.configurations[0].metrics.prohibited_hit_count > 0
    assert report.configurations[-1].results[91].ranked_document_ids
    assert not report.configurations[-1].results[91].cited_document_ids
    assert report.configurations[-1].metrics.denominators["recall_at_5"] == 88
    assert report.configurations[-1].metrics.denominators["abstention_accuracy"] == 96


@pytest.mark.asyncio
async def test_report_rejects_missing_forged_and_stale_measurements(tmp_path):
    from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
    from recallops.evaluation.retrieval_benchmark import (
        load_retrieval_report,
        run_retrieval_benchmark,
    )

    output = tmp_path / "report.json"
    await run_retrieval_benchmark(DATA_DIR / "evals/retrieval_cases.json", output)
    original = json.loads(output.read_bytes())
    for change in (
        "missing",
        "aggregate",
        "contribution",
        "case_digest",
        "index_digest",
        "gate",
        "stop",
        "budget",
    ):
        payload = deepcopy(original)
        if change == "missing":
            payload["configurations"][0]["results"].pop()
        elif change == "aggregate":
            payload["configurations"][0]["metrics"]["recall_at_5"] = 0.123
        elif change == "contribution":
            payload["configurations"][0]["results"][0]["metric_contributions"]["recall_at_5"] = (
                0.123
            )
        elif change == "case_digest":
            payload["retrieval_case_corpus_sha256"] = "0" * 64
        elif change == "index_digest":
            payload["knowledge_corpus_sha256"] = "0" * 64
        elif change == "gate":
            payload["gate_passed"] = not payload["gate_passed"]
        elif change == "stop":
            payload["configurations"][-1]["results"][0]["stop_reason"] = "execution_error"
        else:
            payload["configurations"][-1]["results"][0]["query_count"] = 0
        payload.pop("report_sha256")
        payload["report_sha256"] = canonical_sha256(payload)
        output.write_bytes(canonical_json_bytes(payload))
        with pytest.raises(ValueError):
            load_retrieval_report(output, DATA_DIR / "evals/retrieval_cases.json")


def test_negative_deltas_are_not_clamped():
    from recallops.evaluation.retrieval_benchmark import measured_delta

    assert measured_delta(0.25, 0.75) == -0.5


def test_four_document_fixture_distinguishes_production_rankers():
    from recallops.evaluation.retrieval_benchmark import rank_configuration
    from recallops.retrieval.hybrid import HybridIndex

    corpus = KnowledgeCorpus.load()
    identifiers = (
        "FDA-RECALL-CLASSIFICATION",
        "FDA-RECALL-EFFECTIVENESS",
        "FDA-TRACEABILITY-CONCEPTS",
        "OPENFDA-EVENT-99453",
    )
    index = HybridIndex([corpus.resolve(identifier) for identifier in identifiers])
    expected = {
        "sparse_bm25": (3, 0, 2, 1),
        "dense_lsa": (1, 0, 3, 2),
        "rrf_fusion": (3, 0, 1, 2),
        "rrf_plus_rerank": (0, 1, 3, 2),
    }
    for name, ranks in expected.items():
        results = rank_configuration(index, "recall classification food eggs", name, top_k=4)
        assert tuple(item.document.citation_id for item in results) == tuple(
            identifiers[i] for i in ranks
        )


@pytest.mark.asyncio
async def test_failure_persists_row_and_cannot_pass_gate(tmp_path, monkeypatch):
    from recallops.evaluation.retrieval_benchmark import run_retrieval_benchmark
    from recallops.retrieval.hybrid import HybridIndex

    original = HybridIndex.sparse_search

    def fail_one(self, query, **kwargs):
        if "product ID P-EXACT?" in query:
            raise RuntimeError("SECRET never serialized")
        return original(self, query, **kwargs)

    monkeypatch.setattr(HybridIndex, "sparse_search", fail_one)
    output = tmp_path / "failed.json"
    report = await run_retrieval_benchmark(DATA_DIR / "evals/retrieval_cases.json", output)
    assert all(len(item.results) == 96 for item in report.configurations)
    assert report.configurations[0].results[0].error_code == "retrieval_error"
    assert not report.gate_passed
    assert b"SECRET" not in output.read_bytes()


def test_fact_probes_require_correct_numeric_field():
    from recallops.evaluation.retrieval_benchmark import _supported_facts
    from recallops.evaluation.retrieval_schema import load_retrieval_cases

    corpus = load_retrieval_cases(DATA_DIR / "evals/retrieval_cases.json")
    case = corpus.cases[56].model_copy(update={"required_facts": ("disposed=50", "unaccounted=50")})
    document = KnowledgeCorpus.load().resolve("NORTHSTAR-LOTS-LOT-EXACT-170")
    assert _supported_facts(case, [document]) == ("unaccounted=50",)
    ambiguous = case.model_copy(update={"required_facts": ("plant_code=P-1950?",)})
    assert _supported_facts(ambiguous, [document]) == ()


def test_budget_contract_permits_complete_source_routes_and_rewrite():
    from pydantic import ValidationError

    from recallops.evaluation.retrieval_schema import RetrievalCase, load_retrieval_cases

    corpus = load_retrieval_cases(DATA_DIR / "evals/retrieval_cases.json")
    for case, changes in [
        (corpus.cases[72], {"max_queries": 1}),
        (corpus.cases[80], {"max_queries": 2}),
    ]:
        with pytest.raises(ValidationError, match="complete source route"):
            RetrievalCase.model_validate({**case.model_dump(), **changes})


def test_weighted_production_rrf_uses_both_rankings_and_rejects_invalid_weights():
    from recallops.retrieval.hybrid import reciprocal_rank_fusion

    ranked = reciprocal_rank_fusion(
        sparse=[("A", 1.0), ("B", 0.5)],
        dense=[("B", 1.0), ("C", 0.5)],
        sparse_weight=0.9,
        dense_weight=0.1,
    )
    assert [row.citation_id for row in ranked] == ["B", "A", "C"]
    assert ranked[0].rrf_score == pytest.approx(0.9 / 62 + 0.1 / 61)
    for invalid in [0.0, -1.0, float("nan"), True]:
        with pytest.raises(ValueError, match="weight"):
            reciprocal_rank_fusion(sparse=[], dense=[], sparse_weight=invalid)


def test_mixed_merge_preserves_both_source_families_at_front():
    from recallops.retrieval.models import HybridSearchRequest

    index = load_local_hybrid_index(str(DATA_DIR))
    question = "official recall communication for DC-SOUTH"
    responses = [
        index.search(HybridSearchRequest(query=question, source_filter=source, intent=intent))
        for source, intent in [("official", "regulatory"), ("synthetic", "operational")]
    ]
    merged = AgenticRetriever._merge(responses)
    assert {row.document.source_class for row in merged[:2]} == {"official", "synthetic"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "What product name and UPC are recorded for product ID P-EXACT?",
        "Reconcile lot LOT-EXACT-170: report received, on-hand, quarantined, sold, returned, disposed, and unexplained units.",
        "Which official record concerns coconut almond bites recalled for an allergen missing from the label, and which allergen is involved?",
        "Trace movement event EV-LOT-BG-001-02-MOVE for lot LOT-BG-001-02 backward: which parent event does it reference and which origin facility sent the movement?",
    ],
)
async def test_critic_accepts_supported_record_questions_without_dropping_material_checks(question):
    outcome = await AgenticRetriever(ClosedRetrievalGateway.direct(), top_k_per_source=5).retrieve(
        question
    )
    assert outcome.coverage_satisfied, outcome.evidence_gaps
    assert "NORTHSTAR-PRODUCTS-P-NEAR" not in {item.citation_id for item in outcome.citations}


def test_rerank_signal_scale_bounds_auxiliary_boosts():
    from recallops.retrieval.hybrid import reciprocal_rank_fusion

    index = load_local_hybrid_index(str(DATA_DIR))
    sparse = index.sparse_search("FDA recall", top_k=5)
    fused = reciprocal_rank_fusion(sparse=[(x.citation_id, x.score) for x in sparse], dense=[])
    full = {
        x.document.citation_id: x
        for x in index.rerank_fused("FDA recall", fused, intent="regulatory")
    }
    bounded = index.rerank_fused("FDA recall", fused, intent="regulatory", signal_weight=0.05)
    for row in bounded:
        original = full[row.document.citation_id]
        assert row.rerank_score == pytest.approx(
            row.rrf_score + 0.05 * (original.rerank_score - row.rrf_score)
        )


@pytest.mark.asyncio
async def test_report_regeneration_preserves_every_non_timing_measurement(tmp_path):
    from recallops.evaluation.retrieval_benchmark import run_retrieval_benchmark

    def without_timing(report):
        value = report.model_dump(mode="json")
        value.pop("report_sha256")
        for configuration in value["configurations"]:
            for row in configuration["results"]:
                row.pop("duration_ms")
            for metrics in [configuration["metrics"], *configuration["family_metrics"].values()]:
                metrics.pop("latency_p50_ms")
                metrics.pop("latency_p95_ms")
        return value

    first = await run_retrieval_benchmark(
        DATA_DIR / "evals/retrieval_cases.json", tmp_path / "first.json"
    )
    second = await run_retrieval_benchmark(
        DATA_DIR / "evals/retrieval_cases.json", tmp_path / "second.json"
    )
    assert without_timing(first) == without_timing(second)


def test_abstaining_on_answerable_case_is_a_gate_failure():
    from recallops.evaluation.retrieval_benchmark import _contributions
    from recallops.evaluation.retrieval_schema import RetrievalCaseResult, load_retrieval_cases

    case = load_retrieval_cases(DATA_DIR / "evals/retrieval_cases.json").cases[0]
    row = RetrievalCaseResult(case_id=case.id, family=case.family)
    assert _contributions(case, row)["abstention_accuracy"] == 0.0


@pytest.mark.parametrize(
    ("initial", "final", "expected"),
    [
        (("FDA-RECALL-EFFECTIVENESS",), (), (0, 1, 0)),
        ((), ("FDA-RECALL-EFFECTIVENESS",), (1, 0, 0)),
        (("FDA-RECALL-EFFECTIVENESS",), ("FDA-RECALL-EFFECTIVENESS",), (0, 0, 1)),
    ],
)
def test_rewrite_outcomes_compare_same_agentic_first_pass(initial, final, expected):
    from recallops.evaluation.retrieval_benchmark import _contributions, _metrics
    from recallops.evaluation.retrieval_schema import RetrievalCaseResult, load_retrieval_cases

    case = load_retrieval_cases(DATA_DIR / "evals/retrieval_cases.json").cases[80]
    row = RetrievalCaseResult(
        case_id=case.id,
        family=case.family,
        initial_ranked_document_ids=initial,
        ranked_document_ids=final,
        rewrite_used=True,
    )
    row = row.model_copy(update={"metric_contributions": _contributions(case, row)})
    metrics = _metrics([row], [case], measure_rewrite=True)
    assert (
        metrics.rewrite_win_count,
        metrics.rewrite_loss_count,
        metrics.rewrite_no_change_count,
    ) == expected


def test_review_cached_documents_and_nested_metadata_are_deeply_immutable():
    from recallops.retrieval.models import KnowledgeDocument

    original = KnowledgeCorpus.load().documents[0]
    metadata = {"claims": {"reactor": ["unsupported"]}}
    document = KnowledgeDocument.model_validate({**original.model_dump(), "metadata": metadata})
    metadata["claims"]["reactor"].append("injected")
    assert document.metadata["claims"]["reactor"] == ("unsupported",)
    with pytest.raises(TypeError):
        document.metadata["claims"]["reactor"] = ("injected",)
    with pytest.raises(TypeError):
        KnowledgeCorpus.load().manifest.source_counts["official"] = 999
    cached = load_local_hybrid_index(str(DATA_DIR)).documents[0]
    try:
        with pytest.raises(TypeError):
            cached.metadata["reactor"] = "injected"
    finally:
        load_local_hybrid_index.cache_clear()


@pytest.mark.asyncio
async def test_review_cached_content_mismatch_is_rejected(monkeypatch):
    index = load_local_hybrid_index(str(DATA_DIR))
    changed = index.documents[0].model_copy(update={"metadata": {"reactor": True}})
    monkeypatch.setattr(index, "documents", (changed, *index.documents[1:]))
    with pytest.raises(ValueError, match="corpus"):
        await ClosedRetrievalGateway.direct().call(
            "regulatory_search", "FDA classification", top_k=5, record_types=()
        )


@pytest.mark.asyncio
async def test_review_reactor_query_stays_unsupported_across_cache_reuse():
    for _ in range(2):
        result = await AgenticRetriever(ClosedRetrievalGateway.direct()).retrieve(
            "What reactor classification does FDA provide?"
        )
        assert not result.coverage_satisfied
        assert any("reactor" in gap for gap in result.evidence_gaps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "unsupported"),
    [
        ("Which test determines the classification of recall H-1230-2026?", "test"),
        ("What test was recorded for product P-EXACT?", "test"),
        ("What care is required for Salmonella?", "care"),
        ("Which test does official effectiveness guidance test?", "test"),
        ("What care do visibility semantics care about?", "care"),
        ("Recover from Salmonella.", "recover"),
        ("Decode Salmonella.", "decode"),
    ],
)
async def test_review_substantive_requested_terms_are_not_ignored(question, unsupported):
    result = await AgenticRetriever(ClosedRetrievalGateway.direct()).retrieve(question)
    assert not result.coverage_satisfied
    assert any(f"Unsupported concept: {unsupported}" in gap for gap in result.evidence_gaps)


@pytest.mark.asyncio
async def test_review_accepted_citations_retain_required_general_policy():
    question = "What official effectiveness guidance applies to recall H-1230-2026?"
    result = await AgenticRetriever(ClosedRetrievalGateway.direct()).retrieve(question)
    assert result.coverage_satisfied
    ids = {item.citation_id for item in result.citations}
    assert {"OPENFDA-H-1230-2026", "FDA-RECALL-EFFECTIVENESS"} <= ids
    accepted = tuple(item for item in result.evidence if item.document.citation_id in ids)
    assert AgenticRetriever._critic(question, ("official",), accepted, {}) == ()


@pytest.mark.asyncio
async def test_review_resigned_stop_rewrite_and_calibration_forgeries_fail(tmp_path):
    from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
    from recallops.evaluation.retrieval_benchmark import (
        load_retrieval_report,
        run_retrieval_benchmark,
    )

    path = tmp_path / "report.json"
    await run_retrieval_benchmark(DATA_DIR / "evals/retrieval_cases.json", path)
    original = json.loads(path.read_bytes())
    mutations = [
        (
            "after_rewrite_without_rewrite",
            -1,
            0,
            {"stop_reason": "coverage_satisfied_after_rewrite"},
        ),
        ("disabled_stalled", -1, 88, {"stop_reason": "progress_stalled"}),
        ("successful_baseline_error", 0, 0, {"stop_reason": "execution_error"}),
        ("successful_baseline_stop", 0, 0, {"stop_reason": "coverage_satisfied"}),
        (
            "unnecessary_rewrite",
            -1,
            80,
            {
                "rewrite_used": True,
                "hop_count": 2,
                "query_count": 4,
                "read_count": 4,
                "stop_reason": "coverage_satisfied_after_rewrite",
            },
        ),
    ]
    for name, config, row, changes in mutations:
        payload = deepcopy(original)
        payload["configurations"][config]["results"][row].update(changes)
        payload.pop("report_sha256")
        payload["report_sha256"] = canonical_sha256(payload)
        path.write_bytes(canonical_json_bytes(payload))
        with pytest.raises(ValueError, match=".") as failure:
            load_retrieval_report(path, DATA_DIR / "evals/retrieval_cases.json")
        assert failure.value, name
    for changes in [
        {"rrf_sparse_weight": 0.1, "rrf_dense_weight": 0.9},
        {"rrf_rank_constant": 60},
        {"rerank_signal_weight": 1.0},
        {"agentic_rrf_rank_constant": 1},
        {"agentic_rrf_dense_weight": 0.1},
        {"agentic_rrf_sparse_weight": 0.9},
        {"agentic_rerank_signal_weight": 0.05},
        {"calibrated_configurations": []},
        {"calibrated_configurations": ["rrf_fusion"]},
    ]:
        payload = {**deepcopy(original), **changes}
        payload.pop("report_sha256")
        payload["report_sha256"] = canonical_sha256(payload)
        path.write_bytes(canonical_json_bytes(payload))
        with pytest.raises(ValueError):
            load_retrieval_report(path, DATA_DIR / "evals/retrieval_cases.json")


@pytest.mark.asyncio
async def test_review_resigned_answer_cannot_borrow_support_from_uncited_candidates(tmp_path):
    from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256
    from recallops.evaluation.retrieval_benchmark import (
        _configuration,
        _contributions,
        _gates,
        _supported_facts,
        load_retrieval_report,
        run_retrieval_benchmark,
    )
    from recallops.evaluation.retrieval_schema import RetrievalCaseResult, load_retrieval_cases

    path = tmp_path / "report.json"
    report = await run_retrieval_benchmark(DATA_DIR / "evals/retrieval_cases.json", path)
    corpus = load_retrieval_cases(DATA_DIR / "evals/retrieval_cases.json")
    case = corpus.cases[78]
    original = report.configurations[-1].results[78]
    document = KnowledgeCorpus.load().resolve("NORTHSTAR-FACILITY_ACKNOWLEDGEMENTS-STORE-01")
    assert "FDA-RECALL-EFFECTIVENESS" in original.ranked_document_ids
    row = original.model_copy(
        update={
            "cited_document_ids": (document.citation_id,),
            "cited_facts": _supported_facts(case, [document]),
            "provenance": {document.citation_id: document.source_class},
        }
    )
    row = RetrievalCaseResult.model_validate(
        {**row.model_dump(), "metric_contributions": _contributions(case, row)}
    )
    results = list(report.configurations[-1].results)
    results[78] = row
    configurations = (
        *report.configurations[:-1],
        _configuration("agentic_rag", tuple(results), corpus),
    )
    gates, passed = _gates(configurations)
    forged = report.model_copy(
        update={"configurations": configurations, "gates": gates, "gate_passed": passed}
    )
    payload = forged.model_dump(mode="json", exclude={"report_sha256"})
    payload["report_sha256"] = canonical_sha256(payload)
    path.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ValueError, match="accepted citations"):
        load_retrieval_report(path, DATA_DIR / "evals/retrieval_cases.json")
