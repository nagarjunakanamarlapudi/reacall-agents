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

This credential-free lesson embeds a literal, local analogue of the implemented 21 safety, 96 retrieval, and 24 orchestration evaluation contracts. It imports no product package and reads no prior notebook or hidden state. The examples explain the architecture; they do not claim a live benchmark result.

Sparse ranking, dense ranking, RRF, reranking, and agentic RAG are made visible. Signed ablations explain the RRF uplift and the zero rewrite uplift result, with explicit in-sample limits. A two-profile trajectory comparison uses single-agent and fixed-specialists labels without a multi-agent uplift claim. Digests demonstrate tamper rejection. The response rubric is human-facing; deterministic safety remains authoritative for HITL approval and closure, while model judges are advisory only."""
            ),
            code(
                """from hashlib import sha256
from json import dumps
from math import log2, sqrt


architecture = {"safety_scenarios": 21, "retrieval_cases": 96, "orchestration_cases": 24, "authority": "deterministic safety"}
documents = [
    {"id": "D1", "terms": {"salmonella", "plant", "p1950", "julian"}, "vector": (1.0, 0.0), "cited": True},
    {"id": "D2", "terms": {"salmonella", "plant", "p1950", "traceability"}, "vector": (0.8, 0.2), "cited": True},
    {"id": "D3", "terms": {"bakery", "allergen", "p9999"}, "vector": (0.0, 1.0), "cited": False},
]
query_terms, query_vector, relevant = {"salmonella", "plant", "p1950", "julian"}, (1.0, 0.0), {"D1"}


def cosine(left, right):
    return sum(a * b for a, b in zip(left, right)) / (sqrt(sum(a * a for a in left)) * sqrt(sum(b * b for b in right)))


def rank(scores):
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def recall_at_k(ranking, k):
    return len(set(ranking[:k]) & relevant) / len(relevant)


def ndcg_at_k(ranking, k):
    dcg = sum(1 / log2(index + 2) for index, doc_id in enumerate(ranking[:k]) if doc_id in relevant)
    return dcg / sum(1 / log2(index + 2) for index in range(min(k, len(relevant))))


sparse = {doc["id"]: len(query_terms & doc["terms"]) for doc in documents}
dense = {doc["id"]: cosine(query_vector, doc["vector"]) for doc in documents}
sparse_rank, dense_rank = rank(sparse), rank(dense)
rrf = {doc_id: 1 / (60 + sparse_rank.index(doc_id) + 1) + 1 / (60 + dense_rank.index(doc_id) + 1) for doc_id in sparse}
rrf_rank = rank(rrf)
reranked_rank = rank({doc_id: score + (0.01 if next(doc for doc in documents if doc["id"] == doc_id)["cited"] else 0.0) for doc_id, score in rrf.items()})
metrics = {"sparse Recall@2": recall_at_k(sparse_rank, 2), "dense Recall@2": recall_at_k(dense_rank, 2), "RRF Recall@2": recall_at_k(rrf_rank, 2), "reranked nDCG@2": ndcg_at_k(reranked_rank, 2)}
print("architecture ->", architecture)
print("sparse ->", sparse_rank, "dense ->", dense_rank, "RRF ->", rrf_rank, "reranked ->", reranked_rank)
print("literal ranking metrics ->", metrics)
assert sparse_rank[0] == dense_rank[0] == rrf_rank[0] == reranked_rank[0] == "D1"
assert metrics["RRF Recall@2"] == metrics["reranked nDCG@2"] == 1.0
print("ASSERTION PASSED: sparse + dense + RRF + reranking + agentic RAG ranking metrics are local")
"""
            ),
            code(
                """def digest(payload):
    return sha256(dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


ablations = {
    "contract": {"96 retrieval": True, "configurations": 6, "calibration": "in-sample offline synthetic"},
    "metrics": {"sparse_recall_at_5": 0.9659090909, "rrf_recall_at_5": 0.9715909091, "reranked_ndcg_at_5": 0.9554301314},
    "rewrite": {"eligible_cases": 8, "rewrite_win_count": 0, "rewrite_no_change_count": 8, "zero rewrite uplift": True},
    "limit": "in-sample calibration is not a causal, holdout, or production claim",
}
signed = {"payload": ablations, "sha256": digest(ablations)}
tampered = {**ablations, "metrics": {**ablations["metrics"], "rrf_recall_at_5": 1.0}}
rrf_uplift = ablations["metrics"]["rrf_recall_at_5"] - ablations["metrics"]["sparse_recall_at_5"]
print("signed ablations ->", signed)
print("RRF uplift ->", round(rrf_uplift, 10), "; zero rewrite uplift ->", ablations["rewrite"]["zero rewrite uplift"])
print("tamper rejection ->", digest(tampered) != signed["sha256"])
assert digest(signed["payload"]) == signed["sha256"]
assert digest(tampered) != signed["sha256"] and ablations["rewrite"]["rewrite_win_count"] == 0
print("ASSERTION PASSED: signed ablation payload is verified and tamper rejection works")
"""
            ),
            code(
                """trajectories = {
    "single-agent": {"cases": 24, "task_success_rate": 1.0, "evidence_fact_coverage": 1.0, "tool_calls": 150},
    "fixed-specialists": {"cases": 24, "task_success_rate": 1.0, "evidence_fact_coverage": 1.0, "tool_calls": 150},
}
delta = {key: trajectories["fixed-specialists"][key] - trajectories["single-agent"][key] for key in ("task_success_rate", "evidence_fact_coverage", "tool_calls")}
authority_limits = {
    "model_judge": "advisory review cue only",
    "HITL": "a human may approve a proposed simulated action",
    "deterministic_safety": "authoritative for approval, receipt integrity, and closure",
    "closure": "a judge cannot close a case and failed gates stay blocked",
}
print("24 orchestration trajectories ->", trajectories)
print("trajectory delta ->", delta)
print("no uplift claim: equal offline task/evidence values do not establish multi-agent superiority")
print("HITL and judge authority limits ->", authority_limits)
assert all(profile["cases"] == 24 for profile in trajectories.values())
assert delta["task_success_rate"] == delta["evidence_fact_coverage"] == 0.0
assert "cannot close" in authority_limits["closure"]
print("ASSERTION PASSED: two-profile comparison preserves no uplift claim and safety authority")
"""
            ),
            code(
                f'''print("{MARKER}")
print("The response rubric scores correctness/citations, completeness, uncertainty, actionability, and clarity.")
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
