"""真实接口语义测试：用社区项目中的响应样本验证判定、动态变量与鉴权头。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date

TODAY = date.today()
from pathlib import Path

from checkin.adapters.http import ContractHttpAdapter
from checkin.adapters.registry import AdapterRegistry
from checkin.executor import Executor
from checkin.models import Account, DailyTask, DriftEvent, RunStatus
from checkin.notifier import Notifier
from checkin.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((ROOT / "contracts" / f"{name}.json").read_text(encoding="utf-8"))


class ScriptedHttp:
    """按步骤返回预设响应，并记录所有请求，供断言鉴权头 / 动态 URL。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        payload = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(payload, tuple):
            return payload[0], json.dumps(payload[1], ensure_ascii=False)
        return 200, json.dumps(payload, ensure_ascii=False)


class RealContractTests(unittest.TestCase):
    def test_qoder_dynamic_campaign_id(self):
        http = ScriptedHttp([
            {"campaigns": [
                {"campaignId": "pro-1800", "actionType": "CLAIM_BENEFIT",
                 "benefit": {"kind": "CREDITS", "amount": 1800}, "claimStatus": "CLAIMABLE"},
                {"campaignId": "daily-100", "actionType": "CLAIM_BENEFIT",
                 "benefit": {"kind": "CREDITS", "amount": 100}, "claimStatus": "CLAIMABLE"},
            ]},
            {"status": "CLAIMED", "replayed": False, "benefit": {"kind": "CREDITS", "amount": 100}},
        ])
        adapter = ContractHttpAdapter(load("qoder.cn"), http_fn=http)
        acc = Account(id="q", platform="qoder", version="cn", credential="dt-token")
        out = adapter.checkin(acc, target_date=TODAY)
        self.assertEqual(out.status, "success")
        self.assertEqual(out.credits, 100)
        # 第一个活动是 Pro 升级（应被 select 排除），实际 claim 用的是 daily-100
        self.assertIn("/daily-100/claim", http.calls[1]["url"])
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer dt-token")

    def test_qoder_device_headers_sent_and_empty_dropped(self):
        """国际版活动列表要靠 UMID 设备头才不降级；没有身份时不要发空头。"""
        http = ScriptedHttp([
            {"campaigns": [{"campaignId": "daily-100", "actionType": "CLAIM_BENEFIT",
                            "benefit": {"kind": "CREDITS", "amount": 100},
                            "claimStatus": "CLAIMABLE"}]},
            {"status": "CLAIMED", "benefit": {"kind": "CREDITS", "amount": 100}},
        ])
        acc = Account(id="q", platform="qoder", version="global", credential="dt-token")
        cred = {"token": "dt-token", "fields": {
            "machine_token": "MT-1", "machine_code": "MC-1", "machine_type": "TYPE-1",
            "machine_id": "MID-1", "machine_os": "windows",
            "machine_hostname": "HOST-1", "client_version": "1.0.0"}}
        out = ContractHttpAdapter(load("qoder.global"), http_fn=http).checkin(
            acc, target_date=TODAY, credential=cred)
        self.assertEqual(out.status, "success")
        headers = http.calls[0]["headers"]
        self.assertEqual(headers["Cosy-MachineToken"], "MT-1")
        self.assertEqual(headers["Cosy-MachineId"], "MID-1")
        self.assertEqual(headers["Cosy-MachineType"], "TYPE-1")
        self.assertEqual(headers["Cosy-MachineOS"], "windows")

        # 没有设备身份时，占位符解析为空 —— 头应被丢弃而不是发空值
        plain = ScriptedHttp([{"campaigns": []}])
        ContractHttpAdapter(load("qoder.global"), http_fn=plain).checkin(
            Account(id="q", platform="qoder", version="global", credential="dt-token"),
            target_date=TODAY)
        self.assertNotIn("Cosy-MachineToken", plain.calls[0]["headers"])

    def test_qoder_blocked_is_already(self):
        http = ScriptedHttp([
            {"campaigns": [{"campaignId": "daily-100", "actionType": "CLAIM_BENEFIT",
                            "benefit": {"kind": "CREDITS", "amount": 100}, "claimStatus": "CLAIMABLE"}]},
            {"status": "BLOCKED", "failureCode": "SAME_PERSON_ALREADY_CLAIMED"},
        ])
        out = ContractHttpAdapter(load("qoder.cn"), http_fn=http).checkin(
            Account(id="q", platform="qoder", version="cn", credential="dt-token"))
        self.assertEqual(out.status, "already")

    def test_trae_already_from_status(self):
        http = ScriptedHttp([{"code": 0, "checked_in": True, "credits": 200, "enable": True}])
        out = ContractHttpAdapter(load("trae.cn"), http_fn=http).checkin(
            Account(id="t", platform="trae", version="cn", credential="jwt"))
        self.assertEqual(out.status, "already")
        self.assertEqual(len(http.calls), 1, "已签时不应再发 claim")

    def test_trae_rate_limit_and_auth_headers(self):
        http = ScriptedHttp([
            {"code": 0, "checked_in": False, "credits": 200, "enable": True},
            {"code": 9074, "message": "too many users"},
        ])
        acc = Account(id="t", platform="trae", version="cn", credential="jwt",
                      metadata={"cred_device_id": "1234567890123456"})
        out = ContractHttpAdapter(load("trae.cn"), http_fn=http).checkin(acc)
        self.assertEqual(out.status, "failed")
        self.assertIn("9074", out.message)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Cloud-IDE-JWT jwt")
        self.assertEqual(http.calls[0]["headers"]["x-device-id"], "1234567890123456")
        self.assertEqual(json.loads(http.calls[0]["body"]), {"req_source": 1})

    def test_workbuddy_claim_success_and_headers(self):
        # 真实响应：业务字段都在 $.data.* 下
        http = ScriptedHttp([
            {"code": 0, "msg": "OK", "data": {"active": True, "today_checked_in": False}},
            {"code": 0, "msg": "OK", "data": {"credit": 100, "streak_days": 7}},
        ])
        acc = Account(id="w", platform="workbuddy", version="cn", credential="tok",
                      metadata={"cred_uid": "u-1", "cred_domain": "www.workbuddy.cn",
                                "cred_endpoint": "https://copilot.tencent.com"})
        out = ContractHttpAdapter(load("workbuddy.cn"), http_fn=http).checkin(acc)
        self.assertEqual(out.status, "success")
        self.assertEqual(out.credits, 100)
        self.assertEqual(http.calls[0]["url"],
                         "https://copilot.tencent.com/v2/billing/meter/checkin-activity-status")
        self.assertEqual(http.calls[0]["headers"]["X-User-Id"], "u-1")
        self.assertEqual(http.calls[1]["headers"]["Authorization"], "Bearer tok")

    def test_workbuddy_already_code(self):
        http = ScriptedHttp([{"code": 0, "msg": "OK", "data": {"active": True, "today_checked_in": False}},
                             {"code": 10001, "msg": "已签"}])
        out = ContractHttpAdapter(load("workbuddy.cn"), http_fn=http).checkin(
            Account(id="w", platform="workbuddy", version="cn", credential="tok",
                    metadata={"cred_endpoint": "https://copilot.tencent.com"}))
        self.assertEqual(out.status, "already")

    def test_workbuddy_real_status_already_single_request(self):
        """真实响应结构：data.today_checked_in=true 时只发 1 次请求即返回已签。"""
        http = ScriptedHttp([{"code": 0, "msg": "OK", "data": {
            "active": True, "today_checked_in": True, "today_credit": 100,
            "streak_days": 2, "total_credits": 200}}])
        out = ContractHttpAdapter(load("workbuddy.cn"), http_fn=http).checkin(
            Account(id="w", platform="workbuddy", version="cn", credential="tok",
                    metadata={"cred_endpoint": "https://copilot.tencent.com"}))
        self.assertEqual(out.status, "already")
        self.assertEqual(len(http.calls), 1)

    def test_qoder_no_campaign_is_skipped(self):
        """真实场景：campaigns 为空数组 + showCampaign=false 应判 skipped，而非漂移。"""
        real = {"shouldShowCampaignPopup": False, "uid": "u1", "showCampaign": False,
                "claimable": False, "campaignUrl": "", "campaigns": []}
        http = ScriptedHttp([real])
        out = ContractHttpAdapter(load("qoder.cn"), http_fn=http).checkin(
            Account(id="q", platform="qoder", version="cn", credential="dt-token"))
        self.assertEqual(out.status, "skipped")
        self.assertEqual(len(http.calls), 1, "无活动时不应再发 claim")

    def test_qoder_non_claimable_campaign_is_skipped(self):
        """有活动但不是 CLAIMABLE（如已领）也应 skipped，不应漂移。"""
        http = ScriptedHttp([{"campaigns": [
            {"campaignId": "x", "actionType": "CLAIM_BENEFIT",
             "benefit": {"kind": "CREDITS", "amount": 100}, "claimStatus": "CLAIMED"}]}])
        out = ContractHttpAdapter(load("qoder.cn"), http_fn=http).checkin(
            Account(id="q", platform="qoder", version="cn", credential="dt-token"))
        self.assertEqual(out.status, "skipped")

    def test_drift_on_unknown_response(self):
        from checkin.adapters.base import ContractDriftError
        http = ScriptedHttp([{"totally": "unexpected"}])
        with self.assertRaises(ContractDriftError):
            ContractHttpAdapter(load("workbuddy.cn"), http_fn=http).checkin(
                Account(id="w", platform="workbuddy", version="cn", credential="tok",
                        metadata={"cred_endpoint": "https://copilot.tencent.com"}))

    def test_env_fields_injected_as_cred_placeholders(self):
        """凭据提供器从环境变量解析的字段应可通过 ${cred.xxx} 注入请求头/URL。"""
        import os
        from unittest.mock import patch
        from checkin.credentials import resolve
        acc = Account(id="w", platform="workbuddy", version="cn", credential="tok")
        with patch.dict(os.environ, {"WORKBUDDY_UID": "u-env",
                                     "WORKBUDDY_ENDPOINT": "https://copilot.tencent.com"}):
            cred = resolve(acc)
            http = ScriptedHttp([{"active": True, "today_checked_in": False}, {"credit": 100}])
            out = ContractHttpAdapter(load("workbuddy.cn"), http_fn=http).checkin(
                acc, credential={"token": cred.token, "fields": cred.fields,
                                 "headers": cred.headers})
        self.assertEqual(out.status, "success")
        self.assertEqual(http.calls[0]["headers"]["X-User-Id"], "u-env")
        self.assertIn("https://copilot.tencent.com/", http.calls[0]["url"])

    def test_registry_loads_all_real_contracts(self):
        registry = AdapterRegistry(ROOT / "contracts")
        self.assertEqual(len(registry.contracts), 6)
        self.assertFalse(registry.errors)
        for row in registry.summary():
            self.assertIn(row["status"], ("active", "experimental", "unsupported"))
        # 没有签到活动的版本必须标明原因，避免被当成可启用版本
        for platform, needle in (("trae", "没有每日签到"), ("workbuddy", "没有每日签到")):
            row = next(r for r in registry.summary()
                       if r["platform"] == platform and r["version"] == "global")
            self.assertEqual(row["status"], "unsupported", f"{platform}.global 应标 unsupported")
            self.assertIn(needle, row["note"])

    def test_executor_records_drift_for_real_contract(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "db.sqlite")
            # 真实响应结构突变 → 漂移事件
            def bad(method, url, headers, body, timeout):
                return 200, json.dumps({"weird": True})
            registry = AdapterRegistry(ROOT / "contracts", http_fn=bad)
            acc = Account(id="w", platform="workbuddy", version="cn", credential="tok",
                          metadata={"cred_endpoint": "https://copilot.tencent.com"})
            cfg = {
                "timezone": "Asia/Shanghai", "catchup_days": 0,
                "retry": {"max_attempts": 2, "backoff_seconds": [0]},
                "notify": {"channels": [], "on_failure": False, "on_drift": False},
                "accounts": [acc],
            }
            exe = Executor(cfg, storage, registry, Notifier(cfg["notify"]))
            rec = exe.run_task(DailyTask("workbuddy", "cn", "w", TODAY))
            self.assertEqual(rec.status, RunStatus.DRIFT.value)
            self.assertEqual(len(storage.drifts(10)), 1)
            storage.close()


if __name__ == "__main__":
    unittest.main()
