# AI Coding and Coordination Log

## Accuracy boundary

This log separates authorized tools from tools with recorded invocation evidence. It does not attribute work to Claude Code, Grok, or a model alias merely because the user made them available.

## Recorded coordination

| Tool/workflow | Recorded role |
|---|---|
| Codex coordinator | End-to-end design, worktree orchestration, integration decisions, runtime/evaluator/UI coordination, review loops, and final verification coordination |
| Codex delegated workers | Data scale, foundation/MCP, notebooks, middleware, RAG, specialists/Deep Agents, business docs, durable workflow, evaluator, UI/CLI, and final docs |
| Mermaid CLI 11.12.0 | Pinned local diagram rendering and byte-parity verification |
| Claude Code | Available to the user; no invocation evidence is claimed in this log |
| Grok CLI | Available to the user; no invocation evidence is claimed in this log |

Coordinator attribution: **Codex (GPT-5 family; exact host alias not surfaced to this task)**. Known delegated worker identifiers include `gpt-5.6-terra` and `gpt-5.6-luna`. Exact model aliases are reported only where the task environment exposed them.

## Human-authored constraints preserved

The user required a new genuine domain, reasonable data volume, `uv`, self-contained notebooks, business-first documentation and diagrams, LangGraph orchestration, planning, multi-agent/Deep Agents, MCP, middleware around model/agent/tool boundaries, HITL, hybrid sparse+dense retrieval with fusion/reranking, agentic RAG, full backend/frontend/tests, Git-ready history, and an exact video script. They also asked whether web search/You.com was needed; the recorded decision was no general search dependency, with only an allowlisted openFDA lookup plus frozen/committed evidence.

## Iteration record

1. Converted the project handout/course concepts into a business-domain design and implementation plan.
2. Built source/MCP/operations foundations and expanded the digital twin while retaining hand-auditable anchors.
3. Added policy/synthetic corpus generation, hybrid retrieval, then a bounded agentic evidence loop.
4. Built fixed specialists and a real optional Deep Agents graph; repeatedly tightened capability and configuration sealing.
5. Added middleware and durable LangGraph HITL; review identified that approval must not execute, leading to dual consent and one-write-per-version transitions.
6. Hardened cross-store resume against stale/concurrent/copied state with checkpoint-owner, head, and request fencing.
7. Built the evaluator and UI/CLI in isolated worktrees, then reconciled product docs with actual behavior and machine-readable contracts.
8. Rendered canonical diagrams from source, ran double-render parity, and retained only evidence actually executed in [Verification](VERIFICATION.md).

## Review discipline

Feature work used test-first cycles where behavior changed, followed by focused tests, full-suite integration, and adversarial review/fix rounds. Documentation checks validate artifact/data/demo contracts and rendered diagram structure; prose remains subject to human review for clarity and domain accuracy. Generated evaluation timing is treated as a run-specific observation rather than a reproducible performance claim.
