"""Measured, read-only ablations over the production local retrieval stack.

Ranking metrics macro-average answerable cases (empty relevance is undefined as
a quality target). Abstention accuracy requires supported accepted evidence for
answerable cases and abstention for unanswerable cases. The
denominators are persisted, including zero for inapplicable family metrics.
Facts are evidence-support probes from the labels, never inputs to retrieval.
Latency is measured wall time; deterministic comparisons exclude latency only.
The calibrated weights apply to the single-pass RRF ablations; agentic retrieval
retains the production search defaults, including its source diversity selector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter_ns

import numpy as np

from recallops.evaluation.digests import canonical_json_bytes, canonical_sha256, verify_sha256
from recallops.evaluation.ranking import ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank
from recallops.evaluation.retrieval_schema import (
    EXPECTED_RETRIEVAL_FAMILY_COUNTS,
    RetrievalCase,
    RetrievalCaseResult,
    RetrievalConfigurationMetrics,
    RetrievalConfigurationName,
    RetrievalConfigurationResult,
    RetrievalEvalCorpus,
    RetrievalEvalGates,
    RetrievalEvalReport,
    load_retrieval_cases,
)
from recallops.paths import DATA_DIR
from recallops.retrieval.agentic import (
    AgenticRetriever,
    ClosedRetrievalGateway,
    RetrievalBudgets,
    RetrievalInterruption,
)
from recallops.retrieval.corpus import KnowledgeCorpus
from recallops.retrieval.hybrid import HybridIndex, load_local_hybrid_index, reciprocal_rank_fusion
from recallops.retrieval.models import HybridSearchResult, KnowledgeDocument

CONFIGURATIONS: tuple[RetrievalConfigurationName, ...] = (
    "sparse_bm25",
    "dense_lsa",
    "naive_hybrid",
    "rrf_fusion",
    "rrf_plus_rerank",
    "agentic_rag",
)
RANKING_METRICS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "precision_at_5",
    "mean_reciprocal_rank",
    "ndcg_at_5",
)
RATE_METRICS = (
    "citation_precision",
    "required_fact_coverage",
    "route_accuracy",
    "abstention_accuracy",
    "provenance_label_accuracy",
    "budget_compliance",
)
# Identifier-heavy local business records benefit from retaining sparse order;
# dense evidence still contributes at every rank and supplies unique candidates.
RRF_SPARSE_WEIGHT = 0.9
RRF_DENSE_WEIGHT = 0.1
RRF_RANK_CONSTANT = 1
RERANK_SIGNAL_WEIGHT = 0.05


def measured_delta(value: float, baseline: float) -> float:
    """Retain negative and zero measurements as well as positive differences."""
    return value - baseline


def rank_configuration(
    index: HybridIndex,
    question: str,
    name: RetrievalConfigurationName,
    *,
    top_k: int,
    sparse_weight: float = 1.0,
    dense_weight: float = 1.0,
    rank_constant: int = 60,
    signal_weight: float = 1.0,
) -> tuple[HybridSearchResult, ...]:
    """Run one single-pass ablation; ranking is owned by production components."""
    intent, sources = AgenticRetriever._plan(question)
    source_filter = sources[0] if len(sources) == 1 else "all"
    candidate_k = min(len(index.documents), max(20, top_k * 4))
    sparse = (
        ()
        if name == "dense_lsa"
        else index.sparse_search(
            question,
            top_k=candidate_k,
            source_filter=source_filter,
        )
    )
    dense = (
        ()
        if name == "sparse_bm25"
        else index.dense_search(
            question,
            top_k=candidate_k,
            source_filter=source_filter,
        )
    )
    fused = reciprocal_rank_fusion(
        sparse=[(item.citation_id, item.score) for item in sparse],
        dense=[(item.citation_id, item.score) for item in dense],
        sparse_weight=sparse_weight,
        dense_weight=dense_weight,
        rank_constant=rank_constant,
    )
    if name == "rrf_plus_rerank":
        return index.rerank_fused(question, fused, intent=intent, signal_weight=signal_weight)[
            :top_k
        ]
    if name in {"sparse_bm25", "dense_lsa", "naive_hybrid"}:
        # The explicitly specified naive baseline concatenates sparse then dense.
        order = tuple(dict.fromkeys(item.citation_id for item in (*sparse, *dense)))
    elif name == "rrf_fusion":
        order = tuple(item.citation_id for item in fused)
    else:
        raise ValueError("agentic_rag requires the asynchronous production retriever")
    documents = {item.citation_id: item for item in index.documents}
    records = {item.citation_id: item for item in fused}
    return tuple(
        HybridSearchResult(
            document=documents[identifier],
            **records[identifier].model_dump(exclude={"citation_id"}),
            rerank_score=records[identifier].rrf_score,
            explanation=(),
        )
        for identifier in order[:top_k]
    )


def _supported_facts(
    case: RetrievalCase, documents: Sequence[KnowledgeDocument]
) -> tuple[str, ...]:
    """Conservative literal support probes; no fact is inferred from a document ID.

    Labels include paraphrases, so this is a lower bound on semantic grounding.
    Numeric/identifier probes require whole token matches, and each probe must
    appear within a single cited document from its declared provenance family.
    """

    def normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9?]+", value.casefold()))

    supported = []
    for fact in case.required_facts:
        key, _, value = fact.partition("=")
        if not value:
            continue
        for document in documents:
            if key.startswith("official_") and document.source_class != "official":
                continue
            if key.startswith("synthetic_") and document.source_class != "synthetic":
                continue
            needle = normalize(value)
            if document.source_class == "synthetic":
                field = key.removeprefix("synthetic_")
                field = {
                    "product_name": "name",
                    "facility_kind": "kind",
                    "first_movement_destination": "to_facility",
                    "first_movement_type": "event_type",
                    "movement_origin": "from_facility",
                    "destination": "to_facility",
                }.get(field, field)
                needle = f"{normalize(field)} {needle}"
            if needle and f" {needle} " in f" {normalize(document.text)} ":
                supported.append(fact)
                break
    return tuple(supported)


def _contributions(case: RetrievalCase, row: RetrievalCaseResult) -> dict[str, float | int | bool]:
    relevance = {key: item.relevance for key, item in case.judgments.items()}
    ranked = row.ranked_document_ids
    cited = set(row.cited_document_ids)
    relevant = {key for key, grade in relevance.items() if grade > 0}
    ok = row.error_code is None
    return {
        "recall_at_1": recall_at_k(relevance, ranked, 1),
        "recall_at_3": recall_at_k(relevance, ranked, 3),
        "recall_at_5": recall_at_k(relevance, ranked, 5),
        "precision_at_5": precision_at_k(relevance, ranked, 5),
        "mean_reciprocal_rank": reciprocal_rank(relevance, ranked),
        "ndcg_at_5": ndcg_at_k(relevance, ranked, 5),
        "ranking_denominator": int(not case.unanswerable),
        "citation_precision": len(cited & relevant) / len(cited) if cited else 0.0,
        "citation_denominator": int(bool(cited)),
        "required_fact_coverage": len(set(row.cited_facts) & set(case.required_facts))
        / len(case.required_facts)
        if case.required_facts
        else 0.0,
        "fact_denominator": int(bool(case.required_facts)),
        "route_accuracy": float(ok and row.route_actual == case.expected_route),
        "abstention_accuracy": float(
            ok
            and row.answered != case.unanswerable
            and (case.unanswerable or bool(cited & relevant))
        ),
        "abstention_denominator": 1,
        "provenance_label_accuracy": float(ok and set(row.provenance) == cited),
        "budget_compliance": float(
            ok
            and row.query_count <= case.max_queries
            and row.hop_count <= case.max_hops
            and row.read_count <= case.max_reads
            and (case.rewrite_allowed or not row.rewrite_used)
        ),
        "prohibited_hit_count": len(cited & set(case.prohibited_document_ids)),
        "candidate_prohibited_hit_count": len(set(ranked) & set(case.prohibited_document_ids)),
        "unsupported_answer_count": int(
            row.answered and (case.unanswerable or not cited & relevant)
        ),
        "error_count": int(not ok),
    }


async def _execute(
    case: RetrievalCase,
    name: RetrievalConfigurationName,
    index: HybridIndex,
    gateway: ClosedRetrievalGateway,
) -> RetrievalCaseResult:
    started = perf_counter_ns()
    try:
        if name == "agentic_rag":
            retriever = AgenticRetriever(
                gateway,
                top_k_per_source=case.top_k,
                budgets=RetrievalBudgets(
                    max_queries=case.max_queries,
                    max_hops=case.max_hops if case.rewrite_allowed else 1,
                    max_read_calls=case.max_reads,
                ),
            )
            state = retriever.start(case.question)
            outcome = await retriever.resume(state, max_new_reads=len(state.sources))
            if isinstance(outcome, RetrievalInterruption):
                initial = AgenticRetriever._merge(list(outcome.state.accumulated_responses))
                outcome = await retriever.resume(outcome.state)
            else:
                initial = outcome.evidence
            if isinstance(outcome, RetrievalInterruption):
                raise RuntimeError("unexpected unbounded retrieval interruption")
            evidence = outcome.evidence
            row = RetrievalCaseResult(
                case_id=case.id,
                family=case.family,
                ranked_document_ids=tuple(item.document.citation_id for item in evidence),
                initial_ranked_document_ids=tuple(item.document.citation_id for item in initial),
                route_actual=tuple(dict.fromkeys(item.source for item in outcome.query_trace)),
                cited_document_ids=tuple(item.citation_id for item in outcome.citations)
                if outcome.coverage_satisfied
                else (),
                answered=outcome.coverage_satisfied,
                rewrite_used=outcome.rewrite_used,
                query_count=outcome.query_count,
                hop_count=outcome.hop_count,
                read_count=outcome.read_count,
                evidence_gaps=outcome.evidence_gaps,
                stop_reason=outcome.stop_reason,
            )
        else:
            evidence = rank_configuration(
                index,
                case.question,
                name,
                top_k=case.top_k,
                sparse_weight=RRF_SPARSE_WEIGHT,
                dense_weight=RRF_DENSE_WEIGHT,
                rank_constant=RRF_RANK_CONSTANT,
                signal_weight=RERANK_SIGNAL_WEIGHT,
            )
            _, sources = AgenticRetriever._plan(case.question)
            identifiers = tuple(item.document.citation_id for item in evidence)
            row = RetrievalCaseResult(
                case_id=case.id,
                family=case.family,
                ranked_document_ids=identifiers,
                route_actual=sources,
                cited_document_ids=identifiers,
                answered=bool(evidence),
                query_count=1,
                hop_count=1,
                read_count=1,
            )
        documents = tuple(
            item.document
            for item in evidence
            if item.document.citation_id in row.cited_document_ids
        )
        row = row.model_copy(
            update={
                "cited_facts": _supported_facts(case, documents),
                "provenance": {item.citation_id: item.source_class for item in documents},
            }
        )
    except Exception as exc:
        # No exception payloads or raw document text enter the artifact.
        code = "validation_error" if isinstance(exc, ValueError) else "retrieval_error"
        row = RetrievalCaseResult(
            case_id=case.id,
            family=case.family,
            error_code=code,
            stop_reason="execution_error",
        )
    return RetrievalCaseResult.model_validate(
        {
            **row.model_dump(mode="json"),
            "duration_ms": (perf_counter_ns() - started) // 1_000_000,
            "metric_contributions": _contributions(case, row),
        }
    )


def _metrics(
    rows: Sequence[RetrievalCaseResult],
    cases: Sequence[RetrievalCase],
    measure_rewrite: bool = False,
) -> RetrievalConfigurationMetrics:
    denominators: dict[str, int] = {}
    values: dict[str, float | int | dict[str, int]] = {"case_count": len(rows)}
    for metric in (*RANKING_METRICS, *RATE_METRICS):
        selector = (
            "ranking_denominator"
            if metric in RANKING_METRICS
            else {
                "citation_precision": "citation_denominator",
                "required_fact_coverage": "fact_denominator",
                "abstention_accuracy": "abstention_denominator",
            }.get(metric)
        )
        applicable = [row for row in rows if selector is None or row.metric_contributions[selector]]
        denominators[metric] = len(applicable)
        values[metric] = (
            sum(float(row.metric_contributions[metric]) for row in applicable) / len(applicable)
            if applicable
            else 0.0
        )
    for count in ("prohibited_hit_count", "unsupported_answer_count"):
        values[count] = sum(int(row.metric_contributions[count]) for row in rows)
        denominators[count] = len(rows)
    for percentile in (50, 95):
        metric = f"latency_p{percentile}_ms"
        values[metric] = (
            float(np.percentile([row.duration_ms for row in rows], percentile)) if rows else 0.0
        )
        denominators[metric] = len(rows)
    counts = {"rewrite_win_count": 0, "rewrite_loss_count": 0, "rewrite_no_change_count": 0}
    if measure_rewrite:
        for row, case in zip(rows, cases, strict=True):
            if case.rewrite_allowed:
                relevance = {key: item.relevance for key, item in case.judgments.items()}
                delta = measured_delta(
                    float(row.metric_contributions["ndcg_at_5"]),
                    ndcg_at_k(relevance, row.initial_ranked_document_ids, 5),
                )
                counts[
                    "rewrite_win_count"
                    if delta > 0
                    else "rewrite_loss_count"
                    if delta < 0
                    else "rewrite_no_change_count"
                ] += 1
    values.update(counts)
    denominators.update({key: sum(counts.values()) for key in counts})
    return RetrievalConfigurationMetrics(**values, denominators=denominators)


def _configuration(
    name: RetrievalConfigurationName,
    rows: tuple[RetrievalCaseResult, ...],
    corpus: RetrievalEvalCorpus,
) -> RetrievalConfigurationResult:
    return RetrievalConfigurationResult(
        name=name,
        results=rows,
        metrics=_metrics(rows, corpus.cases, name == "agentic_rag"),
        family_metrics={
            family: _metrics(
                [row for row in rows if row.family == family],
                [case for case in corpus.cases if case.family == family],
                name == "agentic_rag",
            )
            for family in EXPECTED_RETRIEVAL_FAMILY_COUNTS
        },
    )


def _gates(
    configurations: Sequence[RetrievalConfigurationResult],
) -> tuple[RetrievalEvalGates, bool]:
    metrics = {item.name: item.metrics for item in configurations}
    agentic = metrics["agentic_rag"]
    gates = RetrievalEvalGates(
        **{
            name: getattr(agentic, name)
            for name in (
                "route_accuracy",
                "abstention_accuracy",
                "provenance_label_accuracy",
                "budget_compliance",
            )
        },
        **{
            name: getattr(agentic, name)
            for name in (
                "prohibited_hit_count",
                "unsupported_answer_count",
            )
        },
        agentic_recall_at_5=agentic.recall_at_5,
        agentic_ndcg_at_5=agentic.ndcg_at_5,
        fusion_recall_delta=measured_delta(
            metrics["rrf_fusion"].recall_at_5,
            max(metrics["sparse_bm25"].recall_at_5, metrics["dense_lsa"].recall_at_5),
        ),
        rerank_ndcg_delta=measured_delta(
            metrics["rrf_plus_rerank"].ndcg_at_5, metrics["rrf_fusion"].ndcg_at_5
        ),
    )
    passed = (
        gates.route_accuracy
        == gates.abstention_accuracy
        == gates.provenance_label_accuracy
        == gates.budget_compliance
        == 1.0
        and gates.prohibited_hit_count == gates.unsupported_answer_count == 0
        and gates.agentic_recall_at_5 >= 0.95
        and gates.agentic_ndcg_at_5 >= 0.90
        and gates.fusion_recall_delta >= 0.0
        and gates.rerank_ndcg_delta >= 0.0
        and not any(row.error_code for item in configurations for row in item.results)
    )
    return gates, passed


def validate_retrieval_report(
    report: RetrievalEvalReport,
    corpus: RetrievalEvalCorpus,
    knowledge: KnowledgeCorpus,
) -> None:
    """Recompute all contributions, denominators, aggregates, deltas and gates."""
    if report.retrieval_case_corpus_sha256 != canonical_sha256(corpus.model_dump(mode="json")):
        raise ValueError("retrieval case corpus digest mismatch")
    if (
        report.knowledge_corpus_sha256 != knowledge.manifest.corpus_sha256
        or corpus.knowledge_corpus_sha256 != report.knowledge_corpus_sha256
    ):
        raise ValueError("knowledge corpus digest mismatch")
    payload = report.model_dump(mode="json", exclude={"report_sha256"})
    verify_sha256(payload, report.report_sha256)
    if tuple(item.name for item in report.configurations) != CONFIGURATIONS:
        raise ValueError("retrieval configurations must be complete and ordered")
    documents = {item.citation_id: item for item in knowledge.documents}
    for configuration in report.configurations:
        if tuple(row.case_id for row in configuration.results) != tuple(
            case.id for case in corpus.cases
        ):
            raise ValueError("missing, duplicate, or unordered case results")
        for row, case in zip(configuration.results, corpus.cases, strict=True):
            if row.family != case.family or not set(row.ranked_document_ids) <= documents.keys():
                raise ValueError("case family or document identity mismatch")
            if (
                len(row.ranked_document_ids) != len(set(row.ranked_document_ids))
                or len(row.cited_document_ids) != len(set(row.cited_document_ids))
                or not set(row.cited_document_ids) <= set(row.ranked_document_ids)
            ):
                raise ValueError("invalid ranked/cited document IDs")
            if configuration.name == "agentic_rag" and not row.answered and row.cited_document_ids:
                raise ValueError("abstained agentic results cannot retain accepted citations")
            if not set(row.initial_ranked_document_ids) <= documents.keys() or len(
                row.initial_ranked_document_ids
            ) != len(set(row.initial_ranked_document_ids)):
                raise ValueError("invalid pre-rewrite ranking")
            cited_documents = [documents[key] for key in row.cited_document_ids]
            if dict(row.provenance) != {
                item.citation_id: item.source_class for item in cited_documents
            }:
                raise ValueError("provenance label mismatch")
            if row.cited_facts != _supported_facts(case, cited_documents):
                raise ValueError("unsupported cited facts")
            if row.error_code not in {None, "validation_error", "retrieval_error"}:
                raise ValueError("unbounded error code")
            if row.error_code and (
                row.answered or row.ranked_document_ids or row.stop_reason != "execution_error"
            ):
                raise ValueError("error results must fail closed")
            if row.answered and row.evidence_gaps:
                raise ValueError("answered result has unresolved evidence gaps")
            if row.error_code is None and configuration.name == "agentic_rag":
                candidates = tuple(
                    HybridSearchResult(document=documents[key], rrf_score=0.0, rerank_score=0.0, explanation=())
                    for key in row.ranked_document_ids
                )
                _, sources = AgenticRetriever._plan(case.question)
                expected_hops = 2 if row.rewrite_used else 1
                expected_reads = len(sources) * expected_hops
                if (
                    row.query_count != expected_reads or row.read_count != expected_reads
                    or row.hop_count != expected_hops or row.route_actual != sources
                ):
                    raise ValueError("inconsistent agentic route or budget observation")
                gaps = AgenticRetriever._critic(case.question, sources, candidates, {})
                if row.evidence_gaps != gaps or row.answered != (not gaps):
                    raise ValueError("forged critic observation")
                expected_citations = tuple(
                    item.citation_id for item in AgenticRetriever._citations(candidates, case.question)
                ) if row.answered else ()
                if row.cited_document_ids != expected_citations:
                    raise ValueError("forged accepted citations")
                if row.answered and row.stop_reason not in {"coverage_satisfied", "coverage_satisfied_after_rewrite"}:
                    raise ValueError("accepted evidence has inconsistent stop reason")
                if not row.answered and row.stop_reason not in {"evidence_gap_after_rewrite", "budget_exhausted", "progress_stalled"}:
                    raise ValueError("abstained evidence has inconsistent stop reason")
                if not row.rewrite_used and row.initial_ranked_document_ids != row.ranked_document_ids:
                    raise ValueError("single-pass agentic ranking changed without a rewrite")
            if dict(row.metric_contributions) != _contributions(case, row):
                raise ValueError("forged metric contribution")
        rebuilt = _configuration(
            configuration.name,
            configuration.results,
            corpus,
        )
        if rebuilt != configuration:
            raise ValueError("forged aggregate or family metric")
    gates, passed = _gates(report.configurations)
    if report.gates != gates or report.gate_passed != passed:
        raise ValueError("forged retrieval gates")


def load_retrieval_report(
    path: Path, case_path: Path, *, data_dir: Path = DATA_DIR
) -> RetrievalEvalReport:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if raw != canonical_json_bytes(payload):
        raise ValueError("retrieval report must use canonical JSON")
    report = RetrievalEvalReport.model_validate(payload)
    corpus = load_retrieval_cases(case_path, data_dir=data_dir)
    validate_retrieval_report(report, corpus, KnowledgeCorpus.load(data_dir=data_dir))
    return report


async def run_retrieval_benchmark(
    case_path: Path,
    output_path: Path,
    data_dir: Path = DATA_DIR,
) -> RetrievalEvalReport:
    corpus = load_retrieval_cases(case_path, data_dir=data_dir)
    knowledge = KnowledgeCorpus.load(data_dir=data_dir)
    index = load_local_hybrid_index(str(data_dir.resolve()))
    if index.metadata.corpus_sha256 != knowledge.manifest.corpus_sha256:
        raise ValueError("index corpus digest mismatch")
    gateway = ClosedRetrievalGateway.direct(data_dir=data_dir)
    configurations = []
    for name in CONFIGURATIONS:
        rows = tuple([await _execute(case, name, index, gateway) for case in corpus.cases])
        configurations.append(_configuration(name, rows, corpus))
    gates, passed = _gates(configurations)
    report = RetrievalEvalReport(
        schema_version="1.0",
        retrieval_case_corpus_sha256=canonical_sha256(corpus.model_dump(mode="json")),
        knowledge_corpus_sha256=knowledge.manifest.corpus_sha256,
        configurations=tuple(configurations),
        gates=gates,
        gate_passed=passed,
        rrf_sparse_weight=RRF_SPARSE_WEIGHT,
        rrf_dense_weight=RRF_DENSE_WEIGHT,
        rrf_rank_constant=RRF_RANK_CONSTANT,
        rerank_signal_weight=RERANK_SIGNAL_WEIGHT,
    )
    report = report.model_copy(
        update={
            "report_sha256": canonical_sha256(
                report.model_dump(mode="json", exclude={"report_sha256"})
            )
        }
    )
    validate_retrieval_report(report, corpus, knowledge)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json_bytes(report.model_dump(mode="json")))
    return report
