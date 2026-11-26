"""Web 面板后端测试：数据源、脱敏、执行接口、页面资源。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from checkin.adapters.registry import AdapterRegistry
from checkin.executor import Executor
from checkin.models import Account, DailyTask, RunStatus
from checkin.notifier import Notifier
from checkin.storage import Storage
from checkin.web import STATIC_DIR, WebState, _handle_run, _run_overview, create_server

ROOT = Path(__file__).resolve().parents[1]


def make_state(tmp: Path, simulate=True):
    storage = Storage(tmp / "db.sqlite")
    acc = Account(id="wb-1", platform="workbuddy", version="cn", credential="tok")
    cfg = {
        "timezone": "Asia/Shanghai",
        "checkin_window": {"start": "09:00", "end": "12:00"},
        "catchup_days": 3,
        "retry": {"max_attempts": 1, "backoff_seconds": [0]},
        "notify": {"channels": [], "on_failure": False, "on_drift": False},
        "accounts": [acc],
    }
    registry = AdapterRegistry(ROOT / "contracts", simulate=simulate)
    notifier = Notifier(cfg["notify"])
    logger = mock.MagicMock()
    executor = Executor(cfg, storage, registry, notifier, logger)
    ctx = {"cfg": cfg, "storage": storage, "registry": registry,
           "executor": executor, "notifier": notifier, "logger": logger}
    return WebState(ctx), ctx


class WebTests(unittest.TestCase):
    def test_static_page_exists_and_has_tabs(self):
        page = STATIC_DIR / "index.html"
        self.assertTrue(page.is_file(), "面板页面必须存在")
        html = page.read_text(encoding="utf-8")
        for key in ("今日概览", "执行历史", "账号配置", "契约状态", "异常与漂移",
                    "/api/run", "/api/account", "/api/credential", "/api/schedule",
                    "/api/quota", "账号额度与到期", "btn-refresh-quota", "quota.overview",
                    "quotaAccountCell", "accountInfoCell", "identity_note", "acctinfo",
                    "第一次使用", "以管理员身份运行", "setup-guide", "schedule-card"):
            self.assertIn(key, html)
        overview = html.split('<section id="tab-overview">')[1].split('</section>')[0]
        accounts = html.split('<section id="tab-accounts"')[1].split('</section>')[0]
        self.assertIn('id="quota-box"', overview, "额度信息应放在今日概览的账号状态卡片里")
        self.assertNotIn('id="quota-box"', accounts, "账号配置页不应再有单独的额度卡片")
        self.assertNotIn('id="stats"', html, "概览页顶部的统计卡片应已移除")
        self.assertNotIn('今日任务', html, "概览页不应再显示统计卡片文案")
        self.assertIn('openQoderAuth', html, "凭据过期应能拉起 Qoder 设备授权")
        self.assertIn('need_login', html, "前端应处理需页面授权信号")
        runs_fn = html.split('function renderRuns(')[1].split('\n}')[0]
        self.assertNotIn("'次数'", runs_fn, "次数列对使用者没意义，且会把表格挤宽")
        self.assertNotIn("'补跑'", runs_fn, "不做跨天补签，补跑列恒为否，应移除")

    def test_overview_shape_and_masking(self):
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            data = _run_overview(state)
            self.assertIn("today", data)
            self.assertIn("stats", data)
            self.assertEqual(len(data["accounts"]), 1)
            acc = data["accounts"][0]
            self.assertEqual(acc["credential_source"], "inline")
            self.assertNotIn("tok", json.dumps(data, ensure_ascii=False))
            self.assertTrue(acc["credential_masked"].startswith("to"))
            self.assertIn("*", acc["credential_masked"])
            ctx["storage"].close()

    def test_vault_identity_and_phone_masking(self):
        from checkin.web import _vault_identity_map
        fake = mock.MagicMock()
        fake.list_keys.return_value = ["workbuddy:cn:wb-1"]
        fake.get.return_value = {"token": "x",
                                 "fields": {"account_name": "喝口橙汁",
                                            "phone": "13308444140"}}
        with mock.patch("checkin.credentials.default_vault", return_value=fake):
            m = _vault_identity_map()
        self.assertEqual(m["workbuddy:cn:wb-1"]["identity"], "喝口橙汁")
        # 手机号必须脱敏后才返回页面
        self.assertEqual(m["workbuddy:cn:wb-1"]["phone"], "133****4140")

    def test_overview_exposes_identity(self):
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            ident = {"workbuddy:cn:wb-1": {"identity": "喝口橙汁", "phone": "133****4140"}}
            with mock.patch("checkin.web._vault_identity_map", return_value=ident):
                data = _run_overview(state)
            acc = data["accounts"][0]
            self.assertEqual(acc["identity"], "喝口橙汁")
            self.assertEqual(acc["identity_phone"], "133****4140")
            ctx["storage"].close()

    def test_run_api_simulate(self):
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            result = _handle_run(state, {"simulate": True, "no_catchup": True})
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["records"][0]["status"], RunStatus.SUCCESS.value)
            self.assertEqual(result["records"][0]["credits"], 100)
            ctx["storage"].close()

    def test_run_api_filter_platform(self):
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            result = _handle_run(state, {"simulate": True, "platform": "qoder", "no_catchup": True})
            self.assertEqual(result["records"], [], "过滤到不匹配平台时不应执行")
            ctx["storage"].close()

    def test_enable_unsupported_version_returns_400(self):
        """启用无签到活动的版本应返回 400 并说明原因，而不是让服务端报错断连。"""
        from checkin.config import sample_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps(sample_config(), ensure_ascii=False), encoding="utf-8")
            state, ctx = make_state(root)
            ctx["config_path"] = str(cfg_path)
            server = create_server(ctx, host="127.0.0.1", port=0)
            host, port = server.server_address
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                payload = json.dumps({"action": "enable", "platform": "trae",
                                      "version": "global", "id": "trae-gl-01",
                                      "enabled": True}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://{host}:{port}/api/account", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 400)
                body = json.loads(cm.exception.read().decode("utf-8"))
                self.assertIn("没有每日签到", body["error"])
            finally:
                server.shutdown()
                server.server_close()
                ctx["storage"].close()

    def test_identity_context_hides_reused_cn_identity(self):
        """国际版复用国内凭据时拿不到真实账号，必须留空而不是显示国内账号名。"""
        from checkin.web import _identity_context
        fake = mock.MagicMock()
        fake.list_keys.return_value = ["workbuddy:cn:wb-1", "workbuddy:global:wb-gl-1",
                                       "qoder:cn:q1", "qoder:global:q2"]
        fields = {
            ("workbuddy", "cn", "wb-1"): {"account_name": "喝口橙汁", "phone": "13308444140", "uid": "u-1"},
            ("workbuddy", "global", "wb-gl-1"): {"account_name": "喝口橙汁", "phone": "13308444140", "uid": "u-1"},
            ("qoder", "cn", "q1"): {"account_name": "q1", "uid": "q1"},
            ("qoder", "global", "q2"): {"account_name": "q2", "uid": "q2"},
        }
        fake.get.side_effect = lambda p, v, i: {"token": "x", "fields": fields[(p, v, i)]}
        accounts = [
            {"platform": "workbuddy", "version": "cn", "id": "wb-1"},
            {"platform": "workbuddy", "version": "global", "id": "wb-gl-1"},
            {"platform": "qoder", "version": "cn", "id": "q1"},
            {"platform": "qoder", "version": "global", "id": "q2"},
        ]
        with mock.patch("checkin.credentials.default_vault", return_value=fake):
            ctx = _identity_context(accounts)
        self.assertEqual(ctx["workbuddy:cn:wb-1"]["identity"], "喝口橙汁")
        self.assertEqual(ctx["workbuddy:global:wb-gl-1"]["identity"], "")
        self.assertEqual(ctx["workbuddy:global:wb-gl-1"]["phone"], "")
        self.assertEqual(ctx["workbuddy:global:wb-gl-1"]["uid"], "")
        self.assertIn("未获取到真实账号", ctx["workbuddy:global:wb-gl-1"]["identity_note"])
        # Qoder 国际版有独立登录态，身份不应被误判
        self.assertEqual(ctx["qoder:global:q2"]["identity"], "q2")

    def test_qoder_identity_falls_back_to_local_client(self):
        """旧凭据只存 uid 时，面板应从对应版本客户端补出真实账号名。"""
        from checkin.web import _QODER_LIVE_CACHE, _vault_identity_map
        fake = mock.MagicMock()
        fake.list_keys.return_value = ["qoder:cn:q1", "qoder:global:q2"]
        fake.get.side_effect = lambda p, v, i: {
            "token": "x", "fields": {"uid": f"uid-{i}"}}
        live = {"cn": {"identity": "aliyun2275463430", "phone": "133****4140"},
                "global": {"identity": "xin tu", "phone": ""}}
        with mock.patch("checkin.credentials.default_vault", return_value=fake), \
             mock.patch("checkin.web._qoder_live_identity", side_effect=lambda v: live[v]):
            _QODER_LIVE_CACHE.clear()
            m = _vault_identity_map()
        self.assertEqual(m["qoder:cn:q1"]["identity"], "aliyun2275463430")
        self.assertEqual(m["qoder:global:q2"]["identity"], "xin tu")
        # 补不到时仍退回 uid，不能显示空白
        with mock.patch("checkin.credentials.default_vault", return_value=fake), \
             mock.patch("checkin.web._qoder_live_identity", return_value={"identity": "", "phone": ""}):
            _QODER_LIVE_CACHE.clear()
            m2 = _vault_identity_map()
        self.assertEqual(m2["qoder:cn:q1"]["identity"], "uid-q1")

    def test_autofill_rejects_global_for_domestic_only_clients(self):
        """WorkBuddy/Trae 国际版点自动获取会误绑国内账号，应直接拒绝。"""
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            server = create_server(ctx, host="127.0.0.1", port=0)
            host, port = server.server_address
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(
                        f"http://{host}:{port}/api/autofill?platform=workbuddy"
                        "&version=global&id=wb-gl-01", timeout=5)
                self.assertEqual(cm.exception.code, 400)
                body = json.loads(cm.exception.read().decode("utf-8"))
                self.assertIn("国内版客户端", body["error"])
            finally:
                server.shutdown()
                server.server_close()
                ctx["storage"].close()

    def test_server_binds_localhost_only(self):
        with tempfile.TemporaryDirectory() as td:
            state, ctx = make_state(Path(td))
            server = create_server(ctx, host="127.0.0.1", port=0)
            host, port = server.server_address
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
            server.server_close()
            ctx["storage"].close()


if __name__ == "__main__":
    unittest.main()
