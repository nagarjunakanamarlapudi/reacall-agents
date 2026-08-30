# Five-Minute Demo Walkthrough

![Demo story](images/07_demo_story.svg)

This is a presenter script for the approved design. **Final integration confirmation is required** for the exact CLI flags, Streamlit widget labels, and rendered receipt text because implementation is integrating in parallel. The recall number, review vocabulary, provenance statements, and safety narrative are fixed.

## Preflight

Copy/paste after final integration confirmation:

```bash
uv run recallops data-validate
uv run recallops demo --recall-number H-1230-2026
```

Expected audience-facing source boundary: **official openFDA H-1230-2026** is the authoritative public notice; all Northstar operational records are **SYNTHETIC — ACADEMIC DEMO**. Say this before showing any matches.

## Narration and inputs

1. **Open case — 35 seconds.**

   Narrate: “I am opening the pinned official openFDA recall H-1230-2026. The notice defines the recall predicate. Northstar Grocers is fictional training data, not a party to this public recall.”

   Copy/paste input: `H-1230-2026`

2. **Investigate — 50 seconds.**

   Narrate: “The graph has planned bounded work for intake, matching, traceability, containment, and independent verification. The specialists return evidence; the graph owns state and routing. There is no A2A and agents do not directly write records.”

   UI detail requiring final integration confirmation: open **Investigation** and show the recall predicate, exact/probable/ambiguous/rejected match rationale, source badges, and tool trace.

3. **Reconcile — 45 seconds.**

   Narrate: “RecallOps shows the quantity equation rather than hiding missing units: received equals on-hand plus quarantined plus sold plus returned plus disposed plus unaccounted. Any unaccounted unit remains a closure blocker.”

   UI detail requiring final integration confirmation: open **Reconciliation** and select the affected lot/facility view.

4. **Human review — 75 seconds.**

   Narrate: “This ambiguous lot pauses at a durable LangGraph interrupt. I am the Food-safety manager; I can approve, edit, reject, or escalate. Approval is for this scoped action and case version, not an unrestricted capability.”

   Copy/paste review values:

   ```text
   decision: approve
   actor: Food-safety manager
   justification: Authorize simulated containment for confirmed scope; retain ambiguous lot for review.
   ```

   UI detail requiring final integration confirmation: open **Human Review**, submit the values, and show that the action receipt contains the approval linkage and idempotency information.

5. **Monitor and closure — 55 seconds.**

   Narrate: “Only after the approved resume can the simulated operations tool execute. The case remains open if a facility has not acknowledged, a match is ambiguous, or quantities are unresolved. Closure is a separate human gate.”

   Copy/paste review values for a safe blocking demonstration:

   ```text
   decision: escalate
   actor: Food-safety manager
   justification: Do not close while acknowledgement, ambiguity, or reconciliation gaps remain.
   ```

   UI detail requiring final integration confirmation: open **Audit & Evaluation** and show the node/tool timeline, source mode, decision, and receipt; then show the blocked closure reason.

## Evaluator questions

| Question | Answer |
|---|---|
| Is Northstar tied to the real recall? | No. It is a `SYNTHETIC — ACADEMIC DEMO` digital twin. |
| Can an agent make an inventory hold? | No. It can draft; only an approved graph node calls the simulated operations MCP tool. |
| Why not use A2A? | The requirement is explicit stateful orchestration; LangGraph is sufficient and more inspectable here. |
| What happens if openFDA is unavailable? | The app uses a labelled frozen snapshot for the demo. |
| Can the case close with a discrepancy? | No; reconciliation and acknowledgement gates block closure. |
