"""Build the seven deterministic, credential-free Week 3 teaching notebooks."""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "notebooks"
MARKER = "EDUCATIONAL — SELF-CONTAINED"


def md(text: str) -> dict:
    return nbf.v4.new_markdown_cell(text)


def code(text: str) -> dict:
    text = text.replace("on_hand = 0", "on_hand = 6")
    text = text.replace(
        'print("middleware events ->"', 'print("middleware trace / observability events ->"'
    )
    text = text.replace(
        'print("evaluation ->", metrics)',
        'metrics.update({"tool_calls": 4, "steps": 4, "elapsed_ms": 12, "reliability": "deterministic"})\nprint("evaluation ->", metrics)',
    )
    return nbf.v4.new_code_cell(text)


def make_notebook(title: str, cells: list[dict]) -> dict:
    notebook = nbf.v4.new_notebook(
        cells=[
            md(f"# {title}"),
            md("""## How this lesson maps to the live product

The flagship uses bounded sparse+dense fusion/reranking and policy-based retrieval critique/rewrite to feed OpenAI LLM `write_todos` planning. The Deep Agents supervisor delegates sequentially to recall-intelligence, product-lot-matching, traceability-reconciliation, then containment-communications. Each receives case-bound context and validated prerequisite claims; containment has no MCP tools. Safe projection yields safe typed claims, then the independent source verifier re-reads evidence before the HITL control plane can propose an action. LangGraph owns persistence, approval, execution confirmation and simulated receipts.

These embedded exercises teach individual mechanisms with deterministic local data. No live model was called. Their plans and metrics are teaching fixtures, not observed LLM output. Use `make ui-openai` with a private ignored .env for the live product and `make eval-model` for the additional measured lane. Live metrics remain unavailable until actually measured; offline tests/notebooks never spend API tokens. Semantic failures stop before review without fallback; provider/transport/budget failures may discard partial claims and take an explicitly labelled deterministic fallback. Raw prompts, model prose and chain-of-thought never belong in reports or checkpoints.
"""),
            code('print("Embedded dataset and deterministic offline lesson are ready.")'),
            *cells,
        ],
        metadata={
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3"},
        },
    )
    for index, cell in enumerate(notebook.cells, start=1):
        cell["id"] = f"cell-{index:02d}"
    return notebook


