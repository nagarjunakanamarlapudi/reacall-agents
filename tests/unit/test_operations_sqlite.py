import hashlib
import json
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest

from recallops.models import (
    ApprovalBinding,
    ApprovalDecision,
    ProposedAction,
    Reconciliation,
    proposed_action_digest,
)
from recallops.services.operations import (
    INITIAL_CHECKPOINT_HEAD,
    ApprovalRequiredError,
    ClosureBlockedError,
    IdempotencyConflictError,
    OperationStoreError,
    StaleCaseVersionError,
    _workflow_authorization_broker,
)
from recallops.services.operations import OperationsService as RawOperationsService
from recallops.services.traceability import TraceabilityService

TRACEABILITY = TraceabilityService()
SUCCESS_LOT = "LOT-PROBABLE-160"
GAPPED_LOT = "LOT-EXACT-170"


def trusted_execute(
    service: RawOperationsService,
    method_name: str,
    /,
    _invoke=None,
    **kwargs: Any,
):
    """Exercise the real service through the same active workflow-fence/grant contract."""
    operation = _invoke or getattr(RawOperationsService, method_name).__get__(
        service, RawOperationsService
    )
    case_id = kwargs["case_id"]
    expected = kwargs["expected_case_version"]
    action = kwargs["proposed_action"]
    approval = kwargs["approval"]
    key = kwargs["idempotency_key"]
    if service.get_receipt(key) is not None:
        return operation(**kwargs)
    state = service.get_case(case_id)
    thread_id = kwargs.get("thread_id") or (state.thread_id if state else case_id)
    with sqlite3.connect(service.storage_path) as connection:
        owner_row = connection.execute(
            "SELECT owner_token FROM workflow_identities WHERE case_id=?", (case_id,)
        ).fetchone()
    owner = owner_row[0] if owner_row and owner_row[0] else str(uuid4())
    broker = _workflow_authorization_broker(service, owner)
    if service.get_thread_for_case(case_id) is None:
        broker.reserve_workflow_identity(case_id, thread_id)
    with sqlite3.connect(service.storage_path) as connection:
        row = connection.execute(
            "SELECT checkpoint_head FROM workflow_identities WHERE case_id=?", (case_id,)
        ).fetchone()
    head = row[0]
    request_digest = hashlib.sha256(
        f"{case_id}:{thread_id}:{head}:{method_name}:{expected}:{key}".encode()
    ).hexdigest()
    attempt = str(uuid4())
    execution_id = f"EXECUTION:{method_name}:{expected}:{key}"
    execution_request_digest = hashlib.sha256(
        f"execution:{case_id}:{method_name}:{expected}:{key}".encode()
    ).hexdigest()
    broker.claim_workflow_mutation(
        case_id,
        thread_id,
        head,
        attempt,
        request_digest,
        execution_id=execution_id,
        execution_request_digest=execution_request_digest,
    )
    detail_fields = {
        "create_case": (
            "recall_number",
            "question",
            "thread_id",
            "confirmed_lot_ids",
            "trace_event_ids",
            "required_facilities",
            "reconciliation",
            "evidence_gaps",
        ),
        "apply_inventory_hold": ("lot_ids",),
        "create_facility_tasks": ("facility_ids",),
        "record_acknowledgment": ("facility_id",),
        "record_disposition": ("lot_id", "disposition", "evidence_id"),
        "close_case": (),
    }[method_name]
    details: dict[str, Any] = {}
    for name in detail_fields:
        if name == "question":
            details[name] = kwargs.get(name, "")
        elif name == "thread_id":
            details[name] = thread_id
        elif name == "reconciliation":
            details[name] = [
                Reconciliation.model_validate(item).model_dump(mode="json") for item in kwargs[name]
            ]
        else:
            details[name] = kwargs[name]
    try:
        grant = broker.issue_workflow_execution_grant(
            case_id=case_id,
            thread_id=thread_id,
            proposed_action=action,
            approval=approval,
            expected_case_version=expected,
            idempotency_key=key,
            execution_id=execution_id,
            execution_request_digest=execution_request_digest,
            details=details,
            target_ids=list(action.target_ids),
            evidence_ids=(
                list(kwargs["trace_event_ids"])
                if method_name == "create_case"
                else [kwargs["evidence_id"]]
                if method_name == "record_disposition"
                else None
            ),
        )
        receipt = operation(**kwargs, execution_grant=grant)
    except BaseException:
        broker.release_workflow_mutation(case_id, thread_id, head, attempt, request_digest)
        raise
    new_head = f"CHECKPOINT:{case_id}:{receipt.case_version}:{receipt.receipt_id}"
    broker.advance_workflow_mutation(case_id, thread_id, head, new_head, attempt, request_digest)
    return receipt


class OperationsService(RawOperationsService):
    """Test facade that drives writes through the real workflow-grant boundary."""

    def _workflow_write(self, method_name: str, kwargs: dict[str, Any]):
        return trusted_execute(
            self,
            method_name,
            _invoke=getattr(super(), method_name),
            **kwargs,
        )

    def create_case(self, **kwargs: Any):
        return self._workflow_write("create_case", kwargs)

    def apply_inventory_hold(self, **kwargs: Any):
        return self._workflow_write("apply_inventory_hold", kwargs)

    def create_facility_tasks(self, **kwargs: Any):
        return self._workflow_write("create_facility_tasks", kwargs)

    def record_acknowledgment(self, **kwargs: Any):
        return self._workflow_write("record_acknowledgment", kwargs)

    def record_disposition(self, **kwargs: Any):
        return self._workflow_write("record_disposition", kwargs)

    def close_case(self, **kwargs: Any):
        return self._workflow_write("close_case", kwargs)

    def _request_hash(self, *args: Any, **kwargs: Any) -> str:
        store = object.__getattribute__(self, "_OperationsService__store")
        return store._request_hash(*args, **kwargs)


