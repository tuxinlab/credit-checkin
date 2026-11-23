"""适配器层基础：契约定义、响应解析、形状校验与漂移检测。"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import Account


class ContractDriftError(RuntimeError):
    """接口 / 鉴权 / 响应结构与契约不一致，需要人工更新契约。"""

    def __init__(self, reason: str, *, sample: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.sample = sample


@dataclass
class CheckinOutcome:
    status: str                 # success | already | failed | drift | skipped
    message: str = ""
    credits: Optional[int] = None
    simulated: bool = False
    response: Dict[str, Any] = field(default_factory=dict)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    retryable: bool = True      # 鉴权失效等确定性失败置 False，不再浪费重试
    auth_failure: bool = False  # 鉴权类失败（契约 verdict 标 auth），可触发自动续期


class BaseAdapter(ABC):
    platform: str = ""
    version: str = ""

    @abstractmethod
    def checkin(self, account: Account, *, simulate: bool = False) -> CheckinOutcome:
        ...


# ---------- JSON path 工具 ----------
_PATH_PART = re.compile(r"^([^\[]+)(?:\[(\d+)\])?$")


def get_path(obj: Any, path: str, default: Any = None) -> Any:
    """支持 $.data.items[0].code 形式的路径读取。"""
    if not path:
        return default
    parts = path[2:].split(".") if path.startswith("$.") else path.split(".")
    cur = obj
    for part in parts:
        if not part:
            continue
        m = _PATH_PART.match(part)
        if not m:
            return default
        key, idx = m.group(1), m.group(2)
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, list) and key.isdigit():
            i = int(key)
            if i >= len(cur):
                return default
            cur = cur[i]
        else:
            return default
        if idx is not None:
            if isinstance(cur, list) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return default
    return cur


def set_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    """在嵌套 dict 中设置路径值（用于模拟响应）。"""
    parts = path[2:].split(".") if path.startswith("$.") else path.split(".")
    parts = [p for p in parts if p]
    cur: Any = obj
    for i, part in enumerate(parts):
        m = _PATH_PART.match(part)
        if not m:
            return
        key, idx = m.group(1), m.group(2)
        last = i == len(parts) - 1
        if idx is None:
            if last:
                cur[key] = value
            else:
                cur = cur.setdefault(key, {})
        else:
            lst = cur.setdefault(key, [])
            while len(lst) <= int(idx):
                lst.append({})
            if last:
                lst[int(idx)] = value
            else:
                cur = lst[int(idx)]


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def build_mapping(account: Account, token: str, date_str: str,
                  variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造占位符映射。支持 ${var.xxx} 取上一步捕获值、${cred.xxx} 取凭据附加字段。"""
    mapping: Dict[str, Any] = {
        "token": token,
        "account_id": account.id,
        "date": date_str,
        "platform": account.platform,
        "version": account.version,
    }
    for key, value in (variables or {}).items():
        mapping[f"var.{key}"] = value
    for key, value in (account.metadata or {}).items():
        if key.startswith("cred_"):
            mapping[f"cred.{key[5:]}"] = value
    return mapping


def apply_placeholders(value: Any, account: Account, token: str, date_str: str,
                       variables: Optional[Dict[str, Any]] = None,
                       mapping: Optional[Dict[str, Any]] = None) -> Any:
    """替换契约中的 ${token}/${var.xxx}/${cred.xxx}/${date} 等占位符。"""
    m = mapping if mapping is not None else build_mapping(account, token, date_str, variables)

    def repl(mo):
        key = mo.group(1)
        if key in m:
            return str(m[key])
        return ""

    if isinstance(value, str):
        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: apply_placeholders(v, account, token, date_str, variables, m)
                for k, v in value.items()}
    if isinstance(value, list):
        return [apply_placeholders(v, account, token, date_str, variables, m) for v in value]
    return value


# ---------- 响应判定规则（真实语义） ----------
_MISSING = object()


