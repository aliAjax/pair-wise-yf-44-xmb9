import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _future(hours=4):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.operator = Actor("op-1", "operator")
        self.safety = Actor("sf-1", "safety")
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        self.change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "replace pump seal"},
        )
        for action, entity, payload in [
            ("assess", self.change, {"risk_level": "low", "analyst": "e-1"}),
            ("approve", None, {"approvals": ["s-1"], "permit_id": "MOC-9"}),
        ]:
            target = entity or self.service.get(self.change["id"])
            self.change = self.service.transition(
                self.admin, target["id"], action, payload
            )
        self.item = self.service.create(
            self.admin,
            "action_item",
            {"change_id": self.change["id"], "description": "blind flange", "owner": "o-2"},
        )
        self.isolation = self.service.create(
            self.admin,
            "isolation",
            {
                "change_id": self.change["id"],
                "lines": ["feed-A", "return-B"],
                "valves": ["HV-1", "HV-2"],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _blocker_codes(self, entity_id=None):
        return {
            item["code"]
            for item in self.service.blockers(entity_id or self.isolation["id"])
        }

    def _fully_isolate(self):
        self.service.transition(
            self.operator, self.isolation["id"], "confirm_operator", {}
        )
        self.service.transition(
            self.safety, self.isolation["id"], "confirm_safety", {}
        )
        self.isolation = self.service.get(self.isolation["id"])
        self.assertEqual(self.isolation["status"], "isolated")

    def test_registration_requires_lines_and_valves(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "isolation",
                {"change_id": self.change["id"], "lines": ["x"], "valves": []},
            )

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "isolation",
                {
                    "change_id": self.change["id"],
                    "lines": ["feed-A"],
                    "valves": ["HV-3"],
                },
            )

    def test_confirmations_are_role_separated(self):
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, self.isolation["id"], "confirm_safety", {}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.safety, self.isolation["id"], "confirm_operator", {}
            )
        codes = self._blocker_codes()
        self.assertEqual(
            codes, {"operator_confirm_required", "safety_confirm_required"}
        )

    def test_withdraw_returns_to_pending_and_invalidates_gas_test(self):
        self._fully_isolate()
        self.service.transition(
            self.operator,
            self.isolation["id"],
            "gas_test",
            {"tested_by": "op-1", "passed": True, "valid_until": _future()},
        )
        withdrawn = self.service.transition(
            self.safety,
            self.isolation["id"],
            "withdraw",
            {"reason": "发现邻线阀门未关"},
        )
        self.assertEqual(withdrawn["status"], "pending")
        self.assertIsNone(withdrawn["data"].get("safety_confirmed"))
        self.assertIsNotNone(withdrawn["data"].get("operator_confirmed"))
        self.assertIsNone(withdrawn["data"].get("gas_test"))
        self.assertIn("safety_confirm_required", self._blocker_codes())

    def test_start_work_blocked_until_gas_test_and_action_items_clear(self):
        self._fully_isolate()
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.operator, self.isolation["id"], "start_work", {}
            )
        message = str(ctx.exception)
        self.assertIn("气体检测", message)
        self.assertIn(self.item["id"], message)

    def test_expired_gas_test_rejected_and_stops_live_work(self):
        self._fully_isolate()
        # 有效期在过去 -> 不能登记为合格检测
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.operator,
                self.isolation["id"],
                "gas_test",
                {"tested_by": "op-1", "passed": True, "valid_until": _past()},
            )
        # 模拟检测登记时有效、施工期间过期（行动项仍未验证）
        working_entity = {
            "kind": "isolation",
            "id": self.isolation["id"],
            "status": "work_in_progress",
            "data": {
                "change_id": self.change["id"],
                "valves": ["HV-1", "HV-2"],
                "gas_test": {"tested_by": "op-1", "valid_until": _past()},
            },
        }
        codes = {
            item["code"]
            for item in self.service.rules.blockers(
                working_entity, self.service._lookup
            )
        }
        self.assertIn("gas_test_expired", codes)
        self.assertIn("action_item_unverified", codes)
        self.assertIn("restoration_test_required", codes)
        self.assertIn("valve_check_incomplete", codes)

    def test_stop_work_clears_gas_tests(self):
        self._fully_isolate()
        self.service.transition(
            self.admin,
            self.item["id"],
            "complete",
            {"completed_by": "o-2", "evidence": "photo"},
        )
        self.service.transition(
            self.admin, self.item["id"], "verify", {"verifier": "v-1"}
        )
        self.service.transition(
            self.operator,
            self.isolation["id"],
            "gas_test",
            {"tested_by": "op-1", "passed": True, "valid_until": _future()},
        )
        working = self.service.transition(
            self.operator, self.isolation["id"], "start_work", {}
        )
        self.assertEqual(working["status"], "work_in_progress")
        stopped = self.service.transition(
            self.safety,
            self.isolation["id"],
            "stop_work",
            {"reason": "检测过期"},
        )
        self.assertEqual(stopped["status"], "isolated")
        self.assertIsNone(stopped["data"].get("gas_test"))
        self.assertIn("gas_test_required", self._blocker_codes())

    def test_restore_requires_retest_and_valve_by_valve_check(self):
        self._fully_isolate()
        self.service.transition(
            self.admin,
            self.item["id"],
            "complete",
            {"completed_by": "o-2", "evidence": "photo"},
        )
        self.service.transition(
            self.admin, self.item["id"], "verify", {"verifier": "v-1"}
        )
        self.service.transition(
            self.operator,
            self.isolation["id"],
            "gas_test",
            {"tested_by": "op-1", "passed": True, "valid_until": _future(8)},
        )
        working = self.service.transition(
            self.operator, self.isolation["id"], "start_work", {}
        )
        # 未重新检测不能恢复
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.operator,
                self.isolation["id"],
                "restore_feed",
                {"valve_checks": [
                    {"valve": "HV-1", "checked_by": "op-1"},
                    {"valve": "HV-2", "checked_by": "op-1"},
                ]},
            )
        self.assertIn("重新进行气体检测", str(ctx.exception))

        self.service.transition(
            self.operator,
            self.isolation["id"],
            "retest",
            {"tested_by": "op-1", "passed": True, "valid_until": _future(2)},
        )
        # 漏核对一个阀门
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.operator,
                self.isolation["id"],
                "restore_feed",
                {"valve_checks": [{"valve": "HV-1", "checked_by": "op-1"}]},
            )
        self.assertIn("HV-2", str(ctx.exception))
        # 核对清单外的阀门
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.operator,
                self.isolation["id"],
                "restore_feed",
                {"valve_checks": [
                    {"valve": "HV-1", "checked_by": "op-1"},
                    {"valve": "HV-2", "checked_by": "op-1"},
                    {"valve": "HV-X", "checked_by": "op-1"},
                ]},
            )
        self.assertIn("HV-X", str(ctx.exception))

        restored = self.service.transition(
            self.operator,
            self.isolation["id"],
            "restore_feed",
            {"valve_checks": [
                {"valve": "HV-1", "checked_by": "op-1"},
                {"valve": "HV-2", "checked_by": "op-1"},
            ]},
        )
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.service.blockers(self.change["id"]), [])

    def test_change_implement_blocked_without_restored_isolation(self):
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.admin,
                self.change["id"],
                "implement",
                {"procedure_version": "v1"},
            )
        self.assertIn("待隔离", str(ctx.exception))
        blockers = {
            item["code"] for item in self.service.blockers(self.change["id"])
        }
        self.assertEqual(blockers, {"isolation_not_restored"})


if __name__ == "__main__":
    unittest.main()
