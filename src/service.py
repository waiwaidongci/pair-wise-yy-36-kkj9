from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CLAIMABLE_STATES, CLAIM_ROLES, CREATE_ROLES,
                    ENTITY, HANDOFF_DECISIONS, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
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
        return self._with_assignment(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        assignment = self.repository.get_assignment(item_id)
        if assignment is not None and assignment["owner"] != actor:
            raise PermissionDenied("只有当前领办人可以提交处置意见")
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def claim(self, item_id: int, payload: Dict[str, Any], actor: str,
              role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) \
                or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] not in CLAIMABLE_STATES:
            raise ConflictError("事件尚未进入评估，不能领单")
        if self.repository.get_pending_handoff(item_id) is not None:
            raise ConflictError("存在待确认的交接，不能重复领单")
        self.repository.claim(item_id, actor, expected_version)
        self.repository.append_audit("claim", ENTITY, item_id, actor, {
            "version": expected_version,
        })
        return self._with_assignment(self.repository.get_item(item_id))

    def handoff(self, item_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        to_actor = require_text(payload.get("to_actor"), "to_actor", 100)
        reason = require_text(payload.get("reason"), "reason")
        item = self.repository.get_item(item_id)
        assignment = self.repository.get_assignment(item_id)
        if assignment is None:
            raise ConflictError("事件尚未被领办，无法转交")
        if assignment["owner"] != actor:
            raise PermissionDenied("只有当前领办人可以发起转交")
        if to_actor == actor:
            raise ConflictError("不能转交给自己")
        if self.repository.get_pending_handoff(item_id) is not None:
            raise ConflictError("已有待确认的交接，请等待接收人处理")
        pending = self.repository.request_handoff(item_id, actor, to_actor, reason)
        self.repository.append_audit("handoff", ENTITY, item_id, actor, {
            "handoff_id": pending["id"], "to_actor": to_actor, "reason": reason,
        })
        return self._with_assignment(item)

    def decide_handoff(self, handoff_id: int, decision: Optional[str], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        if decision not in HANDOFF_DECISIONS:
            raise ValidationError("decision必须是confirm或reject")
        handoff = self.repository.resolve_handoff(handoff_id, decision, actor)
        item = self.repository.get_item(handoff["item_id"])
        action = "handoff_confirm" if decision == "confirm" else "handoff_reject"
        self.repository.append_audit(action, ENTITY, item["id"], actor, {
            "handoff_id": handoff_id, "from_owner": handoff["from_owner"],
        })
        return self._with_assignment(item)

    @staticmethod
    def _assignment_view(assignment: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if assignment is None:
            return None
        return {
            "owner": assignment["owner"],
            "claimed_version": assignment["claimed_version"],
            "claimed_at": assignment["claimed_at"],
        }

    @staticmethod
    def _handoff_view(handoff: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if handoff is None:
            return None
        return {
            "id": handoff["id"],
            "from_owner": handoff["from_owner"],
            "to_actor": handoff["to_actor"],
            "reason": handoff["reason"],
            "status": handoff["status"],
            "requested_at": handoff["requested_at"],
        }

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
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self._with_assignment(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._with_assignment(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._with_assignment(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    def _with_assignment(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self.enrich(item)
        result["owner"] = None
        assignment = self.repository.get_assignment(item["id"])
        if assignment is not None:
            result["owner"] = assignment["owner"]
            result["assignment"] = self._assignment_view(assignment)
        pending = self.repository.get_pending_handoff(item["id"])
        result["pending_handoff"] = self._handoff_view(pending)
        return result