def reviewed(
    case_id: str,
    action_type: str,
    version: int,
    target_ids: list[str],
    *,
    evidence_ids: list[str] | None = None,
    actor: str = "reviewer",
) -> dict[str, Any]:
    if evidence_ids is not None:
        evidence_by_target = {target_id: evidence_ids for target_id in target_ids}
    else:
        evidence_by_target: dict[str, list[str]] = {}
        for target_id in target_ids:
            if target_id.startswith("LOT-"):
                identifiers = sorted(
                    {event["event_id"] for event in TRACEABILITY.trace_forward(target_id)}
                    | {
                        position["position_id"]
                        for position in TRACEABILITY.get_inventory(target_id)
                    }
                )
            else:
                identifiers = [
                    event["event_id"]
                    for event in TRACEABILITY.dataset["events"]
                    if target_id in {event.get("from_facility"), event.get("to_facility")}
                ][:1]
            evidence_by_target[target_id] = identifiers or [f"EVIDENCE-{target_id}"]
    action_evidence = list(
        dict.fromkeys(
            identifier for target_id in target_ids for identifier in evidence_by_target[target_id]
        )
    )
    action = ProposedAction(
        action_id=f"{case_id}-{action_type}-{version}",
        action_type=action_type,
        case_id=case_id,
        target_ids=target_ids,
        rationale=f"Reviewed {action_type} against authoritative evidence.",
        evidence_ids=action_evidence,
        evidence_by_target=evidence_by_target,
        expected_case_version=version,
    )
    approval = ApprovalDecision(
        decision="approve",
        actor=actor,
        justification="evidence",
        approved_at=datetime(2026, 8, 30, 12, version, tzinfo=UTC),
        approved_case_version=version,
        approved_case_id=case_id,
        action_ids=[action.action_id],
        action_bindings=[
            ApprovalBinding(
                action_id=action.action_id,
                action_digest=proposed_action_digest(action),
            )
        ],
    )
    return {"proposed_action": action, "approval": approval}


def reconciliation(*, lot_id: str = SUCCESS_LOT, verified: bool = True) -> Reconciliation:
    value = TRACEABILITY.reconcile_units(lot_id)
    return value.model_copy(update={"verified": verified})


def case_input(
    *,
    lot_id: str = SUCCESS_LOT,
    verified: bool = True,
    evidence_gaps: list[str] | None = None,
) -> dict[str, Any]:
    events = TRACEABILITY.trace_forward(lot_id)
    reconciled = reconciliation(lot_id=lot_id, verified=verified)
    authoritative_gaps = (
        [f"{lot_id}: {reconciled.unaccounted} unaccounted units"] if reconciled.unaccounted else []
    )
    return {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": [lot_id],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": sorted(
            {
                facility
                for event in events
                for facility in (event.get("from_facility"), event.get("to_facility"))
                if facility
            }
        ),
        "reconciliation": [reconciled],
        "evidence_gaps": authoritative_gaps if evidence_gaps is None else evidence_gaps,
    }


