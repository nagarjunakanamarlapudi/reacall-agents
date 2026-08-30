"""Bounded read-only planning and evidence-gap loop over hybrid-search gateways."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from recallops.agents.policies import ProgressStalledError, ProgressWatchdog
from recallops.retrieval.models import (
    HybridSearchResponse,
    HybridSearchResult,
    RetrievalIntent,
)

StopReason = Literal[
    "coverage_satisfied",
    "coverage_satisfied_after_rewrite",
    "evidence_gap_after_rewrite",
    "budget_exhausted",
    "progress_stalled",
    "empty_query",
]
SourceRoute = Literal["official", "synthetic"]
IDENTIFIER_PATTERN = re.compile(
    r"\b(?:H-\d{4}-\d{4}|LOT-[A-Z0-9?-]+|SHIP-[A-Z0-9?-]+|STORE-[A-Z0-9?-]+|"
    r"DC-[A-Z0-9?-]+|P-[A-Z0-9?-]+|\d{8,14})\b",
    flags=re.IGNORECASE,
)


class RetrievalGateway(Protocol):
    async def search_regulatory_evidence(
        self, query: str, *, top_k: int, record_types: tuple[str, ...] = ()
    ) -> dict[str, Any]: ...

    async def search_operational_evidence(
        self, query: str, *, top_k: int, record_types: tuple[str, ...] = ()
    ) -> dict[str, Any]: ...


class RetrievalBudgets(BaseModel):
    """Hard ceilings from the retrieval safety contract."""

    model_config = ConfigDict(frozen=True)

    max_hops: StrictInt = Field(default=2, ge=1, le=2)
    max_queries: StrictInt = Field(default=4, ge=1, le=4)
    max_read_calls: StrictInt = Field(default=8, ge=1, le=8)


class RetrievalToolCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal["search_regulatory_evidence", "search_operational_evidence"]
    source: SourceRoute
    read_only: Literal[True] = True


class RetrievalQueryTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    hop: StrictInt = Field(ge=1, le=2)
    query: str
    source: SourceRoute
    rewritten: bool


class RetrievalToolTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    hop: StrictInt = Field(ge=1, le=2)
    tool_name: Literal["search_regulatory_evidence", "search_operational_evidence"]
    query: str
    status: Literal["ok"]
    result_count: StrictInt = Field(ge=0)


class RetrievalCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_id: str
    source_url: str
    content_hash: str
    origin: str


class AgenticRetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    intent: RetrievalIntent
    coverage_satisfied: bool
    evidence_gaps: tuple[str, ...]
    stop_reason: StopReason
    evidence: tuple[HybridSearchResult, ...]
    citations: tuple[RetrievalCitation, ...]
    query_trace: tuple[RetrievalQueryTrace, ...]
    tool_trace: tuple[RetrievalToolTrace, ...]
    phase_trace: tuple[str, ...]
    authoritative_facts: dict[str, Any]


class AgenticRetriever:
    """Deterministic evidence agent; retrieved text is data and never executable control."""

    def __init__(
        self,
        gateway: RetrievalGateway,
        *,
        budgets: RetrievalBudgets | None = None,
        top_k_per_source: int = 8,
    ) -> None:
        if (
            isinstance(top_k_per_source, bool)
            or not isinstance(top_k_per_source, int)
            or not 1 <= top_k_per_source <= 20
        ):
            raise ValueError("top_k_per_source must be an integer from 1 through 20")
        self.gateway = gateway
        self.budgets = budgets or RetrievalBudgets()
        self.top_k_per_source = top_k_per_source
        self.tool_manifest = (
            RetrievalToolCapability(name="search_regulatory_evidence", source="official"),
            RetrievalToolCapability(name="search_operational_evidence", source="synthetic"),
        )

    @staticmethod
    def _plan(question: str) -> tuple[RetrievalIntent, tuple[SourceRoute, ...]]:
        folded = question.casefold()
        regulatory = any(
            term in folded
            for term in (
                "fda",
                "class i",
                "class ii",
                "class iii",
                "classification",
                "consignee",
                "downstream recipient",
                "effectiveness",
                "hazard",
                "notice",
                "recall",
                "regulatory",
                "termination",
                "traceability rule",
                "withdrawal",
            )
        )
        operational = bool(IDENTIFIER_PATTERN.search(question)) or any(
            term in folded
            for term in (
                "facility",
                "inventory",
                "lot",
                "northstar",
                "reconciliation",
                "shipment",
                "store",
                "supplier",
                "units",
                "upc",
            )
        )
        if regulatory and operational:
            return "mixed", ("official", "synthetic")
        if operational:
            return "operational", ("synthetic",)
        return "regulatory", ("official",)

    @staticmethod
    def _rewrite(question: str, gaps: tuple[str, ...]) -> str:
        folded = question.casefold()
        additions: list[str] = []
        if any(term in folded for term in ("recipient", "acted", "notice", "downstream")):
            additions.append(
                "recall effectiveness checks consignee communication followed instructions"
            )
        if any(term in folded for term in ("batch", "handoff", "movement")):
            additions.append("traceability lot shipping receiving supply chain")
        if any(term in " ".join(gaps).casefold() for term in ("identifier", "facility", "lot")):
            additions.extend(IDENTIFIER_PATTERN.findall(question))
        return " ".join(dict.fromkeys([question, *additions])).strip()

    @staticmethod
    def _merge(responses: list[HybridSearchResponse]) -> tuple[HybridSearchResult, ...]:
        by_citation: dict[str, HybridSearchResult] = {}
        for response in responses:
            expected_source = response.source_filter
            for result in response.results:
                if expected_source != "all" and result.document.source_class != expected_source:
                    raise ValueError("hybrid search returned evidence across its source boundary")
                current = by_citation.get(result.document.citation_id)
                if current is None or (
                    result.rerank_score,
                    result.rrf_score,
                    result.document.citation_id,
                ) > (
                    current.rerank_score,
                    current.rrf_score,
                    current.document.citation_id,
                ):
                    by_citation[result.document.citation_id] = result
        return tuple(
            sorted(
                by_citation.values(),
                key=lambda item: (-item.rerank_score, item.document.citation_id),
            )
        )

    @staticmethod
    def _critic(
        question: str,
        sources: tuple[SourceRoute, ...],
        evidence: tuple[HybridSearchResult, ...],
        authoritative_facts: Mapping[str, Any],
    ) -> tuple[str, ...]:
        gaps: list[str] = []
        for source in sources:
            source_class = "official" if source == "official" else "synthetic"
            if not any(item.document.source_class == source_class for item in evidence):
                gaps.append(f"No relevant {source_class} evidence was retrieved.")
        searchable = " ".join(
            f"{item.document.record_id} {item.document.title} {item.document.text}"
            for item in evidence
        ).casefold()
        for identifier in dict.fromkeys(IDENTIFIER_PATTERN.findall(question)):
            if identifier.casefold() not in searchable:
                gaps.append(
                    f"Requested identifier {identifier} is not covered by retrieved evidence."
                )
        for item in evidence:
            claims = item.document.metadata.get("claims", {})
            if not isinstance(claims, Mapping):
                continue
            for name, authoritative_value in authoritative_facts.items():
                if name in claims and claims[name] != authoritative_value:
                    gaps.append(
                        "Retrieved advisory claim disagrees with authoritative structured fact "
                        f"{name}: retrieved={claims[name]!r}, authoritative={authoritative_value!r}."
                    )
        return tuple(dict.fromkeys(gaps))

    @staticmethod
    def _citations(evidence: tuple[HybridSearchResult, ...]) -> tuple[RetrievalCitation, ...]:
        return tuple(
            RetrievalCitation(
                citation_id=item.document.citation_id,
                source_url=item.document.source_url,
                content_hash=item.document.content_hash,
                origin=item.document.origin,
            )
            for item in evidence
        )

    async def _retrieve_source(
        self,
        source: SourceRoute,
        query: str,
    ) -> HybridSearchResponse:
        if source == "official":
            raw = await self.gateway.search_regulatory_evidence(
                query, top_k=self.top_k_per_source, record_types=()
            )
        else:
            raw = await self.gateway.search_operational_evidence(
                query, top_k=self.top_k_per_source, record_types=()
            )
        return HybridSearchResponse.model_validate(raw)

    async def retrieve(
        self,
        question: str,
        *,
        authoritative_facts: Mapping[str, Any] | None = None,
    ) -> AgenticRetrievalResult:
        normalized_question = question.strip()
        facts = dict(sorted((authoritative_facts or {}).items()))
        if not normalized_question:
            return AgenticRetrievalResult(
                question="",
                intent="auto",
                coverage_satisfied=False,
                evidence_gaps=("Question is empty; no evidence was retrieved.",),
                stop_reason="empty_query",
                evidence=(),
                citations=(),
                query_trace=(),
                tool_trace=(),
                phase_trace=("plan", "stop"),
                authoritative_facts=facts,
            )

        intent, sources = self._plan(normalized_question)
        phases = ["plan", "route"]
        query_trace: list[RetrievalQueryTrace] = []
        tool_trace: list[RetrievalToolTrace] = []
        accumulated: list[HybridSearchResponse] = []
        watchdog = ProgressWatchdog(max_repeats=1)
        query = normalized_question
        rewritten = False
        evidence: tuple[HybridSearchResult, ...] = ()
        gaps: tuple[str, ...] = ()

        for hop in range(1, self.budgets.max_hops + 1):
            required_calls = len(sources)
            if (
                len(query_trace) + required_calls > self.budgets.max_queries
                or len(tool_trace) + required_calls > self.budgets.max_read_calls
            ):
                phases.append("stop")
                return AgenticRetrievalResult(
                    question=normalized_question,
                    intent=intent,
                    coverage_satisfied=False,
                    evidence_gaps=gaps or ("Retrieval budget exhausted before source coverage.",),
                    stop_reason="budget_exhausted",
                    evidence=evidence,
                    citations=self._citations(evidence),
                    query_trace=tuple(query_trace),
                    tool_trace=tuple(tool_trace),
                    phase_trace=tuple(phases),
                    authoritative_facts=facts,
                )
            phases.append("hybrid_retrieve")
            hop_responses: list[HybridSearchResponse] = []
            for source in sources:
                query_trace.append(
                    RetrievalQueryTrace(hop=hop, query=query, source=source, rewritten=rewritten)
                )
                response = await self._retrieve_source(source, query)
                hop_responses.append(response)
                tool_name = (
                    "search_regulatory_evidence"
                    if source == "official"
                    else "search_operational_evidence"
                )
                tool_trace.append(
                    RetrievalToolTrace(
                        hop=hop,
                        tool_name=tool_name,
                        query=query,
                        status="ok",
                        result_count=len(response.results),
                    )
                )
            accumulated.extend(hop_responses)
            phases.append("fuse_rerank")
            evidence = self._merge(accumulated)
            phases.append("gap_critic")
            gaps = self._critic(normalized_question, sources, evidence, facts)
            if not gaps:
                phases.append("stop")
                reason: StopReason = (
                    "coverage_satisfied_after_rewrite" if rewritten else "coverage_satisfied"
                )
                return AgenticRetrievalResult(
                    question=normalized_question,
                    intent=intent,
                    coverage_satisfied=True,
                    evidence_gaps=(),
                    stop_reason=reason,
                    evidence=evidence,
                    citations=self._citations(evidence),
                    query_trace=tuple(query_trace),
                    tool_trace=tuple(tool_trace),
                    phase_trace=tuple(phases),
                    authoritative_facts=facts,
                )
            if rewritten or hop >= self.budgets.max_hops:
                phases.append("stop")
                return AgenticRetrievalResult(
                    question=normalized_question,
                    intent=intent,
                    coverage_satisfied=False,
                    evidence_gaps=gaps,
                    stop_reason="evidence_gap_after_rewrite",
                    evidence=evidence,
                    citations=self._citations(evidence),
                    query_trace=tuple(query_trace),
                    tool_trace=tuple(tool_trace),
                    phase_trace=tuple(phases),
                    authoritative_facts=facts,
                )
            if (
                len(query_trace) + len(sources) > self.budgets.max_queries
                or len(tool_trace) + len(sources) > self.budgets.max_read_calls
            ):
                phases.append("stop")
                return AgenticRetrievalResult(
                    question=normalized_question,
                    intent=intent,
                    coverage_satisfied=False,
                    evidence_gaps=gaps,
                    stop_reason="budget_exhausted",
                    evidence=evidence,
                    citations=self._citations(evidence),
                    query_trace=tuple(query_trace),
                    tool_trace=tuple(tool_trace),
                    phase_trace=tuple(phases),
                    authoritative_facts=facts,
                )
            phases.append("rewrite")
            rewritten_query = self._rewrite(query, gaps)
            try:
                watchdog.observe(
                    {
                        "query": query,
                        "sources": list(sources),
                        "gaps": list(gaps),
                    }
                )
                watchdog.observe(
                    {
                        "query": rewritten_query,
                        "sources": list(sources),
                        "gaps": list(gaps),
                    }
                )
            except ProgressStalledError:
                phases.append("stop")
                return AgenticRetrievalResult(
                    question=normalized_question,
                    intent=intent,
                    coverage_satisfied=False,
                    evidence_gaps=gaps,
                    stop_reason="progress_stalled",
                    evidence=evidence,
                    citations=self._citations(evidence),
                    query_trace=tuple(query_trace),
                    tool_trace=tuple(tool_trace),
                    phase_trace=tuple(phases),
                    authoritative_facts=facts,
                )
            query = rewritten_query
            rewritten = True

        raise RuntimeError("unreachable bounded retrieval state")
