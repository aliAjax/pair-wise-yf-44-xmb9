from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_dt(value):
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


def _normalize_lines(lines):
    normalized = []
    for index, line in enumerate(lines or []):
        if isinstance(line, str):
            line = {"tag": line}
        line = line or {}
        tag = str(line.get("tag", "")).strip()
        if not tag:
            raise ValidationError("line #%d is missing tag" % (index + 1))
        normalized.append(
            {"tag": tag, "description": str(line.get("description", "")).strip()}
        )
    if not normalized:
        raise ValidationError("affected lines are required")
    return normalized


def _normalize_valves(valves):
    normalized = []
    seen = set()
    for index, valve in enumerate(valves or []):
        if isinstance(valve, str):
            valve = {"tag": valve}
        valve = valve or {}
        tag = str(valve.get("tag", "")).strip()
        if not tag:
            raise ValidationError("valve #%d is missing tag" % (index + 1))
        if tag in seen:
            raise ValidationError("duplicate valve tag: " + tag)
        seen.add(tag)
        normal = str(valve.get("normal_position", "")).strip()
        isolated = str(valve.get("isolated_position", "")).strip()
        if not normal or not isolated:
            raise ValidationError(
                "valve %s requires normal_position and isolated_position" % tag
            )
        normalized.append(
            {"tag": tag, "normal_position": normal, "isolated_position": isolated}
        )
    if not normalized:
        raise ValidationError("valves to isolate are required")
    return normalized


def _validate_isolation(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    existing = lookup("isolation", "change_id", change["id"]) or [] if lookup else []
    active = [
        row for row in existing if row["status"] in ("pending_isolation", "isolated")
    ]
    if active:
        raise ConflictError("active isolation already exists: " + active[0]["id"])
    return {
        "lines": _normalize_lines(data.get("lines")),
        "valves": _normalize_valves(data.get("valves")),
    }


def _validate_gas_test(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    result = str(data.get("result", "")).strip().lower()
    if result not in ("pass", "fail"):
        raise ValidationError("result must be pass or fail")
    try:
        minutes = int(data.get("valid_minutes", 30))
    except (TypeError, ValueError):
        raise ValidationError("valid_minutes must be a positive integer")
    if minutes <= 0:
        raise ValidationError("valid_minutes must be a positive integer")
    tested_at = str(data.get("tested_at") or "").strip() or _now_iso()
    try:
        _parse_dt(tested_at)
    except ValueError:
        raise ValidationError("tested_at must be an ISO datetime")
    return {"result": result, "valid_minutes": minutes, "tested_at": tested_at}


def _validate_confirm_operator(actor, entity, data, lookup):
    patch = {
        "operator_confirmed_by": actor.user_id,
        "operator_confirmed_at": _now_iso(),
    }
    if entity["data"].get("safety_confirmed_by"):
        patch["__status__"] = "isolated"
        patch["isolated_at"] = _now_iso()
    return patch


def _validate_confirm_safety(actor, entity, data, lookup):
    patch = {
        "safety_confirmed_by": actor.user_id,
        "safety_confirmed_at": _now_iso(),
    }
    if entity["data"].get("operator_confirmed_by"):
        patch["__status__"] = "isolated"
        patch["isolated_at"] = _now_iso()
    return patch


def _validate_withdraw(actor, entity, data, lookup):
    return {
        "operator_confirmed_by": None,
        "operator_confirmed_at": None,
        "safety_confirmed_by": None,
        "safety_confirmed_at": None,
        "withdrawn_by": actor.user_id,
        "withdrawn_at": _now_iso(),
        "withdraw_reason": data.get("reason"),
    }


def _validate_restore_valve(actor, entity, data, lookup):
    change = _find_one(lookup, "change", "id", entity["data"].get("change_id"))
    if not change or change["status"] != "implemented":
        raise ValidationError("change work is not finished; cannot restore valves")
    tag = str(data.get("tag", "")).strip()
    valves = {valve["tag"]: valve for valve in entity["data"].get("valves", [])}
    if tag not in valves:
        raise ValidationError("unknown valve tag: " + tag)
    position = str(data.get("position", "")).strip()
    expected = valves[tag]["normal_position"]
    if position != expected:
        raise ValidationError(
            "valve %s is %s, expected normal position %s" % (tag, position, expected)
        )
    restored = dict(entity["data"].get("restored_valves") or {})
    restored[tag] = {"position": position, "by": actor.user_id, "at": _now_iso()}
    patch = {"restored_valves": restored}
    if len(restored) == len(valves):
        patch["__status__"] = "restored"
        patch["restored_at"] = _now_iso()
    return patch


def _latest_isolation(lookup, change_id):
    rows = lookup("isolation", "change_id", change_id) or [] if lookup else []
    if not rows:
        return None
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))[-1]


