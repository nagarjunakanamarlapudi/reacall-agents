"""Five-view RecallOps Streamlit command center."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import streamlit as st

from recallops.paths import PROJECT_ROOT, RepositoryPaths
from recallops.ui.adapter import DeterministicDemoAdapter, DurableRuntimeAdapter
from recallops.ui.evaluation_reports import load_evaluation_scorecard
from recallops.ui.presenters import (
    DECISIONS,
    EQUATION,
    ESCALATION_JUSTIFICATION,
    VIEWS,
    build_case_header,
    build_closure_gate_rows,
    build_critic_rows,
    build_evaluation_metric_rows,
    build_evaluation_rows,
    build_evidence_rows,
    build_lineage_rows,
    build_match_rows,
    build_orchestration_delta_rows,
    build_predicate_rows,
    build_receipt_rows,
    build_reconciliation_presentation,
    build_retrieval_ablation_rows,
    build_retrieval_delta_rows,
    build_retrieval_rows,
    build_review_packet,
    build_timeline_rows,
    can_simulate,
    initialize_ui_state,
    mask_display_value,
    reduce_case_snapshot,
    source_badge,
    validate_review_submission,
)
from recallops.ui.theme import CSS


def _build_adapter():
    mode = os.environ.get("RECALLOPS_UI_MODE", "durable").strip().casefold()
    if mode == "demo":
        return DeterministicDemoAdapter()
    if mode != "durable":
        raise ValueError("RECALLOPS_UI_MODE must be 'durable' or 'demo'")
    runtime_dir = Path(os.environ.get("RECALLOPS_RUNTIME_DIR", PROJECT_ROOT / ".recallops-runtime"))
    repository_root = Path(os.environ.get("RECALLOPS_REPOSITORY_ROOT", PROJECT_ROOT))
    transport = os.environ.get("RECALLOPS_MCP_TRANSPORT", "direct").strip().casefold()
    return DurableRuntimeAdapter(
        checkpoint_path=runtime_dir / "checkpoints.sqlite3",
        operations_path=runtime_dir / "operations.sqlite3",
        transport=transport,
        repository_paths=RepositoryPaths(repository_root),
    )


_ADAPTER = _build_adapter()


def _await(value: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    return asyncio.run(value)


def _case():
    raw = st.session_state.get("ui_case")
    return reduce_case_snapshot(raw) if isinstance(raw, dict) else None


def _apply_snapshot(raw: dict[str, Any], *, investigation_state: str | None = None) -> None:
    """Atomically refresh the normalized case and every documented derived key."""

    case = reduce_case_snapshot(raw)
    updates = {
        "ui_case": case.raw,
        "ui_case_id": case.case_id,
        "ui_thread_id": case.thread_id,
        "ui_case_version": case.case_version,
        "ui_source_mode": case.source_mode,
        "ui_model_mode": case.model_mode,
        "ui_pending_interrupt": case.raw.get("pending_interrupt"),
        "ui_review_packet": case.raw.get("review_packet"),
        "ui_human_decision": case.raw.get("human_decision"),
        "ui_approval": case.raw.get("approval"),
        "ui_write_receipts": case.raw.get("receipts", []),
        "ui_node_trace": case.raw.get("node_trace", []),
        "ui_tool_trace": case.raw.get("tool_trace", []),
        "ui_reconciliation": case.raw.get("reconciliation"),
        "ui_verification": case.raw.get("verification"),
        "ui_evaluation_report": case.raw.get("evaluation_report"),
        "ui_last_error": None,
    }
    if investigation_state is not None:
        updates["ui_investigation_state"] = investigation_state
    for key, value in updates.items():
        st.session_state[key] = value
    if case.status == "escalated":
        st.session_state.ui_investigation_state = "error"
        st.session_state.ui_last_error = (
            "Investigation escalated safely because required evidence or progress controls failed."
        )


def _safe_action(
    name: str, callback: Awaitable[dict[str, Any]], *, state: str | None = None
) -> None:
    if st.session_state.ui_loading_action:
        return
    st.session_state.ui_loading_action = name
    try:
        _apply_snapshot(_await(callback), investigation_state=state)
    except Exception as exc:  # UI boundary deliberately converts to a safe, short error.
        message = str(exc).splitlines()[0][:240]
        st.session_state.ui_last_error = message or f"{name} failed safely."
        if name == "Run investigation":
            st.session_state.ui_investigation_state = "error"
    finally:
        st.session_state.ui_loading_action = None


def _open_case() -> None:
    recall_number = st.session_state.ui_recall_number.strip()
    _safe_action("Open case", _ADAPTER.open_case(recall_number), state="not_started")


def _run_investigation() -> None:
    case = _case()
    if case is None:
        st.session_state.ui_last_error = "Run investigation after opening a case."
        return
    st.session_state.ui_investigation_state = "running"
    _safe_action("Run investigation", _ADAPTER.run_investigation(case.raw), state="interrupted")


def _resume_review(decision: str) -> None:
    case = _case()
    issues = validate_review_submission(
        decision,
        st.session_state.ui_actor,
        st.session_state.ui_justification,
        case,
        st.session_state.ui_edited_action,
    )
    if issues:
        st.session_state.ui_last_error = " ".join(issue.message for issue in issues)
        return
    st.session_state.ui_decision = decision
    _safe_action(
        decision.title(),
        _ADAPTER.resume_review(
            case.raw,
            decision=decision,
            actor=st.session_state.ui_actor,
            justification=st.session_state.ui_justification,
            edited_action=st.session_state.ui_edited_action,
        ),
    )


def _simulate() -> None:
    case = _case()
    allowed, reason = can_simulate(case)
    if case is None or not allowed:
        st.session_state.ui_last_error = reason
        return
    _safe_action("Simulate approved actions", _ADAPTER.simulate_approved_actions(case.raw))


def _request_closure() -> None:
    case = _case()
    if case is None:
        st.session_state.ui_last_error = "Open a case before requesting closure."
        return
    _safe_action("Request closure", _ADAPTER.request_closure(case.raw))


def _inject_failure() -> None:
    case = _case()
    scenario = st.session_state.ui_failure_scenario
    if case is None or not scenario:
        st.session_state.ui_last_error = "Open a case and select an available failure scenario."
        return
    _safe_action("Run failure fixture", _ADAPTER.inject_failure(case.raw, scenario))


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## RecallOps")
        st.caption("Food Recall Command Center")
        with st.container(border=True):
            st.markdown("**Pinned flagship case**")
            st.write(st.session_state.ui_recall_number)
            st.caption(
                f"Source mode: {st.session_state.ui_source_mode or 'not opened'} · "
                f"Model mode: {st.session_state.ui_model_mode}"
            )
            st.caption(f"Execution mode: {_ADAPTER.runtime_label}")
            st.caption(f"Transport: {_ADAPTER.transport_label}")
            st.caption("Offline-first academic demonstration; no production system writes.")
        st.markdown(
            '<span class="source-official">OFFICIAL — openFDA snapshot</span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<span class="source-synthetic">SYNTHETIC — ACADEMIC DEMO</span>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Northstar Grocers is fictional training data; it is not a party to the public recall."
        )
        st.radio("Workspace", VIEWS, key="ui_active_view", label_visibility="collapsed")
        st.caption("Agents draft; only an approved graph node can make a simulated write.")


def _render_header() -> None:
    case = _case()
    header = build_case_header(case)
    st.subheader(st.session_state.ui_active_view)
    if (
        st.session_state.ui_active_view == "Human Review"
        and case is not None
        and case.raw.get("pending_interrupt", {}).get("kind") in {"action_review", "closure_review"}
    ):
        st.warning("Review required")
    columns = st.columns(5)
    columns[0].metric("Recall", header.recall_number)
    columns[1].metric("Case / thread", f"{header.case_id} / {header.thread_id}")
    columns[2].metric("Version", header.case_version)
    columns[3].metric("Source mode", header.source_mode)
    columns[4].metric("Model mode", header.model_mode)
    st.caption(f"Phase: {header.status} · Current node: {header.current_node}")
    if st.session_state.ui_last_error:
        st.error(st.session_state.ui_last_error)


def _render_command_center() -> None:
    st.caption(
        "Open the official notice first. Northstar operational records are a separate synthetic digital twin."
    )
    left, right = st.columns([4, 1])
    left.text_input("Recall number", key="ui_recall_number")
    right.write("")
    right.write("")
    right.button(
        "Open case",
        key="open_case_button",
        type="primary",
        width="stretch",
        disabled=not st.session_state.ui_recall_number.strip(),
        on_click=_open_case,
    )
    case = _case()
    if case is None:
        st.info(
            "Ready to open the checksummed openFDA notice. No investigation or simulated write has run."
        )
    else:
        with st.container(border=True):
            st.markdown("### Official notice")
            st.markdown(f"**{source_badge(case.source_mode).label}**")
            if case.raw.get("source_detail"):
                st.caption(str(case.raw["source_detail"]))
            summary = case.raw.get("recall", {}).get("summary", {})
            if summary:
                st.table({"Field": list(summary), "Value": list(summary.values())})
            predicate = build_predicate_rows(case)
            if predicate:
                st.table([asdict(row) for row in predicate])
            evidence = build_evidence_rows(case, "")
            if evidence:
                st.table([asdict(row) for row in evidence])
    with st.container(border=True):
        st.markdown("### Synthetic operations boundary")
        st.markdown("**SYNTHETIC — ACADEMIC DEMO**")
        st.write(
            "Northstar Grocers is a fictional digital twin used to demonstrate matching, traceability, reconciliation and simulated operations."
        )
    st.markdown("### Lifecycle map")
    st.markdown(
        '<div class="lifecycle">intake → plan → specialist fan-out → reconcile → verify → human review → execute approved writes → monitor → close or escalate</div>',
        unsafe_allow_html=True,
    )
    st.caption("Explanatory map only; the returned current node is shown in the header.")


def _render_investigation() -> None:
    case = _case()
    st.button(
        "Run investigation",
        key="run_investigation_button",
        type="primary",
        disabled=case is None,
        on_click=_run_investigation,
    )
    if case is None:
        st.info(
            "Open a case first. Investigation is bounded, read-only work and will not write records."
        )
        return
    if case.status == "escalated":
        st.error("Fail-closed investigation outcome")
        warnings = case.raw.get("warnings", [])
        if warnings:
            st.table([{"Returned warning": mask_display_value(value)} for value in warnings])
        retrieval = build_retrieval_rows(case)
        if retrieval:
            st.markdown("### Agentic RAG retrieval trace")
            st.dataframe([asdict(row) for row in retrieval], width="stretch", hide_index=True)
            rag = case.raw.get("retrieval", {})
            st.warning(
                f"RAG stopped safely: {mask_display_value(rag.get('stop_reason'))}. "
                "No unsupported evidence was promoted to an operational fact."
            )
        return
    if not case.raw.get("matches"):
        st.info(
            "Run investigation to plan bounded matching, traceability and containment work. No records will be written."
        )
        return

    st.markdown("### Plan and specialist fan-out")
    st.caption(
        "Deterministic offline planner · bounded four-specialist plan · independent verifier"
    )
    st.dataframe(case.raw.get("specialists", []), width="stretch", hide_index=True)

    st.markdown("### Agentic RAG retrieval trace")
    st.write(
        "BM25 sparse + LSA dense → reciprocal-rank fusion (RRF) → deterministic rerank → evidence critic"
    )
    st.caption(
        "Read-only advisory context; structured predicate, trace and closure controls remain authoritative."
    )
    retrieval = build_retrieval_rows(case)
    st.dataframe([asdict(row) for row in retrieval], width="stretch", hide_index=True)
    citations = case.raw.get("retrieval", {}).get("citations", [])
    bounds = case.raw.get("retrieval", {}).get("bounds", {})
    st.caption(f"Citations: {', '.join(citations)} · Bounds: {bounds}")
    retrieval_state = case.raw.get("retrieval", {})
    if retrieval_state.get("coverage_satisfied") is False:
        st.warning(
            f"Evidence critic did not establish full coverage; stop reason: "
            f"{mask_display_value(retrieval_state.get('stop_reason'))}. Structured controls remain authoritative."
        )
        gaps = retrieval_state.get("evidence_gaps", [])
        if gaps:
            st.table([{"Agentic RAG evidence gap": mask_display_value(value)} for value in gaps])

    st.markdown("### Product and lot matches")
    matches = build_match_rows(case)
    if matches:
        st.dataframe([asdict(row) for row in matches], width="stretch", hide_index=True)
    else:
        st.info("No candidate products or lots were returned.")

    st.markdown("### Synthetic digital-twin lineage and evidence")
    lots = [row.lot_id for row in matches]
    selected = st.selectbox("Selected lot", lots, key="ui_selected_lot") if lots else None
    lineage = build_lineage_rows(case, selected)
    if lineage:
        st.dataframe([asdict(row) for row in lineage], width="stretch", hide_index=True)
    evidence = build_evidence_rows(case, selected or "")
    if evidence:
        st.dataframe([asdict(row) for row in evidence], width="stretch", hide_index=True)
    st.caption(
        "This is a synthetic digital-twin trace; it is not proof Northstar was involved in the public notice."
    )

    st.markdown("### Affected facilities and proposed containment")
    packet = build_review_packet(case)
    st.dataframe(packet.facilities if packet else [], width="stretch", hide_index=True)
    st.warning("Draft only — approval required before simulated action.")
    st.dataframe(packet.proposed_actions if packet else [], width="stretch", hide_index=True)
    with st.expander("Node and tool trace"):
        st.dataframe(
            [asdict(row) for row in build_timeline_rows(case)], width="stretch", hide_index=True
        )
        st.caption(
            "Structured execution summary; no hidden reasoning or chain-of-thought is shown."
        )


def _render_reconciliation() -> None:
    case = _case()
    st.markdown(f"### `{EQUATION}`")
    if case is None or not case.raw.get("reconciliation"):
        st.info("No reconciliation has been returned. Run investigation after opening a case.")
        return
    presentation = build_reconciliation_presentation(case)
    st.table(
        [{"Component": key, "Returned value": value} for key, value in presentation.totals.items()]
    )
    equality = (
        "Balanced"
        if presentation.balanced is True
        else "Not balanced"
        if presentation.balanced is False
        else "Unknown — required value missing"
    )
    st.caption(f"Equality check: {equality}")
    st.dataframe(presentation.rows, width="stretch", hide_index=True)
    st.markdown("### Evidence gaps and closure implications")
    if presentation.gaps:
        st.dataframe([asdict(row) for row in presentation.gaps], width="stretch", hide_index=True)
    else:
        st.info("No evidence gaps were returned for this scope.")
    st.warning(
        "Any unaccounted quantity, ambiguous lot, unacknowledged facility, unapproved write, contradiction or missing evidence remains a closure blocker."
    )


def _render_human_review() -> None:
    case = _case()
    packet = build_review_packet(case) if case else None
    if packet is None:
        st.info("No review packet is pending for this case.")
    else:
        st.write(
            f"Scope: **{packet.scope}** · case version **{packet.case_version}**. Approval is scoped to this action/version, not blanket permission."
        )
        st.markdown("### Review packet")
        st.table([asdict(row) for row in packet.predicate])
        st.dataframe([asdict(row) for row in packet.matches], width="stretch", hide_index=True)
        st.dataframe(packet.facilities, width="stretch", hide_index=True)
        st.code(packet.reconciliation.equation, language=None)
        st.table(
            [
                {"Component": key, "Returned value": value}
                for key, value in packet.reconciliation.totals.items()
            ]
        )
        if packet.reconciliation.gaps:
            st.dataframe(
                [asdict(row) for row in packet.reconciliation.gaps],
                width="stretch",
                hide_index=True,
            )
        st.dataframe(packet.proposed_actions, width="stretch", hide_index=True)
        if packet.remaining_actions:
            st.markdown("### Remaining versioned action lifecycle")
            st.table(
                [
                    {"Order": index, "Action": action}
                    for index, action in enumerate(packet.remaining_actions, start=1)
                ]
            )
        st.dataframe([asdict(row) for row in packet.citations], width="stretch", hide_index=True)
        with st.expander("Review trace summary"):
            st.dataframe([asdict(row) for row in packet.timeline], width="stretch", hide_index=True)

    st.markdown("### Human decision")
    st.radio("Decision", DECISIONS, key="ui_decision", horizontal=True)
    st.text_input("Actor", key="ui_actor")
    st.text_area("Justification", key="ui_justification")
    if st.session_state.ui_decision == "edit":
        st.text_area("Edited proposed action", key="ui_edited_action")
    st.caption(f"Escalation alternative: {ESCALATION_JUSTIFICATION}")
    columns = st.columns(4)
    columns[0].button(
        "Approve",
        key="approve_button",
        disabled=packet is None,
        on_click=_resume_review,
        args=("approve",),
    )
    columns[1].button(
        "Edit", key="edit_button", disabled=packet is None, on_click=_resume_review, args=("edit",)
    )
    columns[2].button(
        "Reject",
        key="reject_button",
        disabled=packet is None,
        on_click=_resume_review,
        args=("reject",),
    )
    columns[3].button(
        "Escalate",
        key="escalate_button",
        disabled=packet is None,
        on_click=_resume_review,
        args=("escalate",),
    )

    st.markdown("### Approved action")
    allowed, reason = can_simulate(case)
    recovery_pending = bool(
        case and case.raw.get("pending_interrupt", {}).get("kind") == "write_outcome_recovery"
    )
    st.write("Simulated operation only")
    st.caption(reason)
    st.button(
        "Recover recorded outcome (same key)" if recovery_pending else "Simulate approved actions",
        key="simulate_button",
        type="primary",
        disabled=not allowed,
        on_click=_simulate,
    )
    if case:
        receipts = build_receipt_rows(case)
        if receipts:
            st.success("Simulated action recorded")
            st.dataframe([asdict(row) for row in receipts], width="stretch", hide_index=True)


def _render_evaluation() -> None:
    paths = RepositoryPaths(Path(os.environ.get("RECALLOPS_REPOSITORY_ROOT", PROJECT_ROOT)))
    projection = load_evaluation_scorecard(paths)
    st.markdown("### Evaluation scorecard")
    if projection.verification_status != "verified":
        st.warning(
            "Unavailable — evaluation artifacts are missing, stale, invalid, or incomplete. No passing score is claimed."
        )
        for section in ("Safety", "Retrieval quality", "Orchestration quality"):
            st.markdown(f"#### {section}")
            st.write("Unavailable")
        st.caption(
            "Verification: unavailable · Artifact time, mode, counts and digests: unavailable · Optional Deep Agents: unavailable"
        )
        return
    if projection.offline_gate_passed:
        st.success("Verified offline scorecard · PASS")
    else:
        st.error("Verified offline scorecard · FAIL")
    st.caption(
        f"Verification: verified · Scorecard generated: {projection.generated_at} · Mode: {projection.execution_mode}"
    )
    st.caption(
        "Integrity and recorded contract checks; execution authenticity is not established by digests. Read-only artifacts; this view does not run benchmarks."
    )
    st.markdown("#### Safety")
    safety = projection.safety
    st.caption(
        f"{safety.scenario_count} safety scenarios · {safety.result_count} results · R01–R21 · Gate: {'PASS' if safety.gate_passed else 'FAIL'}"
    )
    counters = {name: value for name, value in safety.metrics.items() if name.endswith("_count")}
    rates = {name: value for name, value in safety.metrics.items() if not name.endswith("_count")}
    summary = st.columns(4)
    summary[0].metric("Scenario pass rate", f"{rates['scenario_pass_rate']:.1%}")
    summary[1].metric("Safety-critical pass rate", f"{rates['safety_critical_pass_rate']:.1%}")
    summary[2].metric("Scenarios", safety.scenario_count)
    summary[3].metric("Unsafe counters", sum(counters.values()))
    report = {
        "status": "verified",
        "scenarios": safety.scenarios,
        "metrics": rates,
        "unsafe_counters": counters,
    }
    # Existing scenario presenter accepts JSON lists at its public boundary.
    report["scenarios"] = list(safety.scenarios)
    st.dataframe(
        [asdict(row) for row in build_evaluation_rows(report)], width="stretch", hide_index=True
    )
    st.dataframe(
        [asdict(row) for row in build_evaluation_metric_rows(report)],
        width="stretch",
        hide_index=True,
    )
    st.markdown("#### Retrieval quality")
    retrieval = projection.retrieval
    st.caption(
        f"{retrieval.case_count} cases · {retrieval.result_count} results · Six configurations · Gate: {'PASS' if retrieval.gate_passed else 'FAIL'}"
    )
    st.caption(
        "In-sample offline synthetic calibration. Values are recorded measurements; family selection changes the ablation and critic tables. Deltas below cover all cases."
    )
    family = st.selectbox(
        "Case family", ("All families", *retrieval.families), key="ui_evaluation_family"
    )
    selected = None if family == "All families" else family
    rows = build_retrieval_ablation_rows(projection, selected)
    st.dataframe(rows, width="stretch", hide_index=True)
    st.bar_chart(rows, x="Configuration", y=["Recall@5", "nDCG@5"])
    st.dataframe(build_retrieval_delta_rows(projection), width="stretch", hide_index=True)
    st.caption(
        "Critic stop counts; rewrite wins/losses/no-change, citation precision, grounding, routing, latency and budget compliance appear in the ablation table."
    )
    st.dataframe(build_critic_rows(projection, selected), width="stretch", hide_index=True)
    st.markdown("#### Orchestration quality")
    orchestration = projection.orchestration
    st.caption(
        f"{orchestration.case_count} cases · {orchestration.result_count} results · Two offline profiles · Gate: {'PASS' if orchestration.gate_passed else 'FAIL'}"
    )
    st.dataframe(list(orchestration.profiles), width="stretch", hide_index=True)
    st.caption(
        "Measured deltas: fixed specialists minus bounded single agent. Negative differences are retained. Duration includes sequential service setup; offline model tokens and cost are unavailable."
    )
    st.dataframe(build_orchestration_delta_rows(projection), width="stretch", hide_index=True)
    st.dataframe([orchestration.gates], width="stretch", hide_index=True)
    st.markdown("##### Optional Deep Agents live profile")
    st.caption(
        f"Status: {projection.optional_live_status} · Excluded from offline gates · Model judges: not used"
    )
    st.markdown("##### Artifact digests")
    st.caption(
        "Report/corpus SHA-256 values bind exact file bytes. The scorecard SHA-256 is its canonical self-digest with the self-digest field excluded. Source reports do not record generation timestamps."
    )
    st.dataframe(
        [
            {"Artifact": name, "SHA-256": digest}
            for name, digest in projection.artifact_digests.items()
        ],
        width="stretch",
        hide_index=True,
    )


def _render_audit() -> None:
    case = _case()
    st.markdown("### Audit timeline")
    st.caption(
        "Agents draft; the approved graph node calls Operations MCP. No A2A and no direct agent writes."
    )
    if case:
        timeline = build_timeline_rows(case)
        if timeline:
            st.dataframe([asdict(row) for row in timeline], width="stretch", hide_index=True)
        else:
            st.info("No audit timeline has been returned.")
        checkpoint_history = case.raw.get("checkpoint_history", [])
        if checkpoint_history:
            st.markdown("### Durable checkpoint history")
            st.caption(
                "Detached, read-only checkpoint summaries; no graph runner or mutable runtime state is exposed."
            )
            st.dataframe(checkpoint_history, width="stretch", hide_index=True)
        st.markdown("### Receipts and review history")
        history = case.raw.get("review_history", [])
        if history:
            st.dataframe(history, width="stretch", hide_index=True)
        receipts = build_receipt_rows(case)
        if receipts:
            st.dataframe([asdict(row) for row in receipts], width="stretch", hide_index=True)
        elif not history:
            st.info("No decision or simulated-operation receipt has been returned.")

        st.markdown("### Deterministic failure injection")
        st.selectbox(
            "Failure scenario",
            (None, *_ADAPTER.available_failure_scenarios),
            format_func=lambda value: "Select an available scenario" if value is None else value,
            key="ui_failure_scenario",
        )
        st.button("Run failure fixture", key="inject_failure_button", on_click=_inject_failure)
        failure = case.raw.get("failure_result")
        if failure:
            st.info(f"{failure['mode']}: {failure['safe_outcome']}")
            if failure.get("next_step"):
                st.caption(f"Next step: {failure['next_step']}")
    else:
        st.info("No case audit data is available until a case is opened.")

    _render_evaluation()

    st.markdown("### Closure gate")
    st.button(
        "Request closure",
        key="request_closure_button",
        type="primary",
        disabled=case is None,
        on_click=_request_closure,
    )
    case = _case()
    if case and case.raw.get("closure"):
        closure = case.raw["closure"]
        if closure.get("status") == "Open — closure blocked":
            st.warning("Open — closure blocked")
        elif closure.get("status") == "Closed — simulated":
            st.success("Closed — simulated")
        else:
            st.info(
                "All returned gates passed; final human closure review is required. The case is not closed."
            )
        st.dataframe(
            [asdict(row) for row in build_closure_gate_rows(case)], width="stretch", hide_index=True
        )


def main() -> None:
    st.set_page_config(page_title="RecallOps Command Center", page_icon="🥚", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    initialize_ui_state(st.session_state)
    _render_sidebar()
    st.title("RecallOps Command Center")
    _render_header()
    renderers = {
        "Command Center": _render_command_center,
        "Investigation": _render_investigation,
        "Reconciliation": _render_reconciliation,
        "Human Review": _render_human_review,
        "Audit & Evaluation": _render_audit,
    }
    renderers[st.session_state.ui_active_view]()
    st.divider()
    st.caption(
        "Academic simulation only · Offline by default · No customer PII · No production writes"
    )


if __name__ == "__main__":
    main()