def rule_match(response: Any, rule: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    """判定一条 verdict 规则是否命中。

    支持字段条件与变量条件：
      {"path": "$.data.checked_in", "equals": true}
      {"path": "$.code", "in": [0, 200]}
      {"var": "campaign_id", "exists": true}
      {"path": "$.msg", "contains": "已签"}
    多个键为 AND 关系；v2 契约用它表达真实业务码语义。
    """
    for key, expected in (rule.get("match") or {}).items():
        if key.startswith("var."):
            actual = ctx.get(key[4:], _MISSING)
        else:
            actual = get_path(response, key, _MISSING)
        if isinstance(expected, dict):
            if "equals" in expected and not match_any(actual, expected["equals"]):
                return False
            if "in" in expected and not match_any(actual, expected["in"]):
                return False
            if "contains" in expected:
                if not isinstance(actual, str) or expected["contains"] not in actual:
                    return False
            if expected.get("exists") is True and actual is _MISSING:
                return False
            if expected.get("exists") is False and actual is not _MISSING:
                return False
            if expected.get("truthy") is True:
                # 缺失值与 None 都不能算 truthy（_MISSING 是 object()，直接 bool 会误命中）
                if actual is _MISSING or actual is None or not bool(actual):
                    return False
            if expected.get("empty") is True:
                # 字段缺失、null、空数组/空对象/空串 都算空（Qoder 无活动时为 campaigns: []）
                if actual is _MISSING or actual is None:
                    pass
                elif isinstance(actual, (list, dict, str)) and len(actual) > 0:
                    return False
                elif isinstance(actual, (int, float, bool)):
                    return False
            if expected.get("not_empty") is True:
                if actual is _MISSING or actual is None:
                    return False
                if isinstance(actual, (list, dict, str)) and len(actual) == 0:
                    return False
        elif isinstance(expected, list):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return bool(rule.get("match")) or not bool(rule.get("match"))


def first_verdict(response: Any, rules: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for rule in rules or []:
        if rule_match(response, rule, ctx):
            return rule
    return None


def capture_from_response(response: Any, spec: Dict[str, Any]) -> Any:
    """从响应中捕获变量；支持数组条件筛选（如 Qoder 的每日 Credits 活动）。"""
    path = spec.get("path", "")
    value = get_path(response, path, _MISSING)
    if value is _MISSING:
        return _MISSING
    select = spec.get("select")
    if select and isinstance(value, list):
        for row in value:
            if not isinstance(row, dict):
                continue
            ok = True
            for k, v in select.items():
                # 支持嵌套路径：benefit.kind / benefit.amount
                if "." in k or k.startswith("$"):
                    actual = get_path(row, k if k.startswith("$") else "$." + k)
                else:
                    actual = row.get(k)
                if isinstance(v, dict) and "not" in v:
                    if actual in v["not"]:
                        ok = False
                        break
                elif actual != v:
                    ok = False
                    break
            if ok:
                return get_path(row, spec.get("field", "$.campaignId"), _MISSING)
        return _MISSING
    return value


def match_any(value: Any, candidates: List[Any]) -> bool:
    for c in candidates:
        if isinstance(c, bool):
            if isinstance(value, bool) and value is c:
                return True
        elif isinstance(c, (int, float)) and not isinstance(c, bool):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value == c:
                return True
        elif value == c:
            return True
    return False


def false_like_for(candidate: Any) -> Any:
    if isinstance(candidate, bool):
        return False
    if isinstance(candidate, (int, float)):
        return 0
    return ""


def pick_value(candidates: List[Any], avoid: List[Any]) -> Any:
    """从候选值中挑一个不在 avoid 里的值，供模拟响应使用。"""
    for c in candidates:
        if not match_any(c, avoid):
            return c
    for c in candidates:
        if isinstance(c, bool):
            return not c
        if isinstance(c, int) and not isinstance(c, bool):
            return c + 1
        if isinstance(c, str):
            return f"{c}_ok"
    return 0


def validate_shape(response: Any, shape: Dict[str, Any], *, context: str) -> None:
    """校验响应结构；不匹配则视为契约漂移。"""
    if not shape:
        return
    expected_type = shape.get("type")
    if expected_type == "object" and not isinstance(response, dict):
        raise ContractDriftError(
            f"{context}: 响应不是对象（实际 {type(response).__name__}）", sample=response
        )
    if expected_type == "array" and not isinstance(response, list):
        raise ContractDriftError(
            f"{context}: 响应不是数组（实际 {type(response).__name__}）", sample=response
        )
    for path in shape.get("required_paths", []) or []:
        sentinel = object()
        if get_path(response, path, sentinel) is sentinel:
            raise ContractDriftError(
                f"{context}: 响应缺少契约要求的字段 {path}", sample=response
            )
    for path, expected in (shape.get("field_types") or {}).items():
        value = get_path(response, path, None)
        if value is None:
            continue
        ok = (
            (expected == "object" and isinstance(value, dict))
            or (expected == "array" and isinstance(value, list))
            or (expected == "str" and isinstance(value, str))
            or (expected == "int" and isinstance(value, int) and not isinstance(value, bool))
            or (expected == "bool" and isinstance(value, bool))
        )
        if not ok:
            raise ContractDriftError(
                f"{context}: 字段 {path} 类型不符（期望 {expected}）", sample=response
            )


def fake_value_for(path: str, success_values: List[Any], credits: int = 100) -> Any:
    lower = path.lower()
    if lower.endswith(".data") or lower.endswith(".result") or lower in ("$.data", "$.result"):
        return {}
    if lower.endswith(".list") or lower.endswith(".items"):
        return []
    if any(k in lower for k in ("credit", "point", "balance", "amount", "score")):
        return credits
    if any(k in lower for k in ("already", "checked", "signed", "claimed")):
        return False
    if "msg" in lower or "message" in lower or "desc" in lower:
        return "simulated"
    # 优先选择与契约一致的简单成功值
    for v in success_values:
        if isinstance(v, (int, str, bool)):
            return v
    return 0
