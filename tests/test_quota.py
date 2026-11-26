"""账号额度查询模块测试：三平台解析、缓存、失败降级、脱敏。"""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from checkin import credentials as credential_module
from checkin import quota
from checkin.credentials import Credential
from checkin.models import Account


def _account(platform="workbuddy", version="cn", account_id="a-1"):
    return Account(id=account_id, platform=platform, version=version, name="测试号")


def _resp(body):
    data = json.dumps(body).encode("utf-8")
    r = mock.MagicMock()
    r.read.return_value = data
    r.__enter__ = lambda self: self
    r.__exit__ = lambda self, *a: None
    return r


class HelperTests(unittest.TestCase):
    def test_num_ignores_invalid_values(self):
        self.assertEqual(quota._num("1250"), 1250.0)
        self.assertEqual(quota._num(500), 500.0)
        self.assertIsNone(quota._num("NaN"))
        self.assertIsNone(quota._num(""))
        self.assertIsNone(quota._num(None))
        self.assertIsNone(quota._num("-3"))

    def test_sum_packages(self):
        packages = [
            {"CycleTotalCapacity": "1250", "CycleRemainCapacity": "1250", "CycleUsedCapacity": "0"},
            {"CycleTotalCapacity": 500, "CycleRemainCapacity": 0, "CycleUsedCapacity": 500},
            {"CycleTotalCapacity": "-3", "CycleRemainCapacity": "NaN", "CycleUsedCapacity": None},
        ]
        self.assertEqual(quota._sum_packages(packages), (1750.0, 1250.0, 500.0))
        self.assertEqual(quota._sum_packages([]), (None, None, None))

    def test_parse_time_milliseconds_and_beijing_string(self):
        ms = quota._parse_time(1797496308000)
        self.assertIsNotNone(ms)
        self.assertEqual(ms.strftime("%Y-%m-%d"), "2026-12-17")
        text = quota._parse_time("2026-12-17 16:31:48")
        self.assertIsNotNone(text)
        self.assertEqual(text.strftime("%Y-%m-%d %H:%M"), "2026-12-17 16:31")

    def test_earliest_expiry_groups_same_day_and_skips_invalid(self):
        accounts = [
            {"CycleCapacityRemainPrecise": "688.28", "DeductionEndTime": 1797496308000, "CapacityType": 1},
            {"CycleCapacityRemain": 100, "CycleEndTime": "2026-12-17 16:34:09", "CapacityType": 1},
            {"CycleCapacityRemainPrecise": "0", "CycleEndTime": "2026-01-01 00:00:00", "CapacityType": 1},
        ]
        when, credits = quota._earliest_expiry(accounts)
        self.assertEqual(when, "2026-12-17 16:31")
        self.assertEqual(credits, 788.28)
        self.assertEqual(quota._earliest_expiry([]), ("", None))


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.account = _account()
        self.cred = Credential(token="secret-token-value", source="vault",
                               fields={"uid": "u-1", "domain": "www.workbuddy.cn"})

    def test_full_flow_summary_and_packages(self):
        summary = {"code": 0, "data": {
            "Packages": [
                {"PackageCode": "TCACA_code_008_cfWoLwvjU4", "CycleTotalCapacity": "500",
                 "CycleRemainCapacity": "0", "CycleUsedCapacity": "500"},
                {"PackageCode": "TCACA_code_030_BjSt89qTvr", "CycleTotalCapacity": "1500",
                 "CycleRemainCapacity": "688.28", "CycleUsedCapacity": "811.72"},
            ],
            "TotalCredits": "688.28", "SubscriptionPackageName": " 体验版 ",
            "IsPaidUser": False,
        }}
        detail = {"code": 0, "data": {"Accounts": [
            {"PackageCode": "TCACA_code_030_BjSt89qTvr", "CapacityType": 1,
             "CycleEndTime": "2026-12-17 16:31:48", "DeductionEndTime": 1797496308000,
             "CycleCapacityRemainPrecise": "688.28"},
        ]}}
        def fake_post(url, body, headers, timeout=15):
            if url.endswith("get-user-resource-summary"):
                return summary
            if "free-packages" in url:
                return detail
            return {"code": 0, "data": {"Accounts": []}}

        with mock.patch.object(quota, "_post_json", side_effect=fake_post):
            info = quota._workbuddy(self.account, self.cred, quota.QuotaInfo(
                key="wb:cn:a", platform="workbuddy", version="cn", account_id="a"))
        self.assertTrue(info.ok)
        self.assertEqual(info.remaining, 688.28)
        self.assertEqual(info.total, 2000)  # 总额 = 各包 CycleTotalCapacity 之和，不是 TotalCredits
        self.assertEqual(info.plan, "体验版")
        self.assertEqual(info.expires_at, "2026-12-17 16:31")
        self.assertEqual(info.expires_credits, 688.28)
        d = info.to_dict()
        self.assertNotIn("secret-token-value", json.dumps(d, ensure_ascii=False))

    def test_total_is_capacity_sum_not_totalcredits(self):
        """真实响应里 TotalCredits 是剩余积分，总额需从资源包容量汇总。"""
        summary = {"code": 0, "data": {
            "Packages": [
                {"PackageCode": "TCACA_code_008_x", "CycleTotalCapacity": "500",
                 "CycleRemainCapacity": "0", "CycleUsedCapacity": "500"},
                {"PackageCode": "TCACA_code_030_y", "CycleTotalCapacity": "1500",
                 "CycleRemainCapacity": "688.28", "CycleUsedCapacity": "811.72"},
                {"PackageCode": "TCACA_code_007_z", "CycleTotalCapacity": "1350",
                 "CycleRemainCapacity": "1350", "CycleUsedCapacity": "0"},
            ],
            "CreditGroups": [
                {"Kind": "plan_base", "CycleTotalCapacity": "500", "CycleRemainCapacity": "0", "CycleUsedCapacity": "500"},
                {"Kind": "benefit", "CycleTotalCapacity": "2850", "CycleRemainCapacity": "2038.28", "CycleUsedCapacity": "811.72"},
            ],
            "TotalCredits": "2038.28", "SubscriptionPackageName": "体验版",
        }}
        with mock.patch.object(quota, "_post_json", side_effect=lambda url, body, headers, timeout=15: summary):
            info = quota._workbuddy(self.account, self.cred, quota.QuotaInfo())
        self.assertEqual(info.remaining, 2038.28)
        self.assertEqual(info.total, 3350)

    def test_nonzero_code_raises(self):
        with mock.patch.object(quota, "_post_json", return_value={"code": 10001, "msg": "失效"}):
            with self.assertRaises(RuntimeError):
                quota._workbuddy(self.account, self.cred, quota.QuotaInfo())


