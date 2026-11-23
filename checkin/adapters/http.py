"""契约驱动的 HTTP 签到适配器（v1 简单判定 + v2 真实语义）。

v1 契约：response.json_paths + values + expected_shape
v2 契约：response.verdicts（有序业务码判定）、steps[].capture（捕获动态值）、
         steps[].when（条件步骤）、auth.headers 模板（${token}/${cred.*}/${var.*}）
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..models import Account
from .base import (
    BaseAdapter,
    CheckinOutcome,
    ContractDriftError,
    _MISSING,
    apply_placeholders,
    build_mapping,
    capture_from_response,
    fake_value_for,
    first_verdict,
    get_path,
    match_any,
    pick_value,
    set_path,
    validate_shape,
)

HttpFn = Callable[[str, str, Dict[str, str], Optional[bytes], int], Tuple[int, str]]


def default_http(method: str, url: str, headers: Dict[str, str],
                 body: Optional[bytes], timeout: int) -> Tuple[int, str]:
    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return int(exc.code), raw


class ContractHttpAdapter(BaseAdapter):
    def __init__(self, contract: Dict[str, Any], *, http_fn: Optional[HttpFn] = None,
                 simulate: bool = False):
        self.contract = contract
        self.platform = str(contract.get("platform", ""))
        self.version = str(contract.get("version", ""))
        self.name = str(contract.get("name", f"{self.platform}/{self.version}"))
        self.contract_version = str(contract.get("contract_version", ""))
        self.template = bool(contract.get("template", False))
        self.http_fn = http_fn or default_http
        self.simulate = simulate
        self.timeout = int(contract.get("defaults", {}).get("timeout", 15))

        auth = contract.get("auth") or {}
        self.auth_type = str(auth.get("type", "bearer"))
        self.auth_header = str(auth.get("header", "Authorization"))
        self.auth_prefix = str(auth.get("prefix", "Bearer "))
        self.auth_query = str(auth.get("query_param", "token"))
        self.auth_headers_tpl = auth.get("headers") or {}

        resp = contract.get("response") or {}
        self.paths = resp.get("json_paths") or {}
        self.values = resp.get("values") or {}
        self.shape = resp.get("expected_shape") or {}
        self.verdicts = resp.get("verdicts") or []

        self.success_values: List[Any] = list(self.values.get("success", [0, 200, True, "ok", "success"]))
        self.already_values: List[Any] = list(self.values.get("already", [True, 1, "already"]))
        self.failed_values: List[Any] = list(self.values.get("failed", [False, 400, 401, 403, "failed"]))

    @staticmethod
    def _http_failure(status_code: int, message: str, merged: Any, steps_log: list) -> CheckinOutcome:
        """HTTP 非 2xx 时不允许判成功：转成确定性失败，401/403 标记为鉴权失效。"""
        auth = status_code in (401, 403)
        detail = message or f"HTTP {status_code}"
        return CheckinOutcome(
            "failed", message=f"HTTP {status_code}：{detail}",
            simulated=False, response=merged, steps=steps_log,
            retryable=not auth, auth_failure=auth)

    # ---------- 公开接口 ----------
    def check_status(self, account: Account, *, credential: Optional[Dict[str, Any]] = None,
                     target_date: Optional[date] = None) -> Dict[str, Any]:
        """只读探活：只执行第一个「状态/活动查询」步骤，绝不领取。

        返回 {"ok", "health": valid|auth_dead|drift|error|unknown, "message"}。
        """
        try:
            out = self.checkin(account, credential=credential,
                               target_date=target_date, claim=False)
        except ContractDriftError as exc:
            return {"ok": False, "health": "drift", "message": exc.reason[:200]}
        except Exception as exc:
            return {"ok": False, "health": "error",
                    "message": f"{type(exc).__name__}: {exc}"}
        if out.auth_failure or out.status == "failed":
            health = "auth_dead"
        elif out.status in ("success", "already", "skipped"):
            health = "valid"
        else:
            health = "unknown"
        return {"ok": health == "valid", "health": health,
                "message": out.message or out.status, "simulated": out.simulated}

    def checkin(self, account: Account, *, simulate: bool = False,
                target_date: Optional[date] = None,
                credential: Optional[Dict[str, Any]] = None,
                claim: bool = True) -> CheckinOutcome:
        use_sim = bool(simulate or self.simulate)
        if self.template and not use_sim:
            return CheckinOutcome(
                "skipped",
                message="当前契约为模板，未配置真实端点；请用 --simulate 验证，或补全契约后将 template 改为 false",
            )

        cred = dict(credential or {})
        token = str(cred.get("token") or account.token() or "")
        if not use_sim and not token:
            return CheckinOutcome(
                "skipped",
                message=f"缺少凭据：请设置环境变量 {account.credential_env or '(未配置)'}，"
                        f"或运行 `python -m checkin probe` 查看本机凭据状态",
            )

        d = (target_date or date.today()).isoformat()
        ctx: Dict[str, Any] = dict(cred.get("fields") or {})
        steps_log: List[Dict[str, Any]] = []
        merged: Dict[str, Any] = {}

        # 凭据提供器解析出的字段也暴露为 ${cred.xxx}（环境变量 / 本地登录态）
        from dataclasses import replace as _replace
        effective_account = account
        if cred.get("fields"):
            meta = dict(account.metadata or {})
            for k, v in cred["fields"].items():
                meta[f"cred_{k}"] = v
            effective_account = _replace(account, metadata=meta)
        mapping = build_mapping(effective_account, token, d, ctx)
        # 最近一步的 HTTP 状态码。业务码判定优先，但 2xx 之外的响应绝不判成功，
        # 避免网关错误页 / HTTP 500 带 {"code":0} 时被误记为领取成功。
        last_status_code = 200

        for step in (self.contract.get("steps") or []):
            step_id = str(step.get("id", "step"))
            when = step.get("when") or {}
            if when and not self._when_met(when, ctx):
                steps_log.append({"id": step_id, "skipped": True, "reason": "条件不满足"})
                continue
            if use_sim:
                response, status_code = self._simulate_step(step, d)
            else:
                response, status_code = self._request_step(step, account, token, d, mapping, cred)
            last_status_code = status_code

            self._validate_step_shape(step, response, status_code)
            if isinstance(response, dict):
                merged.update(response)
            steps_log.append({"id": step_id, "http_status": status_code,
                              "response": response, "captured": {}})

            # 只读探活模式（claim=False）：执行完第一个状态步骤就返回，
            # 且忽略 verdict 的 steps 限定（鉴权规则多挂在 claim 步骤上）
            if not claim:
                verdict = first_verdict(response, self.verdicts, ctx)
                if verdict:
                    vstatus = str(verdict.get("status", "success")).lower()
                    message = str(verdict.get("message", "")) or self._message(response)
                    if vstatus == "failed":
                        return CheckinOutcome(
                            "failed", message=message or "业务失败",
                            simulated=use_sim, response=merged, steps=steps_log,
                            retryable=False, auth_failure=bool(verdict.get("auth")))
                    # 非 2xx 不允许在探活里判成"有效"
                    if not use_sim and (last_status_code < 200 or last_status_code >= 300):
                        return self._http_failure(last_status_code, message, merged, steps_log)
                    return CheckinOutcome(vstatus, message=message or vstatus,
                                          simulated=use_sim, response=merged, steps=steps_log)
                if not use_sim and (last_status_code < 200 or last_status_code >= 300):
                    return self._http_failure(last_status_code, "", merged, steps_log)
                return CheckinOutcome("skipped", message="状态响应无可识别判定（按有效处理）",
                                      simulated=use_sim, response=merged, steps=steps_log)

            # 捕获动态值（如 Qoder 的 campaignId）
            captured: Dict[str, Any] = {}
            for spec in (step.get("capture") or []):
                name = str(spec.get("name", ""))
                if not name:
                    continue
                if use_sim:
                    value = spec.get("simulate", "sim-captured")
                else:
                    value = capture_from_response(response, spec)
                if value is not _MISSING and value is not None:
                    ctx[name] = value
                    captured[name] = value
                    mapping[f"var.{name}"] = value
            steps_log[-1]["captured"] = captured

            # 每步之后判定（v2 verdicts，v1 简单值）
            verdict = self._verdict(response, ctx, step_id)
            if verdict:
                status = str(verdict.get("status", "success")).lower()
                message = str(verdict.get("message", "")) or self._message(merged)
                credits = self._credits(merged, verdict.get("credits_path"))
                if status in ("success", "already", "skipped", "drift"):
                    # 成功/已签必须以 2xx 为前提；非 2xx body 伪装成成功结构时不能记积分
                    if (status in ("success", "already") and not use_sim
                            and (last_status_code < 200 or last_status_code >= 300)):
                        return self._http_failure(last_status_code, message, merged, steps_log)
                    return CheckinOutcome(status, message=message, credits=credits,
                                          simulated=use_sim, response=merged, steps=steps_log)
                if status == "failed":
                    return CheckinOutcome("failed", message=message or "业务失败",
                                          simulated=use_sim, response=merged, steps=steps_log,
                                          retryable=not bool(verdict.get("fatal")),
                                          auth_failure=bool(verdict.get("auth")))

        # 所有步骤执行完仍未命中：v1 兜底
        if self._detect(merged, "success", self.success_values):
            if not use_sim and (last_status_code < 200 or last_status_code >= 300):
                return self._http_failure(last_status_code, self._message(merged), merged, steps_log)
            return CheckinOutcome("success", message=self._message(merged) or "签到成功",
                                  credits=self._credits(merged), simulated=use_sim,
                                  response=merged, steps=steps_log)
        if self._detect(merged, "failed", self.failed_values):
            return CheckinOutcome("failed", message=self._message(merged) or "签到失败",
                                  simulated=use_sim, response=merged, steps=steps_log)
        raise ContractDriftError(
            "响应中未匹配到已签/成功/失败任一状态，契约字段映射可能已过期",
            sample=merged,
        )

    # ---------- 内部 ----------
    def _when_met(self, when: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
        for key, expected in when.items():
            if key.startswith("var."):
                actual = ctx.get(key[4:], _MISSING)
            elif key in ctx:
                actual = ctx.get(key)
            else:
                return True  # 未知变量视为不限制
            if isinstance(expected, dict):
                if expected.get("exists") is True and actual is _MISSING:
                    return False
                if expected.get("truthy") is True:
                    if actual is _MISSING or actual is None or not bool(actual):
                        return False
                if "equals" in expected and not match_any(actual, expected["equals"]):
                    return False
            elif actual != expected:
                return False
        return True

    def _verdict(self, response: Any, ctx: Dict[str, Any],
                 step_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.verdicts:
            if self._detect(response, "already", self.already_values):
                return {"status": "already", "message": self._message(response) or "今日已签到"}
            return None
        rules = [v for v in self.verdicts
                 if not v.get("steps") or step_id in v["steps"]]
        return first_verdict(response, rules, ctx)

    def _validate_step_shape(self, step: Dict[str, Any], response: Any, status_code: int) -> None:
        shape = step.get("expected_shape") or self.shape
        validate_shape(response, shape, context=f"步骤 {step.get('id')} 响应")

    def _request_step(self, step: Dict[str, Any], account: Account, token: str,
                      d: str, mapping: Dict[str, Any],
                      cred: Dict[str, Any]) -> Tuple[Any, int]:
        method = str(step.get("method", "GET")).upper()
        url = str(apply_placeholders(step.get("url", ""), account, token, d, mapping=mapping))
        if not url:
            raise ContractDriftError(f"步骤 {step.get('id')} 未配置 URL", sample=step)
        if self.auth_type == "query" and token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{self.auth_query}={urllib.parse.quote(token)}"

        headers: Dict[str, str] = {
            **dict(self.contract.get("defaults", {}).get("headers") or {}),
            **dict(step.get("headers") or {}),
        }
        if self.auth_headers_tpl:
            for k, v in self.auth_headers_tpl.items():
                headers[k] = str(apply_placeholders(v, account, token, d, mapping=mapping))
        elif token:
            if self.auth_type == "bearer":
                headers[self.auth_header] = f"{self.auth_prefix}{token}"
            elif self.auth_type == "header":
                headers[self.auth_header] = token
        for k, v in (cred.get("headers") or {}).items():
            headers[k] = str(apply_placeholders(v, account, token, d, mapping=mapping))

        # 统一替换占位符；解析为空的请求头直接丢弃
        # （例如本机没有 Qoder 的 UMID 设备身份时，不要发空的 Cosy-MachineToken）
        resolved_headers: Dict[str, str] = {}
        for k, v in headers.items():
            value = str(apply_placeholders(v, account, token, d, mapping=mapping))
            if value.strip():
                resolved_headers[k] = value
        headers = resolved_headers

        body_tpl = step.get("body")
        body = None
        if body_tpl is not None:
            body_obj = apply_placeholders(body_tpl, account, token, d, mapping=mapping)
            body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

        status_code, raw = self.http_fn(method, url, headers, body, self.timeout)
        try:
            response = json.loads(raw)
        except (ValueError, TypeError):
            if status_code < 200 or status_code >= 300:
                raise ContractDriftError(
                    f"步骤 {step.get('id')} 返回 HTTP {status_code} 且响应不是 JSON",
                    sample=str(raw)[:500],
                )
            response = {"_raw": raw}
        return response, status_code

    def _simulate_step(self, step: Dict[str, Any], d: str) -> Tuple[Dict[str, Any], int]:
        response: Dict[str, Any] = {}
        step_id = str(step.get("id", "step"))
        shape = step.get("expected_shape") or self.shape
        required = shape.get("required_paths", []) or []
        if shape.get("type") == "object":
            for path in required:
                set_path(response, path, fake_value_for(path, self.success_values))

        def is_set(path: str) -> bool:
            return get_path(response, path, _MISSING) is not _MISSING

        def value_for(expected: Any) -> Any:
            if isinstance(expected, dict):
                if "equals" in expected:
                    return pick_value(expected["equals"], [])
                if "in" in expected:
                    return pick_value(expected["in"], [])
                if "contains" in expected:
                    return f"simulated {expected['contains']}"
                if "exists" in expected:
                    return "simulated"
            return expected

        # 让非目标 verdict 的 exists:false 条件不成立（构造字段存在且安全），
        # 否则模拟响应会因「字段缺失」提前命中 skipped/already 规则。
        for rule in self.verdicts:
            if rule.get("steps") and step_id not in rule["steps"]:
                continue
            for path, expected in (rule.get("match") or {}).items():
                if path.startswith("var."):
                    continue
                if isinstance(expected, dict) and expected.get("exists") is False:
                    if not is_set(path):
                        set_path(response, path, "present")

        # 为本步骤的 success verdict 构造命中值 + 积分字段
        for rule in self.verdicts:
            if str(rule.get("status", "")).lower() != "success":
                continue
            if rule.get("steps") and step_id not in rule["steps"]:
                continue
            for path, expected in (rule.get("match") or {}).items():
                if path.startswith("var."):
                    continue
                set_path(response, path, value_for(expected))
            if rule.get("credits_path") and not is_set(rule["credits_path"]):
                set_path(response, rule["credits_path"], 100)
            break

        # 为本步骤的非 success verdict 构造安全值，避免提前误判（不覆盖已有值）
        for rule in self.verdicts:
            if str(rule.get("status", "")).lower() == "success":
                continue
            if rule.get("steps") and step_id not in rule["steps"]:
                continue
            for path, expected in (rule.get("match") or {}).items():
                if path.startswith("var.") or is_set(path):
                    continue
                if isinstance(expected, dict) and "equals" in expected:
                    set_path(response, path, pick_value(expected["equals"], expected["equals"]))
                elif isinstance(expected, dict) and "contains" in expected:
                    set_path(response, path, "not-simulated")

        # 明确排除：积分字段必须是数字（上面若干循环可能写入占位字符串）
        credit_paths = set(self.paths.get("credits") or [])
        for rule in self.verdicts:
            if rule.get("credits_path"):
                credit_paths.add(rule["credits_path"])
        for path in credit_paths:
            if path and not path.startswith("var."):
                val = get_path(response, path, _MISSING)
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    set_path(response, path, 100)

        for path in (self.paths.get("success") or []):
            if not is_set(path):
                set_path(response, path,
                         pick_value(self.success_values, self.already_values + self.failed_values))
        for path in (self.paths.get("failed") or []):
            if not is_set(path):
                set_path(response, path,
                         pick_value(self.failed_values,
                                    self.failed_values + self.success_values + self.already_values))
        for path in (self.paths.get("already") or []):
            if not is_set(path):
                set_path(response, path,
                         pick_value(self.already_values,
                                    self.already_values + self.success_values + self.failed_values))
        for path in (self.paths.get("credits") or []):
            if not is_set(path):
                set_path(response, path, 100)
        for path in (self.paths.get("message") or []):
            if not is_set(path):
                set_path(response, path, "simulated")
        return response, 200

    def _detect(self, merged: Dict[str, Any], group: str, values: List[Any]) -> bool:
        for path in (self.paths.get(group) or []):
            sentinel = object()
            value = get_path(merged, path, sentinel)
            if value is not sentinel and match_any(value, values):
                return True
        return False

    def _credits(self, merged: Dict[str, Any], path: Optional[str] = None) -> Optional[int]:
        paths = [path] if path else (self.paths.get("credits") or [])
        for p in paths:
            if not p:
                continue
            value = get_path(merged, p)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            if isinstance(value, str):
                digits = "".join(ch for ch in value if ch.isdigit())
                if digits:
                    return int(digits)
        return None

    @staticmethod
    def _message(merged: Dict[str, Any]) -> str:
        for path in ("$.msg", "$.message", "$.data.msg", "$.data.message", "$.report"):
            value = get_path(merged, path)
            if isinstance(value, str) and value:
                return value[:200]
        return ""
