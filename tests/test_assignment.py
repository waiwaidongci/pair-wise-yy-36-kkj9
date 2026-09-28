import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

OFFICER = "compliance_officer"
OFFICER2 = "compliance_officer"  # 角色相同，用 actor 名区分


def make_item(service, ref):
    return service.create_item({
        "title": "spill", "description": "into assessment",
        "severity": "exceedance", "quantity": 12, "threshold": 6,
        "external_ref": ref,
    }, "creator", "operator")


def to_assessing(service, item):
    return service.transition(item["id"], STATES[1], item["version"],
                              "reviewer", TRANSITION_ROLES[STATES[1]][0])


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_claim_on_current_version_and_list_shows_owner(self):
        item = to_assessing(self.service, make_item(self.service, "AS-1"))
        claimed = self.service.claim(item["id"], {"expected_version": item["version"]},
                                     "alice", OFFICER)
        self.assertEqual(claimed["owner"], "alice")
        self.assertEqual(claimed["version"], item["version"])
        self.assertIsNone(claimed["pending_handoff"])
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(listed["owner"], "alice")
        self.assertIn("version", listed)
        self.assertIsNone(listed["pending_handoff"])
        self.assertEqual(listed["assignment"]["claimed_version"], item["version"])

    def test_duplicate_claim_is_rejected(self):
        item = to_assessing(self.service, make_item(self.service, "AS-2"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        with self.assertRaises(ConflictError):
            self.service.claim(item["id"], {"expected_version": item["version"]},
                               "bob", OFFICER2)

    def test_stale_version_claim_is_rejected(self):
        item = to_assessing(self.service, make_item(self.service, "AS-3"))
        with self.assertRaises(ConflictError):
            self.service.claim(item["id"], {"expected_version": item["version"] - 1},
                               "alice", OFFICER)

    def test_claim_before_assessment_is_rejected(self):
        item = make_item(self.service, "AS-4")
        with self.assertRaises(ConflictError):
            self.service.claim(item["id"], {"expected_version": item["version"]},
                               "alice", OFFICER)

    def test_only_compliance_officer_can_claim(self):
        item = to_assessing(self.service, make_item(self.service, "AS-5"))
        with self.assertRaises(PermissionDenied):
            self.service.claim(item["id"], {"expected_version": item["version"]},
                               "alice", "operator")

    def test_expected_version_must_be_positive_int(self):
        item = to_assessing(self.service, make_item(self.service, "AS-6"))
        with self.assertRaises(ValueError):
            self.service.claim(item["id"], {"expected_version": "2"},
                               "alice", OFFICER)

    def test_non_owner_disposition_is_rejected(self):
        item = to_assessing(self.service, make_item(self.service, "AS-7"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        with self.assertRaises(PermissionDenied):
            self.service.add_record(item["id"],
                                    {"kind": "opinion", "detail": "not mine"},
                                    "bob", OFFICER2)
        record = self.service.add_record(
            item["id"], {"kind": "opinion", "detail": "owner opinion"},
            "alice", OFFICER)
        self.assertEqual(record["created_by"], "alice")

    def test_handoff_requires_reason_and_recipient(self):
        item = to_assessing(self.service, make_item(self.service, "AS-8"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        with self.assertRaises(ValidationError):
            self.service.handoff(item["id"], {"to_actor": "bob"}, "alice", OFFICER)
        with self.assertRaises(ValidationError):
            self.service.handoff(item["id"], {"reason": "shift change"},
                                 "alice", OFFICER)

    def test_only_owner_can_request_handoff(self):
        item = to_assessing(self.service, make_item(self.service, "AS-9"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        with self.assertRaises(PermissionDenied):
            self.service.handoff(item["id"], {"to_actor": "carol", "reason": "x"},
                                 "bob", OFFICER2)

    def test_pending_handoff_keeps_original_owner(self):
        item = to_assessing(self.service, make_item(self.service, "AS-10"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        result = self.service.handoff(item["id"],
                                      {"to_actor": "bob", "reason": "shift change"},
                                      "alice", OFFICER)
        self.assertEqual(result["owner"], "alice")
        self.assertEqual(result["pending_handoff"]["to_actor"], "bob")
        self.assertEqual(result["pending_handoff"]["status"], "pending")
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(listed["owner"], "alice")
        self.assertEqual(listed["pending_handoff"]["from_owner"], "alice")
        self.assertEqual(listed["pending_handoff"]["reason"], "shift change")

    def test_confirm_handoff_switches_owner(self):
        item = to_assessing(self.service, make_item(self.service, "AS-11"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        pending = self.service.handoff(item["id"],
                                       {"to_actor": "bob", "reason": "shift change"},
                                       "alice", OFFICER)
        handoff_id = pending["pending_handoff"]["id"]
        with self.assertRaises(PermissionDenied):
            self.service.decide_handoff(handoff_id, "confirm", "carol", OFFICER)
        result = self.service.decide_handoff(handoff_id, "confirm", "bob", OFFICER2)
        self.assertEqual(result["owner"], "bob")
        self.assertIsNone(result["pending_handoff"])
        # 换主责后新主责可提交处置意见，原主责被拒绝
        self.assertTrue(self.service.add_record(
            item["id"], {"kind": "opinion", "detail": "new owner"}, "bob", OFFICER2))
        with self.assertRaises(PermissionDenied):
            self.service.add_record(item["id"],
                                    {"kind": "opinion", "detail": "old owner"},
                                    "alice", OFFICER)

    def test_reject_handoff_keeps_original_owner(self):
        item = to_assessing(self.service, make_item(self.service, "AS-12"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        pending = self.service.handoff(item["id"],
                                       {"to_actor": "bob", "reason": "overloaded"},
                                       "alice", OFFICER)
        result = self.service.decide_handoff(pending["pending_handoff"]["id"],
                                             "reject", "bob", OFFICER2)
        self.assertEqual(result["owner"], "alice")
        self.assertIsNone(result["pending_handoff"])
        # 原领办继续履职
        self.assertTrue(self.service.add_record(
            item["id"], {"kind": "opinion", "detail": "still mine"},
            "alice", OFFICER))
        # 驳回后可以再次发起交接
        again = self.service.handoff(item["id"],
                                     {"to_actor": "carol", "reason": "retry"},
                                     "alice", OFFICER)
        self.assertEqual(again["pending_handoff"]["to_actor"], "carol")

    def test_decisions_are_one_shot(self):
        item = to_assessing(self.service, make_item(self.service, "AS-13"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        pending = self.service.handoff(item["id"],
                                       {"to_actor": "bob", "reason": "x"},
                                       "alice", OFFICER)
        hid = pending["pending_handoff"]["id"]
        self.service.decide_handoff(hid, "confirm", "bob", OFFICER2)
        with self.assertRaises(ConflictError):
            self.service.decide_handoff(hid, "reject", "bob", OFFICER2)
        with self.assertRaises(ValidationError):
            self.service.decide_handoff(hid, "maybe", "bob", OFFICER2)

    def test_cannot_open_second_pending_handoff(self):
        item = to_assessing(self.service, make_item(self.service, "AS-14"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        self.service.handoff(item["id"], {"to_actor": "bob", "reason": "x"},
                             "alice", OFFICER)
        with self.assertRaises(ConflictError):
            self.service.handoff(item["id"], {"to_actor": "carol", "reason": "y"},
                                 "alice", OFFICER)
        with self.assertRaises(ConflictError):
            self.service.claim(item["id"], {"expected_version": item["version"]},
                               "carol", OFFICER2)

    def test_handoff_unknown_id(self):
        with self.assertRaises(NotFoundError):
            self.service.decide_handoff(999, "confirm", "bob", OFFICER2)

    def test_audit_records_every_action_and_chain_holds(self):
        item = to_assessing(self.service, make_item(self.service, "AS-15"))
        self.service.claim(item["id"], {"expected_version": item["version"]},
                           "alice", OFFICER)
        pending = self.service.handoff(item["id"],
                                       {"to_actor": "bob", "reason": "shift"},
                                       "alice", OFFICER)
        hid = pending["pending_handoff"]["id"]
        self.service.decide_handoff(hid, "reject", "bob", OFFICER2)
        pending2 = self.service.handoff(item["id"],
                                        {"to_actor": "bob", "reason": "again"},
                                        "alice", OFFICER)
        self.service.decide_handoff(pending2["pending_handoff"]["id"],
                                    "confirm", "bob", OFFICER2)
        events = self.service.audit("viewer", item["id"])
        actions = [e["action"] for e in events]
        for action in ("claim", "handoff", "handoff_reject", "handoff_confirm"):
            self.assertIn(action, actions)
        handoff_event = next(e for e in events if e["action"] == "handoff")
        self.assertEqual(handoff_event["detail"]["reason"], "shift")
        self.assertEqual(handoff_event["detail"]["to_actor"], "bob")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
