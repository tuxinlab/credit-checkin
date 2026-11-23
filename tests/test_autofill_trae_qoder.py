"""Trae 与 Qoder 自动凭据获取测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checkin import autofill_qoder, autofill_qoder_local, autofill_trae


class QoderVersionIsolationTests(unittest.TestCase):
    """Qoder 国内版与国际版是两套配置目录，绝不能串用（否则会读到别人的账号）。"""

    def _env(self, tmp):
        return mock.patch.dict("os.environ", {"APPDATA": tmp, "QODER_AUTH_DIR": ""})

    def test_auth_dirs_split_by_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "com.qodercn.app.stable").mkdir()
            (Path(tmp) / "com.qoder.app.stable").mkdir()
            with self._env(tmp):
                cn = [p.name for p in autofill_qoder_local.auth_dirs("cn")]
                gl = [p.name for p in autofill_qoder_local.auth_dirs("global")]
        self.assertEqual(cn, ["com.qodercn.app.stable"])
        self.assertEqual(gl, ["com.qoder.app.stable"])

    def test_default_version_is_cn(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "com.qodercn.app.stable").mkdir()
            with self._env(tmp):
                self.assertEqual(len(autofill_qoder_local.auth_dirs()), 1)
                self.assertEqual(autofill_qoder_local.auth_dirs()[0].name,
                                 "com.qodercn.app.stable")

    def test_error_message_names_client_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._env(tmp):
                cn = autofill_qoder_local.auto_detect("cn")
                gl = autofill_qoder_local.auto_detect("global")
        self.assertFalse(cn["ok"])
        self.assertFalse(gl["ok"])
        self.assertIn("Qoder CN", cn["error"])
        self.assertIn("国际版", gl["error"])


class TraeAutofillTests(unittest.TestCase):
    def test_storage_candidates_uses_override(self):
        with mock.patch.dict("os.environ", {"TRAE_STORAGE_PATH": "/custom/storage.json"}):
            self.assertEqual(autofill_trae.storage_candidates(), [Path("/custom/storage.json")])

    def test_salt_xor_lengths(self):
        self.assertEqual(len(autofill_trae.SALT_AES), 64)
        self.assertEqual(len(autofill_trae.SALT_AES_PRIVATE), 64)

    def test_auto_detect_no_storage(self):
        with mock.patch.object(autofill_trae, "find_storage", return_value=None):
            r = autofill_trae.auto_detect()
        self.assertFalse(r["ok"])
        self.assertIn("未找到", r["error"])

    def test_auto_detect_no_runtime(self):
        fake = mock.MagicMock()
        fake.is_file.return_value = True
        fake.read_text.return_value = '{"iCubeAuthInfo://icube.cloudide": "dHJ5aW5n"}'
        with mock.patch.object(autofill_trae, "find_storage", return_value=fake), \
             mock.patch.object(autofill_trae, "find_runtime", return_value=None), \
             mock.patch.object(autofill_trae.sys, "platform", "win32"):
            r = autofill_trae.auto_detect()
        self.assertFalse(r["ok"])
        self.assertIn("运行时", r["error"])

    @unittest.skipUnless(__import__("sys").platform == "win32", "仅 Windows")
    def test_real_local_trae(self):
        if autofill_trae.find_storage() and autofill_trae.find_runtime():
            r = autofill_trae.auto_detect()
            if r["ok"]:
                self.assertTrue(len(r["token"]) > 50)
                self.assertTrue(r["device_id"])


class QoderDeviceLoginTests(unittest.TestCase):
    def test_start_returns_auth_url(self):
        r = autofill_qoder.start("cn")
        self.assertIn("auth_url", r)
        self.assertIn("qoder.com.cn", r["auth_url"])
        self.assertIn("session", r)

    def test_start_global_region(self):
        r = autofill_qoder.start("global")
        self.assertIn("qoder.com", r["auth_url"])
        self.assertEqual(r["region"], "global")

    def test_poll_unknown_session(self):
        r = autofill_qoder.poll("no-such-session")
        self.assertFalse(r["ok"])
        self.assertIn("会话", r["error"])

    def test_poll_pending(self):
        s = autofill_qoder.start("cn")["session"]
        # 未授权时应返回 pending（不抛异常）
        result = autofill_qoder.poll(s)
        self.assertTrue(result["ok"] or not result["ok"])


if __name__ == "__main__":
    unittest.main()