def create(service: OperationsService, case_id: str, *, lot_id: str = SUCCESS_LOT) -> None:
    payload = case_input(lot_id=lot_id)
    trusted_execute(
        service,
        "create_case",
        case_id=case_id,
        **payload,
        **reviewed(
            case_id,
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key=f"{case_id}-create",
    )


def test_direct_caller_authored_approval_cannot_create_a_case_without_workflow_grant(
    tmp_path: Path,
) -> None:
    """Break caught: a self-consistent envelope bypasses the durable HITL checkpoint."""
    service = RawOperationsService(storage_path=tmp_path / "no-workflow-grant.sqlite3")
    case_id = "CASE-NO-WORKFLOW-GRANT"
    payload = case_input()

    with pytest.raises(ApprovalRequiredError, match="execution grant"):
        service.create_case(
            case_id=case_id,
            **payload,
            **reviewed(
                case_id,
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key=f"{case_id}-create",
        )

    assert service.get_case(case_id) is None


def test_normal_operations_service_exposes_no_workflow_authority_capability(
    tmp_path: Path,
) -> None:
    """Break caught: a normal mutation consumer can reserve, claim, or mint authority."""
    service = RawOperationsService(storage_path=tmp_path / "public-service.sqlite3")

    for surface in (
        "reserve_workflow_identity",
        "validate_or_claim_workflow_identity",
        "claim_workflow_mutation",
        "advance_workflow_mutation",
        "release_workflow_mutation",
        "recover_workflow_mutation",
        "release_workflow_identity",
        "issue_workflow_execution_grant",
    ):
        with pytest.raises(AttributeError):
            getattr(service, surface)


def test_broker_authenticates_execution_identity_against_the_active_attempt(
    tmp_path: Path,
) -> None:
    """Break caught: grant issuance accepts execution metadata absent from the active resume."""
    service = RawOperationsService(storage_path=tmp_path / "execution-binding.sqlite3")
    broker = _workflow_authorization_broker(service, str(uuid4()))
    case_id = "CASE-EXECUTION-BINDING"
    thread_id = "THREAD-EXECUTION-BINDING"
    payload = case_input()
    review = reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    request_digest = hashlib.sha256(b"trusted-runtime-resume").hexdigest()
    execution_digest = hashlib.sha256(b"trusted-execution-request").hexdigest()
    broker.reserve_workflow_identity(case_id, thread_id)
    broker.claim_workflow_mutation(
        case_id,
        thread_id,
        INITIAL_CHECKPOINT_HEAD,
        str(uuid4()),
        request_digest,
        execution_id="EXECUTION-TRUSTED",
        execution_request_digest=execution_digest,
    )

    with pytest.raises(ApprovalRequiredError, match="active trusted workflow"):
        broker.issue_workflow_execution_grant(
            case_id=case_id,
            thread_id=thread_id,
            **review,
            expected_case_version=0,
            idempotency_key="execution-binding-create",
            execution_id="EXECUTION-SUBSTITUTED",
            execution_request_digest=hashlib.sha256(b"substituted-request").hexdigest(),
            details={
                **payload,
                "question": "Authenticate the exact execution request.",
                "thread_id": thread_id,
                "reconciliation": [
                    item.model_dump(mode="json") for item in payload["reconciliation"]
                ],
            },
            target_ids=payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        )


def test_completed_replay_is_idempotent_but_grant_cannot_cross_action_or_version(
    tmp_path: Path,
) -> None:
    """Break caught: one consumed capability authorizes another action or case version."""
    database = tmp_path / "grant-replay.sqlite3"
    workflow_service = OperationsService(storage_path=database)
    case_id = "CASE-GRANT-REPLAY"
    payload = case_input()
    creation = reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    created = workflow_service.create_case(
        case_id=case_id,
        **payload,
        **creation,
        expected_case_version=0,
        idempotency_key="grant-create",
    )
    raw = RawOperationsService(storage_path=database)
    replay = raw.create_case(
        case_id=case_id,
        **payload,
        **creation,
        expected_case_version=0,
        idempotency_key="grant-create",
    )
    assert replay == created

    with sqlite3.connect(database) as connection:
        create_token = connection.execute(
            "SELECT grant_token FROM execution_grants WHERE action_type='create_case'"
        ).fetchone()[0]
    hold = reviewed(case_id, "apply_inventory_hold", 1, [SUCCESS_LOT])
    with pytest.raises(ApprovalRequiredError, match="execution grant"):
        raw.apply_inventory_hold(
            case_id=case_id,
            lot_ids=[SUCCESS_LOT],
            **hold,
            expected_case_version=1,
            idempotency_key="cross-action",
            execution_grant=create_token,
        )

    workflow_service.apply_inventory_hold(
        case_id=case_id,
        lot_ids=[SUCCESS_LOT],
        **hold,
        expected_case_version=1,
        idempotency_key="valid-hold",
    )
    with sqlite3.connect(database) as connection:
        hold_token = connection.execute(
            "SELECT grant_token FROM execution_grants WHERE action_type='apply_inventory_hold'"
        ).fetchone()[0]
    changed_version = reviewed(case_id, "apply_inventory_hold", 2, [SUCCESS_LOT])
    with pytest.raises(ApprovalRequiredError, match="execution grant"):
        raw.apply_inventory_hold(
            case_id=case_id,
            lot_ids=[SUCCESS_LOT],
            **changed_version,
            expected_case_version=2,
            idempotency_key="cross-version",
            execution_grant=hold_token,
        )
    assert raw.get_case(case_id).case_version == 2


@pytest.mark.parametrize(
    ("recall_number", "lot_id"),
    [
        ("NOT-A-REAL-RECALL", SUCCESS_LOT),
        ("H-1230-2026", "LOT-AMBIG-175"),
        ("H-1230-2026", "LOT-REJECT-190"),
    ],
)
def test_case_creation_recomputes_recall_scope_and_rejects_untrusted_lots(
    tmp_path: Path,
    recall_number: str,
    lot_id: str,
) -> None:
    """Break caught: caller labels can create a case outside the official predicate."""
    service = OperationsService(storage_path=tmp_path / f"{lot_id}.sqlite3")
    case_id = f"CASE-AUTHORITY-{lot_id}"
    payload = case_input(lot_id=lot_id)
    payload["recall_number"] = recall_number
    with pytest.raises(ValueError, match="recall|predicate|eligible|classification"):
        trusted_execute(
            service,
            "create_case",
            case_id=case_id,
            **payload,
            **reviewed(
                case_id,
                "create_case",
                0,
                [lot_id],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key=f"{case_id}-create",
        )
    assert service.get_case(case_id) is None


def test_hold_rejects_lots_and_evidence_outside_the_persisted_case_scope(
    tmp_path: Path,
) -> None:
    """Break caught: a reviewed hold can target a real but unrelated lot or fake evidence."""
    service = OperationsService(storage_path=tmp_path / "hold-authority.sqlite3")
    case_id = "CASE-HOLD-AUTHORITY"
    create(service, case_id)

    with pytest.raises(ClosureBlockedError, match="case|eligible|scope"):
        trusted_execute(
            service,
            "apply_inventory_hold",
            case_id=case_id,
            lot_ids=[GAPPED_LOT],
            **reviewed(case_id, "apply_inventory_hold", 1, [GAPPED_LOT]),
            expected_case_version=1,
            idempotency_key="unrelated-hold",
        )

    with pytest.raises(ClosureBlockedError, match="evidence"):
        trusted_execute(
            service,
            "apply_inventory_hold",
            case_id=case_id,
            lot_ids=[SUCCESS_LOT],
            **reviewed(
                case_id,
                "apply_inventory_hold",
                1,
                [SUCCESS_LOT],
                evidence_ids=["FAKE-EVIDENCE"],
            ),
            expected_case_version=1,
            idempotency_key="fake-evidence-hold",
        )

    assert service.get_case(case_id).case_version == 1


def test_case_cannot_close_without_a_prior_inventory_hold(tmp_path: Path) -> None:
    """Break caught: tasks and acknowledgements alone satisfy the closure predicate."""
    service = OperationsService(storage_path=tmp_path / "no-hold-close.sqlite3")
    case_id = "CASE-NO-HOLD-CLOSE"
    create(service, case_id)
    # Simulate a tampered/legacy store that claims facility completion without holds.
    with sqlite3.connect(service.storage_path) as connection:
        for facility_id in ("DC-SOUTH", "STORE-03"):
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, 'acknowledged')", (case_id, facility_id)
            )
            connection.execute(
                "INSERT INTO acknowledgements VALUES (?, ?, 1)", (case_id, facility_id)
            )
    version = 1
    with pytest.raises(ClosureBlockedError, match="hold"):
        trusted_execute(
            service,
            "close_case",
            case_id=case_id,
            **reviewed(case_id, "close_case", version, []),
            expected_case_version=version,
            idempotency_key="no-hold-close",
        )


def test_disposition_appends_new_authoritative_event_and_preserves_base_evidence(
    tmp_path: Path,
) -> None:
    """Break caught: resolution rewrites raw trace IDs instead of appending disposition evidence."""
    database = tmp_path / "append-only-disposition.sqlite3"
    service = OperationsService(storage_path=database)
    case_id = "CASE-APPEND-DISPOSITION"
    create(service, case_id, lot_id=GAPPED_LOT)
    base = service.get_case(case_id)
    base_trace_ids = list(base.trace_event_ids)
    hold_evidence = sorted(service.get_lot_evidence_ids(GAPPED_LOT))
    trusted_execute(
        service,
        "apply_inventory_hold",
        case_id=case_id,
        lot_ids=[GAPPED_LOT],
        **reviewed(
            case_id,
            "apply_inventory_hold",
            1,
            [GAPPED_LOT],
            evidence_ids=hold_evidence,
        ),
        expected_case_version=1,
        idempotency_key="append-hold",
    )
    source_evidence_id = base.reconciliation[0].component_evidence["unaccounted"][0]
    trusted_execute(
        service,
        "record_disposition",
        case_id=case_id,
        lot_id=GAPPED_LOT,
        disposition="dispose_unaccounted",
        evidence_id=source_evidence_id,
        **reviewed(
            case_id,
            "record_disposition",
            2,
            [GAPPED_LOT],
            evidence_ids=[source_evidence_id],
        ),
        expected_case_version=2,
        idempotency_key="append-disposition",
    )

    resolved = service.get_case(case_id)
    assert resolved.trace_event_ids == base_trace_ids
    assert resolved.reconciliation[0].unaccounted == 0
    assert resolved.reconciliation[0].disposed == base.reconciliation[0].disposed + 50
    assert len(resolved.disposition_events) == 1
    event = resolved.disposition_events[0]
    assert event.lot_id == GAPPED_LOT
    assert event.quantity == 50
    assert event.disposition == "dispose_unaccounted"
    assert event.provenance == "WORKFLOW_APPROVED_SYNTHETIC_DISPOSITION"
    assert event.source_evidence_ids == (source_evidence_id,)
    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT event_json FROM disposition_events WHERE case_id=?", (case_id,)
        ).fetchall()
    assert len(persisted) == 1