class QoderTests(unittest.TestCase):
    def test_cn_quota_merges_user_and_addon(self):
        payload = {
            "userType": "personal_standard",
            "expiresAt": 253402214400000,  # 永不过期占位
            "userQuota": {"total": 1000.0, "used": 400.0, "remaining": 600.0},
            "addOnQuota": {"total": 600.0, "used": 594.0, "remaining": 6.0},
        }
        with mock.patch.object(quota, "_post_json", return_value=payload):
            info = quota._qoder(_account("qoder", "cn"), Credential(token="x"), quota.QuotaInfo())
        self.assertTrue(info.ok)
        self.assertEqual(info.remaining, 606.0)
        self.assertEqual(info.total, 1600.0)
        self.assertEqual(info.expires_at, "")  # 远未来视为永不过期

    def test_token_expire_raises_readable(self):
        with mock.patch.object(quota, "_post_json", return_value={"code": "TOKEN_EXPIRE"}):
            with self.assertRaises(RuntimeError):
                quota._qoder(_account("qoder", "global"), Credential(token="x"), quota.QuotaInfo())


class TraeTests(unittest.TestCase):
    def test_ent_usage_fields(self):
        payload = {"code": 0, "data": {
            "remainingCredits": 200, "totalCredits": 1000,
            "identityStr": "Pro", "expiresAt": "2026-12-20 23:59:59",
        }}
        with mock.patch.object(quota, "_post_json", return_value=payload):
            info = quota._trae(_account("trae", "cn"),
                               Credential(token="x", fields={"device_id": "d-1"}), quota.QuotaInfo())
        self.assertTrue(info.ok)
        self.assertEqual(info.remaining, 200)
        self.assertEqual(info.total, 1000)
        self.assertEqual(info.plan, "Pro")
        self.assertEqual(info.expires_at, "2026-12-20 23:59")

    def test_real_ent_usage_shape(self):
        """2026-11 实测的真实响应字段：total_balance / usage_summary / 秒级 end_time。"""
        payload = {"total_balance": 961.32,
                   "balance_summary": [{"source": 1, "total_balance": 961.32}],
                   "usage_summary": {"consumed_amount": 838.68, "total_amount": 1800},
                   "user_entitlement_pack_list": [
                       {"display_desc": "免费", "status": 1,
                        "entitlement_base_info": {"end_time": 1796054399}},
                       {"display_desc": "签到奖励", "status": 1,
                        "entitlement_base_info": {"end_time": 1797446399}},
                   ]}
        with mock.patch.object(quota, "_post_json", return_value=payload):
            info = quota._trae(_account("trae", "cn"),
                               Credential(token="x", fields={"device_id": "d-1"}), quota.QuotaInfo())
        self.assertTrue(info.ok)
        self.assertEqual(info.remaining, 961.32)
        self.assertEqual(info.total, 1800)
        self.assertEqual(info.used, 838.68)
        self.assertEqual(info.expires_at, "2026-11-30 23:59")

    def test_unknown_shape_raises(self):
        with mock.patch.object(quota, "_post_json", return_value={"code": 0, "data": {}}):
            with self.assertRaises(RuntimeError):
                quota._trae(_account("trae", "cn"), Credential(token="x"), quota.QuotaInfo())


