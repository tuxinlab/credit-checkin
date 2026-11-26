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
                    "第一次使用", "以管理员身份运行", "setup-guide", "schedule-card"):
            self.assertIn(key, html)
        overview = html.split('<section id="tab-overview">')[1].split('</section>')[0]
        accounts = html.split('<section id="tab-accounts"')[1].split('</section>')[0]
        self.assertIn('id="quota-box"', overview, "额度信息应放在今日概览的账号状态卡片里")
        self.assertNotIn('id="quota-box"', accounts, "账号配置页不应再有单独的额度卡片")
        self.assertNotIn('id="stats"', html, "概览页顶部的统计卡片应已移除")
        self.assertNotIn('今日任务', html, "概览页不应再显示统计卡片文案")

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