def test_legacy_cases_are_transactionally_backfilled_into_case_threads(tmp_path: Path) -> None:
    database = tmp_path / "legacy-backfill.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-LEGACY-ONE")
    create(service, "CASE-LEGACY-TWO")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE case_threads")
        connection.execute("DROP TABLE workflow_identities")
        row = connection.execute(
            "SELECT state_json FROM cases WHERE case_id='CASE-LEGACY-TWO'"
        ).fetchone()
        state = json.loads(row[0])
        state["thread_id"] = "CASE-LEGACY-ONE"
        connection.execute(
            "UPDATE cases SET state_json=? WHERE case_id='CASE-LEGACY-TWO'",
            (json.dumps(state),),
        )

    with pytest.raises(OperationStoreError, match="identity|thread"):
        OperationsService(storage_path=database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM case_threads").fetchone()[0] == 0

    with sqlite3.connect(database) as connection:
        state["thread_id"] = "THREAD-LEGACY-TWO"
        connection.execute(
            "UPDATE cases SET state_json=? WHERE case_id='CASE-LEGACY-TWO'",
            (json.dumps(state),),
        )
    migrated = OperationsService(storage_path=database)
    assert migrated.get_thread_for_case("CASE-LEGACY-ONE") == "CASE-LEGACY-ONE"
    assert migrated.get_thread_for_case("CASE-LEGACY-TWO") == "THREAD-LEGACY-TWO"
    assert migrated.get_case_for_thread("THREAD-LEGACY-TWO").case_id == "CASE-LEGACY-TWO"


def test_legacy_two_column_identity_table_gains_owner_token_idempotently(tmp_path: Path) -> None:
    """Break caught: ownership hardening makes an existing Operations DB unreadable."""
    database = tmp_path / "legacy-identity-schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE workflow_identities (case_id TEXT PRIMARY KEY, thread_id TEXT UNIQUE)"
        )
        connection.execute(
            "INSERT INTO workflow_identities VALUES ('CASE-LEGACY', 'THREAD-LEGACY')"
        )

    OperationsService(storage_path=database)
    OperationsService(storage_path=database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_identities)")}
        mapping = connection.execute(
            "SELECT case_id, thread_id, owner_token, checkpoint_head, attempt_token, "
            "attempt_expected_head, attempt_request_digest, attempt_execution_id, "
            "attempt_execution_request_digest, attempt_state, attempt_expires_at "
            "FROM workflow_identities"
        ).fetchone()
    assert columns == {
        "case_id",
        "thread_id",
        "owner_token",
        "checkpoint_head",
        "attempt_token",
        "attempt_expected_head",
        "attempt_request_digest",
        "attempt_execution_id",
        "attempt_execution_request_digest",
        "attempt_state",
        "attempt_expires_at",
    }
    assert mapping == (
        "CASE-LEGACY",
        "THREAD-LEGACY",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_active_fence_token_cannot_be_shared_by_a_second_live_claimant(tmp_path: Path) -> None:
    """Break caught: two live clones reuse one persisted attempt token concurrently."""
    database = tmp_path / "active-fence.sqlite3"
    service = OperationsService(storage_path=database)
    broker = _workflow_authorization_broker(service, str(uuid4()))
    broker.reserve_workflow_identity("CASE-FENCE", "THREAD-FENCE")
    broker.claim_workflow_mutation(
        "CASE-FENCE",
        "THREAD-FENCE",
        "__recallops_initial_checkpoint__",
        "ATTEMPT-FENCE",
        "a" * 64,
    )
    with pytest.raises(ValueError, match="active|uncertain"):
        broker.claim_workflow_mutation(
            "CASE-FENCE",
            "THREAD-FENCE",
            "__recallops_initial_checkpoint__",
            "ATTEMPT-FENCE",
            "a" * 64,
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workflow_identities SET attempt_expires_at=0 WHERE case_id='CASE-FENCE'"
        )
        before_changed_request = connection.execute(
            "SELECT * FROM workflow_identities WHERE case_id='CASE-FENCE'"
        ).fetchone()
    with pytest.raises(ValueError, match="request digest"):
        broker.claim_workflow_mutation(
            "CASE-FENCE",
            "THREAD-FENCE",
            "__recallops_initial_checkpoint__",
            "ATTEMPT-FENCE",
            "b" * 64,
        )
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT * FROM workflow_identities WHERE case_id='CASE-FENCE'"
            ).fetchone()
            == before_changed_request
        )
    broker.claim_workflow_mutation(
        "CASE-FENCE",
        "THREAD-FENCE",
        "__recallops_initial_checkpoint__",
        "ATTEMPT-FENCE",
        "a" * 64,
    )


def test_recovery_requires_the_exact_bound_request_digest(tmp_path: Path) -> None:
    database = tmp_path / "recovery-digest.sqlite3"
    service = OperationsService(storage_path=database)
    broker = _workflow_authorization_broker(service, str(uuid4()))
    broker.reserve_workflow_identity("CASE-RECOVER", "THREAD-RECOVER")
    broker.claim_workflow_mutation(
        "CASE-RECOVER",
        "THREAD-RECOVER",
        "__recallops_initial_checkpoint__",
        "ATTEMPT-RECOVER",
        "a" * 64,
    )
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT * FROM workflow_identities WHERE case_id='CASE-RECOVER'"
        ).fetchone()

    with pytest.raises(ValueError, match="recovery proof"):
        broker.recover_workflow_mutation(
            "CASE-RECOVER",
            "THREAD-RECOVER",
            "__recallops_initial_checkpoint__",
            "CHECKPOINT-RECOVERED",
            "ATTEMPT-RECOVER",
            "b" * 64,
        )
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT * FROM workflow_identities WHERE case_id='CASE-RECOVER'"
            ).fetchone()
            == before
        )

    broker.recover_workflow_mutation(
        "CASE-RECOVER",
        "THREAD-RECOVER",
        "__recallops_initial_checkpoint__",
        "CHECKPOINT-RECOVERED",
        "ATTEMPT-RECOVER",
        "a" * 64,
    )