class CacheAndFetchTests(unittest.TestCase):
    def test_cache_hit_and_force(self):
        cache = quota.QuotaCache(ttl=60)
        calls = []
        def fake_fetch(account, allow):
            calls.append(account.id)
            return quota.QuotaInfo(key=f"{account.platform}:{account.version}:{account.id}",
                                   ok=True, remaining=10, queried_at=time.time())
        a = _account()
        with mock.patch.object(quota, "_fetch_one", side_effect=fake_fetch):
            quota.query_quotas([a], cache=cache)
            quota.query_quotas([a], cache=cache)
            self.assertEqual(len(calls), 1)
            quota.query_quotas([a], cache=cache, force=True)
            self.assertEqual(len(calls), 2)

    def test_fetch_failure_degrades_with_reason(self):
        with mock.patch.object(quota, "resolve_credential", side_effect=RuntimeError("凭据文件损坏")):
            info = quota._fetch_one(_account(), False)
        self.assertFalse(info.ok)
        self.assertIn("凭据文件损坏", info.reason)
        self.assertIsNone(info.remaining)

    def test_global_versions_skip_quota_with_neutral_message(self):
        """国际版额度接口实测不可用：不发请求、不报错，只给中性提示。"""
        for platform in ("workbuddy", "trae"):
            with mock.patch.object(quota, "resolve_credential") as resolve, \
                 mock.patch.object(quota, "_post_json") as post:
                info = quota._fetch_one(_account(platform, "global"), False)
            self.assertFalse(info.ok)
            self.assertTrue(info.no_activity)
            self.assertEqual(info.reason, "未提供活动签到")
            self.assertIsNone(info.remaining)
            self.assertFalse(post.called)
            self.assertFalse(resolve.called)
            d = info.to_dict()
            self.assertTrue(d["no_activity"])
            self.assertNotIn("HTTP 404", d["reason"])

    def test_http_404_on_supported_version_keeps_explicit_reason(self):
        """其它版本的真实接口失败仍保留明确原因，不被中性提示掩盖。"""
        cred = Credential(token="x")
        with mock.patch.object(quota, "resolve_credential", return_value=cred), \
             mock.patch.object(quota, "_post_json", side_effect=RuntimeError("HTTP 404")):
            info = quota._fetch_one(_account("trae", "cn"), False)
        self.assertFalse(info.ok)
        self.assertFalse(info.no_activity)
        self.assertIn("HTTP 404", info.reason)

    def test_credentials_default_endpoint_only_for_cn(self):
        cred = Credential(token="x")
        with mock.patch.dict("os.environ", {"WORKBUDDY_ENDPOINT": ""}):
            credential_module._inject_common_env(cred, _account("workbuddy", "cn"))
            self.assertEqual(cred.fields.get("endpoint"), "https://copilot.tencent.com")
            global_cred = Credential(token="x")
            credential_module._inject_common_env(global_cred, _account("workbuddy", "global"))
            self.assertNotIn("endpoint", global_cred.fields)

    def test_missing_token_reason(self):
        with mock.patch.object(quota, "resolve_credential", return_value=Credential(token="")):
            info = quota._fetch_one(_account(), False)
        self.assertFalse(info.ok)
        self.assertIn("未配置凭据", info.reason)


if __name__ == "__main__":
    unittest.main()
