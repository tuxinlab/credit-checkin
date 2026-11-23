"""更新检查模块测试：版本比对、GitHub 响应解析、失败静默、缓存。"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from checkin import updater
from checkin.updater import UpdateInfo, _is_newer, _version_key, check_update, clear_cache


class VersionCompareTests(unittest.TestCase):
    def test_version_key(self):
        self.assertEqual(_version_key("0.1.0"), (0, 1, 0))
        self.assertEqual(_version_key("v0.2.1"), (0, 2, 1))
        self.assertEqual(_version_key("garbage"), (0,))

    def test_is_newer(self):
        self.assertTrue(_is_newer("0.2.0", "0.1.0"))
        self.assertTrue(_is_newer("0.1.1", "0.1.0"))
        self.assertFalse(_is_newer("0.1.0", "0.1.0"))
        self.assertFalse(_is_newer("0.0.9", "0.1.0"))


class FetchTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    @staticmethod
    def _response(payload):
        body = json.dumps(payload).encode("utf-8")
        r = mock.MagicMock()
        r.read.return_value = body
        r.__enter__ = lambda self: self
        r.__exit__ = lambda self, *a: None
        return r

    def test_newer_release_with_exe_asset(self):
        payload = {
            "tag_name": "v9.9.9", "html_url": "https://github.com/u/r/releases/tag/v9.9.9",
            "body": "修复若干问题",
            "assets": [{"name": "CreditCheckin.exe",
                        "browser_download_url": "https://github.com/u/r/releases/download/v9.9.9/CreditCheckin.exe"}],
        }
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", return_value=self._response(payload)):
            info = check_update(force=True)
        self.assertTrue(info.has_update)
        self.assertEqual(info.latest, "9.9.9")
        self.assertIn("CreditCheckin.exe", info.download_url)
        self.assertFalse(info.error)

    def test_current_version_no_update(self):
        payload = {"tag_name": "v0.0.1", "html_url": "x", "body": "", "assets": []}
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", return_value=self._response(payload)):
            info = check_update(force=True)
        self.assertFalse(info.has_update)
        self.assertEqual(info.latest, "0.0.1")

    def test_network_failure_is_silent(self):
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", side_effect=OSError("no network")):
            info = check_update(force=True)
        self.assertFalse(info.has_update)
        self.assertIn("OSError", info.error)
        self.assertEqual(info.current, "0.1.0")

    def test_not_configured(self):
        with mock.patch.object(updater, "_configured", return_value=False):
            info = check_update(force=True)
        self.assertIn("未配置", info.error)

    def test_cache_avoids_repeated_requests(self):
        payload = {"tag_name": "v0.1.0", "assets": [], "html_url": "", "body": ""}
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", return_value=self._response(payload)) as m:
            check_update(force=True)
            check_update()  # 命中缓存
            self.assertEqual(m.call_count, 1)

    def test_force_bypasses_cache(self):
        payload = {"tag_name": "v0.1.0", "assets": [], "html_url": "", "body": ""}
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", return_value=self._response(payload)) as m:
            check_update(force=True)
            check_update(force=True)
            self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
