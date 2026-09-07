# RecallOps 4:55 Demo Walkthrough

![RecallOps flagship walkthrough from investigation to blocked closure](images/recallops-five-minute-demo.png)

This script is derived from [`demo_contract.json`](demo_contract.json) and the implemented durable runtime/five-view UI. The [five-minute story](images/recallops-five-minute-demo.png) is the presentation overview; the [system architecture](images/recallops-system-architecture.png), reproducible [demo-story diagram](images/07_demo_story.svg), and [evaluation architecture](images/10_evaluation_architecture.svg) provide progressively deeper proof. The timed table below is the exact 4:55 click sequence. Use the [business-domain guide](BUSINESS_DOMAIN.md), [business recall lifecycle](images/08_business_recall_lifecycle.svg), and [domain evidence model](images/09_domain_evidence_model.svg) for a one-slide orientation before the timed product walkthrough.

## Preflight

Start from a clean shell in the repository root:

```bash
uv sync --locked --all-groups
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
uv run recallops eval-scorecard --scorecard data/evals/scorecard.json
uv run streamlit run src/recallops/ui/app.py
```

For the recorded run, launch Streamlit against a fresh explicit runtime directory so an earlier rehearsal cannot supply stale case state:

```bash
RECALLOPS_DEMO_DIR="$(mktemp -d /tmp/recallops-demo.XXXXXX)"
RECALLOPS_RUNTIME_DIR="$RECALLOPS_DEMO_DIR" uv run streamlit run src/recallops/ui/app.py
```

For the MCP protocol version of the UI, replace the last command with:

```bash
RECALLOPS_MCP_TRANSPORT=stdio uv run streamlit run src/recallops/ui/app.py
```

Use durable mode, not the UI fixture mode. Confirm the browser opens on **Command Center** with no existing case. Keep the terminal available for the short CLI proof, but record the UI as the primary walkthrough.

## Failure-recovery rehearsal

Rehearse this once in a separate fresh runtime; do not arm it during the timed flagship take. Open the case, run the investigation, approve the `create_case` packet, and stop before execution. In **Audit & Evaluation**, choose **lost write response → same-key replay** under **Failure scenario** and click **Run failure fixture**. Return to **Human Review** and click **Simulate approved actions**. The durable graph should pause at outcome recovery; click **Recover recorded outcome (same key)**. Show that the retry returns one logical receipt and one version increment rather than a duplicate write. Then close that rehearsal process and use a newly created runtime directory for the timed script.

## Copy/paste card

| Field | Exact value |
|---|---|
| **Recall number** | `H-1230-2026` |
| **Decision** | `approve` |
| **Actor** | `Food-safety manager` |
| **Justification** | `Authorize simulated containment for confirmed scope; retain ambiguous lot for review.` |
| Escalation alternative | `Do not close while acknowledgement, ambiguity, or reconciliation gaps remain.` |

The decision vocabulary is exactly `approve`, `edit`, `reject`, and `escalate`; the visible controls are **Approve**, **Edit**, **Reject**, and **Escalate**.

## Timed script

