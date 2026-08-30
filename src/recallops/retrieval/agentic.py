"""Bounded read-only planning and evidence-gap loop over hybrid-search gateways."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple

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
QUERY_GLUE_WORDS = frozenset(
    {
        "a",
        "after",
        "all",
        "an",
        "and",
        "applies",
        "apply",
        "about",
        "business",
        "can",
        "could",
        "determines",
        "did",
        "do",
        "does",
        "evidence",
        "explain",
        "for",
        "followed",
        "from",
        "have",
        "how",
        "in",
        "inspect",
        "is",
        "it",
        "item",
        "know",
        "mean",
        "may",
        "of",
        "on",
        "or",
        "possible",
        "policy",
        "question",
        "require",
        "requirements",
        "retailer",
        "should",
        "the",
        "their",
        "them",
        "these",
        "this",
        "to",
        "through",
        "under",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)

# The critic compares normalized domain concepts, not literal surface forms.  These
# aliases intentionally remain small and auditable: retrieval is advisory, so an
# unfamiliar material concept becomes an explicit gap instead of being guessed.
CONCEPT_ALIASES = {
    "acted": "effectiveness",
    "actions": "remediation",
    "batch": "lot",
    "batches": "lot",
    "class": "classification",
    "classes": "classification",
    "classification": "classification",
    "classify": "classification",
    "classified": "classification",
    "close": "termination",
    "closed": "termination",
    "closure": "termination",
    "communication": "effectiveness",
    "communications": "effectiveness",
    "consignee": "effectiveness",
    "consignees": "effectiveness",
    "correct": "remediation",
    "corrected": "remediation",
    "correction": "remediation",
    "corrective": "remediation",
    "critical": "cte",
    "cte": "cte",
    "ctes": "cte",
    "danger": "hazard",
    "disposition": "remediation",
    "disposal": "remediation",
    "dispose": "remediation",
    "disposed": "remediation",
    "downstream": "effectiveness",
    "end": "termination",
    "ended": "termination",
    "event": "cte",
    "events": "cte",
    "facility": "facility",
    "facilities": "facility",
    "fda": "regulator",
    "follow": "traceability",
    "following": "traceability",
    "follows": "traceability",
    "finish": "termination",
    "finished": "termination",
    "fix": "remediation",
    "fixes": "remediation",
    "handoff": "traceability",
    "handoffs": "traceability",
    "hazard": "hazard",
    "hazardous": "hazard",
    "health": "hazard",
    "held": "inventory",
    "inventory": "inventory",
    "kde": "kde",
    "kdes": "kde",
    "location": "facility",
    "locations": "facility",
    "lot": "lot",
    "lots": "lot",
    "move": "traceability",
    "moved": "traceability",
    "movement": "traceability",
    "movements": "traceability",
    "notice": "effectiveness",
    "notices": "effectiveness",
    "recipient": "effectiveness",
    "recipients": "effectiveness",
    "recall": "recall",
    "recalled": "recall",
    "recalls": "recall",
    "receive": "receiving",
    "received": "receiving",
    "receiving": "receiving",
    "receipt": "receiving",
    "delivered": "receiving",
    "delivery": "receiving",
    "reconcile": "reconciliation",
    "reconciled": "reconciliation",
    "reconciliation": "reconciliation",
    "regulator": "regulator",
    "regulatory": "regulator",
    "removal": "remediation",
    "remove": "remediation",
    "risk": "hazard",
    "severity": "hazard",
    "ship": "shipping",
    "shipped": "shipping",
    "shipment": "shipping",
    "shipments": "shipping",
    "shipping": "shipping",
    "status": "remediation",
    "stock": "inventory",
    "store": "facility",
    "stores": "facility",
    "site": "facility",
    "sites": "facility",
    "supplier": "facility",
    "suppliers": "facility",
    "terminate": "termination",
    "terminated": "termination",
    "terminates": "termination",
    "termination": "termination",
    "trace": "traceability",
    "traceability": "traceability",
    "traced": "traceability",
    "tracking": "traceability",
    "took": "receiving",
    "units": "inventory",
    "unaccounted": "reconciliation",
    "upc": "product",
    "withdrawal": "recall",
    "withdrawn": "recall",
}
CONCEPT_LABELS = {
    "classification": "health-hazard classification",
    "cte": "critical tracking events",
    "effectiveness": "recall effectiveness",
    "facility": "facility/location",
    "hazard": "health hazard",
    "inventory": "inventory/stock",
    "kde": "key data elements",
    "lot": "traceability lot/batch",
    "product": "product identity",
    "receiving": "receiving event",
    "recall": "recall",
    "reconciliation": "inventory reconciliation",
    "regulator": "regulatory authority",
    "remediation": "correction/disposition",
    "shipping": "shipping event",
    "termination": "recall termination/closure",
    "traceability": "traceability handoff",
}
DOMAIN_CONCEPTS = frozenset(CONCEPT_LABELS)
REGULATORY_ROUTE_CONCEPTS = frozenset(
    {
        "classification",
        "cte",
        "effectiveness",
        "hazard",
        "kde",
        "recall",
        "regulator",
        "remediation",
        "termination",
    }
)
OPERATIONAL_ROUTE_CONCEPTS = frozenset(
    {
        "facility",
        "inventory",
        "lot",
        "product",
        "receiving",
        "reconciliation",
        "shipping",
    }
)
OFFICIAL_SUPPORT_CONCEPTS = REGULATORY_ROUTE_CONCEPTS - {"cte", "kde"}
SYNTHETIC_SUPPORT_CONCEPTS = OPERATIONAL_ROUTE_CONCEPTS - {"receiving", "shipping"}


def _domain_concepts(text: str) -> tuple[set[str], set[str]]:
    """Return canonical concepts and raw tokens consumed by their aliases."""

    raw_tokens = set(CONCEPT_TOKEN_PATTERN.findall(text.casefold()))
    consumed = {token for token in raw_tokens if token in CONCEPT_ALIASES}
    concepts = {CONCEPT_ALIASES[token] for token in consumed}
    return concepts, consumed


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


class _CapabilityDescriptor(NamedTuple):
    connection_id: Literal["regulatory_search", "operational_search"]
    tool_name: Literal["search_regulatory_evidence", "search_operational_evidence"]
    source: SourceRoute
    intent: Literal["regulatory", "operational"]


class _StdioServerIdentity(NamedTuple):
    connection_id: Literal["regulatory_search", "operational_search"]
    server_name: Literal["registry", "traceability"]
    module: Literal[
        "recallops.mcp.recall_registry_server",
        "recallops.mcp.traceability_server",
    ]


class _RetrievalConfig(NamedTuple):
    """Deeply immutable configuration retained by a sealed retrieval adapter."""

    transport: Literal["direct", "stdio"]
    data_dir: Path
    source_mode: str
    corpus_sha256: str
    python_executable: Path | None
    cwd: Path | None
    environment: tuple[tuple[str, str], ...]
    servers: tuple[_StdioServerIdentity, ...]
    capabilities: tuple[_CapabilityDescriptor, ...]
    digest: str


_CAPABILITY_DESCRIPTORS = (
    _CapabilityDescriptor(
        connection_id="regulatory_search",
        tool_name="search_regulatory_evidence",
        source="official",
        intent="regulatory",
    ),
    _CapabilityDescriptor(
        connection_id="operational_search",
        tool_name="search_operational_evidence",
        source="synthetic",
        intent="operational",
    ),
)
_STDIO_SERVER_IDENTITIES = (
    _StdioServerIdentity(
        connection_id="regulatory_search",
        server_name="registry",
        module="recallops.mcp.recall_registry_server",
    ),
    _StdioServerIdentity(
        connection_id="operational_search",
        server_name="traceability",
        module="recallops.mcp.traceability_server",
    ),
)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PYTHON_EXECUTABLE = Path(sys.executable)


def _stdio_environment(data_dir: Path, source_mode: str) -> tuple[tuple[str, str], ...]:
    return (
        ("RECALLOPS_DATA_DIR", str(data_dir)),
        ("RECALLOPS_SOURCE_MODE", source_mode),
    )


def _retrieval_config_digest(config: _RetrievalConfig) -> str:
    payload = json.dumps(
        {
            "transport": config.transport,
            "data_dir": str(config.data_dir),
            "source_mode": config.source_mode,
            "corpus_sha256": config.corpus_sha256,
            "python_executable": (
                str(config.python_executable) if config.python_executable is not None else None
            ),
            "cwd": str(config.cwd) if config.cwd is not None else None,
            "environment": config.environment,
            "servers": config.servers,
            "capabilities": config.capabilities,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _build_retrieval_config(transport: Literal["direct", "stdio"]) -> _RetrievalConfig:
    settings = get_settings()
    corpus = KnowledgeCorpus.load(data_dir=settings.data_dir)
    provisional = _RetrievalConfig(
        transport=transport,
        data_dir=settings.data_dir,
        source_mode=settings.source_mode,
        corpus_sha256=corpus.manifest.corpus_sha256,
        python_executable=_PYTHON_EXECUTABLE if transport == "stdio" else None,
        cwd=_PROJECT_ROOT if transport == "stdio" else None,
        environment=(
            _stdio_environment(settings.data_dir, settings.source_mode)
            if transport == "stdio"
            else ()
        ),
        servers=_STDIO_SERVER_IDENTITIES if transport == "stdio" else (),
        capabilities=_CAPABILITY_DESCRIPTORS,
        digest="",
    )
    return provisional._replace(digest=_retrieval_config_digest(provisional))


def _is_exact_string_pairs(value: Any) -> bool:
    return type(value) is tuple and all(
        type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str and type(pair[1]) is str
        for pair in value
    )


def _validate_retrieval_config(config: _RetrievalConfig, expected_digest: str) -> None:
    path_type = type(_PROJECT_ROOT)
    if type(config) is not _RetrievalConfig or type(expected_digest) is not str:
        raise ValueError("sealed retrieval configuration has an invalid identity")
    if (
        type(config.transport) is not str
        or type(config.data_dir) is not path_type
        or type(config.source_mode) is not str
        or type(config.corpus_sha256) is not str
        or type(config.python_executable) not in {path_type, type(None)}
        or type(config.cwd) not in {path_type, type(None)}
        or not _is_exact_string_pairs(config.environment)
        or type(config.servers) is not tuple
        or any(type(server) is not _StdioServerIdentity for server in config.servers)
        or type(config.capabilities) is not tuple
        or any(type(item) is not _CapabilityDescriptor for item in config.capabilities)
        or type(config.digest) is not str
    ):
        raise ValueError("sealed retrieval configuration has invalid types")
    if (
        not config.data_dir.is_absolute()
        or config.source_mode not in {"snapshot", "live"}
        or re.fullmatch(r"[0-9a-f]{64}", config.corpus_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", config.digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or config.capabilities != _CAPABILITY_DESCRIPTORS
    ):
        raise ValueError("sealed retrieval configuration is invalid")
    if config.transport == "direct":
        if (
            config.python_executable is not None
            or config.cwd is not None
            or config.environment != ()
            or config.servers != ()
        ):
            raise ValueError("sealed retrieval configuration has an invalid direct identity")
    elif config.transport == "stdio":
        if (
            config.python_executable != _PYTHON_EXECUTABLE
            or config.cwd != _PROJECT_ROOT
            or config.environment != _stdio_environment(config.data_dir, config.source_mode)
            or config.servers != _STDIO_SERVER_IDENTITIES
        ):
            raise ValueError("sealed retrieval configuration has an invalid stdio identity")
    else:
        raise ValueError("sealed retrieval configuration has an invalid transport")
    computed_digest = _retrieval_config_digest(config)
    if not hmac.compare_digest(config.digest, computed_digest) or not hmac.compare_digest(
        config.digest, expected_digest
    ):
        raise ValueError("sealed retrieval configuration digest mismatch")


def _manifest_for_config(
    config: _RetrievalConfig,
    expected_digest: str,
) -> tuple[RetrievalToolCapability, ...]:
    _validate_retrieval_config(config, expected_digest)
    return tuple(
        RetrievalToolCapability(
            name=item.tool_name,
            connection_id=item.connection_id,
            source=item.source,
            callable_identity=(
                f"{config.transport}:sealed:config-sha256={expected_digest}:"
                f"corpus-sha256={config.corpus_sha256}:{item.connection_id}"
            ),
        )
        for item in config.capabilities
    )


_GATEWAY_FACTORY_TOKEN = object()


class ClosedRetrievalGateway:
    """Factory-built two-capability adapter with no Operations surface."""

    __slots__ = ("_config", "_expected_digest")

    def __init__(
        self,
        transport: str,
        *,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _GATEWAY_FACTORY_TOKEN:
            raise TypeError("ClosedRetrievalGateway is factory-built; use direct() or stdio()")
        if transport not in {"direct", "stdio"}:
            raise TypeError("closed retrieval gateway requires a fixed transport identity")
        config = _build_retrieval_config(transport)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_expected_digest", config.digest)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("ClosedRetrievalGateway is sealed after factory construction")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("ClosedRetrievalGateway is sealed after factory construction")

    @property
    def connection_ids(self) -> tuple[str, ...]:
        _validate_retrieval_config(self._config, self._expected_digest)
        return tuple(item.connection_id for item in self._config.capabilities)

    @property
    def tool_manifest(self) -> tuple[RetrievalToolCapability, ...]:
        return _manifest_for_config(self._config, self._expected_digest)

    @classmethod
    def direct(cls) -> ClosedRetrievalGateway:
        return cls("direct", _factory_token=_GATEWAY_FACTORY_TOKEN)

    @classmethod
    def stdio(cls) -> ClosedRetrievalGateway:
        return cls("stdio", _factory_token=_GATEWAY_FACTORY_TOKEN)

    async def call(
        self,
        connection_id: str,
        query: str,
        *,
        top_k: int,
        record_types: tuple[str, ...],
    ) -> dict[str, Any]:
        config = self._config
        expected_digest = self._expected_digest
        _validate_retrieval_config(config, expected_digest)
        descriptor = next(
            (item for item in config.capabilities if item.connection_id == connection_id),
            None,
        )
        if descriptor is None:
            raise ValueError(f"unknown closed retrieval connection {connection_id!r}")
        corpus = KnowledgeCorpus.load(data_dir=config.data_dir)
        if corpus.manifest.corpus_sha256 != config.corpus_sha256:
            raise ValueError("sealed retrieval configuration corpus identity mismatch")
        request = HybridSearchRequest(
            query=query,
            top_k=top_k,
            source_filter=descriptor.source,
            intent=descriptor.intent,
            record_types=record_types,
        )
        if config.transport == "direct":
            return HybridIndex(corpus.documents).search(request).model_dump(mode="json")

        server = next(
            item for item in config.servers if item.connection_id == descriptor.connection_id
        )
        client = MultiServerMCPClient(
            {
                server.server_name: {
                    "transport": "stdio",
                    "command": str(config.python_executable),
                    "args": ["-m", server.module],
                    "cwd": str(config.cwd),
                    "env": dict(config.environment),
                }
            }
        )
        tool = next(
            item
            for item in await client.get_tools(server_name=server.server_name)
            if item.name == descriptor.tool_name
        )
        result = await tool.ainvoke(
            request.model_dump(mode="json", exclude={"source_filter", "intent"})
        )
        content = result.content if hasattr(result, "content") else result
        if isinstance(content, list) and content and isinstance(content[0], dict):
            content = content[0].get("text", content)
        if isinstance(content, str):
            return json.loads(content)
        if not isinstance(content, dict):
            raise ValueError("retrieval MCP returned a non-object response")
        return content


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
        concepts, _ = _domain_concepts(question)
        regulatory = bool(concepts & REGULATORY_ROUTE_CONCEPTS)
        operational = bool(IDENTIFIER_PATTERN.search(question)) or bool(
            concepts & OPERATIONAL_ROUTE_CONCEPTS
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
        searchable_concepts, _ = _domain_concepts(searchable)
        official_concepts, _ = _domain_concepts(
            " ".join(
                f"{item.document.title} {item.document.text}"
                for item in evidence
                if item.document.source_class == "official"
            )
        )
        synthetic_concepts, _ = _domain_concepts(
            " ".join(
                f"{item.document.title} {item.document.text}"
                for item in evidence
                if item.document.source_class == "synthetic"
            )
        )
        question_without_identifiers = IDENTIFIER_PATTERN.sub(" ", question.casefold())
        question_tokens = set(CONCEPT_TOKEN_PATTERN.findall(question_without_identifiers))
        question_concepts, consumed = _domain_concepts(question_without_identifiers)
        for concept in sorted(question_concepts & DOMAIN_CONCEPTS):
            if concept in OFFICIAL_SUPPORT_CONCEPTS and "official" in sources:
                supporting_concepts = official_concepts
            elif concept in SYNTHETIC_SUPPORT_CONCEPTS and "synthetic" in sources:
                supporting_concepts = synthetic_concepts
            else:
                supporting_concepts = searchable_concepts
            if concept not in supporting_concepts:
                gaps.append(
                    f"Requested concept {CONCEPT_LABELS[concept]!r} is not covered by "
                    "retrieved evidence."
                )
        for token in sorted(question_tokens - consumed - QUERY_GLUE_WORDS):
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
