import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "handover item", "description": "claim and transfer",
             "severity": 'exceedance', "quantity": 5, "threshold": 10,
             "external_ref": "HO-1"}, "creator", 'operator')
        self.assessing = self.service.transition(
            self.item["id"], STATES[1], self.item["version"],
            "reviewer", 'compliance_officer')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_claim_by_current_version_and_duplicate_rejected(self):
        claimed = self.service.claim(
            self.item["id"], self.assessing["version"], "alice", 'compliance_officer')
        self.assertEqual(claimed["owner"], "alice")
        self.assertEqual(claimed["version"], self.assessing["version"] + 1)
        with self.assertRaises(ConflictError):
            self.service.claim(self.item["id"], claimed["version"], "bob", 'compliance_officer')

    def test_stale_version_and_wrong_state_and_role_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.claim(self.item["id"], self.assessing["version"] - 1,
                               "alice", 'compliance_officer')
        fresh = self.service.create_item(
            {"title": "fresh", "description": "still reported",
             "severity": 'watch', "quantity": 1, "threshold": 10}, "creator", 'operator')
        with self.assertRaises(ConflictError):
            self.service.claim(fresh["id"], fresh["version"], "alice", 'compliance_officer')
        with self.assertRaises(PermissionDenied):
            self.service.claim(self.item["id"], self.assessing["version"],
                               "viewer", 'viewer')

    def test_transfer_requires_reason_and_owner_only(self):
        self.service.claim(self.item["id"], self.assessing["version"],
                           "alice", 'compliance_officer')
        with self.assertRaises(ValidationError):
            self.service.transfer(self.item["id"], {"to_actor": "bob"},
                                  "alice", 'compliance_officer')
        with self.assertRaises(PermissionDenied):
            self.service.transfer(self.item["id"], {"to_actor": "carol", "reason": "r"},
                                  "bob", 'compliance_officer')
        with self.assertRaises(ValidationError):
            self.service.transfer(self.item["id"], {"to_actor": "alice", "reason": "r"},
                                  "alice", 'compliance_officer')

    def test_confirm_changes_owner_and_version(self):
        claimed = self.service.claim(self.item["id"], self.assessing["version"],
                                     "alice", 'compliance_officer')
        transfer = self.service.transfer(
            self.item["id"], {"to_actor": "bob", "reason": "工作量交接"},
            "alice", 'compliance_officer')
        self.assertEqual(transfer["status"], "pending")
        listed = self.service.list_items('viewer')
        row = next(r for r in listed if r["id"] == self.item["id"])
        self.assertEqual(row["owner"], "alice")
        self.assertIsNotNone(row["pending_transfer"])
        self.assertEqual(row["pending_transfer"]["to_actor"], "bob")

        confirmed = self.service.confirm_transfer(transfer["id"], "bob",
                                                  'compliance_officer')
        self.assertEqual(confirmed["status"], "confirmed")
        current = self.service.get_item(self.item["id"], 'viewer')
        self.assertEqual(current["owner"], "bob")
        self.assertEqual(current["version"], claimed["version"] + 1)
        self.assertIsNone(current["pending_transfer"])

    def test_reject_keeps_original_owner(self):
        self.service.claim(self.item["id"], self.assessing["version"],
                           "alice", 'compliance_officer')
        transfer = self.service.transfer(
            self.item["id"], {"to_actor": "bob", "reason": "请接手"},
            "alice", 'compliance_officer')
        rejected = self.service.reject_transfer(transfer["id"], "bob",
                                                'compliance_officer')
        self.assertEqual(rejected["status"], "rejected")
        current = self.service.get_item(self.item["id"], 'viewer')
        self.assertEqual(current["owner"], "alice")
        self.assertIsNone(current["pending_transfer"])
        # original owner can continue working and re-transfer
        self.service.add_record(self.item["id"], {"kind": "note", "detail": "继续处置"},
                                "alice", 'compliance_officer')
        again = self.service.transfer(
            self.item["id"], {"to_actor": "carol", "reason": "再次交接"},
            "alice", 'compliance_officer')
        self.assertEqual(again["status"], "pending")

    def test_only_receiver_can_decide_and_decision_is_once(self):
        self.service.claim(self.item["id"], self.assessing["version"],
                           "alice", 'compliance_officer')
        transfer = self.service.transfer(
            self.item["id"], {"to_actor": "bob", "reason": "r"},
            "alice", 'compliance_officer')
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(transfer["id"], "carol", 'compliance_officer')
        self.service.confirm_transfer(transfer["id"], "bob", 'compliance_officer')
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(transfer["id"], "bob", 'compliance_officer')

    def test_record_requires_owner_once_assessing(self):
        # unclaimed: nobody may submit disposal opinions
        with self.assertRaises(ConflictError):
            self.service.add_record(self.item["id"], {"kind": "note", "detail": "x"},
                                    "alice", 'compliance_officer')
        self.service.claim(self.item["id"], self.assessing["version"],
                           "alice", 'compliance_officer')
        # non-owner rejected
        with self.assertRaises(PermissionDenied):
            self.service.add_record(self.item["id"], {"kind": "note", "detail": "x"},
                                    "bob", 'compliance_officer')
        # owner accepted
        self.service.add_record(self.item["id"], {"kind": "note", "detail": "owner ok"},
                                "alice", 'compliance_officer')

    def test_audit_trail_and_chain(self):
        self.service.claim(self.item["id"], self.assessing["version"],
                           "alice", 'compliance_officer')
        transfer = self.service.transfer(
            self.item["id"], {"to_actor": "bob", "reason": "审计交接"},
            "alice", 'compliance_officer')
        self.service.confirm_transfer(transfer["id"], "bob", 'compliance_officer')
        actions = [e["action"] for e in self.service.audit('viewer', self.item["id"])]
        for expected in ("claim", "transfer", "confirm"):
            self.assertIn(expected, actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
