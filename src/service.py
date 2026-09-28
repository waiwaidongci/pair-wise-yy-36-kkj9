from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CLAIMABLE_STATUS, CLAIM_ROLES, CREATE_ROLES,
                    ENTITY, RECORD_ROLES, TITLE, TRANSFER_CONFIRMED,
                    TRANSFER_PENDING, TRANSFER_REJECTED, TRANSFER_ROLES,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        self._require_owner(self.repository.get_item(item_id), actor)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    @staticmethod
    def _require_owner(item: Dict[str, Any], actor: str) -> None:
        if item["status"] != CLAIMABLE_STATUS:
            return
        if not item.get("owner"):
            raise ConflictError("事件尚无领办人，请先领单后再提交处置意见")
        if item["owner"] != actor:
            raise PermissionDenied("只有领办人可以提交处置意见")

    def claim(self, item_id: int, expected_version: int, actor: str,
              role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] != CLAIMABLE_STATUS:
            raise ConflictError("事件未在评估阶段，不能领办")
        if item["version"] != expected_version:
            raise ConflictError("版本冲突，请刷新后重试")
        if item["owner"] is not None:
            raise ConflictError("该事件已有领办人，不能重复领单")
        updated = self.repository.claim_item(item_id, actor, expected_version)
        self.repository.append_audit("claim", ENTITY, item_id, actor, {
            "expected_version": expected_version, "owner": actor,
        })
        return self.enrich(updated)

    def transfer(self, item_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        to_actor = require_text(payload.get("to_actor"), "接收人", 100)
        reason = require_text(payload.get("reason"), "交接原因", 500)
        if to_actor == actor:
            from .domain import ValidationError
            raise ValidationError("不能转交给自己")
        item = self.repository.get_item(item_id)
        if item["status"] != CLAIMABLE_STATUS:
            raise ConflictError("事件未在评估阶段，不能转交")
        if item["owner"] != actor:
            raise PermissionDenied("只有当前领办人可以转交该事件")
        if self.repository.get_pending_transfer(item_id) is not None:
            raise ConflictError("该事件已有待确认的交接，不能重复发起")
        transfer = self.repository.create_transfer(item_id, actor, to_actor, reason)
        self.repository.append_audit("transfer", ENTITY, item_id, actor, {
            "transfer_id": transfer["id"], "from": actor,
            "to": to_actor, "reason": reason,
        })
        return transfer

    def confirm_transfer(self, transfer_id: int, actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_pending_receiver(transfer_id, actor)
        transfer = self.repository.decide_transfer(
            transfer_id, TRANSFER_CONFIRMED, actor)
        self.repository.append_audit("confirm", ENTITY, transfer["item_id"], actor, {
            "transfer_id": transfer_id, "from": transfer["from_actor"], "to": actor,
        })
        return transfer

    def reject_transfer(self, transfer_id: int, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        pending = self._require_pending_receiver(transfer_id, actor)
        transfer = self.repository.decide_transfer(
            transfer_id, TRANSFER_REJECTED, actor)
        self.repository.append_audit("reject", ENTITY, transfer["item_id"], actor, {
            "transfer_id": transfer_id, "from": pending["from_actor"],
            "to": actor, "owner_unchanged": pending["from_actor"],
        })
        return transfer

    def _require_pending_receiver(self, transfer_id: int, actor: str) -> Dict[str, Any]:
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["status"] != TRANSFER_PENDING:
            raise ConflictError("交接已处理，不能重复操作")
        if transfer["to_actor"] != actor:
            raise PermissionDenied("只有接收人可以确认或驳回该交接")
        return transfer

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["pending_transfer"] = self.repository.get_pending_transfer(item["id"])
        return result