| Time | Click/show | Say |
|---|---|---|
| 00:00 | In **Command Center**, keep `H-1230-2026` in **Recall number** and click **Open case**. Point to the official and synthetic badges. | “This is a real openFDA enforcement record and a separate fictional Northstar digital twin. The public notice does not prove retailer involvement. There is no You.com or general web search; the default path is frozen and checksummed.” |
| 00:35 | Open **Investigation**, click **Run investigation**, then show the retrieval trace, four specialist rows, critic, match classifications, and synthetic lineage. | “LangGraph owns the durable state. Agentic RAG plans a source route, combines BM25 sparse and local LSA dense retrieval with RRF and reranking, critiques coverage, and stops within two hops/four queries/eight reads. The planner delegates bounded intake, match, trace, and containment work; the verifier remains independent.” |
| 01:20 | Open **Reconciliation**. Point to the exact equation, `LOT-EXACT-170` gap, ambiguous lot, and evidence links. | “The model cannot explain away a missing unit. Structured trace and reconciliation stay authoritative: received equals on-hand, quarantined, sold, returned, disposed, plus unaccounted. Fifty exact-lot units and ambiguity remain visible closure blockers.” |
| 02:00 | Open **Human Review**. Show **Review required**, action `create_case`, case version 0, digest, sources, gaps, and remaining actions. Confirm **Decision**, **Actor**, and **Justification** have the copy/paste values. | “The first interrupt is one exact proposal at one exact version. Approval is not execution. Edit returns through verification; Reject writes nothing; Escalate stops safely.” |
| 02:35 | Click **Approve**. Explicitly show that no receipt appeared and that a separate execution confirmation is pending. Then click **Simulate approved actions** and show **Simulated action recorded**, action `create_case`, and the v0→v1 receipt. | “Approve recorded zero writes. This second confirmation lets only the approved graph node call Operations MCP once. The receipt binds actor, justification, action digest, idempotency key, and version.” |
| 03:05 | Stay in **Human Review** and show the next **Review required** packet: `apply_inventory_hold`, version 1, confirmed lot targets, and a new digest/key. | “The first approval expired when the case version changed. The graph replans, so the hold needs a fresh review. The ambiguous lot is retained for review and cannot be smuggled into confirmed scope.” |
| 03:40 | Click **Approve**, show the second execution confirmation, click **Simulate approved actions**, and show the `apply_inventory_hold` v1→v2 receipt plus **Simulated action recorded**. | “This is one write per version, not a batch convenience call. Both records are simulated; no real inventory system was touched.” |
| 04:00 | Open **Audit & Evaluation**. Show the verified scorecard, then reveal **Safety**, **Retrieval quality**, and **Orchestration quality** in that order. Point to 21/21; the six configuration rows; `+0.00568`, `+0.00527`, and `0/0/8`; the two profile rows and zero quality/tool-call deltas; and `not_run_missing_credentials`. Mention the separately rehearsed exact-key recovery. If the scorecard cannot load, show the preflight `eval-scorecard` summary and committed reports instead—never substitute a fixture. | “Safety tests prove no approval bypass or false close: 21 of 21 scenarios pass. Retrieval evals compare six ablations—BM25, local LSA, naive hybrid, RRF, reranking, and agentic RAG—on the same 96 labelled questions. Fusion Recall-at-five delta is plus 0.00568; rerank nDCG-at-five delta is plus 0.00527; eight rewrite cases are unchanged, so no uplift is assumed. The 24-case orchestration benchmark compares a bounded single agent with four specialists. Evidence coverage, task success, duplicate work, and tool-call deltas are zero: multi-agent value is measured, not assumed. Optional live Deep Agents was not run because credentials were missing and is excluded from offline gates.” |
| 04:45 | Click **Request closure** and point to **Open — closure blocked** and its returned blockers; finish by 04:55. | “Closure is a separate decision, not a side effect of containment. Ambiguity and reconciliation evidence keep this synthetic retailer case open; FDA termination remains separate.” |

## What the audience should have seen

1. Official and **SYNTHETIC — ACADEMIC DEMO** evidence never merge into a claim of retailer involvement.
2. Agentic RAG, planning, four specialists, independent verification, and MCP calls are visible, bounded, and cited.
3. **Approve** and **Simulate approved actions** are two different human decisions.
4. `create_case` v0→v1 and `apply_inventory_hold` v1→v2 each produce one durable receipt.
5. Audit/recovery evidence makes retries and stale/concurrent requests inspectable.
6. Three digest-verified evaluation sections report measured offline evidence: 21 safety cases, 96 retrieval cases across six configurations, and 24 orchestration cases across two profiles.
7. The observed retrieval deltas are modest, the deterministic orchestration quality/tool-call deltas are zero, and the optional live Deep Agents run is visibly excluded—not narrated as uplift.
8. **Request closure** ends **Open — closure blocked**, which is the intended flagship safety outcome.

## Optional positive-close proof (outside the timed flagship)

Do not replace the blocked flagship with a happy path. If a reviewer asks whether closure can ever succeed, open `data/evals/report.json` at R17 or run the evaluator and explain: the isolated `LOT-PROBABLE-160` scope has zero unaccounted units; the runtime performs reviewed disposition if needed, facility tasks, one acknowledgement per required facility, a separate closure review and execution confirmation, then one `close_case` receipt. Operations revalidates all gates transactionally.

## Presenter Q&A

| Question | Answer |
|---|---|
| Why not use You.com/web search? | General search adds nondeterministic/untrusted critical-path data. RecallOps uses an allowlisted openFDA lookup plus a frozen snapshot and committed FDA/GS1 references. |
| Is LSA really dense retrieval? | Yes: TF-IDF vectors are projected into a local 64-dimensional latent semantic space. It is deliberately called local LSA, not neural embeddings. |
| Is Deep Agents actually present? | The repository builds a real fixed-subagent Deep Agents graph, but the committed live comparison status is `not_run_missing_credentials` and excluded from offline gates. The default durable workflow uses the deterministic plan; agents never receive Operations tools. |
| Did multi-agent orchestration outperform the baseline? | Not in this deterministic 24-case corpus: evidence coverage, task success, duplicate work, and total tool-call deltas are zero. The profiles make delegation observable; the report does not claim unsupported uplift. |
| Why two approvals? | The first approves the evidence-bound action. The second confirms execution with the persisted execution ID/key. This prevents “approve” from silently becoming a write. |
| What if the process restarts? | Reopen the same checkpoint and Operations database paths. The same thread/checkpoint/interrupt resumes; completed reasoning is not rerun. |
| Are Northstar holds real? | No. Every operational record/receipt is **SYNTHETIC — ACADEMIC DEMO** and status `simulated`. |
| Is internal close FDA termination? | No. FDA termination is an external regulatory decision; RecallOps only simulates closing a fictional retailer case. |
