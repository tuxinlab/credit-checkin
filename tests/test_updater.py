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
    def _make_resp(body: bytes):
        r = mock.MagicMock()
        r.read.return_value = body
        r.__enter__ = lambda self: self
        r.__exit__ = lambda self, *a: None
        return r

    @classmethod
    def _atom(cls, tag: str, title: str = "") -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/{tag}</id>
    <title>{title or tag}</title>
    <link rel="alternate" type="text/html"
          href="https://github.com/tuxinlab/credit-checkin/releases/tag/{tag}"/>
  </entry>
</feed>""".encode("utf-8")

    @classmethod
    def _api_json(cls, tag: str) -> bytes:
        return json.dumps({
            "tag_name": tag, "html_url": f"https://github.com/u/r/releases/tag/{tag}",
            "body": "说明", "assets": [{"name": "CreditCheckin.exe",
                                        "browser_download_url": "https://x/CreditCheckin.exe"}],
        }).encode("utf-8")

    def test_atom_newer_release(self):
        """atom feed 是主路径：无需认证、不受 API 速率限制。"""
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=self._make_resp(self._atom("v9.9.9", "v9.9.9 测试"))):
            info = check_update(force=True)
        self.assertTrue(info.has_update)
        self.assertEqual(info.latest, "9.9.9")
        self.assertIn("/releases/download/v9.9.9/CreditCheckin.exe", info.download_url)
        self.assertFalse(info.error)

    def test_atom_current_version_no_update(self):
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=self._make_resp(self._atom("v0.1.0"))):
            info = check_update(force=True)
        self.assertFalse(info.has_update)
        self.assertEqual(info.latest, "0.1.0")

    def test_api_fallback_when_atom_fails(self):
        """atom 解析失败时回退到 API。"""
        def side_effect(*a, **k):
            if side_effect.calls == 0:
                side_effect.calls += 1
                raise ValueError("bad feed")
            return self._make_resp(self._api_json("v9.9.9"))
        side_effect.calls = 0
        with mock.patch.object(updater, "_configured", return_value=True), \
             mock.patch.object(updater.urllib.request, "urlopen", side_effect=side_effect):
            info = check_update(force=True)
        self.assertTrue(info.has_update)
        self.assertEqual(info.latest, "9.9.9")
        self.assertEqual(info.notes, "说明")

    def test_all_sources_fail_is_silent(self):
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
        fake = UpdateInfo(current="0.1.0", latest="0.1.0", checked_at=1e18)
        with mock.patch.object(updater, "_fetch", return_value=fake) as m:
            check_update(force=True)
            check_update()  # 命中缓存
            self.assertEqual(m.call_count, 1)

    def test_force_bypasses_cache(self):
        fake = UpdateInfo(current="0.1.0", latest="0.1.0", checked_at=1e18)
        with mock.patch.object(updater, "_fetch", return_value=fake) as m:
            check_update(force=True)
            check_update(force=True)
            self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
