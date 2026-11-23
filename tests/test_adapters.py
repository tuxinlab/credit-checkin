"""适配层测试：响应解析、漂移检测、模拟模式。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from checkin.adapters import ContractDriftError, ContractHttpAdapter, get_path
from checkin.adapters.base import rule_match
from checkin.adapters.registry import AdapterRegistry
from checkin.models import Account

CONTRACT = {
    "platform": "workbuddy",
    "version": "cn",
    "name": "测试契约",
    "contract_version": "test-1",
    "template": False,
    "defaults": {"timeout": 5},
    "auth": {"type": "bearer", "header": "Authorization", "prefix": "Bearer "},
    "steps": [{
        "id": "checkin", "method": "POST", "url": "https://example.com/checkin",
        "headers": {"Content-Type": "application/json"}, "body": {"source": "test"},
    }],
    "response": {
        "json_paths": {
            "success": ["$.code"],
            "already": ["$.data.checked_in"],
            "failed": ["$.code"],
            "credits": ["$.data.points"],
            "message": ["$.msg"],
        },
        "values": {"success": [0], "already": [True], "failed": [400, 401]},
        "expected_shape": {
            "type": "object",
            "required_paths": ["$.code", "$.data"],
            "field_types": {"$.data": "object"},
        },
    },
}


def http_returning(payload, status=200):
    def fn(method, url, headers, body, timeout):
        return status, json.dumps(payload, ensure_ascii=False)
    return fn


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.acc = Account(id="a1", platform="workbuddy", version="cn", credential="secret-token")

    def test_get_path(self):
        obj = {"a": {"b": [{"c": 7}]}}
        self.assertEqual(get_path(obj, "$.a.b[0].c"), 7)
        self.assertIsNone(get_path(obj, "$.a.b[2].c"))

    def test_success_and_credits(self):
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning({"code": 0, "data": {"checked_in": False, "points": 120}, "msg": "ok"}),
        )
        out = adapter.checkin(self.acc, target_date=date(2026, 11, 17))
        self.assertEqual(out.status, "success")
        self.assertEqual(out.credits, 120)
        self.assertFalse(out.simulated)

    def test_already(self):
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning({"code": 0, "data": {"checked_in": True, "points": 0}}),
        )
        out = adapter.checkin(self.acc)
        self.assertEqual(out.status, "already")

    def test_failed(self):
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning({"code": 401, "data": {}, "msg": "unauthorized"}),
        )
        out = adapter.checkin(self.acc)
        self.assertEqual(out.status, "failed")

    def test_non_2xx_success_body_is_not_counted(self):
        """HTTP 5xx/401 即使 body 伪装成成功结构，也绝不判成功、绝不记积分。

        回归：网关错误页若返回 {"code":0,...}，旧逻辑只看 body 会误记领取成功。
        """
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning(
                {"code": 0, "data": {"checked_in": False, "points": 100}, "msg": "ok"},
                status=500),
        )
        out = adapter.checkin(self.acc)
        self.assertEqual(out.status, "failed")
        self.assertIsNone(out.credits)
        self.assertIn("HTTP 500", out.message)

    def test_http_401_marks_auth_failure(self):
        """HTTP 401 应标记为鉴权失效，以便执行器触发 token 自动续期。"""
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning(
                {"code": 0, "data": {"checked_in": False, "points": 100}},
                status=401),
        )
        out = adapter.checkin(self.acc)
        self.assertEqual(out.status, "failed")
        self.assertTrue(out.auth_failure)
        self.assertFalse(out.retryable)

    def test_2xx_success_still_works(self):
        """正常 2xx + 成功 body 不受状态码防护影响。"""
        adapter = ContractHttpAdapter(
            CONTRACT,
            http_fn=http_returning({"code": 0, "data": {"checked_in": False, "points": 100}}),
        )
        out = adapter.checkin(self.acc)
        self.assertEqual(out.status, "success")
        self.assertEqual(out.credits, 100)

    def test_truthy_does_not_match_missing_or_null(self):
        """truthy:true 遇到缺失字段或 null 不能命中（缺失哨兵 bool 为真会误判）。"""
        self.assertFalse(rule_match({}, {"match": {"$.x": {"truthy": True}}}, {}))
        self.assertFalse(rule_match({"x": None}, {"match": {"$.x": {"truthy": True}}}, {}))
        self.assertTrue(rule_match({"x": 1}, {"match": {"$.x": {"truthy": True}}}, {}))

    def test_in_operator_is_type_strict(self):
        """in 判定要区分 bool 与数字，True 不应匹配数字 1。"""
        self.assertFalse(rule_match({"x": True}, {"match": {"$.x": {"in": [1, 2]}}}, {}))
        self.assertTrue(rule_match({"x": 401}, {"match": {"$.x": {"in": [401, 403]}}}, {}))

    def test_drift_on_missing_required_field(self):
        adapter = ContractHttpAdapter(CONTRACT, http_fn=http_returning({"code": 0}))
        with self.assertRaises(ContractDriftError) as ctx:
            adapter.checkin(self.acc)
        self.assertIn("$.data", str(ctx.exception))

    def test_drift_on_non_json_error(self):
        def fn(method, url, headers, body, timeout):
            return 502, "<html>bad gateway</html>"
        adapter = ContractHttpAdapter(CONTRACT, http_fn=fn)
        with self.assertRaises(ContractDriftError):
            adapter.checkin(self.acc)

    def test_missing_credential_skipped(self):
        acc = Account(id="a2", platform="workbuddy", version="cn")
        adapter = ContractHttpAdapter(CONTRACT, http_fn=http_returning({"code": 0, "data": {}}))
        out = adapter.checkin(acc)
        self.assertEqual(out.status, "skipped")

    def test_simulation_without_http(self):
        def boom(*args, **kwargs):
            raise AssertionError("模拟模式不应发起真实请求")
        adapter = ContractHttpAdapter(CONTRACT, http_fn=boom, simulate=True)
        out = adapter.checkin(Account(id="a3", platform="workbuddy", version="cn"))
        self.assertEqual(out.status, "success")
        self.assertTrue(out.simulated)

    def test_registry_load_and_validate(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "workbuddy.cn.json").write_text(json.dumps(CONTRACT), encoding="utf-8")
            registry = AdapterRegistry(d, http_fn=http_returning({"code": 0, "data": {}}))
            self.assertTrue(registry.has("workbuddy", "cn"))
            self.assertIsNotNone(registry.get("workbuddy", "cn"))
            self.assertEqual(len(registry.summary()), 1)
            self.assertFalse(registry.has("qoder", "global"))

    def test_registry_rejects_bad_filename(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "wrong.name.json").write_text(json.dumps(CONTRACT), encoding="utf-8")
            registry = AdapterRegistry(d)
            self.assertEqual(len(registry.contracts), 0)
            self.assertTrue(registry.errors)


if __name__ == "__main__":
    unittest.main()
