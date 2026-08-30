"""Bounded read-only planning and evidence-gap loop over hybrid-search gateways."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from recallops.agents.policies import (
    ProgressStalledError,
    ProgressWatchdog,
    strict_json_value,
)
from recallops.config import get_settings
from recallops.retrieval.corpus import KnowledgeCorpus
from recallops.retrieval.hybrid import HybridIndex
from recallops.retrieval.models import (
    HybridSearchRequest,
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
CONCEPT_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
CONCEPT_STOP_WORDS = frozenset(
    {
        "about",
        "business",
        "does",
        "evidence",
        "followed",
        "from",
        "have",
        "inspect",
        "item",
        "know",
        "mean",
        "possible",
        "question",
        "retailer",
        "should",
        "through",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)
CONCEPT_GROUPS: tuple[tuple[str, frozenset[str], frozenset[str]], ...] = (
    (
        "regulatory authority",
        frozenset({"fda", "regulatory", "regulator"}),
        frozenset({"fda", "regulatory", "regulator", "enforcement"}),
    ),
    (
        "recall",
        frozenset({"recall", "withdrawal"}),
        frozenset({"recall", "removal", "correction", "withdrawal"}),
    ),
    (
        "health-hazard classification",
        frozenset({"class", "classification", "classify", "hazard"}),
        frozenset({"class", "classification", "hazard", "health", "consequences"}),
    ),
    (
        "traceability handoffs",
        frozenset(
            {
                "follow",
                "handoff",
                "handoffs",
                "movement",
                "movements",
                "move",
                "moved",
                "trace",
                "traceability",
                "tracking",
            }
        ),
        frozenset(
            {
                "event",
                "movement",
                "receiving",
                "shipping",
                "traceability",
                "tracking",
                "visibility",
            }
        ),
    ),
    (
        "recall effectiveness",
        frozenset(
            {
                "acted",
                "communication",
                "consignee",
                "downstream",
                "effectiveness",
                "notice",
                "recipient",
                "recipients",
            }
        ),
        frozenset(
            {
                "communication",
                "consignee",
                "effectiveness",
                "followed",
                "instructions",
                "received",
            }
        ),
    ),
    (
        "inventory reconciliation",
        frozenset({"inventory", "reconcile", "reconciliation", "units", "unaccounted"}),
        frozenset({"inventory", "on", "hand", "reconciliation", "units", "unaccounted"}),
    ),
    (
        "facility shipment",
        frozenset({"facility", "shipment", "store", "supplier"}),
        frozenset({"facility", "shipment", "shipping", "store", "supplier"}),
    ),
)


class RetrievalBudgets(BaseModel):
    """Hard ceilings from the retrieval safety contract."""

    model_config = ConfigDict(frozen=True)

    max_hops: StrictInt = Field(default=2, ge=1, le=2)
    max_queries: StrictInt = Field(default=4, ge=1, le=4)
    max_read_calls: StrictInt = Field(default=8, ge=1, le=8)


class RetrievalToolCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal["search_regulatory_evidence", "search_operational_evidence"]
    connection_id: Literal["regulatory_search", "operational_search"]
    source: SourceRoute
    read_only: Literal[True] = True
    callable_identity: str = Field(min_length=1)


SearchCallable = Callable[[str, int, tuple[str, ...]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class _RetrievalConnection:
    connection_id: Literal["regulatory_search", "operational_search"]
    tool_name: Literal["search_regulatory_evidence", "search_operational_evidence"]
    source: SourceRoute
    callable_identity: str
    search: SearchCallable


_GATEWAY_FACTORY_TOKEN = object()


class ClosedRetrievalGateway:
    """Factory-built two-capability adapter with no Operations surface."""

    __slots__ = ("_connections", "tool_manifest")
    _CONNECTION_ORDER = ("regulatory_search", "operational_search")

    def __init__(
        self,
        transport: str,
        *,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _GATEWAY_FACTORY_TOKEN:
            raise TypeError("ClosedRetrievalGateway is factory-built; use direct() or stdio()")
        if transport == "direct":
            connections = ClosedRetrievalGateway._build_direct_connections()
        elif transport == "stdio":
            connections = ClosedRetrievalGateway._build_stdio_connections()
        else:
            raise TypeError("closed retrieval gateway requires a fixed transport identity")
        if tuple(item.connection_id for item in connections) != self._CONNECTION_ORDER:
            raise ValueError("closed retrieval gateway requires its two fixed read connections")
        if len({item.search for item in connections}) != len(connections):
            raise ValueError("closed retrieval connections must use distinct callables")
        self._connections = connections
        self.tool_manifest = tuple(
            RetrievalToolCapability(
                name=item.tool_name,
                connection_id=item.connection_id,
                source=item.source,
                callable_identity=item.callable_identity,
            )
            for item in connections
        )

    @property
    def connection_ids(self) -> tuple[str, ...]:
        return tuple(item.connection_id for item in self._connections)

    @classmethod
    def direct(cls) -> ClosedRetrievalGateway:
        return cls("direct", _factory_token=_GATEWAY_FACTORY_TOKEN)

    @staticmethod
    def _build_direct_connections() -> tuple[_RetrievalConnection, ...]:
        settings = get_settings()
        corpus = KnowledgeCorpus.load(data_dir=settings.data_dir)
        index = HybridIndex(corpus.documents)

        async def regulatory_search(
            query: str, top_k: int, record_types: tuple[str, ...]
        ) -> dict[str, Any]:
            return index.search(
                HybridSearchRequest(
                    query=query,
                    top_k=top_k,
                    source_filter="official",
                    intent="regulatory",
                    record_types=record_types,
                )
            ).model_dump(mode="json")

        async def operational_search(
            query: str, top_k: int, record_types: tuple[str, ...]
        ) -> dict[str, Any]:
            return index.search(
                HybridSearchRequest(
                    query=query,
                    top_k=top_k,
                    source_filter="synthetic",
                    intent="operational",
                    record_types=record_types,
                )
            ).model_dump(mode="json")

        digest = corpus.manifest.corpus_sha256
        return (
            _RetrievalConnection(
                connection_id="regulatory_search",
                tool_name="search_regulatory_evidence",
                source="official",
                callable_identity=f"direct:anchored:{digest}:regulatory_search",
                search=regulatory_search,
            ),
            _RetrievalConnection(
                connection_id="operational_search",
                tool_name="search_operational_evidence",
                source="synthetic",
                callable_identity=f"direct:anchored:{digest}:operational_search",
                search=operational_search,
            ),
        )

    @classmethod
    def stdio(cls) -> ClosedRetrievalGateway:
        return cls("stdio", _factory_token=_GATEWAY_FACTORY_TOKEN)

    @staticmethod
    def _build_stdio_connections() -> tuple[_RetrievalConnection, ...]:
        settings = get_settings()
        corpus = KnowledgeCorpus.load(data_dir=settings.data_dir)
        project_root = Path(__file__).resolve().parents[3]
        environment = {
            "RECALLOPS_DATA_DIR": str(settings.data_dir),
            "RECALLOPS_SOURCE_MODE": settings.source_mode,
        }
        server_modules = {
            "registry": "recallops.mcp.recall_registry_server",
            "traceability": "recallops.mcp.traceability_server",
        }
        client = MultiServerMCPClient(
            {
                server: {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", module],
                    "cwd": str(project_root),
                    "env": environment,
                }
                for server, module in server_modules.items()
            }
        )

        async def call_stdio(
            server: str,
            tool_name: str,
            query: str,
            top_k: int,
            record_types: tuple[str, ...],
        ) -> dict[str, Any]:
            tool = next(
                item
                for item in await client.get_tools(server_name=server)
                if item.name == tool_name
            )
            result = await tool.ainvoke(
                {"query": query, "top_k": top_k, "record_types": list(record_types)}
            )
            content = result.content if hasattr(result, "content") else result
            if isinstance(content, list) and content and isinstance(content[0], dict):
                content = content[0].get("text", content)
            if isinstance(content, str):
                return json.loads(content)
            if not isinstance(content, dict):
                raise ValueError("retrieval MCP returned a non-object response")
            return content

        async def regulatory_search(
            query: str, top_k: int, record_types: tuple[str, ...]
        ) -> dict[str, Any]:
            return await call_stdio(
                "registry", "search_regulatory_evidence", query, top_k, record_types
            )

        async def operational_search(
            query: str, top_k: int, record_types: tuple[str, ...]
        ) -> dict[str, Any]:
            return await call_stdio(
                "traceability", "search_operational_evidence", query, top_k, record_types
            )

        digest = corpus.manifest.corpus_sha256
        return (
            _RetrievalConnection(
                connection_id="regulatory_search",
                tool_name="search_regulatory_evidence",
                source="official",
                callable_identity=(
                    f"stdio:fixed:{sys.executable}:{server_modules['registry']}:"
                    f"{digest}:regulatory_search"
                ),
                search=regulatory_search,
            ),
            _RetrievalConnection(
                connection_id="operational_search",
                tool_name="search_operational_evidence",
                source="synthetic",
                callable_identity=(
                    f"stdio:fixed:{sys.executable}:{server_modules['traceability']}:"
                    f"{digest}:operational_search"
                ),
                search=operational_search,
            ),
        )

    async def call(
        self,
        connection_id: str,
        query: str,
        *,
        top_k: int,
        record_types: tuple[str, ...],
    ) -> dict[str, Any]:
        connection = next(
            (item for item in self._connections if item.connection_id == connection_id),
            None,
        )
        if connection is None:
            raise ValueError(f"unknown closed retrieval connection {connection_id!r}")
        return await connection.search(query, top_k, record_types)


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
    hop_count: StrictInt = Field(ge=0, le=2)
    query_count: StrictInt = Field(ge=0, le=4)
    read_count: StrictInt = Field(ge=0, le=8)
    rewrite_used: bool


class RetrievalLoopState(BaseModel):
    """JSON checkpoint for one bounded retrieval run, including an in-hop cursor."""

    model_config = ConfigDict(frozen=True)

    question: str
    intent: RetrievalIntent
    sources: tuple[SourceRoute, ...]
    budgets: RetrievalBudgets
    top_k_per_source: StrictInt = Field(ge=1, le=20)
    gateway_manifest: tuple[RetrievalToolCapability, ...]
    hop: StrictInt = Field(ge=0, le=2)
    current_query: str
    next_source_index: StrictInt = Field(ge=0, le=2)
    rewrite_used: bool
    query_count: StrictInt = Field(ge=0, le=4)
    read_count: StrictInt = Field(ge=0, le=8)
    accumulated_responses: tuple[HybridSearchResponse, ...]
    evidence: tuple[HybridSearchResult, ...]
    evidence_gaps: tuple[str, ...]
    query_trace: tuple[RetrievalQueryTrace, ...]
    tool_trace: tuple[RetrievalToolTrace, ...]
    phase_trace: tuple[str, ...]
    progress_signature: str | None = None
    progress_repeat_count: StrictInt = Field(default=0, ge=0)
    authoritative_facts: dict[str, Any]

    @field_validator("authoritative_facts", mode="before")
    @classmethod
    def validate_authoritative_facts(cls, value: Any) -> dict[str, Any]:
        normalized = strict_json_value(value)
        if not isinstance(normalized, dict):
            raise ValueError("authoritative_facts must be a JSON object")
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def validate_cursor_and_counts(self) -> RetrievalLoopState:
        if self.query_count != len(self.query_trace):
            raise ValueError("query_count must equal the query trace length")
        if self.read_count != len(self.tool_trace):
            raise ValueError("read_count must equal the tool trace length")
        if self.read_count != len(self.accumulated_responses):
            raise ValueError("read_count must equal accumulated response count")
        if self.query_count != self.read_count:
            raise ValueError("each planned query must correspond to one read call")
        if self.next_source_index > len(self.sources):
            raise ValueError("next source cursor exceeds the planned source route")
        if self.hop == 0 and (self.sources or self.current_query):
            raise ValueError("hop zero is reserved for an empty question")
        if self.hop > 0 and (not self.sources or not self.current_query.strip()):
            raise ValueError("active retrieval state requires a query and source route")
        return self


class RetrievalInterruption(BaseModel):
    """A safe read boundary at which the state may be serialized and resumed."""

    model_config = ConfigDict(frozen=True)

    reason: Literal["read_boundary"] = "read_boundary"
    state: RetrievalLoopState


class AgenticRetriever:
    """Deterministic evidence agent; retrieved text is data and never executable control."""

    def __init__(
        self,
        gateway: ClosedRetrievalGateway,
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
        if type(gateway) is not ClosedRetrievalGateway:
            raise TypeError("gateway must be an exact ClosedRetrievalGateway")
        self.gateway = gateway
        self.budgets = budgets or RetrievalBudgets()
        self.top_k_per_source = top_k_per_source
        self.tool_manifest = gateway.tool_manifest

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
        searchable_tokens = set(CONCEPT_TOKEN_PATTERN.findall(searchable))
        question_without_identifiers = IDENTIFIER_PATTERN.sub(" ", question.casefold())
        question_tokens = set(CONCEPT_TOKEN_PATTERN.findall(question_without_identifiers))
        consumed: set[str] = set()
        for label, triggers, support_terms in CONCEPT_GROUPS:
            present = question_tokens & triggers
            if not present:
                continue
            consumed.update(present)
            if not searchable_tokens & support_terms:
                gaps.append(f"Requested concept {label!r} is not covered by retrieved evidence.")
        for token in sorted(question_tokens - consumed - CONCEPT_STOP_WORDS):
            if len(token) < 4 or token in searchable_tokens:
                continue
            gaps.append(f"Requested concept {token!r} is not covered by retrieved evidence.")
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
            connection_id = "regulatory_search"
            expected_filter = "official"
            expected_intent = "regulatory"
            expected_origins = {
                "OFFICIAL_OPENFDA_SNAPSHOT",
                "OFFICIAL_POLICY_REFERENCE",
            }
        else:
            connection_id = "operational_search"
            expected_filter = "synthetic"
            expected_intent = "operational"
            expected_origins = {"SYNTHETIC_RETAILER_DIGITAL_TWIN"}
        raw = await self.gateway.call(
            connection_id,
            query,
            top_k=self.top_k_per_source,
            record_types=(),
        )
        response = HybridSearchResponse.model_validate(raw)
        if response.source_filter != expected_filter or response.intent != expected_intent:
            raise ValueError(f"retrieval response does not match requested {expected_filter} route")
        for result in response.results:
            if (
                result.document.source_class != expected_filter
                or result.document.origin not in expected_origins
            ):
                raise ValueError(
                    f"retrieval document does not match requested {expected_filter} route"
                )
        return response

    def start(
        self,
        question: str,
        *,
        authoritative_facts: Mapping[str, Any] | None = None,
    ) -> RetrievalLoopState:
        normalized_question = question.strip()
        facts = dict(authoritative_facts or {})
        if not normalized_question:
            intent: RetrievalIntent = "auto"
            sources: tuple[SourceRoute, ...] = ()
            hop = 0
            phases = ("plan",)
        else:
            intent, sources = self._plan(normalized_question)
            hop = 1
            phases = ("plan", "route")
        return RetrievalLoopState(
            question=normalized_question,
            intent=intent,
            sources=sources,
            budgets=self.budgets,
            top_k_per_source=self.top_k_per_source,
            gateway_manifest=self.tool_manifest,
            hop=hop,
            current_query=normalized_question,
            next_source_index=0,
            rewrite_used=False,
            query_count=0,
            read_count=0,
            accumulated_responses=(),
            evidence=(),
            evidence_gaps=(),
            query_trace=(),
            tool_trace=(),
            phase_trace=phases,
            authoritative_facts=facts,
        )

    def _result(
        self,
        state: RetrievalLoopState,
        *,
        coverage_satisfied: bool,
        gaps: tuple[str, ...],
        stop_reason: StopReason,
        phases: tuple[str, ...] | None = None,
    ) -> AgenticRetrievalResult:
        phase_trace = phases or (*state.phase_trace, "stop")
        return AgenticRetrievalResult(
            question=state.question,
            intent=state.intent,
            coverage_satisfied=coverage_satisfied,
            evidence_gaps=gaps,
            stop_reason=stop_reason,
            evidence=state.evidence,
            citations=self._citations(state.evidence),
            query_trace=state.query_trace,
            tool_trace=state.tool_trace,
            phase_trace=phase_trace,
            authoritative_facts=state.authoritative_facts,
            hop_count=state.hop,
            query_count=state.query_count,
            read_count=state.read_count,
            rewrite_used=state.rewrite_used,
        )

    async def resume(
        self,
        state: RetrievalLoopState,
        *,
        max_new_reads: int | None = None,
    ) -> AgenticRetrievalResult | RetrievalInterruption:
        if type(state) is not RetrievalLoopState:
            raise TypeError("resume requires an exact RetrievalLoopState")
        if max_new_reads is not None and (
            isinstance(max_new_reads, bool)
            or not isinstance(max_new_reads, int)
            or max_new_reads <= 0
        ):
            raise ValueError("max_new_reads must be a positive integer or None")
        if (
            state.gateway_manifest != self.tool_manifest
            or state.budgets != self.budgets
            or state.top_k_per_source != self.top_k_per_source
        ):
            raise ValueError("retrieval checkpoint does not match this retriever")
        if state.hop == 0:
            return self._result(
                state,
                coverage_satisfied=False,
                gaps=("Question is empty; no evidence was retrieved.",),
                stop_reason="empty_query",
            )

        hop = state.hop
        query = state.current_query
        next_source_index = state.next_source_index
        rewritten = state.rewrite_used
        query_count = state.query_count
        read_count = state.read_count
        accumulated = list(state.accumulated_responses)
        evidence = state.evidence
        gaps = state.evidence_gaps
        query_trace = list(state.query_trace)
        tool_trace = list(state.tool_trace)
        phases = list(state.phase_trace)
        progress_signature = state.progress_signature
        progress_repeat_count = state.progress_repeat_count
        reads_this_run = 0

        def checkpoint() -> RetrievalLoopState:
            return RetrievalLoopState(
                question=state.question,
                intent=state.intent,
                sources=state.sources,
                budgets=state.budgets,
                top_k_per_source=state.top_k_per_source,
                gateway_manifest=state.gateway_manifest,
                hop=hop,
                current_query=query,
                next_source_index=next_source_index,
                rewrite_used=rewritten,
                query_count=query_count,
                read_count=read_count,
                accumulated_responses=tuple(accumulated),
                evidence=evidence,
                evidence_gaps=gaps,
                query_trace=tuple(query_trace),
                tool_trace=tuple(tool_trace),
                phase_trace=tuple(phases),
                progress_signature=progress_signature,
                progress_repeat_count=progress_repeat_count,
                authoritative_facts=state.authoritative_facts,
            )

        def finish(
            *,
            coverage: bool,
            result_gaps: tuple[str, ...],
            reason: StopReason,
        ) -> AgenticRetrievalResult:
            current = checkpoint()
            return self._result(
                current,
                coverage_satisfied=coverage,
                gaps=result_gaps,
                stop_reason=reason,
            )

        while hop <= state.budgets.max_hops:
            if next_source_index < len(state.sources):
                if (
                    query_count >= state.budgets.max_queries
                    or read_count >= state.budgets.max_read_calls
                ):
                    return finish(
                        coverage=False,
                        result_gaps=gaps or ("Retrieval budget exhausted before source coverage.",),
                        reason="budget_exhausted",
                    )
                if next_source_index == 0:
                    phases.append("hybrid_retrieve")
                source = state.sources[next_source_index]
                query_trace.append(
                    RetrievalQueryTrace(
                        hop=hop,
                        query=query,
                        source=source,
                        rewritten=rewritten,
                    )
                )
                response = await self._retrieve_source(source, query)
                accumulated.append(response)
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
                query_count += 1
                read_count += 1
                reads_this_run += 1
                next_source_index += 1
                if max_new_reads is not None and reads_this_run >= max_new_reads:
                    return RetrievalInterruption(state=checkpoint())
                continue

            phases.append("fuse_rerank")
            evidence = self._merge(accumulated)
            phases.append("gap_critic")
            gaps = self._critic(
                state.question,
                state.sources,
                evidence,
                state.authoritative_facts,
            )
            if not gaps:
                reason: StopReason = (
                    "coverage_satisfied_after_rewrite" if rewritten else "coverage_satisfied"
                )
                return finish(coverage=True, result_gaps=(), reason=reason)
            if rewritten or hop >= state.budgets.max_hops:
                return finish(
                    coverage=False,
                    result_gaps=gaps,
                    reason="evidence_gap_after_rewrite",
                )
            if (
                query_count + len(state.sources) > state.budgets.max_queries
                or read_count + len(state.sources) > state.budgets.max_read_calls
            ):
                return finish(
                    coverage=False,
                    result_gaps=gaps,
                    reason="budget_exhausted",
                )

            phases.append("rewrite")
            rewritten_query = self._rewrite(query, gaps)
            watchdog = ProgressWatchdog(max_repeats=1)
            watchdog.current_signature = progress_signature
            watchdog.repeat_count = progress_repeat_count
            try:
                watchdog.observe(
                    {"query": query, "sources": list(state.sources), "gaps": list(gaps)}
                )
                watchdog.observe(
                    {
                        "query": rewritten_query,
                        "sources": list(state.sources),
                        "gaps": list(gaps),
                    }
                )
            except ProgressStalledError:
                return finish(
                    coverage=False,
                    result_gaps=gaps,
                    reason="progress_stalled",
                )
            progress_signature = watchdog.current_signature
            progress_repeat_count = watchdog.repeat_count
            query = rewritten_query
            rewritten = True
            hop += 1
            next_source_index = 0

        raise RuntimeError("unreachable bounded retrieval state")

    async def retrieve(
        self,
        question: str,
        *,
        authoritative_facts: Mapping[str, Any] | None = None,
    ) -> AgenticRetrievalResult:
        outcome = await self.resume(self.start(question, authoritative_facts=authoritative_facts))
        if isinstance(outcome, RetrievalInterruption):
            raise RuntimeError("unbounded retrieve unexpectedly interrupted")
        return outcome
