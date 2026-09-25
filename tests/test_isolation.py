import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, gas_test_status
from src.service import DomainService

ADMIN = Actor("admin-1", "admin")
OPERATOR = Actor("op-1", "operator")
SAFETY = Actor("sf-1", "safety")
ENGINEER = Actor("eng-1", "engineer")
VIEWER = Actor("v-1", "viewer")


def iso_ago(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec="seconds")


class IsolationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_change(self):
        unit = self.service.create(ADMIN, "unit", {"name": "U-1", "location": "Plant-A"})
        change = self.service.create(
            ENGINEER, "change", {"unit_id": unit["id"], "description": "更换进料泵"}
        )
        self.service.transition(
            ENGINEER, change["id"], "assess", {"risk_level": "medium", "analyst": "E-1"}
        )
        return self.service.transition(
            SAFETY, change["id"], "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )

    def _isolation(self, change):
        return self.service.create(ENGINEER, "isolation", {
            "change_id": change["id"],
            "lines": [{"tag": "P-1001", "description": "进料管线"}],
            "valves": [
                {"tag": "XV-1001", "normal_position": "开", "isolated_position": "关"},
                {"tag": "XV-1002", "normal_position": "开", "isolated_position": "关+盲板"},
            ],
        })

    def _confirm_isolation(self, isolation):
        self.service.transition(OPERATOR, isolation["id"], "confirm_operator", {})
        return self.service.transition(SAFETY, isolation["id"], "confirm_safety", {})

    def _gas_test(self, change, **overrides):
        data = {"change_id": change["id"], "result": "pass", "tester": "T-1", "valid_minutes": 60}
        data.update(overrides)
        return self.service.create(SAFETY, "gas_test", data)

    def _start_work(self, change):
        return self.service.transition(
            ENGINEER, change["id"], "start_work", {"procedure_version": "v2"}
        )

    def _ready_change(self):
        change = self._approved_change()
        isolation = self._isolation(change)
        self._confirm_isolation(isolation)
        self._gas_test(change, tested_at=iso_ago(minutes=10))
        return change, isolation

    def test_isolation_requires_both_confirmations(self):
        change = self._approved_change()
        isolation = self._isolation(change)
        self.assertEqual(isolation["status"], "pending_isolation")
        step = self.service.transition(OPERATOR, isolation["id"], "confirm_operator", {})
        self.assertEqual(step["status"], "pending_isolation")
        self.assertEqual(step["data"]["operator_confirmed_by"], "op-1")
        step = self.service.transition(SAFETY, isolation["id"], "confirm_safety", {})
        self.assertEqual(step["status"], "isolated")
        self.assertEqual(step["data"]["safety_confirmed_by"], "sf-1")

    def test_withdraw_returns_to_pending_and_clears_confirmations(self):
        change = self._approved_change()
        isolation = self._isolation(change)
        self._confirm_isolation(isolation)
        step = self.service.transition(
            OPERATOR, isolation["id"], "withdraw", {"reason": "邻线阀门漏关"}
        )
        self.assertEqual(step["status"], "pending_isolation")
        self.assertIsNone(step["data"]["operator_confirmed_by"])
        self.assertIsNone(step["data"]["safety_confirmed_by"])
        self._gas_test(change)
        with self.assertRaises(ValidationError):
            self._start_work(change)

    def test_start_work_blocked_without_isolation_and_gas(self):
        change = self._approved_change()
        with self.assertRaises(ValidationError) as ctx:
            self._start_work(change)
        self.assertIn("隔离", str(ctx.exception))
        isolation = self._confirm_isolation(self._isolation(change))
        self.assertEqual(isolation["status"], "isolated")
        with self.assertRaises(ValidationError) as ctx:
            self._start_work(change)
        self.assertIn("气体检测", str(ctx.exception))
        self._gas_test(change)
        started = self._start_work(change)
        self.assertEqual(started["status"], "in_progress")

    def test_start_work_blocked_by_expired_gas_test(self):
        change = self._approved_change()
        self._confirm_isolation(self._isolation(change))
        self._gas_test(change, tested_at=iso_ago(hours=2), valid_minutes=30)
        with self.assertRaises(ValidationError) as ctx:
            self._start_work(change)
        self.assertIn("过期", str(ctx.exception))

    def test_open_action_item_blocks_start_work(self):
        change, _ = self._ready_change()
        item = self.service.create(
            SAFETY, "action_item",
            {"change_id": change["id"], "description": "更新操作规程", "owner": "O-1"},
        )
        with self.assertRaises(ValidationError) as ctx:
            self._start_work(change)
        self.assertIn("行动项", str(ctx.exception))
        self.service.transition(
            ENGINEER, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "doc-v2"},
        )
        self.assertEqual(self._start_work(change)["status"], "in_progress")

    def test_failed_retest_blocks_finish_and_flags_must_stop(self):
        change, _ = self._ready_change()
        self._start_work(change)
        self._gas_test(change, result="fail")
        blockers = self.service.blockers(change["id"])
        self.assertTrue(blockers["must_stop"])
        self.assertIn("gas_test_failed", [b["code"] for b in blockers["blockers"]])
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(ENGINEER, change["id"], "finish_work", {})
        self.assertIn("停止施工", str(ctx.exception))

    def test_suspend_and_resume_requires_valid_gas_test(self):
        change, _ = self._ready_change()
        started = self._start_work(change)
        suspended = self.service.transition(
            OPERATOR, change["id"], "suspend", {"reason": "暴雨"}
        )
        self.assertEqual(suspended["status"], "suspended")
        self._gas_test(change, result="fail")
        with self.assertRaises(ValidationError):
            self.service.transition(ENGINEER, change["id"], "resume", {})
        self._gas_test(change)
        resumed = self.service.transition(ENGINEER, change["id"], "resume", {})
        self.assertEqual(resumed["status"], "in_progress")

    def test_restore_requires_valve_checks_and_fresh_gas_test(self):
        change, isolation = self._ready_change()
        self._start_work(change)
        finished = self.service.transition(ENGINEER, change["id"], "finish_work", {})
        self.assertEqual(finished["status"], "implemented")
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(OPERATOR, change["id"], "restore", {})
        message = str(ctx.exception)
        self.assertIn("XV-1001", message)
        self.assertIn("重新气体检测", message)
        with self.assertRaises(ValidationError):
            self.service.transition(
                OPERATOR, isolation["id"], "restore_valve",
                {"tag": "XV-1001", "position": "关"},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                OPERATOR, isolation["id"], "restore_valve",
                {"tag": "XV-9999", "position": "开"},
            )
        step = self.service.transition(
            OPERATOR, isolation["id"], "restore_valve",
            {"tag": "XV-1001", "position": "开"},
        )
        self.assertEqual(step["status"], "isolated")
        step = self.service.transition(
            OPERATOR, isolation["id"], "restore_valve",
            {"tag": "XV-1002", "position": "开"},
        )
        self.assertEqual(step["status"], "restored")
        with self.assertRaises(ValidationError):
            self.service.transition(OPERATOR, change["id"], "restore", {})
        self._gas_test(change)
        restored = self.service.transition(OPERATOR, change["id"], "restore", {})
        self.assertEqual(restored["status"], "restored")

    def test_restore_valve_rejected_before_work_finished(self):
        change, isolation = self._ready_change()
        self._start_work(change)
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                OPERATOR, isolation["id"], "restore_valve",
                {"tag": "XV-1001", "position": "开"},
            )
        self.assertIn("not finished", str(ctx.exception))

    def test_commission_requires_restored_change(self):
        change, _ = self._ready_change()
        self._start_work(change)
        self.service.transition(ENGINEER, change["id"], "finish_work", {})
        with self.assertRaises(Exception) as ctx:
            self.service.transition(ENGINEER, change["id"], "commission", {"tests_passed": True})
        self.assertIn("commission", str(ctx.exception))

    def test_confirmation_roles_enforced(self):
        change = self._approved_change()
        isolation = self._isolation(change)
        with self.assertRaises(PermissionDenied):
            self.service.transition(VIEWER, isolation["id"], "confirm_operator", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(OPERATOR, isolation["id"], "confirm_safety", {})

    def test_duplicate_active_isolation_rejected(self):
        change = self._approved_change()
        self._isolation(change)
        with self.assertRaises(ConflictError):
            self._isolation(change)

    def test_blockers_report_details(self):
        change = self._approved_change()
        blockers = self.service.blockers(change["id"])
        codes = [b["code"] for b in blockers["blockers"]]
        self.assertIn("isolation_missing", codes)
        self.assertIn("gas_test_missing", codes)
        isolation = self._isolation(change)
        blockers = self.service.blockers(change["id"])
        entry = [b for b in blockers["blockers"] if b["code"] == "isolation_not_confirmed"][0]
        self.assertIn("操作员未确认", entry["items"])
        self.assertIn("安全员未确认", entry["items"])
        self._confirm_isolation(isolation)
        self._gas_test(change)
        blockers = self.service.blockers(change["id"])
        self.assertEqual(blockers["blockers"], [])
        self.assertEqual(blockers["isolation"]["status"], "isolated")
        self.assertTrue(blockers["gas_test"]["valid"])

    def test_blockers_only_for_changes(self):
        unit = self.service.create(ADMIN, "unit", {"name": "U-1", "location": "Plant-A"})
        with self.assertRaises(ValidationError):
            self.service.blockers(unit["id"])

    def test_gas_test_status_expiry(self):
        fresh = {
            "status": "recorded",
            "created_at": iso_ago(minutes=5),
            "data": {"result": "pass", "valid_minutes": 30, "tested_at": iso_ago(minutes=5)},
        }
        self.assertTrue(gas_test_status(fresh)["valid"])
        stale = {
            "status": "recorded",
            "created_at": iso_ago(hours=2),
            "data": {"result": "pass", "valid_minutes": 30, "tested_at": iso_ago(hours=2)},
        }
        info = gas_test_status(stale)
        self.assertTrue(info["expired"])
        self.assertFalse(info["valid"])
        failed = {
            "status": "recorded",
            "created_at": iso_ago(minutes=1),
            "data": {"result": "fail", "valid_minutes": 30, "tested_at": iso_ago(minutes=1)},
        }
        self.assertFalse(gas_test_status(failed)["valid"])


if __name__ == "__main__":
    unittest.main()
