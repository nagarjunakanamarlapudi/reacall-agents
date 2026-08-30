# RecallOps Command Center — Proposal

**Submission:** GenAI Academy, Mastering Agentic AI — Week 3  
**Product:** evidence-first food-recall response decision support

**Reviewer orientation:** read the [food-recall business-domain guide](docs/BUSINESS_DOMAIN.md), [business recall lifecycle](docs/images/08_business_recall_lifecycle.svg), and [domain evidence model](docs/images/09_domain_evidence_model.svg) before the AI design. They separate FDA termination from fictional retailer case closure and assign every risk-bearing decision to a human.

## Executive summary

Food recalls demand fast but accountable action. A coordinator must translate a notice into a precise predicate, locate the affected inventory, account for every unit, organize containment, and retain an explanation for each action. RecallOps demonstrates that workflow without pretending to operate a real retailer.

The system combines **official openFDA H-1230-2026** with a frozen reproducible snapshot and fictional Northstar Grocers records labelled **SYNTHETIC — ACADEMIC DEMO**. Public notice data defines the recall scope; synthetic records only exercise matching, tracing, reconciliation, approvals, and closure. The two sources remain visibly separate in data, UI, documentation, and diagrams.

## Users and retained decisions

| User | What RecallOps provides | Human decision retained |
|---|---|---|
| Recall coordinator | Predicate, evidence, matches, and gaps | Accept or edit scope |
| Food-safety manager | Containment packet and closure evidence | Approve, edit, reject, escalate, or close |
| Distribution/store operations | Facility impact and simulated tasks | Confirm counts and acknowledgements |
| Auditor/evaluator | Trace, provenance, approvals, receipts | Judge whether the evidence supports the decision |

## Flagship outcome

For `H-1230-2026`, the case makes five questions inspectable: what applies, what matched, where units went, what containment is justified, and whether closure is safe. Its non-negotiable accounting control is:

`received = on_hand + quarantined + sold + returned + disposed + unaccounted`

Closure remains blocked for unaccounted units, missing facility acknowledgements, ambiguous lots, or an unapproved proposed write.

## Design choices

| Plane | Design | Why it matters |
|---|---|---|
| Control | Explicit LangGraph `StateGraph` | State, routes, checkpoints, interrupts, retries, and side-effect order are inspectable. |
| Reasoning | Deterministic planner plus optional Deep Agent supervisor | The offline demo is credential-free; live mode can demonstrate bounded delegation. |
| Action | Three narrow FastMCP servers | Agents use typed tools, not raw databases. |
| Assurance | Separate Verification/Critic node | The supervisor does not assess its own output in the same context. |
| Governance | Approval, actor, justification, version, idempotency key | Simulated writes are reviewable and replay-safe. |

## Scope

Included: openFDA lookup/frozen fallback; seeded synthetic digital twin; LangGraph; optional Deep Agents; MCP; middleware; durable human review; simulated writes; CLI/UI; evaluations; notebooks; and reproducible diagrams.

Excluded: real operational connectors, real holds or notifications, production authentication, real customer PII, production regulatory decisions, deployment, 24/7 monitoring, FSIS as a critical dependency, and A2A. These exclusions are product safeguards, not hidden omissions.

## Honest implementation note

This document describes the approved product contract while implementation branches are integrating. Final verification evidence, exact command output, and UI labels belong in the coordinator’s final verification pass; this proposal does not claim those runs have already completed.
