# Human Presentation Evaluation Rubric

This rubric evaluates how a person presents or reviews an evidence-first RecallOps response. It is a communication and review aid, not an authorization mechanism. Score each dimension from 1 to 5 using the anchored examples below, then record the evidence reviewed and any disagreement.

## Authority boundary

The deterministic safety suite is authoritative for approval, simulated writes, receipt integrity, reconciliation, and closure gates. Those fields are deterministic-only: a human presentation score and a model judge must not approve an action, must not close a case, and must not override a failed safety invariant. A judge may flag a response for human review, but it cannot convert a blocked action or closure into an allowed one.

Model judges are optional and advisory. They may help prioritize review of wording, citations, or omissions; they do not establish factual correctness, measure production quality, replace source inspection, or contribute to the offline safety verdict. Keep their prompt, model/version, input, output, and any calibration limits visible when they are used.

## Scoring anchors

### Correctness and citation alignment

- **Score 1:** States that a lot is recalled with no citation, or cites an unrelated facility record as proof.
- **Score 2:** Names the correct recall but mixes an official notice with an unlabelled synthetic claim.
- **Score 3:** Gives the central fact with a citation, but leaves one material claim unsupported or imprecisely attributed.
- **Score 4:** Correctly distinguishes the official predicate from synthetic trace evidence and cites each material claim.
- **Score 5:** Every material claim is accurate, source-labelled, traceable to a cited record, and the response explicitly rejects unsupported inference.

### Completeness

- **Score 1:** Answers only the recall number and ignores scope, facilities, quantities, or open gaps.
- **Score 2:** Lists affected lots but omits a material destination or the reconciliation status.
- **Score 3:** Covers scope and traceability but misses one required check, such as acknowledgement or unresolved quantity.
- **Score 4:** Covers the requested scope, evidence, destinations, reconciliation, and known gaps.
- **Score 5:** Covers every requested element, names the applicable gate checks, and separates completed evidence from follow-up work.

### Uncertainty and abstention

- **Score 1:** Invents an answer or asserts closure despite missing evidence.
- **Score 2:** Hedges vaguely while still presenting an unsupported conclusion as likely true.
- **Score 3:** Notes uncertainty but does not say what evidence would resolve it.
- **Score 4:** Clearly abstains from unsupported claims and identifies the missing evidence or next check.
- **Score 5:** Preserves exact/probable/ambiguous distinctions, refuses unsupported requests, explains the blocking condition, and identifies a bounded path to resolution.

### Actionability

- **Score 1:** Gives generic advice such as “investigate further.”
- **Score 2:** Suggests an action without an owner, scope, or evidence basis.
- **Score 3:** Provides a plausible next action but omits an approval or version boundary.
- **Score 4:** Gives a scoped, evidence-backed next action with owner, facility or lot, and approval requirement.
- **Score 5:** Gives ordered, bounded follow-up actions tied to cited evidence, explicitly preserves HITL and idempotency/version controls, and states what remains blocked.

### Clarity

- **Score 1:** Is contradictory, jargon-heavy, or impossible to follow.
- **Score 2:** Contains the right facts but buries scope and gaps in an unstructured narrative.
- **Score 3:** Is understandable but conflates findings, recommendations, and safety status.
- **Score 4:** Uses clear sections or bullets to separate evidence, uncertainty, and next actions.
- **Score 5:** Is concise, audience-appropriate, easy to audit, and makes the distinction among facts, uncertainty, advice, and deterministic gate status unmistakable.

## Recommended review record

Record the response identifier, cited evidence IDs, five dimension scores, reviewer rationale, any model-judge output (if used), and the deterministic safety result separately. A high presentation score never changes a deterministic safety result.
