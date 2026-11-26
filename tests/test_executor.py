"""执行器测试：重试、漂移落库、通知开关。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta

TODAY = date.today()
from pathlib import Path

from checkin.adapters.registry import AdapterRegistry
from checkin.executor import Executor, RunBusy
from checkin.models import Account, DailyTask, DriftEvent, RunStatus
from checkin.notifier import Notifier
from checkin.storage import Storage

CONTRACT = {
    "platform": "trae", "version": "cn", "name": "测试", "contract_version": "v1",
    "template": False, "defaults": {"timeout": 5},
    "auth": {"type": "bearer", "header": "Authorization", "prefix": "Bearer "},
    "steps": [{"id": "checkin", "method": "POST", "url": "https://example.com/checkin",
               "headers": {}, "body": {}}],
    "response": {
        "json_paths": {
            "success": ["$.code"], "already": ["$.data.checked_in"],
            "failed": ["$.code"], "credits": ["$.data.points"], "message": ["$.msg"],
        },
        "values": {"success": [0], "already": [True], "failed": [400, 401, 500]},
        "expected_shape": {"type": "object", "required_paths": ["$.code", "$.data"],
                           "field_types": {}},
    },
}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "contracts").mkdir()
        (self.root / "contracts" / "trae.cn.json").write_text(
            json.dumps(CONTRACT), encoding="utf-8")
        self.storage = Storage(self.root / "db.sqlite")
        self.acc = Account(id="t1", platform="trae", version="cn", credential="token")
        self.cfg = {
            "timezone": "Asia/Shanghai", "catchup_days": 0,
            "retry": {"max_attempts": 3, "backoff_seconds": [0, 0, 0]},
            "notify": {"channels": [], "on_failure": False, "on_drift": False},
            "accounts": [self.acc],
            "contracts_dir": str(self.root / "contracts"),
        }
        self.notifier = Notifier(self.cfg["notify"])

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _executor(self, http_fn):
        registry = AdapterRegistry(self.cfg["contracts_dir"], http_fn=http_fn)
        return Executor(self.cfg, self.storage, registry, self.notifier)

    def test_retry_then_success(self):
        calls = []

        def flaky(method, url, headers, body, timeout):
            calls.append(1)
            if len(calls) < 3:
                return 500, json.dumps({"code": 500, "data": {}, "msg": "temp"})
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 200}})

        task = DailyTask("trae", "cn", "t1", TODAY)
        rec = self._executor(flaky).run_task(task)
        self.assertEqual(rec.status, RunStatus.SUCCESS.value)
        self.assertEqual(rec.attempts, 3)
        self.assertEqual(len(self.storage.attempts_for(task.key)), 3)

    def test_drift_recorded_once_without_retry(self):
        calls = []

        def broken(method, url, headers, body, timeout):
            calls.append(1)
            return 200, json.dumps({"code": 0})  # 缺 $.data

        task = DailyTask("trae", "cn", "t1", TODAY)
        rec = self._executor(broken).run_task(task)
        self.assertEqual(rec.status, RunStatus.DRIFT.value)
        self.assertEqual(len(calls), 1, "漂移应停止重试")
        events = self.storage.drifts(10)
        self.assertEqual(len(events), 1)
        self.assertIn("trae", events[0].platform)

    def test_dedupe_skips_completed_task(self):
        def ok(method, url, headers, body, timeout):
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 100}})

        executor = self._executor(ok)
        task = DailyTask("trae", "cn", "t1", TODAY)
        first = executor.run_task(task)
        self.assertEqual(first.status, RunStatus.SUCCESS.value)

        def boom(*a, **k):
            raise AssertionError("去重后不应再次请求")
        second = self._executor(boom).run_task(task)
        self.assertEqual(second.status, RunStatus.SUCCESS.value)
        self.assertEqual(second.key, first.key)

    def test_auth_failure_is_not_retried(self):
        """凭据失效是确定性失败：只尝试一次，不要等 60s/300s 退避。"""
        calls = []

        def unauthorized(method, url, headers, body, timeout):
            calls.append(1)
            return 401, json.dumps({"code": "TOKEN_EXPIRE", "message": "token is not active"})

        fatal_contract = json.loads(json.dumps(CONTRACT))
        fatal_contract.update({"version": "global", "contract_version": "v2"})
        # 401 响应里不会有 data，结构校验只约束 code 字段
        fatal_contract["response"]["expected_shape"] = {
            "type": "object", "required_paths": ["$.code"], "field_types": {}}
        fatal_contract["response"]["verdicts"] = [{
            "status": "failed", "fatal": True, "steps": ["checkin"],
            "message": "凭据已失效",
            "match": {"$.code": {"in": ["TOKEN_EXPIRE"]}},
        }]
        (self.root / "contracts" / "trae.global.json").write_text(
            json.dumps(fatal_contract), encoding="utf-8")

        self.cfg["accounts"].append(
            Account(id="t2", platform="trae", version="global", credential="tok"))
        executor = self._executor(unauthorized)
        rec = executor.run_task(DailyTask("trae", "global", "t2", TODAY))

        self.assertEqual(len(calls), 1, "鉴权失效不应重试")
        self.assertEqual(rec.status, RunStatus.FAILED.value)
        self.assertEqual(rec.attempts, 1)

    def test_success_is_not_downgraded_to_skipped(self):
        """重跑时若本次判为「跳过」，不能把今天已成功的记录和积分覆盖掉。"""
        contract = json.loads(json.dumps(CONTRACT))
        contract["response"]["verdicts"] = [
            {"status": "skipped", "steps": ["checkin"], "message": "今日无可领",
             "match": {"$.data.no_activity": {"equals": [True]}}},
            {"status": "success", "steps": ["checkin"], "message": "领取成功",
             "credits_path": "$.data.points",
             "match": {"$.code": {"equals": [0]}}},
        ]
        (self.root / "contracts" / "trae.cn.json").write_text(
            json.dumps(contract), encoding="utf-8")

        def ok(method, url, headers, body, timeout):
            return 200, json.dumps({"code": 0, "data": {"points": 100}})

        task = DailyTask("trae", "cn", "t1", TODAY)
        first = self._executor(ok).run_task(task)
        self.assertEqual(first.status, RunStatus.SUCCESS.value)

        def no_activity(method, url, headers, body, timeout):
            return 200, json.dumps({"code": 0, "data": {"no_activity": True}})

        second = self._executor(no_activity).run_task(task, force=True)
        self.assertEqual(second.status, RunStatus.SUCCESS.value,
                         "已有成功记录不应被「跳过」覆盖")
        self.assertEqual(second.credits, 100)
        # 库里也不能残留 running（upsert_start 会先把状态改掉，必须写回）
        stored = self.storage.get_run(task.key)
        self.assertEqual(stored.status, RunStatus.SUCCESS.value)
        self.assertEqual(stored.credits, 100)

    def test_auth_failure_triggers_refresh_then_retry(self):
        """鉴权失败 → 自动续期一次 → 用新凭据重试成功。"""
        http_calls = []

        def http(method, url, headers, body, timeout):
            http_calls.append(1)
            if len(http_calls) == 1:
                return 401, json.dumps({"code": "TOKEN_EXPIRE", "message": "expired"})
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 100}})

        fatal_contract = json.loads(json.dumps(CONTRACT))
        fatal_contract.update({"version": "global", "contract_version": "v2"})
        fatal_contract["response"]["expected_shape"] = {
            "type": "object", "required_paths": ["$.code"], "field_types": {}}
        fatal_contract["response"]["verdicts"] = [{
            "status": "failed", "fatal": True, "auth": True, "steps": ["checkin"],
            "message": "凭据已失效",
            "match": {"$.code": {"in": ["TOKEN_EXPIRE"]}},
        }]
        (self.root / "contracts" / "trae.global.json").write_text(
            json.dumps(fatal_contract), encoding="utf-8")

        self.cfg["accounts"].append(
            Account(id="t3", platform="trae", version="global", credential="tok"))
        executor = self._executor(http)

        class FakeRefresher:
            calls = 0

            def refresh(self, account, cred):
                self.calls += 1
                return True, "ok"

        executor.refresher = FakeRefresher()
        rec = executor.run_task(DailyTask("trae", "global", "t3", TODAY))
        self.assertEqual(rec.status, RunStatus.SUCCESS.value)
        self.assertEqual(len(http_calls), 2, "续期后应用新凭据重试")
        self.assertEqual(executor.refresher.calls, 1, "每轮最多续期一次")

    def test_refresh_uses_new_token_even_when_credential_from_env(self):
        """环境变量来源的凭据续期后，重试必须用新 token，而不是重新 resolve 拿回旧值。

        回归：保险库优先级低于环境变量，续期写回保险库后若重新解析，
        account.token() 仍从环境变量拿到过期 token，导致续期白做、重试再次 401。
        """
        import os
        from unittest import mock

        auth_headers = []

        def http(method, url, headers, body, timeout):
            auth_headers.append(dict(headers))
            if len(auth_headers) == 1:
                return 401, json.dumps({"code": "TOKEN_EXPIRE", "message": "expired"})
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 100}})

        fatal_contract = json.loads(json.dumps(CONTRACT))
        fatal_contract.update({"version": "global", "contract_version": "v2"})
        fatal_contract["response"]["verdicts"] = [{
            "status": "failed", "fatal": True, "auth": True, "steps": ["checkin"],
            "message": "凭据已失效",
            "match": {"$.code": {"in": ["TOKEN_EXPIRE"]}},
        }, {
            "status": "success", "steps": ["checkin"], "message": "领取成功",
            "credits_path": "$.data.points",
            "match": {"$.code": {"equals": [0]}},
        }]
        # 401 与 200 响应结构不同，expected_shape 只校验最小公共字段，避免误判 drift
        fatal_contract["response"]["expected_shape"] = {
            "type": "object", "required_paths": ["$.code"], "field_types": {}}
        (self.root / "contracts" / "trae.global.json").write_text(
            json.dumps(fatal_contract), encoding="utf-8")

        # 账号凭据来自环境变量（旧 token）
        env_name = "TEST_TRAE_TOKEN_REFRESH"
        with mock.patch.dict(os.environ, {env_name: "OLD-EXPIRED-TOKEN"}):
            acc = Account(id="t4", platform="trae", version="global",
                          credential_env=env_name)
            self.cfg["accounts"].append(acc)
            executor = self._executor(http)

            class FakeRefresher:
                def __init__(self):
                    self.last_token = None

                def refresh(self, account, cred):
                    self.last_token = "NEW-REFRESHED-TOKEN"
                    return True, "ok"

            executor.refresher = FakeRefresher()
            rec = executor.run_task(DailyTask("trae", "global", "t4", TODAY))

        self.assertEqual(rec.status, RunStatus.SUCCESS.value)
        self.assertEqual(len(auth_headers), 2, "应有一次续期后的重试")
        # 关键断言：第二次请求带的是续期后的新 token，而非环境变量里的旧值
        auth2 = auth_headers[1].get("Authorization", "")
        self.assertIn("NEW-REFRESHED-TOKEN", auth2,
                      "续期后重试必须使用新 token，不能因环境变量优先而用回旧 token")
        self.assertNotIn("OLD-EXPIRED-TOKEN", auth2)

    def test_expired_catchup_is_skipped_without_request(self):
        """追补过去日期不再发起请求（平台不允许补领过期积分，防止统计错位）。"""
        calls = []

        def http(method, url, headers, body, timeout):
            calls.append(1)
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 100}})

        executor = self._executor(http)
        yesterday = date.today() - timedelta(days=1)
        rec = executor.run_task(DailyTask("trae", "cn", "t1", yesterday))
        self.assertEqual(calls, [], "过期日期不应发出任何请求")
        self.assertEqual(rec.status, RunStatus.SKIPPED.value)
        self.assertIn("过期", rec.message)

    def test_run_tasks_locked_against_concurrency(self):
        """已有进程持锁时，run_tasks 应抛 RunBusy 而不是并行执行。"""
        from checkin.utils import RunLock

        lock = RunLock(self.root / "run.lock")
        ok, _ = lock.acquire()
        self.assertTrue(ok)
        try:
            executor = self._executor(lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应执行")))
            task = DailyTask("trae", "cn", "t1", date.today())
            with self.assertRaises(RunBusy):
                executor.run_tasks([task])
        finally:
            lock.release()

    def test_force_reruns(self):
        calls = []

        def ok(method, url, headers, body, timeout):
            calls.append(1)
            return 200, json.dumps({"code": 0, "data": {"checked_in": False, "points": 100}})

        executor = self._executor(ok)
        task = DailyTask("trae", "cn", "t1", TODAY)
        executor.run_task(task)
        executor.run_task(task, force=True)
        self.assertEqual(len(calls), 2)

    def test_simulate_does_not_touch_database(self):
        """模拟运行不得写入数据库，否则会占用去重位，导致真实签到显示"没有待执行"。"""
        def boom(*a, **k):
            raise AssertionError("模拟模式不应发真实请求")

        task = DailyTask("trae", "cn", "t1", TODAY)
        rec = self._executor(boom).run_task(task, simulate=True)
        self.assertEqual(rec.status, RunStatus.SUCCESS.value)
        # 数据库里不应有任何真实记录
        self.assertIsNone(self.storage.get_run(task.key))
        self.assertEqual(self.storage.runs_for_date(task.target_date.isoformat()), [])


    def _auth_contract(self, version="cn", platform="trae"):
        """构造一个把 TOKEN_EXPIRE 判为 fatal/auth 失败、随后可成功的契约。"""
        c = json.loads(json.dumps(CONTRACT))
        c.update({"platform": platform, "version": version, "contract_version": "v2"})
        c["auth"] = {"type": "header", "header": "Authorization",
                     "prefix": "Bearer ", "headers": {
                         "Authorization": "Bearer ${token}",
                         "x-device-id": "${cred.device_id}"}}
        c["response"]["expected_shape"] = {
            "type": "object", "required_paths": ["$.code"], "field_types": {}}
        c["response"]["verdicts"] = [{
            "status": "failed", "fatal": True, "auth": True, "steps": ["checkin"],
            "message": "凭据已失效",
            "match": {"$.code": {"in": ["TOKEN_EXPIRE"]}},
        }, {
            "status": "success", "steps": ["checkin"], "message": "领取成功",
            "credits_path": "$.data.points",
            "match": {"$.code": {"equals": [0]}},
        }]
        return c

    def test_local_recovery_then_retry_uses_new_token_and_fields(self):
        """凭据过期 → 本机自动获取 → 用新 token 和新字段重试成功。"""
        from unittest import mock
        from checkin import autofill_trae
        (self.root / "contracts" / "trae.cn.json").write_text(
            json.dumps(self._auth_contract()), encoding="utf-8")
        auth_headers = []

        def http(method, url, headers, body, timeout):
            auth_headers.append(dict(headers))
            if len(auth_headers) == 1:
                return 401, json.dumps({"code": "TOKEN_EXPIRE"})
            return 200, json.dumps({"code": 0, "data": {"points": 100}})

        with mock.patch.object(autofill_trae, "auto_detect", return_value={
                "ok": True, "token": "LOCAL-NEW", "refresh_token": "rt-new",
                "device_id": "dev-new"}):
            rec = self._executor(http).run_task(DailyTask("trae", "cn", "t1", TODAY))

        self.assertEqual(rec.status, RunStatus.SUCCESS.value)
        self.assertEqual(len(auth_headers), 2, "恢复后应重试一次")
        self.assertIn("LOCAL-NEW", auth_headers[1].get("Authorization", ""),
                      "重试必须用本机新凭据")
        self.assertEqual(auth_headers[1].get("x-device-id"), "dev-new",
                         "本机新字段 device_id 必须带进重试请求")

    def test_need_login_signal_when_all_recovery_fails(self):
        """Qoder 本机与续期都失败：记录里带 need_login，供面板弹授权窗。"""
        from unittest import mock
        import checkin.autofill_qoder_local as qmod
        contract = self._auth_contract("global", platform="qoder")
        (self.root / "contracts" / "qoder.global.json").write_text(
            json.dumps(contract), encoding="utf-8")
        self.cfg["accounts"].append(
            Account(id="q1", platform="qoder", version="global", credential="tok"))

        def http(method, url, headers, body, timeout):
            return 401, json.dumps({"code": "TOKEN_EXPIRE"})

        with mock.patch.object(qmod, "auto_detect", return_value={
                "ok": False, "error": "未找到登录态"}):
            executor = self._executor(http)
            rec = executor.run_task(DailyTask("qoder", "global", "q1", TODAY))

        self.assertEqual(rec.status, RunStatus.FAILED.value)
        self.assertIn("need_login", (rec.detail or ""),
                      "Qoder 全部自动恢复失败后应透出页面授权信号")


if __name__ == "__main__":
    unittest.main()