def _latest_gas_test(lookup, change_id):
    tests = lookup("gas_test", "change_id", change_id) or [] if lookup else []
    tests = [test for test in tests if test["status"] != "void"]
    if not tests:
        return None
    return sorted(tests, key=lambda row: (row["created_at"], row["id"]))[-1]


def gas_test_status(test, now=None):
    now = now or datetime.now(timezone.utc)
    tested_at = _parse_dt(test["data"].get("tested_at") or test["created_at"])
    expires_at = tested_at + timedelta(minutes=int(test["data"].get("valid_minutes", 30)))
    passed = test["data"].get("result") == "pass"
    expired = now > expires_at
    return {
        "result": test["data"].get("result"),
        "tested_at": tested_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "passed": passed,
        "expired": expired,
        "valid": passed and not expired,
    }


def _open_action_items(lookup, change_id):
    items = lookup("action_item", "change_id", change_id) or [] if lookup else []
    return [item for item in items if item["status"] == "open"]


def _work_blockers(change, lookup, now=None):
    blockers = []
    isolation = _latest_isolation(lookup, change["id"])
    if not isolation:
        blockers.append({
            "code": "isolation_missing",
            "message": "未登记隔离方案（受影响管线和阀门）",
            "items": [],
        })
    elif isolation["status"] != "isolated":
        data = isolation["data"]
        missing = []
        if not data.get("operator_confirmed_by"):
            missing.append("操作员未确认")
        if not data.get("safety_confirmed_by"):
            missing.append("安全员未确认")
        if not missing:
            missing.append("隔离状态为 %s" % isolation["status"])
        blockers.append({
            "code": "isolation_not_confirmed",
            "message": "隔离未确认完成：" + "、".join(missing),
            "items": missing,
        })
    test = _latest_gas_test(lookup, change["id"])
    if not test:
        blockers.append({
            "code": "gas_test_missing",
            "message": "无气体检测记录",
            "items": [],
        })
    else:
        info = gas_test_status(test, now)
        if not info["passed"]:
            blockers.append({
                "code": "gas_test_failed",
                "message": "最近一次气体检测不合格",
                "items": [test["id"]],
            })
        elif info["expired"]:
            blockers.append({
                "code": "gas_test_expired",
                "message": "气体检测已过期（有效期至 %s）" % info["expires_at"],
                "items": [test["id"]],
            })
    open_items = _open_action_items(lookup, change["id"])
    if open_items:
        labels = [
            "%s(%s)" % (item["data"].get("description") or item["id"], item["status"])
            for item in open_items
        ]
        blockers.append({
            "code": "action_items_open",
            "message": "关联行动项未完成：" + "、".join(labels),
            "items": [item["id"] for item in open_items],
        })
    return blockers


def _restore_blockers(change, lookup, now=None):
    blockers = []
    isolation = _latest_isolation(lookup, change["id"])
    if not isolation or isolation["status"] not in ("isolated", "restored"):
        blockers.append({
            "code": "isolation_not_ready",
            "message": "隔离方案未处于待恢复状态",
            "items": [],
        })
    elif isolation["status"] == "isolated":
        restored = isolation["data"].get("restored_valves") or {}
        pending = [
            valve["tag"]
            for valve in isolation["data"].get("valves", [])
            if valve["tag"] not in restored
        ]
        blockers.append({
            "code": "valves_pending",
            "message": "阀门未逐阀核对完成：" + "、".join(pending),
            "items": pending,
        })
    test = _latest_gas_test(lookup, change["id"])
    finished_at = change["data"].get("work_finished_at")
    if not test:
        blockers.append({
            "code": "gas_test_missing",
            "message": "恢复供料前未重新气体检测",
            "items": [],
        })
    else:
        info = gas_test_status(test, now)
        if not info["passed"]:
            blockers.append({
                "code": "gas_test_failed",
                "message": "最近一次气体检测不合格",
                "items": [test["id"]],
            })
        elif info["expired"]:
            blockers.append({
                "code": "gas_test_expired",
                "message": "气体检测已过期（有效期至 %s）" % info["expires_at"],
                "items": [test["id"]],
            })
        elif not finished_at or _parse_dt(info["tested_at"]) < _parse_dt(finished_at):
            blockers.append({
                "code": "gas_test_stale",
                "message": "完工后未重新气体检测，恢复供料前须重新检测",
                "items": [test["id"]],
            })
    return blockers