def test_legacy_create_hash_replays_once_then_migrates_to_thread_bound_hash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-hash.sqlite3"
    service = OperationsService(storage_path=database)
    case_id = "CASE-LEGACY-HASH"
    payload = case_input()
    decision = reviewed(
        case_id,
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    receipt = service.create_case(
        case_id=case_id,
        **payload,
        **decision,
        expected_case_version=0,
        idempotency_key="legacy-create-key",
    )
    legacy_details = dict(receipt.details)
    legacy_details.pop("reviewed_action")
    legacy_details.pop("thread_id")
    legacy_hash = service._request_hash(
        case_id,
        "create_case",
        0,
        legacy_details,
        decision["approval"],
        decision["proposed_action"],
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE receipts SET request_hash=? WHERE idempotency_key='legacy-create-key'",
            (legacy_hash,),
        )

    restarted = OperationsService(storage_path=database)
    replay = restarted.create_case(
        case_id=case_id,
        thread_id=case_id,
        **payload,
        **decision,
        expected_case_version=0,
        idempotency_key="legacy-create-key",
    )
    assert replay == receipt
    current_details = {**legacy_details, "thread_id": case_id}
    expected_hash = restarted._request_hash(
        case_id,
        "create_case",
        0,
        current_details,
        decision["approval"],
        decision["proposed_action"],
    )
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT request_hash FROM receipts WHERE idempotency_key='legacy-create-key'"
        ).fetchone()[0]
    assert stored == expected_hash


