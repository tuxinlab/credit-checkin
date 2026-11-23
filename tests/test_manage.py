"""配置管理、凭据保险库与定时任务配置的测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checkin import manage
from checkin.config import load_config, sample_config
from checkin.credentials import resolve as resolve_credential
from checkin.models import Account
from checkin.vault import CredentialVault, _dpapi_available


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(sample_config(), ensure_ascii=False), encoding="utf-8")
        # 关键：把 CHECKIN_HOME 指向临时目录，避免测试读写/删除用户真实保险库
        self._env = mock.patch.dict("os.environ", {"CHECKIN_HOME": str(self.root)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_add_and_list_account(self):
        manage.add_account(self.config, "qoder", "cn", "qoder-cn-02", "小号")
        cfg = load_config(self.config)
        ids = [a.id for a in cfg["accounts"]]
        self.assertIn("qoder-cn-02", ids)
        self.assertEqual(len(ids), 7)

    def test_add_account_auto_generates_unique_id(self):
        """不传 ID 时按 平台缩写-版本-序号 自动分配，且不冲突。"""
        first = manage.add_account(self.config, "qoder", "global", "")
        self.assertEqual(first["id"], "qoder-gl-02")  # 模板里已有 qoder-gl-01
        second = manage.add_account(self.config, "qoder", "global", "")
        self.assertEqual(second["id"], "qoder-gl-03")
        wb = manage.add_account(self.config, "workbuddy", "cn", "")
        self.assertEqual(wb["id"], "wb-cn-02", "WorkBuddy 用 wb 缩写前缀")

    def test_add_rejects_duplicate_and_bad_id(self):
        with self.assertRaises(manage.ManageError):
            manage.add_account(self.config, "qoder", "cn", "qoder-cn-01")
        with self.assertRaises(manage.ManageError):
            manage.add_account(self.config, "qoder", "cn", "bad id!")

    def test_remove_account(self):
        self.assertTrue(manage.remove_account(self.config, "qoder", "cn", "qoder-cn-01"))
        cfg = load_config(self.config)
        self.assertNotIn("qoder-cn-01", [a.id for a in cfg["accounts"]])
        self.assertFalse(manage.remove_account(self.config, "qoder", "cn", "no-such"))

    def test_enable_account_also_enables_version(self):
        """只点「启用账号」也必须真的生效：自动打开上层版本/平台开关。"""
        raw = manage.read_raw(self.config)
        self.assertFalse(raw["platforms"]["qoder"]["versions"]["global"]["enabled"])
        listed = manage.list_accounts(load_config(self.config), raw)
        row = next(r for r in listed if r["id"] == "qoder-gl-01")
        self.assertFalse(row["version_enabled"])

        manage.set_account_enabled(self.config, "qoder", "global", "qoder-gl-01", True)

        cfg = load_config(self.config)
        acc = next(a for a in cfg["accounts"] if a.id == "qoder-gl-01")
        self.assertTrue(acc.enabled, "启用账号后有效状态应为启用")
        raw2 = manage.read_raw(self.config)
        self.assertTrue(raw2["platforms"]["qoder"]["versions"]["global"]["enabled"])
        row2 = next(r for r in manage.list_accounts(cfg, raw2) if r["id"] == "qoder-gl-01")
        self.assertTrue(row2["version_enabled"])
        self.assertTrue(row2["enabled"])

    def test_unsupported_version_cannot_be_enabled(self):
        """无签到活动的版本：可以停用，但拒绝启用/新增，避免每天白跑。"""
        for platform, version, account_id in (
                ("trae", "global", "trae-gl-01"),
                ("workbuddy", "global", "wb-gl-01")):
            with self.assertRaises(manage.ManageError):
                manage.set_account_enabled(self.config, platform, version, account_id, True)
            with self.assertRaises(manage.ManageError):
                manage.set_version_enabled(self.config, platform, version, True)
            with self.assertRaises(manage.ManageError):
                manage.add_account(self.config, platform, version, "")
            self.assertTrue(
                manage.set_account_enabled(self.config, platform, version, account_id, False))

    def test_unsupported_version_forced_disabled_in_config(self):
        """即使手改配置把它打开，加载时也必须强制停用。"""
        raw = manage.read_raw(self.config)
        raw["platforms"]["trae"]["versions"]["global"]["enabled"] = True
        raw["platforms"]["trae"]["versions"]["global"]["accounts"][0]["enabled"] = True
        manage.write_raw(self.config, raw)
        cfg = load_config(self.config)
        acc = next(a for a in cfg["accounts"] if a.id == "trae-gl-01")
        self.assertFalse(acc.enabled, "Trae 国际版无签到活动，不应被执行")

    def test_console_decode_handles_non_utf8(self):
        """中文 Windows 上 PowerShell 输出 GBK，解码不能崩（否则注册任务会误报失败）。"""
        from checkin.manage import _decode_console
        self.assertEqual(_decode_console("拒绝访问".encode("gbk")), "拒绝访问")
        self.assertEqual(_decode_console(b"OK|2026/11/19 10:20:00"), "OK|2026/11/19 10:20:00")

    def test_schedule_config(self):
        info = manage.set_schedule(self.config, True, "08:30")
        self.assertTrue(info["enabled"])
        self.assertEqual(info["time"], "08:30")
        raw = manage.read_raw(self.config)
        self.assertEqual(manage.get_schedule(raw)["time"], "08:30")
        with self.assertRaises(manage.ManageError):
            manage.set_schedule(self.config, True, "25:99")

    def test_token_guides(self):
        for p in ("workbuddy", "trae", "qoder"):
            guide = manage.token_guide(p)
            self.assertTrue(guide["title"])
            self.assertTrue(len(guide["steps"]) > 0)


class VaultIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault_path = self.root / "vault.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_qoder_device_identity_from_local_cache(self):
        """本机 UMID 缓存要能解析成 Cosy-* 头需要的字段；缺失时返回空。"""
        from checkin.credentials import qoder_device_identity
        base = self.root / ".qoder"
        (base / ".auth").mkdir(parents=True)
        (base / ".auth" / "machine_id").write_text("mid-123", encoding="utf-8")
        (base / "umid-cache.json").write_text(json.dumps({
            "version": 1, "platform": "win32", "arch": "x64",
            "info": {"machineToken": "tok-abc", "machineCode": "code-abc",
                     "machineType": "type-abc", "vmInfo": {"isVm": False}},
        }), encoding="utf-8")
        got = qoder_device_identity(base)
        self.assertEqual(got["machine_token"], "tok-abc")
        self.assertEqual(got["machine_code"], "code-abc")
        self.assertEqual(got["machine_type"], "type-abc")
        self.assertEqual(got["machine_id"], "mid-123")
        self.assertTrue(got["machine_hostname"])
        self.assertEqual(qoder_device_identity(self.root / "missing"), {})

    def test_vault_secrets_roundtrip(self):
        """refresh_token 等附加秘密要加密存储，文件里不能有明文。"""
        v = CredentialVault(self.vault_path)
        v.put("trae", "cn", "t1", "tok-123", {"device_id": "d1"},
              secrets={"refresh_token": "rt-secret-456"})
        item = v.get("trae", "cn", "t1")
        self.assertEqual(item["token"], "tok-123")
        self.assertEqual(item["secrets"]["refresh_token"], "rt-secret-456")
        raw = self.vault_path.read_text(encoding="utf-8")
        self.assertNotIn("rt-secret-456", raw, "refresh_token 不得明文落盘")

    def test_vault_roundtrip(self):
        v = CredentialVault(self.vault_path)
        enc = v.put("qoder", "cn", "a1", "pt-secret-token-abc", {"uid": "u1"})
        self.assertIn(enc, ("dpapi", "plain"))
        got = v.get("qoder", "cn", "a1")
        self.assertEqual(got["token"], "pt-secret-token-abc")
        self.assertEqual(got["fields"]["uid"], "u1")
        self.assertTrue(v.delete("qoder", "cn", "a1"))
        self.assertIsNone(v.get("qoder", "cn", "a1"))

    def test_vault_encrypted_on_windows(self):
        v = CredentialVault(self.vault_path)
        v.put("trae", "cn", "a1", "jwt-token-value-123")
        raw = self.vault_path.read_text(encoding="utf-8")
        self.assertNotIn("jwt-token-value-123", raw, "token 不应以明文落盘")

    def test_resolve_vault_via_env_override(self):
        """把 CHECKIN_HOME 指向临时目录后，凭据提供器应读到 vault 里的 token。"""
        import os
        from unittest import mock
        (self.root / "data").mkdir(exist_ok=True)
        v = CredentialVault(self.root / "data" / "vault.json")
        v.put("qoder", "cn", "acc1", "pt-from-vault-token", {"uid": "v1"})
        acc = Account(id="acc1", platform="qoder", version="cn",
                      credential_env="UNSET_TOKEN_VAR")
        with mock.patch.dict(os.environ, {"CHECKIN_HOME": str(self.root)}):
            cred = resolve_credential(acc)
        self.assertEqual(cred.token, "pt-from-vault-token")
        self.assertEqual(cred.source, "vault")
        self.assertEqual(cred.fields["uid"], "v1")


if __name__ == "__main__":
    unittest.main()