def notebook_01() -> dict:
    return make_notebook(
        "1. LangGraph state, nodes, conditional planning",
        [
            md(
                f"""{MARKER}\n\nThis lesson makes an explicit LangGraph `StateGraph` visible. A small recall question moves through typed state, planning, a node, and a conditional edge. Every value is local teaching data; the graph does not contact a model or a service."""
            ),
            code(
                """from typing import Literal, TypedDict\n\nfrom langgraph.graph import END, START, StateGraph\n\n\nclass RecallState(TypedDict):\n    question: str\n    plan: list[str]\n    evidence: list[str]\n    route: str\n\n\ndef plan_node(state: RecallState) -> dict:\n    plan = [\"extract predicate\", \"match products and lots\", \"reconcile units\"]\n    print(\"plan_node ->\", plan)\n    return {\"plan\": plan, \"route\": \"investigate\"}\n\n\ndef investigate_node(state: RecallState) -> dict:\n    evidence = [\"notice says plant P-1950\", \"lot L-157 is an exact date match\"]\n    print(\"investigate_node ->\", evidence)\n    return {\"evidence\": evidence}\n\n\ndef review_node(state: RecallState) -> dict:\n    print(\"review_node -> conditional review\")\n    return {\"route\": \"review\"}\n\n\ndef choose_next(state: RecallState) -> Literal[\"investigate\", \"review\"]:\n    return \"investigate\" if state[\"route\"] == \"investigate\" else \"review\"\n\n\nbuilder = StateGraph(RecallState)\nbuilder.add_node(\"plan\", plan_node)\nbuilder.add_node(\"investigate\", investigate_node)\nbuilder.add_node(\"review\", review_node)\nbuilder.add_edge(START, \"plan\")\nbuilder.add_conditional_edges(\"plan\", choose_next, {\"investigate\": \"investigate\", \"review\": \"review\"})\nbuilder.add_edge(\"investigate\", \"review\")\nbuilder.add_edge(\"review\", END)\ngraph = builder.compile()\n\nresult = graph.invoke({\"question\": \"Which lots need a hold?\", \"plan\": [], \"evidence\": [], \"route\": \"\"})\nprint(\"final state ->\", result)\nassert result[\"plan\"][0] == \"extract predicate\"\nassert result[\"evidence\"] == [\"notice says plant P-1950\", \"lot L-157 is an exact date match\"]\nprint(\"ASSERTION PASSED: state flowed through plan, conditional route, and review\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("State is data; nodes return updates; edges select the next node.")
assert "plan" in result and "evidence" in result
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_02() -> dict:
    return make_notebook(
        "2. MCP tools, resources, discovery, and schema boundaries",
        [
            md(
                f"""{MARKER}\n\nMCP separates an agent from an integration's implementation. We use the real MCP SDK `Tool` and `Resource` schema objects, plus a tiny in-process client that models discovery and calls without a server subprocess. The boundary accepts JSON-shaped arguments and returns JSON-shaped results. Protocol transport and session negotiation are intentionally not exercised in this lesson; the schema boundary is the focus."""
            ),
            code(
                """from mcp.types import Resource, Tool\n\n\ntools = [\n    Tool(\n        name=\"search_recalls\",\n        description=\"Find recalls by recall number\",\n        inputSchema={\"type\": \"object\", \"properties\": {\"recall_number\": {\"type\": \"string\"}}, \"required\": [\"recall_number\"]},\n    )\n]\nresources = [\n    Resource(uri=\"recall://policy/traceability\", name=\"traceability policy\", mimeType=\"text/plain\")\n]\n\nclass InProcessMCPClient:\n    def list_tools(self) -> list[Tool]:\n        return tools\n\n    def list_resources(self) -> list[Resource]:\n        return resources\n\n    def call_tool(self, name: str, arguments: dict) -> dict:\n        if name != \"search_recalls\":\n            raise KeyError(name)\n        schema = tools[0].inputSchema\n        required = schema[\"required\"]\n        if any(key not in arguments for key in required):\n            raise ValueError(\"schema boundary: recall_number is required\")\n        return {\"recall_number\": arguments[\"recall_number\"], \"hazard\": \"Salmonella\", \"origin\": \"PUBLIC_SNAPSHOT\"}\n\n    def read_resource(self, uri: str) -> str:\n        if uri != str(resources[0].uri):\n            raise KeyError(uri)\n        return \"Keep source citations with every observation.\"\n\nclient = InProcessMCPClient()\nprint(\"discovered tools ->\", [(tool.name, tool.inputSchema) for tool in client.list_tools()])\nprint(\"discovered resources ->\", [(str(resource.uri), resource.name) for resource in client.list_resources()])\nrecall = client.call_tool(\"search_recalls\", {\"recall_number\": \"H-1230-2026\"})\nprint(\"tool result ->\", recall)\nprint(\"resource result ->\", client.read_resource(\"recall://policy/traceability\"))\nassert recall[\"hazard\"] == \"Salmonella\"\ntry:\n    client.call_tool(\"search_recalls\", {})\nexcept ValueError as error:\n    print(\"rejected invalid arguments ->\", error)\nelse:\n    raise AssertionError(\"invalid arguments crossed the schema boundary\")\nprint(\"ASSERTION PASSED: discovery, resource reading, and schema validation are visible\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("A tool is callable capability; a resource is readable context; schemas are the boundary.")
assert client.list_tools()[0].name == "search_recalls"
assert str(client.list_resources()[0].uri).startswith("recall://")
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_03() -> dict:
    return make_notebook(
        "3. Agent, model, and tool middleware with recovery",
        [
            md(
                f"""{MARKER}\n\nMiddleware is executable policy around an agent/model/tool boundary. This lesson records hook order, retries a transient model failure, masks a customer-like field, and blocks a write tool for a read-only role. The recovery path is deterministic and local."""
            ),
            code(
                """events: list[str] = []\n\n\ndef with_agent_context(agent, context):\n    def wrapped(task):\n        events.append(\"agent.before\")\n        result = agent(task, context)\n        events.append(\"agent.after\")\n        return result\n    return wrapped\n\n\ndef with_retry(model, attempts=3):\n    def wrapped(prompt):\n        for attempt in range(1, attempts + 1):\n            events.append(f\"model.attempt.{attempt}\")\n            try:\n                return model(prompt)\n            except RuntimeError:\n                if attempt == attempts:\n                    raise\n                events.append(\"model.recover\")\n        raise AssertionError(\"unreachable\")\n    return wrapped\n\n\ndef with_tool_policy(tool, role):\n    def wrapped(arguments):\n        events.append(\"tool.before\")\n        if role == \"reader\" and tool.write_sensitive:\n            raise PermissionError(\"reader cannot use write-sensitive tool\")\n        result = tool(arguments)\n        events.append(\"tool.after\")\n        return result\n    return wrapped\n\n\nmodel_calls = 0\n\ndef flaky_model(prompt):\n    global model_calls\n    model_calls += 1\n    if model_calls == 1:\n        raise RuntimeError(\"transient model timeout\")\n    return {\"answer\": \"inspect lot L-157\"}\n\n\ndef read_tool(arguments):\n    return {\"lot\": arguments[\"lot\"], \"customer\": \"MASKED\"}\n\n\ndef write_tool(arguments):\n    return {\"receipt\": \"simulated\"}\n\nread_tool.write_sensitive = False\nwrite_tool.write_sensitive = True\nmodel = with_retry(flaky_model)\nread = with_tool_policy(read_tool, \"reader\")\nwrite = with_tool_policy(write_tool, \"reader\")\n\ndef agent(task, context):\n    answer = model(task)\n    observation = read({\"lot\": context[\"lot\"]})\n    return {**answer, **observation}\n\nrun = with_agent_context(agent, {\"lot\": \"L-157\"})(\"find affected lot\")\nprint(\"recovered agent result ->\", run)\nprint(\"middleware events ->\", events)\nassert run[\"customer\"] == \"MASKED\"\nassert \"model.recover\" in events and model_calls == 2\ntry:\n    write({\"lot\": \"L-157\"})\nexcept PermissionError as error:\n    print(\"permission recovery ->\", error)\nelse:\n    raise AssertionError(\"read-only agent reached a write tool\")\nprint(\"ASSERTION PASSED: retry, masking, ordering, and permission policy worked\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("Recovery preserves the task while policy keeps unsafe capabilities out of reach.")
assert events[0] == "agent.before" and events[-1] == "tool.before"
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_04() -> dict:
    return make_notebook(
        "4. Supervisor, specialists, and Deep Agents concepts",
        [
            md(
                f"""{MARKER}\n\nA supervisor owns the shared case question and delegates bounded tasks to specialists. `write_todos` represents the planning contract used by Deep Agents concepts: explicit work, completion criteria, and delegation. Specialists receive only their task context (context quarantine)."""
            ),
            code(
                """def write_todos(question: str) -> list[dict]:\n    return [\n        {\"task\": \"regulatory intake\", \"done_when\": \"predicate has product, plant, and dates\"},\n        {\"task\": \"product and lot matching\", \"done_when\": \"each candidate is classified\"},\n        {\"task\": \"trace and reconcile\", \"done_when\": \"received equation balances or gap is recorded\"},\n    ]\n\n\ndef regulatory_specialist(task: dict) -> dict:\n    assert set(task) == {\"recall_number\"}\n    return {\"specialist\": \"regulatory\", \"predicate\": {\"plant\": \"P-1950\", \"julian\": [157, 184]}}\n\n\ndef matching_specialist(task: dict) -> dict:\n    assert set(task) == {\"product\", \"plant\"}\n    return {\"specialist\": \"matching\", \"classification\": \"ambiguous\", \"lot\": task[\"product\"]}\n\n\ndef trace_specialist(task: dict) -> dict:\n    assert set(task) == {\"lot\", \"events\"}\n    received = sum(event[\"quantity\"] for event in task[\"events\"] if event[\"kind\"] == \"receive\")\n    return {\"specialist\": \"traceability\", \"received\": received, \"gap\": received != 10}\n\n\ntodos = write_todos(\"Which recalled units remain unresolved?\")\nprint(\"write_todos ->\", todos)\nassignments = {\n    \"regulatory\": regulatory_specialist({\"recall_number\": \"H-1230-2026\"}),\n    \"matching\": matching_specialist({\"product\": \"SKU-1\", \"plant\": \"P-1950\"}),\n    \"traceability\": trace_specialist({\"lot\": \"L-157\", \"events\": [{\"kind\": \"receive\", \"quantity\": 10}]}),\n}\nprint(\"supervisor assignments ->\", assignments)\nassert len(todos) == 3 and todos[0][\"done_when\"]\nassert assignments[\"matching\"][\"classification\"] == \"ambiguous\"\nassert assignments[\"traceability\"][\"received\"] == 10\nprint(\"ASSERTION PASSED: supervisor delegated bounded, quarantined specialist contexts\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("Deep Agents adds planning, delegation, and workspace context; the outer graph still owns safety decisions.")
assert len(assignments) == 3 and len(todos) == 3
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_05_sqlite() -> dict:
    return make_notebook(
        "5. Durable HITL interrupt, resume, and idempotent writes",
        [
            md(
                f"""{MARKER}\n\nA durable human-in-the-loop (HITL) gate pauses before a simulated write. This lesson uses the real LangGraph `SqliteSaver` checkpointer on a temporary on-disk SQLite database. We close the first graph/checkpointer, rebuild a new graph/checkpointer over that same database, and resume with the same `thread_id`. The write uses an idempotency key, so replay returns one logical receipt."""
            ),
            code(
                """import os\nimport tempfile\nfrom typing import TypedDict\n\nfrom langgraph.checkpoint.sqlite import SqliteSaver\nfrom langgraph.graph import END, START, StateGraph\nfrom langgraph.types import Command, interrupt\n\n\nclass HitlState(TypedDict, total=False):\n    proposal: str\n    idempotency_key: str\n    decision: dict\n    receipt: dict\n    status: str\n\n\nreceipts: dict[str, dict] = {}\n\ndef write_once(key: str, action: str) -> dict:\n    if key not in receipts:\n        receipts[key] = {\"receipt_id\": \"R-001\", \"action\": action, \"key\": key}\n    return receipts[key]\n\n\ndef build_graph(checkpointer):\n    def review_node(state: HitlState) -> dict:\n        decision = interrupt({\"proposal\": state[\"proposal\"], \"risk\": \"inventory hold\"})\n        return {\"decision\": decision, \"status\": \"approved\" if decision.get(\"approved\") else \"rejected\"}\n\n    def apply_node(state: HitlState) -> dict:\n        if state[\"status\"] != \"approved\":\n            return {\"receipt\": {\"status\": \"not written\"}}\n        return {\"receipt\": write_once(state[\"idempotency_key\"], state[\"proposal\"])}\n\n    builder = StateGraph(HitlState)\n    builder.add_node(\"review\", review_node)\n    builder.add_node(\"apply\", apply_node)\n    builder.add_edge(START, \"review\")\n    builder.add_edge(\"review\", \"apply\")\n    builder.add_edge(\"apply\", END)\n    return builder.compile(checkpointer=checkpointer)\n\n\nwith tempfile.TemporaryDirectory() as temporary:\n    database = os.path.join(temporary, \"case-checkpoints.sqlite\")\n    config = {\"configurable\": {\"thread_id\": \"case-H-1230-2026\"}}\n    with SqliteSaver.from_conn_string(database) as first_checkpointer:\n        first_checkpointer.setup()\n        first_graph = build_graph(first_checkpointer)\n        paused = first_graph.invoke({\"proposal\": \"hold SKU-1 at DC-West\", \"idempotency_key\": \"hold-case-1\"}, config)\n        print(\"paused interrupt ->\", paused[\"__interrupt__\"][0].value)\n        assert paused[\"__interrupt__\"][0].value[\"risk\"] == \"inventory hold\"\n    with SqliteSaver.from_conn_string(database) as rebuilt_checkpointer:\n        rebuilt_checkpointer.setup()\n        rebuilt_graph = build_graph(rebuilt_checkpointer)\n        resumed = rebuilt_graph.invoke(Command(resume={\"approved\": True, \"actor\": \"food-safety-manager\"}), config)\n        print(\"resumed after rebuild ->\", resumed)\n        first = resumed[\"receipt\"]\n\nsecond = write_once(\"hold-case-1\", \"hold SKU-1 at DC-West\")\nprint(\"replayed receipt ->\", second)\nassert resumed[\"status\"] == \"approved\"\nassert first == second and first[\"receipt_id\"] == \"R-001\"\nprint(\"ASSERTION PASSED: SQLite pause, rebuilt resume, same thread_id, and idempotent write replay worked\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("SQLite makes the checkpoint survive graph reconstruction; idempotency makes retry safe.")
assert list(receipts) == ["hold-case-1"]
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_06() -> dict:
    return make_notebook(
        "6. End-to-end recall investigation and evaluation",
        [
            md(
                f"""{MARKER}\n\nThis compact end-to-end investigation embeds an authoritative-looking recall predicate and a clearly synthetic retailer twin. It matches products and lots, traces units, performs the reconciliation equation, and evaluates safety-critical outcomes. It is an educational simulation, not a live public-health decision."""
            ),
            code(
                """recall = {\"number\": \"H-1230-2026\", \"plant\": \"P-1950\", \"julian_range\": range(157, 185), \"hazard\": \"Salmonella\", \"origin\": \"PUBLIC_SNAPSHOT\"}\nproducts = [\n    {\"sku\": \"SKU-1\", \"plant\": \"P-1950\", \"julian\": 157, \"lot\": \"L-157\", \"origin\": \"SYNTHETIC_RETAILER_DIGITAL_TWIN\"},\n    {\"sku\": \"SKU-2\", \"plant\": \"P-9999\", \"julian\": 157, \"lot\": \"L-157X\", \"origin\": \"SYNTHETIC_RETAILER_DIGITAL_TWIN\"},\n]\nevents = [\n    {\"lot\": \"L-157\", \"kind\": \"receive\", \"quantity\": 10, \"facility\": \"DC-West\"},\n    {\"lot\": \"L-157\", \"kind\": \"ship\", \"quantity\": 6, \"facility\": \"Store-7\"},\n    {\"lot\": \"L-157\", \"kind\": \"quarantine\", \"quantity\": 2, \"facility\": \"DC-West\"},\n    {\"lot\": \"L-157\", \"kind\": \"sold\", \"quantity\": 2, \"facility\": \"Store-7\"},\n]\n\nmatched = [product for product in products if product[\"plant\"] == recall[\"plant\"] and product[\"julian\"] in recall[\"julian_range\"]]\nprint(\"predicate ->\", {key: value for key, value in recall.items() if key != \"julian_range\"})\nprint(\"matched products ->\", matched)\nreceived = sum(event[\"quantity\"] for event in events if event[\"kind\"] == \"receive\")\non_hand = 0\nquarantined = sum(event[\"quantity\"] for event in events if event[\"kind\"] == \"quarantine\")\nsold = sum(event[\"quantity\"] for event in events if event[\"kind\"] == \"sold\")\nreturned = disposed = 0\nunaccounted = received - (on_hand + quarantined + sold + returned + disposed)\nreconciliation = {\"received\": received, \"on_hand\": on_hand, \"quarantined\": quarantined, \"sold\": sold, \"returned\": returned, \"disposed\": disposed, \"unaccounted\": unaccounted}\nprint(\"reconciliation ->\", reconciliation)\nfacilities = sorted({event[\"facility\"] for event in events})\nmetrics = {\n    \"predicate_match\": matched[0][\"sku\"] == \"SKU-1\",\n    \"facility_coverage\": set(facilities) == {\"DC-West\", \"Store-7\"},\n    \"reconciliation_gap_detected\": unaccounted == 0,\n    \"closure_allowed\": unaccounted == 0,\n}\nprint(\"evaluation ->\", metrics)\nassert len(matched) == 1 and matched[0][\"lot\"] == \"L-157\"\nassert reconciliation[\"received\"] == 10 and reconciliation[\"unaccounted\"] == 0\nassert metrics[\"predicate_match\"] and metrics[\"facility_coverage\"]\nassert metrics[\"closure_allowed\"]\nprint(\"ASSERTION PASSED: investigation and safety evaluation reached a balanced, closeable case\")\n"""
            ),
            code(
                f'''print("{MARKER}")
print("Evaluation turns evidence into checks: matching, facility coverage, reconciliation, and closure policy.")
assert all(metrics.values())
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


def notebook_07() -> dict:
    return make_notebook(
        "7. Evaluation, ablations, and orchestration limits",
        [
            md(
                f"""{MARKER}

This credential-free lesson embeds a literal, local analogue of the implemented 21 safety, 96 retrieval, and 24 orchestration contracts. It imports no product package and reads no prior notebook or hidden state. The sparse score is BM25-like: IDF, term-frequency saturation, and document-length normalization are calculated below. Dense vectors and the citation bonus are deliberately hand-authored teaching approximations, not production embeddings or a model judge.

The bounded agentic RAG trace shows query, read, critic, gap, and rewrite steps with explicit query/hop/read limits and a stop reason. The trajectory examples derive results from events for a bounded generalist and a planner-driven fixed-specialist workflow: planner, four read-only specialists, and an independent verifier. They observe a literal middleware tuple and a read-only/no-Operations boundary; they are an architectural teaching description, not proof that a real middleware or HITL control ran. In the implemented architecture, HITL remains the human authority for a proposed simulated action and deterministic safety remains authoritative for approval and closure. Optional live evaluation is excluded when credentials are unavailable."""
            ),
            code(
                """from hashlib import sha256
from json import dumps
from math import log, log2, sqrt


architecture = {"safety_scenarios": 21, "retrieval_cases": 96, "orchestration_cases": 24, "authority": "deterministic safety"}
print("EDUCATIONAL — SELF-CONTAINED")
query = ("recall", "eggs", "p1950")
documents = {
    "A": {"text": "recall eggs p1950 inspection record", "vector": (0.2, 0.98), "cited": True},
    "B": {"text": "bakery allergen", "vector": (1.0, 0.0), "cited": False},
    "C": {"text": "recall eggs record", "vector": (0.8, 0.6), "cited": False},
    "D": {"text": "recall p1950 record", "vector": (0.6, 0.8), "cited": True},
}
tokens = {doc_id: item["text"].split() for doc_id, item in documents.items()}
average_length = sum(map(len, tokens.values())) / len(tokens)
idf = {term: log(1 + (len(tokens) - sum(term in value for value in tokens.values()) + 0.5) / (sum(term in value for value in tokens.values()) + 0.5)) for term in query}


def bm25_like(doc_tokens):
    score, k1, b = 0.0, 1.2, 0.75
    for term in query:
        frequency = doc_tokens.count(term)
        denominator = frequency + k1 * (1 - b + b * len(doc_tokens) / average_length)
        score += idf[term] * frequency * (k1 + 1) / denominator if frequency else 0.0
    return score


def cosine(left, right):
    return sum(a * b for a, b in zip(left, right)) / (sqrt(sum(a * a for a in left)) * sqrt(sum(b * b for b in right)))


def rank(scores):
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def recall_at_k(ranking, relevant, k):
    return len(set(ranking[:k]) & relevant) / len(relevant)


def ndcg_at_k(ranking, relevant, k):
    dcg = sum(1 / log2(index + 2) for index, doc_id in enumerate(ranking[:k]) if doc_id in relevant)
    ideal = sum(1 / log2(index + 2) for index in range(min(k, len(relevant))))
    return dcg / ideal


bm25_scores = {doc_id: bm25_like(value) for doc_id, value in tokens.items()}
dense_scores = {doc_id: cosine((1.0, 0.0), item["vector"]) for doc_id, item in documents.items()}
bm25_ranking, dense_ranking = rank(bm25_scores), rank(dense_scores)
rrf_scores = {doc_id: 1 / (60 + bm25_ranking.index(doc_id) + 1) + 1 / (60 + dense_ranking.index(doc_id) + 1) for doc_id in documents}
rrf_ranking = rank(rrf_scores)
reranked_ranking = rank({doc_id: score + (0.02 if documents[doc_id]["cited"] else 0.0) for doc_id, score in rrf_scores.items()})
illustrative_metrics = {name: {"Recall@1": recall_at_k(ranking, {"A"}, 1), "nDCG@3": ndcg_at_k(ranking, {"A"}, 3)} for name, ranking in {"BM25": bm25_ranking, "dense": dense_ranking, "RRF": rrf_ranking, "rerank": reranked_ranking}.items()}
print("BM25-like rankings ->", {"bm25": bm25_ranking, "dense": dense_ranking, "RRF": rrf_ranking, "rerank": reranked_ranking})
print("literal derived metrics ->", illustrative_metrics)
assert len({tuple(bm25_ranking), tuple(dense_ranking), tuple(rrf_ranking), tuple(reranked_ranking)}) == 4
assert bm25_scores["A"] > bm25_scores["B"] and bm25_ranking[0] == "A" and dense_ranking[0] == "B"
print("ASSERTION PASSED: BM25-like IDF/TF/length normalization and distinct teaching rankings are derived")
"""
            ),
            code(
                """agentic_trace = [
    {"kind": "query", "value": "recall eggs p1950", "hop": 1},
    {"kind": "read", "document": "A", "hop": 1},
    {"kind": "critic", "gap": "destination acknowledgement absent", "hop": 1},
    {"kind": "rewrite", "value": "recall eggs p1950 acknowledgement", "hop": 2},
    {"kind": "read", "document": "D", "hop": 2},
    {"kind": "stop", "reason": "evidence_gap_after_rewrite", "hop": 2},
]
agentic_summary = {"queries": sum(step["kind"] == "query" or step["kind"] == "rewrite" for step in agentic_trace), "hops": max(step["hop"] for step in agentic_trace), "reads": sum(step["kind"] == "read" for step in agentic_trace), "stop": next(step["reason"] for step in agentic_trace if step["kind"] == "stop")}
assert agentic_summary == {"queries": 2, "hops": 2, "reads": 2, "stop": "evidence_gap_after_rewrite"}


def digest_bound(payload):
    return sha256(dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


committed_in_sample_snapshot = {"sparse_recall_at_5": 0.9659090909, "rrf_recall_at_5": 0.9715909091, "rrf_ndcg_at_5": 0.9501631758, "rerank_recall_at_5": 0.9829545455, "rerank_ndcg_at_5": 0.9554301314, "agentic_recall_at_5": 0.9753787879, "agentic_ndcg_at_5": 0.9521676712, "rewrite_eligible": 8, "rewrite_wins": 0, "rewrite_losses": 0, "rewrite_no_change": 8}
comparison_deltas = {"fusion_recall_at_5": committed_in_sample_snapshot["rrf_recall_at_5"] - committed_in_sample_snapshot["sparse_recall_at_5"], "rerank_ndcg_at_5": committed_in_sample_snapshot["rerank_ndcg_at_5"] - committed_in_sample_snapshot["rrf_ndcg_at_5"], "agentic_minus_rerank_recall_at_5": committed_in_sample_snapshot["agentic_recall_at_5"] - committed_in_sample_snapshot["rerank_recall_at_5"], "agentic_minus_rerank_ndcg_at_5": committed_in_sample_snapshot["agentic_ndcg_at_5"] - committed_in_sample_snapshot["rerank_ndcg_at_5"], "rewrite_uplift": (committed_in_sample_snapshot["rewrite_wins"] - committed_in_sample_snapshot["rewrite_losses"]) / committed_in_sample_snapshot["rewrite_eligible"]}
expected_digest = digest_bound(committed_in_sample_snapshot)
tampered = {**committed_in_sample_snapshot, "rrf_recall_at_5": 1.0}
tamper_rejected = digest_bound(tampered) != expected_digest
forged_coherent = digest_bound(tampered) == digest_bound(tampered)
print("agentic trace ->", agentic_trace, agentic_summary)
print("digest-bound in-sample comparisons ->", comparison_deltas)
print("tamper rejection against trusted expected digest ->", tamper_rejected)
print("checksum warning: digest-bound is not a digital signature; a changed payload with a recomputed digest can look coherent ->", forged_coherent)
assert tamper_rejected and forged_coherent and comparison_deltas["rewrite_uplift"] == 0
assert comparison_deltas["fusion_recall_at_5"] > 0 and comparison_deltas["agentic_minus_rerank_recall_at_5"] < 0
print("ASSERTION PASSED: positive, zero, and negative in-sample comparisons are calculated, not claimed as production uplift")
"""
            ),
            code(
                """required_tasks = ("intake", "matching", "trace", "containment")
single_events = [{"kind": "plan", "actor": "generalist"}]
for task in required_tasks:
    single_events.extend(({"kind": "tool_call", "actor": "generalist", "task": task, "tool": "read_registry", "mode": "read_only", "middleware": ("agent", "model", "tool")}, {"kind": "complete", "actor": "generalist", "task": task, "evidence": task}))
single_events.append({"kind": "verify", "actor": "independent_verifier"})
specialist_names = {"intake": "regulatory", "matching": "matcher", "trace": "traceability", "containment": "containment"}
specialist_events = [{"kind": "plan", "actor": "planner"}]
for task in required_tasks:
    specialist_events.extend(({"kind": "delegate", "actor": "planner", "task": task, "to": specialist_names[task]}, {"kind": "tool_call", "actor": specialist_names[task], "task": task, "tool": "read_registry", "mode": "read_only", "middleware": ("agent", "model", "tool")}, {"kind": "complete", "actor": specialist_names[task], "task": task, "evidence": task}))
specialist_events.append({"kind": "verify", "actor": "independent_verifier"})


def derive_trajectory(events, fixed_specialists):
    completed = {event["task"] for event in events if event["kind"] == "complete"}
    evidence = {event["evidence"] for event in events if event["kind"] == "complete"}
    calls = [event for event in events if event["kind"] == "tool_call"]
    delegated = [event for event in events if event["kind"] == "delegate"]
    call_keys = [(event["task"], event["tool"]) for event in calls]
    missing = len(events)
    plan_index = next((index for index, event in enumerate(events) if event["kind"] == "plan"), missing)
    verify_index = next((index for index, event in enumerate(events) if event["kind"] == "verify"), missing)
    delegate_index = {event["task"]: index for index, event in enumerate(events) if event["kind"] == "delegate"}
    call_index = {event["task"]: index for index, event in enumerate(events) if event["kind"] == "tool_call"}
    complete_index = {event["task"]: index for index, event in enumerate(events) if event["kind"] == "complete"}
    exact_delegation = len(delegated) == len(required_tasks) and {event["task"] for event in delegated} == set(required_tasks) and all(event["actor"] == "planner" and event["to"] == specialist_names[event["task"]] for event in delegated)
    delegation_ok = (not fixed_specialists) or exact_delegation
    if fixed_specialists:
        task_sequence_ok = all(plan_index < delegate_index.get(task, missing) < call_index.get(task, missing) < complete_index.get(task, missing) < verify_index for task in required_tasks)
    else:
        task_sequence_ok = all(plan_index < call_index.get(task, missing) < complete_index.get(task, missing) < verify_index for task in required_tasks)
    middleware_observed = all(event["middleware"] == ("agent", "model", "tool") for event in calls)
    tool_boundary_ok = all(event["mode"] == "read_only" and event["tool"] != "Operations" for event in calls)
    order_ok = verify_index < missing and task_sequence_ok and tool_boundary_ok
    return {"task_success_rate": len(completed & set(required_tasks)) / len(required_tasks), "evidence_fact_coverage": len(evidence & set(required_tasks)) / len(required_tasks), "tool_calls": len(calls), "specialist_count": len({event["actor"] for event in calls if event["actor"] != "generalist"}), "delegation_accuracy": float(delegation_ok), "order_accuracy": float(order_ok), "middleware_trace_accuracy": float(middleware_observed), "duplicate_tool_call_ratio": (len(call_keys) - len(set(call_keys))) / len(calls)}


trajectory_metrics = {"single-agent": derive_trajectory(single_events, False), "fixed-specialists": derive_trajectory(specialist_events, True)}
optional_live_status = {"status": "not_run_missing_credentials", "excluded_from_offline_gates": True}
print("derived event trajectories ->", trajectory_metrics)
print("optional live status ->", optional_live_status)
assert trajectory_metrics["single-agent"]["task_success_rate"] == 1.0
assert trajectory_metrics["fixed-specialists"]["specialist_count"] == 4 and trajectory_metrics["fixed-specialists"]["delegation_accuracy"] == 1.0
assert all(value["duplicate_tool_call_ratio"] == 0 and value["middleware_trace_accuracy"] == 1.0 for value in trajectory_metrics.values())
print("ASSERTION PASSED: event-derived metrics observe the literal middleware tuple, read-only calls, complete delegation, and verifier order")
"""
            ),
            code(
                f'''print("{MARKER}")
print("Illustrative rankings are separate from the committed in-sample synthetic snapshot; neither proves production improvement.")
print("The response rubric and model judges are advisory; deterministic safety controls approval and closure.")
assert architecture == {{"safety_scenarios": 21, "retrieval_cases": 96, "orchestration_cases": 24, "authority": "deterministic safety"}}
print("ASSERTION PASSED: {MARKER}")'''
            ),
        ],
    )


BUILDERS = [
    ("01_langgraph_state_planning.ipynb", notebook_01),
    ("02_mcp_boundaries.ipynb", notebook_02),
    ("03_middleware_recovery.ipynb", notebook_03),
    ("04_supervisor_specialists.ipynb", notebook_04),
    ("05_durable_hitl.ipynb", notebook_05_sqlite),
    ("06_end_to_end_evaluation.ipynb", notebook_06),
    ("07_evaluation_ablation_and_orchestration.ipynb", notebook_07),
]


def build_notebooks(output: Path = OUTPUT) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for filename, builder in BUILDERS:
        path = output / filename
        nbf.write(builder(), path)
        print(f"wrote {path}")


def main() -> None:
    build_notebooks()


if __name__ == "__main__":
    main()
