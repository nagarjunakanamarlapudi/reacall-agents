"""Approval-gated simulated writes with versioning and idempotent audit receipts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from recallops.models import ApprovalDecision, AuditReceipt, RecallCaseState


class ApprovalRequiredError(PermissionError):
    pass


class StaleCaseVersionError(ValueError):
    pass


class ClosureBlockedError(ValueError):
    pass


class OperationsService:
    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path
        self.cases: dict[str, RecallCaseState] = {}
        self.receipts: dict[str, AuditReceipt] = {}
        if storage_path and storage_path.exists():
            stored = json.loads(storage_path.read_text())
            self.cases = {
                case_id: RecallCaseState.model_validate(case)
                for case_id, case in stored.get("cases", {}).items()
            }
            self.receipts = {
                key: AuditReceipt.model_validate(receipt)
                for key, receipt in stored.get("receipts", {}).items()
            }

    def _persist(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps(
                {
                    "cases": {
                        case_id: case.model_dump(mode="json")
                        for case_id, case in self.cases.items()
                    },
                    "receipts": {
                        key: receipt.model_dump(mode="json")
                        for key, receipt in self.receipts.items()
                    },
                },
                sort_keys=True,
            )
        )

    def _require_approval(self, approval: ApprovalDecision) -> None:
        if approval.decision != "approve":
            raise ApprovalRequiredError("simulated writes require an explicit approve decision")

    def _write(
        self,
        *,
        case_id: str,
        action_type: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        details: dict[str, object],
    ) -> AuditReceipt:
        self._require_approval(approval)
        existing = self.receipts.get(idempotency_key)
        if existing:
            return existing
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(f"unknown case {case_id}")
        if case.case_version != expected_case_version:
            raise StaleCaseVersionError(
                f"expected version {expected_case_version}, current version is {case.case_version}"
            )
        receipt = AuditReceipt(
            receipt_id=str(uuid5(NAMESPACE_URL, f"{case_id}:{action_type}:{idempotency_key}")),
            case_id=case_id,
            action_type=action_type,
            actor=approval.actor,
            justification=approval.justification,
            idempotency_key=idempotency_key,
            case_version=case.case_version + 1,
            status="simulated",
            details=details,
        )
        self.receipts[idempotency_key] = receipt
        self.cases[case_id] = case.model_copy(
            update={
                "case_version": receipt.case_version,
                "write_receipts": [*case.write_receipts, receipt],
            }
        )
        self._persist()
        return receipt

    def create_case(
        self,
        *,
        case_id: str,
        recall_number: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
        question: str = "",
    ) -> AuditReceipt:
        self._require_approval(approval)
        existing = self.receipts.get(idempotency_key)
        if existing:
            return existing
        if expected_case_version != 0:
            raise StaleCaseVersionError("new cases must use expected version 0")
        if case_id in self.cases:
            raise ValueError(f"case {case_id} already exists")
        case = RecallCaseState(
            case_id=case_id, thread_id=case_id, recall_number=recall_number, question=question
        )
        self.cases[case_id] = case
        return self._write(
            case_id=case_id,
            action_type="create_case",
            approval=approval,
            expected_case_version=0,
            idempotency_key=idempotency_key,
            details={"recall_number": recall_number},
        )

    def apply_inventory_hold(
        self,
        *,
        case_id: str,
        lot_ids: list[str],
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        return self._write(
            case_id=case_id,
            action_type="apply_inventory_hold",
            approval=approval,
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            details={"lot_ids": lot_ids},
        )

    def create_facility_tasks(
        self,
        *,
        case_id: str,
        facility_ids: list[str],
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        return self._write(
            case_id=case_id,
            action_type="create_facility_tasks",
            approval=approval,
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            details={"facility_ids": facility_ids},
        )

    def record_acknowledgment(
        self,
        *,
        case_id: str,
        facility_id: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        receipt = self._write(
            case_id=case_id,
            action_type="record_acknowledgment",
            approval=approval,
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            details={"facility_id": facility_id},
        )
        case = self.cases[case_id]
        self.cases[case_id] = case.model_copy(
            update={"acknowledgements": {**case.acknowledgements, facility_id: True}}
        )
        self._persist()
        return receipt

    def record_disposition(
        self,
        *,
        case_id: str,
        lot_id: str,
        disposition: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        return self._write(
            case_id=case_id,
            action_type="record_disposition",
            approval=approval,
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            details={"lot_id": lot_id, "disposition": disposition},
        )

    def close_case(
        self,
        *,
        case_id: str,
        approval: ApprovalDecision,
        expected_case_version: int,
        idempotency_key: str,
    ) -> AuditReceipt:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(f"unknown case {case_id}")
        if any(item.unaccounted != 0 for item in case.reconciliation):
            raise ClosureBlockedError("closure blocked: unaccounted units remain")
        if any(not acknowledged for acknowledged in case.acknowledgements.values()):
            raise ClosureBlockedError("closure blocked: facility acknowledgement remains")
        receipt = self._write(
            case_id=case_id,
            action_type="close_case",
            approval=approval,
            expected_case_version=expected_case_version,
            idempotency_key=idempotency_key,
            details={"closed_at": datetime.now(UTC).isoformat()},
        )
        latest = self.cases[case_id]
        self.cases[case_id] = latest.model_copy(update={"status": "closed"})
        self._persist()
        return receipt