def make_ready(service: OperationsService, case_id: str) -> int:
    create(service, case_id)
    service.apply_inventory_hold(
        case_id=case_id,
        lot_ids=[SUCCESS_LOT],
        **reviewed(case_id, "apply_inventory_hold", 1, [SUCCESS_LOT]),
        expected_case_version=1,
        idempotency_key=f"{case_id}-hold",
    )
    service.create_facility_tasks(
        case_id=case_id,
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed(case_id, "create_facility_tasks", 2, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=2,
        idempotency_key=f"{case_id}-tasks",
    )
    version = 3
    for facility_id in ("DC-SOUTH", "STORE-03"):
        service.record_acknowledgment(
            case_id=case_id,
            facility_id=facility_id,
            **reviewed(case_id, "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"{case_id}-ack-{facility_id}",
        )
        version += 1
    return version


def assert_begin_immediate_is_locked(database: Path) -> None:
    connection = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connection.execute("BEGIN IMMEDIATE")
    finally:
        connection.close()


def _process_hold(database: str, key: str) -> str:
    try:
        OperationsService(storage_path=Path(database)).apply_inventory_hold(
            case_id="CASE-CAS",
            lot_ids=[SUCCESS_LOT],
            **reviewed("CASE-CAS", "apply_inventory_hold", 1, [SUCCESS_LOT]),
            expected_case_version=1,
            idempotency_key=key,
        )
        return "won"
    except StaleCaseVersionError:
        return "stale"
    except ValueError:
        return "fenced"


def _process_close_or_task(database: str, action: str) -> str:
    service = OperationsService(storage_path=Path(database))
    try:
        if action == "close":
            service.close_case(
                case_id="CASE-RACE",
                **reviewed("CASE-RACE", "close_case", 4, []),
                expected_case_version=4,
                idempotency_key="race-close",
            )
        else:
            service.create_facility_tasks(
                case_id="CASE-RACE",
                facility_ids=["DC-SOUTH"],
                **reviewed("CASE-RACE", "create_facility_tasks", 4, ["DC-SOUTH"]),
                expected_case_version=4,
                idempotency_key="race-task",
            )
        return action
    except StaleCaseVersionError:
        return "stale"
    except ValueError:
        return "fenced"


def test_sqlite_idempotency_binds_the_full_request_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    first = OperationsService(storage_path=database)
    payload = case_input()
    approved = reviewed(
        "CASE-SQL",
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    receipt = first.create_case(
        case_id="CASE-SQL",
        **payload,
        **approved,
        expected_case_version=0,
        idempotency_key="request-1",
    )
    assert (
        OperationsService(storage_path=database)
        .create_case(
            case_id="CASE-SQL",
            **payload,
            **approved,
            expected_case_version=0,
            idempotency_key="request-1",
        )
        .receipt_id
        == receipt.receipt_id
    )
    with pytest.raises(IdempotencyConflictError):
        OperationsService(storage_path=database).create_case(
            case_id="OTHER",
            **payload,
            **reviewed(
                "OTHER",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key="request-1",
        )


def test_sqlite_compare_and_swap_allows_only_one_separate_process_expected_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    create(OperationsService(storage_path=database), "CASE-CAS")

    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        outcomes = set(pool.map(_process_hold, [str(database)] * 2, ["hold-a", "hold-b"]))
    assert "won" in outcomes
    assert outcomes <= {"won", "stale", "fenced"}


def test_idempotency_binds_approval_identity_and_close_replays(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    payload = case_input()
    approved = reviewed(
        "CASE-REPLAY",
        "create_case",
        0,
        payload["confirmed_lot_ids"],
        evidence_ids=payload["trace_event_ids"],
    )
    service.create_case(
        case_id="CASE-REPLAY",
        **payload,
        **approved,
        expected_case_version=0,
        idempotency_key="create",
    )
    with pytest.raises(IdempotencyConflictError):
        service.create_case(
            case_id="CASE-REPLAY",
            **payload,
            **reviewed(
                "CASE-REPLAY",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
                actor="other",
            ),
            expected_case_version=0,
            idempotency_key="create",
        )
    service.apply_inventory_hold(
        case_id="CASE-REPLAY",
        lot_ids=[SUCCESS_LOT],
        **reviewed("CASE-REPLAY", "apply_inventory_hold", 1, [SUCCESS_LOT]),
        expected_case_version=1,
        idempotency_key="hold",
    )
    service.create_facility_tasks(
        case_id="CASE-REPLAY",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed("CASE-REPLAY", "create_facility_tasks", 2, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=2,
        idempotency_key="tasks",
    )
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=3):
        service.record_acknowledgment(
            case_id="CASE-REPLAY",
            facility_id=facility_id,
            **reviewed("CASE-REPLAY", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"ack-{facility_id}",
        )
    first = service.close_case(
        case_id="CASE-REPLAY",
        **reviewed("CASE-REPLAY", "close_case", 5, []),
        expected_case_version=5,
        idempotency_key="close",
    )
    assert (
        OperationsService(storage_path=database)
        .close_case(
            case_id="CASE-REPLAY",
            **reviewed("CASE-REPLAY", "close_case", 5, []),
            expected_case_version=5,
            idempotency_key="close",
        )
        .receipt_id
        == first.receipt_id
    )


def test_create_case_requires_real_evidence_and_fresh_case_cannot_close(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    for missing in (
        "confirmed_lot_ids",
        "trace_event_ids",
        "required_facilities",
        "reconciliation",
    ):
        payload = case_input()
        payload[missing] = []
        targets = payload["confirmed_lot_ids"]
        evidence = payload["trace_event_ids"] or [
            event["event_id"] for event in TRACEABILITY.trace_forward(SUCCESS_LOT)
        ]
        with pytest.raises(ValueError, match=missing):
            RawOperationsService(
                storage_path=tmp_path / f"raw-empty-{missing}.sqlite3"
            ).create_case(
                case_id=f"CASE-EMPTY-{missing}",
                **payload,
                **reviewed(
                    f"CASE-EMPTY-{missing}",
                    "create_case",
                    0,
                    targets,
                    evidence_ids=evidence,
                ),
                expected_case_version=0,
                idempotency_key=f"empty-{missing}",
            )

    create(service, "CASE-FRESH")
    with pytest.raises(ClosureBlockedError, match="hold"):
        service.close_case(
            case_id="CASE-FRESH",
            **reviewed("CASE-FRESH", "close_case", 1, []),
            expected_case_version=1,
            idempotency_key="fresh-close",
        )


def test_realistic_case_sequence_persists_tasks_and_closes(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    create(service, "CASE-SUCCESS")
    service.apply_inventory_hold(
        case_id="CASE-SUCCESS",
        lot_ids=[SUCCESS_LOT],
        **reviewed("CASE-SUCCESS", "apply_inventory_hold", 1, [SUCCESS_LOT]),
        expected_case_version=1,
        idempotency_key="success-hold",
    )
    service.create_facility_tasks(
        case_id="CASE-SUCCESS",
        facility_ids=["DC-SOUTH", "STORE-03"],
        **reviewed("CASE-SUCCESS", "create_facility_tasks", 2, ["DC-SOUTH", "STORE-03"]),
        expected_case_version=2,
        idempotency_key="success-tasks",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone() == (0,)
    for version, facility_id in enumerate(("DC-SOUTH", "STORE-03"), start=3):
        service.record_acknowledgment(
            case_id="CASE-SUCCESS",
            facility_id=facility_id,
            **reviewed("CASE-SUCCESS", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"success-ack-{facility_id}",
        )
    receipt = service.close_case(
        case_id="CASE-SUCCESS",
        **reviewed("CASE-SUCCESS", "close_case", 5, []),
        expected_case_version=5,
        idempotency_key="success-close",
    )

    state = service.get_case("CASE-SUCCESS")
    assert receipt.case_version == 6
    assert state is not None and state.status == "closed"
    with sqlite3.connect(database) as connection:
        task = connection.execute(
            "SELECT status FROM tasks WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone()
        acknowledgement = connection.execute(
            "SELECT acknowledged FROM acknowledgements WHERE case_id=? AND facility_id=?",
            ("CASE-SUCCESS", "DC-SOUTH"),
        ).fetchone()
    assert task == ("acknowledged",)
    assert acknowledgement == (1,)


def test_acknowledgement_requires_a_persisted_task(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-NO-TASK")
    with pytest.raises(ClosureBlockedError, match="existing facility task"):
        service.record_acknowledgment(
            case_id="CASE-NO-TASK",
            facility_id="DC-NORTH",
            **reviewed("CASE-NO-TASK", "record_acknowledgment", 1, ["DC-NORTH"]),
            expected_case_version=1,
            idempotency_key="no-task-ack",
        )


def test_disposition_rejects_values_outside_the_concrete_domain(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-BAD-DISPOSITION", lot_id=GAPPED_LOT)
    with pytest.raises(ValueError, match="disposition"):
        service.record_disposition(
            case_id="CASE-BAD-DISPOSITION",
            lot_id="LOT-EXACT-170",
            disposition="made_up",
            evidence_id="EV-NOPE",
            **reviewed(
                "CASE-BAD-DISPOSITION",
                "record_disposition",
                1,
                ["LOT-EXACT-170"],
                evidence_ids=["EV-NOPE"],
            ),
            expected_case_version=1,
            idempotency_key="bad-disposition",
        )


def test_failure_after_receipt_insert_rolls_back_the_whole_transaction(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite3"

    def fail(stage: str) -> None:
        if stage == "after_receipt_insert":
            raise RuntimeError("injected receipt failure")

    service = OperationsService(storage_path=database, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected receipt failure"):
        create(service, "CASE-ROLLBACK")

    recovered = OperationsService(storage_path=database)
    assert recovered.get_case("CASE-ROLLBACK") is None
    create(recovered, "CASE-ROLLBACK")
    assert recovered.get_case("CASE-ROLLBACK").case_version == 1


def test_corrupt_or_invalid_database_fails_with_typed_store_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database")
    with pytest.raises(OperationStoreError):
        OperationsService(storage_path=corrupt)

    invalid = tmp_path / "directory.sqlite3"
    invalid.mkdir()
    with pytest.raises(OperationStoreError):
        OperationsService(storage_path=invalid)


def test_concurrent_task_creation_can_never_leave_a_closed_case_with_pending_ack(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite3"
    service = OperationsService(storage_path=database)
    make_ready(service, "CASE-RACE")

    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        outcomes = set(pool.map(_process_close_or_task, [str(database)] * 2, ["close", "task"]))
    assert outcomes and outcomes <= {"stale", "fenced"}

    state = service.get_case("CASE-RACE")
    with sqlite3.connect(database) as connection:
        pending_count = connection.execute(
            """
            SELECT COUNT(*) FROM tasks AS task
            LEFT JOIN acknowledgements AS ack
              ON ack.case_id=task.case_id AND ack.facility_id=task.facility_id
            WHERE task.case_id=? AND COALESCE(ack.acknowledged, 0)=0
            """,
            ("CASE-RACE",),
        ).fetchone()[0]
    assert not (state is not None and state.status == "closed" and pending_count)


def test_no_new_task_can_be_added_after_close(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    version = make_ready(service, "CASE-CLOSED")
    service.close_case(
        case_id="CASE-CLOSED",
        **reviewed("CASE-CLOSED", "close_case", version, []),
        expected_case_version=version,
        idempotency_key="closed-close",
    )
    with pytest.raises(ClosureBlockedError, match="closed"):
        service.create_facility_tasks(
            case_id="CASE-CLOSED",
            facility_ids=["DC-SOUTH", "STORE-03"],
            **reviewed(
                "CASE-CLOSED",
                "create_facility_tasks",
                version + 1,
                ["DC-SOUTH", "STORE-03"],
            ),
            expected_case_version=version + 1,
            idempotency_key="closed-late-task",
        )


@pytest.mark.parametrize(
    ("case_id", "case_payload", "message"),
    [
        ("CASE-GAP", case_input(evidence_gaps=["missing shipment"]), "unresolved evidence"),
    ],
)
def test_close_requires_zero_gaps_and_verified_reconciliation(
    tmp_path: Path, case_id: str, case_payload: dict[str, Any], message: str
) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    del message
    with pytest.raises(ValueError, match="authoritative evidence gaps"):
        service.create_case(
            case_id=case_id,
            **case_payload,
            **reviewed(
                case_id,
                "create_case",
                0,
                case_payload["confirmed_lot_ids"],
                evidence_ids=case_payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key=f"{case_id}-create",
        )


def test_close_rejects_approval_for_an_older_case_version(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    make_ready(service, "CASE-OLD-APPROVAL")
    with pytest.raises(ApprovalRequiredError, match="current expected case version"):
        stale_review = reviewed("CASE-OLD-APPROVAL", "close_case", 3, [])
        stale_review["proposed_action"] = stale_review["proposed_action"].model_copy(
            update={"expected_case_version": 4}
        )
        service.close_case(
            case_id="CASE-OLD-APPROVAL",
            **stale_review,
            expected_case_version=4,
            idempotency_key="old-approval-close",
        )


def test_case_creation_rejects_non_authoritative_trace_evidence(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    payload = case_input()
    payload["trace_event_ids"] = ["EV-001"]
    with pytest.raises(ValueError, match="authoritative"):
        service.create_case(
            case_id="CASE-EVIDENCE-MISMATCH",
            **payload,
            **reviewed(
                "CASE-EVIDENCE-MISMATCH",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key="evidence-mismatch-create",
        )


def test_changed_approved_case_version_conflicts_with_existing_idempotency_key(
    tmp_path: Path,
) -> None:
    service = OperationsService(storage_path=tmp_path / "operations.sqlite3")
    create(service, "CASE-APPROVAL-HASH")
    service.apply_inventory_hold(
        case_id="CASE-APPROVAL-HASH",
        lot_ids=[SUCCESS_LOT],
        **reviewed("CASE-APPROVAL-HASH", "apply_inventory_hold", 1, [SUCCESS_LOT]),
        expected_case_version=1,
        idempotency_key="approval-version-key",
    )
    with pytest.raises(IdempotencyConflictError):
        changed_review = reviewed("CASE-APPROVAL-HASH", "apply_inventory_hold", 1, [SUCCESS_LOT])
        changed_review["approval"] = changed_review["approval"].model_copy(
            update={"approved_case_version": 0}
        )
        service.apply_inventory_hold(
            case_id="CASE-APPROVAL-HASH",
            lot_ids=[SUCCESS_LOT],
            **changed_review,
            expected_case_version=1,
            idempotency_key="approval-version-key",
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "bogus_lot",
        "missing_event",
        "unrelated_event",
        "bogus_inventory",
        "wrong_quantities",
        "legacy_fake_event",
        "legacy_fake_inventory",
        "wrong_facility",
    ],
)
def test_case_creation_rejects_each_non_authoritative_evidence_class(
    tmp_path: Path, tamper: str
) -> None:
    payload = case_input()
    if tamper == "bogus_lot":
        payload["confirmed_lot_ids"] = ["LOT-BOGUS"]
        payload["reconciliation"] = [
            payload["reconciliation"][0].model_copy(update={"lot_id": "LOT-BOGUS"})
        ]
    elif tamper in {"missing_event", "legacy_fake_event"}:
        payload["trace_event_ids"] = ["EV-NOT-FOUND" if tamper == "missing_event" else "EV-RECEIVE"]
    elif tamper == "unrelated_event":
        payload["trace_event_ids"] = [*payload["trace_event_ids"], "EV-001"]
    elif tamper == "wrong_facility":
        payload["required_facilities"] = ["DC-NORTH"]
    else:
        raw = payload["reconciliation"][0].model_dump(mode="json")
        if tamper == "wrong_quantities":
            raw["received"] += 1
            raw["unaccounted"] += 1
        else:
            replacement = "INV-NOT-FOUND" if tamper == "bogus_inventory" else "INV-EXACT"
            original = raw["component_evidence"]["on_hand"][0]
            raw["component_evidence"]["on_hand"] = [replacement]
            raw["component_evidence"]["unaccounted"] = [
                replacement if item == original else item
                for item in raw["component_evidence"]["unaccounted"]
            ]
            raw["evidence_ids"] = [
                replacement if item == original else item for item in raw["evidence_ids"]
            ]
        payload["reconciliation"] = [Reconciliation.model_validate(raw)]

    service = OperationsService(storage_path=tmp_path / f"{tamper}.sqlite3")
    with pytest.raises(ValueError, match="authoritative"):
        service.create_case(
            case_id=f"CASE-{tamper}",
            **payload,
            **reviewed(
                f"CASE-{tamper}",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key=f"create-{tamper}",
        )


def test_exact_lot_authoritative_fifty_unit_gap_cannot_be_closed(tmp_path: Path) -> None:
    service = OperationsService(storage_path=tmp_path / "exact.sqlite3")
    create(service, "CASE-EXACT-GAP", lot_id=GAPPED_LOT)
    service.apply_inventory_hold(
        case_id="CASE-EXACT-GAP",
        lot_ids=[GAPPED_LOT],
        **reviewed("CASE-EXACT-GAP", "apply_inventory_hold", 1, [GAPPED_LOT]),
        expected_case_version=1,
        idempotency_key="exact-hold",
    )
    service.create_facility_tasks(
        case_id="CASE-EXACT-GAP",
        facility_ids=["DC-NORTH", "STORE-01", "STORE-02"],
        **reviewed(
            "CASE-EXACT-GAP",
            "create_facility_tasks",
            2,
            ["DC-NORTH", "STORE-01", "STORE-02"],
        ),
        expected_case_version=2,
        idempotency_key="exact-tasks",
    )
    version = 3
    for facility_id in ("DC-NORTH", "STORE-01", "STORE-02"):
        service.record_acknowledgment(
            case_id="CASE-EXACT-GAP",
            facility_id=facility_id,
            **reviewed("CASE-EXACT-GAP", "record_acknowledgment", version, [facility_id]),
            expected_case_version=version,
            idempotency_key=f"exact-ack-{facility_id}",
        )
        version += 1
    with pytest.raises(ClosureBlockedError, match="50|unaccounted"):
        service.close_case(
            case_id="CASE-EXACT-GAP",
            **reviewed("CASE-EXACT-GAP", "close_case", version, []),
            expected_case_version=version,
            idempotency_key="exact-close",
        )


def test_closure_revalidates_authority_after_caller_disposition_changes_evidence(
    tmp_path: Path,
) -> None:
    service = OperationsService(storage_path=tmp_path / "closure-authority.sqlite3")
    version = make_ready(service, "CASE-CLOSURE-AUTHORITY")
    with pytest.raises(ClosureBlockedError, match="positive unaccounted residual"):
        service.record_disposition(
            case_id="CASE-CLOSURE-AUTHORITY",
            lot_id=SUCCESS_LOT,
            disposition="dispose_unaccounted",
            evidence_id="EV-RECEIVE",
            **reviewed(
                "CASE-CLOSURE-AUTHORITY",
                "record_disposition",
                version,
                [SUCCESS_LOT],
                evidence_ids=["EV-RECEIVE"],
            ),
            expected_case_version=version,
            idempotency_key="caller-disposition",
        )


def test_authoritative_traceability_dependency_is_injectable(tmp_path: Path) -> None:
    dataset = deepcopy(TRACEABILITY.dataset)
    next(item for item in dataset["events"] if item["event_id"] == "EV-004")["quantity"] += 1
    next(
        item
        for item in dataset["inventory_positions"]
        if item["position_id"] == "INV-LOT-PROBABLE-160"
    )["on_hand"] += 1
    injected = TraceabilityService(dataset=dataset)
    events = injected.trace_forward(SUCCESS_LOT)
    payload = {
        "recall_number": "H-1230-2026",
        "confirmed_lot_ids": [SUCCESS_LOT],
        "trace_event_ids": [event["event_id"] for event in events],
        "required_facilities": ["DC-SOUTH", "STORE-03"],
        "reconciliation": [injected.reconcile_units(SUCCESS_LOT)],
        "evidence_gaps": [],
    }
    service = OperationsService(storage_path=tmp_path / "injected.sqlite3", traceability=injected)
    service.create_case(
        case_id="CASE-INJECTED-AUTHORITY",
        **payload,
        **reviewed(
            "CASE-INJECTED-AUTHORITY",
            "create_case",
            0,
            payload["confirmed_lot_ids"],
            evidence_ids=payload["trace_event_ids"],
        ),
        expected_case_version=0,
        idempotency_key="injected-authority",
    )
    assert service.get_case("CASE-INJECTED-AUTHORITY").reconciliation[0].received == 901

    default_service = OperationsService(storage_path=tmp_path / "default.sqlite3")
    with pytest.raises(ValueError, match="authoritative reconciliation"):
        default_service.create_case(
            case_id="CASE-DEFAULT-AUTHORITY",
            **payload,
            **reviewed(
                "CASE-DEFAULT-AUTHORITY",
                "create_case",
                0,
                payload["confirmed_lot_ids"],
                evidence_ids=payload["trace_event_ids"],
            ),
            expected_case_version=0,
            idempotency_key="default-authority",
        )


def test_close_wins_deterministically_while_competing_task_waits_outside_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "close-wins.sqlite3"
    seed = OperationsService(storage_path=database)
    version = make_ready(seed, "CASE-CLOSE-WINS")
    close_validated = Event()
    release_close = Event()
    task_started = Event()
    task_done = Event()
    outcomes: dict[str, str] = {}

    def barrier(action: str) -> None:
        if action == "close_case":
            close_validated.set()
            assert release_close.wait(timeout=5)

    close_service = OperationsService(storage_path=database, before_cas_hook=barrier)
    task_service = OperationsService(storage_path=database)

    def close() -> None:
        close_service.close_case(
            case_id="CASE-CLOSE-WINS",
            **reviewed("CASE-CLOSE-WINS", "close_case", version, []),
            expected_case_version=version,
            idempotency_key="close-wins-close",
        )
        outcomes["close"] = "won"

    def task() -> None:
        task_started.set()
        try:
            task_service.create_facility_tasks(
                case_id="CASE-CLOSE-WINS",
                facility_ids=["DC-SOUTH"],
                **reviewed(
                    "CASE-CLOSE-WINS",
                    "create_facility_tasks",
                    version,
                    ["DC-SOUTH"],
                ),
                expected_case_version=version,
                idempotency_key="close-wins-task",
            )
            outcomes["task"] = "won"
        except (StaleCaseVersionError, ValueError):
            outcomes["task"] = "stale"
        finally:
            task_done.set()

    close_thread = Thread(target=close)
    task_thread = Thread(target=task)
    close_thread.start()
    assert close_validated.wait(timeout=5)
    task_thread.start()
    assert task_started.wait(timeout=5)
    assert_begin_immediate_is_locked(database)
    assert not task_done.is_set()
    release_close.set()
    close_thread.join(timeout=5)
    task_thread.join(timeout=5)
    assert not close_thread.is_alive() and not task_thread.is_alive()
    assert outcomes == {"close": "won", "task": "stale"}
    state = seed.get_case("CASE-CLOSE-WINS")
    assert state is not None and state.status == "closed"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE case_id=? AND status='pending'",
            ("CASE-CLOSE-WINS",),
        ).fetchone() == (0,)


def test_completed_facility_tasks_cannot_be_recreated_before_close(tmp_path: Path) -> None:
    """Break caught: recreating tasks erases authoritative acknowledgements."""
    service = OperationsService(storage_path=tmp_path / "repeat-tasks.sqlite3")
    version = make_ready(service, "CASE-REPEAT-TASKS")

    with pytest.raises(ClosureBlockedError, match="already exist"):
        service.create_facility_tasks(
            case_id="CASE-REPEAT-TASKS",
            facility_ids=["DC-SOUTH", "STORE-03"],
            **reviewed(
                "CASE-REPEAT-TASKS",
                "create_facility_tasks",
                version,
                ["DC-SOUTH", "STORE-03"],
            ),
            expected_case_version=version,
            idempotency_key="repeat-tasks",
        )

    assert service.get_case("CASE-REPEAT-TASKS").acknowledgements == {
        "DC-SOUTH": True,
        "STORE-03": True,
    }