def _raise_blockers(prefix, blockers):
    raise ValidationError(prefix + "：" + "；".join(item["message"] for item in blockers))


def _validate_start_work(actor, entity, data, lookup):
    blockers = _work_blockers(entity, lookup)
    if blockers:
        _raise_blockers("开工条件不满足", blockers)
    return {"started_by": actor.user_id, "started_at": _now_iso()}


def _validate_suspend(actor, entity, data, lookup):
    return {"suspended_by": actor.user_id, "suspended_at": _now_iso()}


def _validate_resume(actor, entity, data, lookup):
    blockers = _work_blockers(entity, lookup)
    if blockers:
        _raise_blockers("复工条件不满足", blockers)
    return {"resumed_by": actor.user_id, "resumed_at": _now_iso()}


def _validate_finish_work(actor, entity, data, lookup):
    blockers = _work_blockers(entity, lookup)
    if blockers:
        _raise_blockers("存在阻塞项，应停止施工", blockers)
    return {"work_finished_by": actor.user_id, "work_finished_at": _now_iso()}


def _validate_restore(actor, entity, data, lookup):
    blockers = _restore_blockers(entity, lookup)
    if blockers:
        _raise_blockers("恢复条件不满足", blockers)
    return {"feed_restored_by": actor.user_id, "feed_restored_at": _now_iso()}


def _isolation_summary(isolation):
    if not isolation:
        return None
    data = isolation["data"]
    restored = data.get("restored_valves") or {}
    return {
        "id": isolation["id"],
        "status": isolation["status"],
        "operator_confirmed_by": data.get("operator_confirmed_by"),
        "safety_confirmed_by": data.get("safety_confirmed_by"),
        "lines": data.get("lines", []),
        "valves": [
            {
                "tag": valve["tag"],
                "normal_position": valve["normal_position"],
                "isolated_position": valve["isolated_position"],
                "restored": valve["tag"] in restored,
                "restored_by": (restored.get(valve["tag"]) or {}).get("by"),
            }
            for valve in data.get("valves", [])
        ],
    }


def compute_change_blockers(change, lookup, now=None):
    now = now or datetime.now(timezone.utc)
    status = change["status"]
    blockers = []
    must_stop = False
    if status in ("approved", "suspended"):
        blockers = _work_blockers(change, lookup, now)
    elif status == "in_progress":
        blockers = _work_blockers(change, lookup, now)
        must_stop = bool(blockers)
    elif status == "implemented":
        blockers = _restore_blockers(change, lookup, now)
    elif status == "restored":
        items = lookup("action_item", "change_id", change["id"]) or [] if lookup else []
        unverified = [item for item in items if item["status"] != "verified"]
        if unverified:
            blockers = [{
                "code": "action_items_unverified",
                "message": "行动项未全部验证：" + "、".join(item["id"] for item in unverified),
                "items": [item["id"] for item in unverified],
            }]
    elif status in ("draft", "assessed"):
        blockers = [{
            "code": "pending_approval",
            "message": "变更尚未审批通过",
            "items": [],
        }]
    test = _latest_gas_test(lookup, change["id"])
    items = lookup("action_item", "change_id", change["id"]) or [] if lookup else []
    return {
        "change_id": change["id"],
        "status": status,
        "must_stop": must_stop,
        "blockers": blockers,
        "isolation": _isolation_summary(_latest_isolation(lookup, change["id"])),
        "gas_test": (
            dict(
                {"id": test["id"], "tester": test["data"].get("tester")},
                **gas_test_status(test, now),
            )
            if test
            else None
        ),
        "action_items": [
            {
                "id": item["id"],
                "status": item["status"],
                "description": item["data"].get("description"),
                "owner": item["data"].get("owner"),
            }
            for item in items
        ],
    }


CUSTOM_CREATE = {
    "change": _validate_change,
    "isolation": _validate_isolation,
    "gas_test": _validate_gas_test,
}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "approve"): _validate_approve,
    ("change", "start_work"): _validate_start_work,
    ("change", "suspend"): _validate_suspend,
    ("change", "resume"): _validate_resume,
    ("change", "finish_work"): _validate_finish_work,
    ("change", "restore"): _validate_restore,
    ("change", "commission"): _validate_commission,
    ("isolation", "confirm_operator"): _validate_confirm_operator,
    ("isolation", "confirm_safety"): _validate_confirm_safety,
    ("isolation", "withdraw"): _validate_withdraw,
    ("isolation", "restore_valve"): _validate_restore_valve,
}


