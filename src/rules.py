from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


STATUS_LABELS = {
    "pending": "待隔离",
    "isolated": "已隔离",
    "work_in_progress": "施工中",
    "restored": "已恢复供料",
}

# 开工前必须满足的阻塞项编码
WORK_READINESS_CODES = {
    "operator_confirm_required",
    "safety_confirm_required",
    "gas_test_required",
    "gas_test_expired",
    "action_item_unverified",
}
# 施工期间必须停工的阻塞项编码
WORK_STOP_CODES = {
    "gas_test_expired",
    "action_item_unverified",
}
# 恢复供料前必须解除的阻塞项编码
RESTORE_GATE_CODES = WORK_STOP_CODES | {
    "restoration_test_required",
    "restoration_test_expired",
}


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat(timespec="seconds")


def _parse_ts(value):
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clean_string_list(value):
    """归一化管线/阀门登记项：去空白、去重，拒绝非列表输入。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _unverified_action_items(lookup, change_id):
    if lookup is None or not change_id:
        return []
    items = lookup("action_item", "change_id", change_id) or []
    return [
        {"id": item["id"], "status": item["status"]}
        for item in items
        if item["status"] != "verified"
    ]


def _gas_expired(test, now=None):
    if not test:
        return False
    try:
        return _parse_ts(test.get("valid_until")) <= (now or _now())
    except (ValueError, TypeError):
        return True


def _record_gas_test(data, field):
    """校验一次气体检测并生成要写入实体的检测记录。"""
    if data.get("passed") is not True:
        raise ValidationError("气体检测不合格，不能作为合格检测记录")
    tested_by = str(data.get("tested_by", "")).strip()
    if not tested_by:
        raise ValidationError("missing required field: tested_by")
    raw_valid_until = data.get("valid_until")
    try:
        expiry = _parse_ts(raw_valid_until)
    except (ValueError, TypeError):
        raise ValidationError(
            "valid_until 必须是 ISO 时间，例如 2026-09-25T12:00:00+00:00"
        )
    if expiry <= _now():
        raise ValidationError("气体检测有效期已过，请重新检测")
    return {
        field: {
            "tested_by": tested_by,
            "tested_at": _now_iso(),
            "valid_until": str(raw_valid_until),
        }
    }


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


def _validate_implement(actor, entity, data, lookup):
    """变更实施前，隔离与恢复供料必须已经闭环。"""
    isolations = lookup("isolation", "change_id", entity["id"]) if lookup else []
    if not isolations:
        raise ValidationError("尚未登记隔离单，施工前必须先登记受影响管线与阀门")
    isolation = isolations[0]
    if isolation["status"] != "restored":
        label = STATUS_LABELS.get(isolation["status"], isolation["status"])
        raise ValidationError(
            "隔离单 %s 当前为「%s」，需完成隔离、检测、开工与恢复供料后才能实施"
            % (isolation["id"], label)
        )
    return {}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


def _validate_isolation_create(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    lines = _clean_string_list(data.get("lines"))
    valves = _clean_string_list(data.get("valves"))
    if not lines:
        raise ValidationError("受影响管线不能为空")
    if not valves:
        raise ValidationError("受影响阀门不能为空")
    existing = lookup("isolation", "change_id", change["id"]) if lookup else []
    if existing:
        raise ValidationError("该变更已登记隔离单：" + existing[0]["id"])
    data["lines"] = lines
    data["valves"] = valves
    return data


def _validate_confirm(actor, entity, data, lookup, role_key, label, other_key):
    field = role_key + "_confirmed"
    if entity["data"].get(field):
        raise ValidationError(label + "已确认；如需改变意见请先撤回")
    patch = {field: {"by": actor.user_id, "at": _now_iso()}}
    if entity["data"].get(other_key + "_confirmed"):
        # 双方都已确认，隔离正式生效
        return "isolated", patch
    return patch


def _validate_confirm_operator(actor, entity, data, lookup):
    return _validate_confirm(
        actor, entity, data, lookup, "operator", "操作员", "safety"
    )


def _validate_confirm_safety(actor, entity, data, lookup):
    return _validate_confirm(
        actor, entity, data, lookup, "safety", "安全员", "operator"
    )


def _validate_withdraw(actor, entity, data, lookup):
    """操作员或安全员任一方撤回，隔离立即回到待隔离，既往检测作废。"""
    field = "operator_confirmed" if actor.role == "operator" else "safety_confirmed"
    label = "操作员" if actor.role == "operator" else "安全员"
    if not entity["data"].get(field):
        raise ValidationError(label + "尚未确认，无需撤回")
    return "pending", {
        field: None,
        "gas_test": None,
        "restoration_gas_test": None,
        "last_withdraw": {
            "by": actor.user_id,
            "role": actor.role,
            "reason": str(data.get("reason", "")).strip(),
            "at": _now_iso(),
        },
    }


def _validate_gas_test(actor, entity, data, lookup):
    return _record_gas_test(data, "gas_test")


def _validate_start_work(actor, entity, data, lookup):
    blockers = [
        item
        for item in _isolation_blockers(entity, lookup)
        if item["code"] in WORK_READINESS_CODES
    ]
    if blockers:
        raise ValidationError(
            "无法开工：" + "；".join(item["message"] for item in blockers)
        )
    return {"started_by": actor.user_id, "work_started_at": _now_iso()}


def _validate_stop_work(actor, entity, data, lookup):
    """停工后隔离边界与检测均需重新确认/检测，因此清空检测记录。"""
    return "isolated", {
        "gas_test": None,
        "restoration_gas_test": None,
        "last_stop": {
            "by": actor.user_id,
            "reason": str(data.get("reason", "")).strip(),
            "at": _now_iso(),
        },
    }


def _validate_retest(actor, entity, data, lookup):
    live = [
        item
        for item in _isolation_blockers(entity, lookup)
        if item["code"] in WORK_STOP_CODES
    ]
    if live:
        raise ValidationError(
            "施工存在未解除阻塞，请先停止施工："
            + "；".join(item["message"] for item in live)
        )
    return _record_gas_test(data, "restoration_gas_test")


def _validate_valve_checks(data, registered_valves):
    raw = data.get("valve_checks")
    if not isinstance(raw, list):
        raise ValidationError("valve_checks 必须是逐阀核对记录列表")
    normalized = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValidationError("每条核对记录必须包含 valve 与 checked_by")
        valve = str(entry.get("valve", "")).strip()
        checker = str(entry.get("checked_by", "")).strip()
        if not valve:
            raise ValidationError("存在缺少阀门编号的核对记录")
        if valve in seen:
            raise ValidationError("阀门 %s 被重复核对" % valve)
        seen.add(valve)
        if valve not in registered_valves:
            raise ValidationError("阀门 %s 不在隔离登记清单中" % valve)
        if not checker:
            raise ValidationError("阀门 %s 缺少核对人" % valve)
        normalized.append({"valve": valve, "checked_by": checker})
    missing = [valve for valve in registered_valves if valve not in seen]
    if missing:
        raise ValidationError("尚待逐阀核对：" + "、".join(missing))
    return normalized


def _validate_restore_feed(actor, entity, data, lookup):
    blocking = [
        item
        for item in _isolation_blockers(entity, lookup)
        if item["code"] in RESTORE_GATE_CODES
    ]
    if blocking:
        raise ValidationError(
            "无法恢复供料：" + "；".join(item["message"] for item in blocking)
        )
    checks = _validate_valve_checks(data, entity["data"].get("valves", []))
    return {
        "valve_checks": checks,
        "restored_by": actor.user_id,
        "restored_at": _now_iso(),
    }


CUSTOM_CREATE = {"change": _validate_change, "isolation": _validate_isolation_create}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "approve"): _validate_approve,
    ("change", "implement"): _validate_implement,
    ("change", "commission"): _validate_commission,
    ("isolation", "confirm_operator"): _validate_confirm_operator,
    ("isolation", "confirm_safety"): _validate_confirm_safety,
    ("isolation", "withdraw"): _validate_withdraw,
    ("isolation", "gas_test"): _validate_gas_test,
    ("isolation", "start_work"): _validate_start_work,
    ("isolation", "stop_work"): _validate_stop_work,
    ("isolation", "retest"): _validate_retest,
    ("isolation", "restore_feed"): _validate_restore_feed,
}


def _isolation_blockers(entity, lookup, now=None):
    """计算隔离单当前具体的阻塞项，供动作校验与页面展示共用。"""
    data = entity.get("data", {})
    status = entity.get("status")
    now = now or _now()
    blockers = []

    def unverified_items():
        return _unverified_action_items(lookup, data.get("change_id"))

    if status == "pending":
        if not data.get("operator_confirmed"):
            blockers.append({
                "code": "operator_confirm_required",
                "message": "等待操作员现场确认隔离（逐阀确认阀门已关断）",
            })
        if not data.get("safety_confirmed"):
            blockers.append({
                "code": "safety_confirm_required",
                "message": "等待安全员复核隔离",
            })
    elif status == "isolated":
        gas = data.get("gas_test")
        if not gas:
            blockers.append({
                "code": "gas_test_required",
                "message": "尚未进行合格气体检测，不能开工",
            })
        elif _gas_expired(gas, now):
            blockers.append({
                "code": "gas_test_expired",
                "message": "气体检测已于 %s 过期，需重新检测合格后才能开工"
                % gas.get("valid_until"),
                "detail": {"valid_until": gas.get("valid_until")},
            })
        items = unverified_items()
        if items:
            blockers.append({
                "code": "action_item_unverified",
                "message": "关联行动项未完成："
                + "、".join("%s(%s)" % (item["id"], item["status"]) for item in items),
                "detail": {"items": items},
            })
    elif status == "work_in_progress":
        gas = data.get("gas_test")
        if not gas:
            blockers.append({
                "code": "gas_test_required",
                "message": "缺少合格气体检测记录，必须停止施工",
            })
        elif _gas_expired(gas, now):
            blockers.append({
                "code": "gas_test_expired",
                "message": "施工前气体检测已于 %s 过期，必须立即停止施工并重新检测"
                % gas.get("valid_until"),
                "detail": {"valid_until": gas.get("valid_until")},
            })
        items = unverified_items()
        if items:
            blockers.append({
                "code": "action_item_unverified",
                "message": "关联行动项未完成，必须停止施工："
                + "、".join("%s(%s)" % (item["id"], item["status"]) for item in items),
                "detail": {"items": items},
            })
        restoration = data.get("restoration_gas_test")
        if not restoration:
            blockers.append({
                "code": "restoration_test_required",
                "message": "恢复供料前必须重新进行气体检测并合格",
            })
        elif _gas_expired(restoration, now):
            blockers.append({
                "code": "restoration_test_expired",
                "message": "恢复供料前的气体检测已于 %s 过期，需重新检测"
                % restoration.get("valid_until"),
                "detail": {"valid_until": restoration.get("valid_until")},
            })
        checked = {
            entry.get("valve"): entry.get("checked_by")
            for entry in data.get("valve_checks", [])
            if isinstance(entry, dict)
        }
        pending_valves = [
            valve
            for valve in data.get("valves", [])
            if not checked.get(valve)
        ]
        if pending_valves:
            blockers.append({
                "code": "valve_check_incomplete",
                "message": "恢复供料尚待逐阀核对：" + "、".join(pending_valves),
                "detail": {"valves": pending_valves},
            })
    return blockers


def _change_blockers(entity, lookup):
    if entity.get("status") != "approved" or lookup is None:
        return []
    isolations = lookup("isolation", "change_id", entity["id"]) or []
    if not isolations:
        return [{
            "code": "isolation_missing",
            "message": "尚未登记隔离单：施工前必须登记受影响管线与阀门",
        }]
    isolation = isolations[0]
    if isolation["status"] != "restored":
        label = STATUS_LABELS.get(isolation["status"], isolation["status"])
        return [{
            "code": "isolation_not_restored",
            "message": "隔离单 %s 当前为「%s」，需完成隔离、检测、开工并恢复供料后才能实施"
            % (isolation["id"], label),
            "detail": {"isolation_id": isolation["id"], "status": isolation["status"]},
        }]
    return []


class RuleEngine:
    ALIASES = {
        'units': 'unit',
        'changes': 'change',
        'action_items': 'action_item',
        'isolations': 'isolation',
    }
    INITIAL_STATUS = {
        'unit': 'operating',
        'change': 'draft',
        'action_item': 'open',
        'isolation': 'pending',
    }
    TRANSITIONS = {
        'unit': {
            'shutdown': (('operating',), 'shutdown'),
            'startup': (('shutdown',), 'operating'),
            'freeze': (('operating',), 'frozen'),
            'unfreeze': (('frozen',), 'operating'),
        },
        'change': {
            'assess': (('draft',), 'assessed'),
            'approve': (('assessed',), 'approved'),
            'implement': (('approved',), 'implemented'),
            'commission': (('implemented',), 'commissioned'),
            'rollback': (('implemented', 'commissioned'), 'rolled_back'),
            'close': (('rolled_back',), 'closed'),
        },
        'action_item': {
            'complete': (('open',), 'completed'),
            'verify': (('completed',), 'verified'),
            'reopen': (('verified',), 'open'),
        },
        'isolation': {
            'confirm_operator': (('pending',), 'pending'),
            'confirm_safety': (('pending',), 'pending'),
            'withdraw': (('pending', 'isolated'), 'pending'),
            'gas_test': (('isolated',), 'isolated'),
            'start_work': (('isolated',), 'work_in_progress'),
            'stop_work': (('work_in_progress',), 'isolated'),
            'retest': (('work_in_progress',), 'work_in_progress'),
            'restore_feed': (('work_in_progress',), 'restored'),
        },
    }
    CREATE_REQUIRED = {
        'unit': ('name', 'location'),
        'change': ('unit_id', 'description'),
        'action_item': ('change_id', 'description', 'owner'),
        'isolation': ('change_id', 'lines', 'valves'),
    }
    ACTION_REQUIRED = {
        ('unit', 'shutdown'): ('reason',),
        ('unit', 'freeze'): ('reason',),
        ('change', 'assess'): ('risk_level', 'analyst'),
        ('change', 'approve'): ('approvals', 'permit_id'),
        ('change', 'implement'): ('procedure_version',),
        ('change', 'commission'): ('tests_passed',),
        ('change', 'rollback'): ('reason',),
        ('change', 'close'): ('outcome',),
        ('action_item', 'complete'): ('completed_by', 'evidence'),
        ('action_item', 'verify'): ('verifier',),
        ('action_item', 'reopen'): ('reason',),
        ('isolation', 'withdraw'): ('reason',),
        ('isolation', 'gas_test'): ('tested_by', 'valid_until', 'passed'),
        ('isolation', 'stop_work'): ('reason',),
        ('isolation', 'retest'): ('tested_by', 'valid_until', 'passed'),
        ('isolation', 'restore_feed'): ('valve_checks',),
    }
    CREATE_ROLES = {
        'unit': ('admin', 'engineer'),
        'change': ('admin', 'engineer'),
        'action_item': ('admin', 'safety'),
        'isolation': ('admin', 'engineer', 'safety', 'operator'),
    }
    ROLE_ACTIONS = {
        'shutdown': ('admin', 'operator'),
        'startup': ('admin', 'operator'),
        'freeze': ('admin', 'operator'),
        'unfreeze': ('admin', 'operator'),
        'assess': ('admin', 'engineer'),
        'approve': ('admin', 'safety'),
        'implement': ('admin', 'engineer'),
        'commission': ('admin', 'engineer'),
        'rollback': ('admin', 'engineer'),
        'close': ('admin', 'safety'),
        'complete': ('admin', 'engineer'),
        'verify': ('admin', 'verifier'),
        'reopen': ('admin', 'verifier'),
        ('isolation', 'confirm_operator'): ('admin', 'operator'),
        ('isolation', 'confirm_safety'): ('admin', 'safety'),
        ('isolation', 'withdraw'): ('operator', 'safety'),
        ('isolation', 'gas_test'): ('admin', 'operator', 'safety'),
        ('isolation', 'start_work'): ('admin', 'operator'),
        ('isolation', 'stop_work'): ('admin', 'operator', 'safety'),
        ('isolation', 'retest'): ('admin', 'operator', 'safety'),
        ('isolation', 'restore_feed'): ('admin', 'operator'),
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
            custom(actor, data, lookup)
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
        result = custom(actor, entity, data, lookup) if custom else {}
        # 自定义校验可只返回 patch，也可返回 (next_status, patch) 覆盖目标状态
        if isinstance(result, tuple):
            next_status, extra = result
        else:
            extra = result
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def blockers(self, entity, lookup=None, now=None):
        """返回实体当前所有具体阻塞项，结构为 {code, message, detail?}。"""
        kind = self.normalize_kind(entity.get("kind"))
        if kind == "isolation":
            return _isolation_blockers(entity, lookup, now=now)
        if kind == "change":
            return _change_blockers(entity, lookup)
        return []


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