class RuleEngine:
    ALIASES = {
        "units": "unit",
        "changes": "change",
        "action_items": "action_item",
        "isolations": "isolation",
        "gas_tests": "gas_test",
    }
    INITIAL_STATUS = {
        "unit": "operating",
        "change": "draft",
        "action_item": "open",
        "isolation": "pending_isolation",
        "gas_test": "recorded",
    }
    TRANSITIONS = {
        "unit": {
            "shutdown": (("operating",), "shutdown"),
            "startup": (("shutdown",), "operating"),
            "freeze": (("operating",), "frozen"),
            "unfreeze": (("frozen",), "operating"),
        },
        "change": {
            "assess": (("draft",), "assessed"),
            "approve": (("assessed",), "approved"),
            "start_work": (("approved",), "in_progress"),
            "suspend": (("in_progress",), "suspended"),
            "resume": (("suspended",), "in_progress"),
            "finish_work": (("in_progress",), "implemented"),
            "restore": (("implemented",), "restored"),
            "commission": (("restored",), "commissioned"),
            "rollback": (
                ("in_progress", "suspended", "implemented", "restored", "commissioned"),
                "rolled_back",
            ),
            "close": (("rolled_back",), "closed"),
        },
        "action_item": {
            "complete": (("open",), "completed"),
            "verify": (("completed",), "verified"),
            "reopen": (("verified",), "open"),
        },
        "isolation": {
            "confirm_operator": (("pending_isolation",), "pending_isolation"),
            "confirm_safety": (("pending_isolation",), "pending_isolation"),
            "withdraw": (("pending_isolation", "isolated"), "pending_isolation"),
            "restore_valve": (("isolated",), "isolated"),
        },
        "gas_test": {
            "void": (("recorded",), "void"),
        },
    }
    CREATE_REQUIRED = {
        "unit": ("name", "location"),
        "change": ("unit_id", "description"),
        "action_item": ("change_id", "description", "owner"),
        "isolation": ("change_id", "lines", "valves"),
        "gas_test": ("change_id", "result", "tester"),
    }
    ACTION_REQUIRED = {
        ("unit", "shutdown"): ("reason",),
        ("unit", "freeze"): ("reason",),
        ("change", "assess"): ("risk_level", "analyst"),
        ("change", "approve"): ("approvals", "permit_id"),
        ("change", "start_work"): ("procedure_version",),
        ("change", "suspend"): ("reason",),
        ("change", "commission"): ("tests_passed",),
        ("change", "rollback"): ("reason",),
        ("change", "close"): ("outcome",),
        ("action_item", "complete"): ("completed_by", "evidence"),
        ("action_item", "verify"): ("verifier",),
        ("action_item", "reopen"): ("reason",),
        ("isolation", "withdraw"): ("reason",),
        ("isolation", "restore_valve"): ("tag", "position"),
        ("gas_test", "void"): ("reason",),
    }
    CREATE_ROLES = {
        "unit": ("admin", "engineer"),
        "change": ("admin", "engineer"),
        "action_item": ("admin", "safety"),
        "isolation": ("admin", "engineer"),
        "gas_test": ("admin", "safety", "operator"),
    }
    ROLE_ACTIONS = {
        "shutdown": ("admin", "operator"),
        "startup": ("admin", "operator"),
        "freeze": ("admin", "operator"),
        "unfreeze": ("admin", "operator"),
        "assess": ("admin", "engineer"),
        "approve": ("admin", "safety"),
        "start_work": ("admin", "engineer"),
        "suspend": ("admin", "engineer", "operator", "safety"),
        "resume": ("admin", "engineer"),
        "finish_work": ("admin", "engineer"),
        "restore": ("admin", "operator"),
        "commission": ("admin", "engineer"),
        "rollback": ("admin", "engineer"),
        "close": ("admin", "safety"),
        "complete": ("admin", "engineer"),
        "verify": ("admin", "verifier"),
        "reopen": ("admin", "verifier"),
        "confirm_operator": ("admin", "operator"),
        "confirm_safety": ("admin", "safety"),
        "withdraw": ("admin", "operator", "safety"),
        "restore_valve": ("admin", "operator"),
        "void": ("admin", "safety"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            extra = custom(actor, data, lookup)
            if extra:
                data.update(extra)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra and "__status__" in extra:
            next_status = extra.pop("__status__")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
