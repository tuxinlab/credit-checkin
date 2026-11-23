"""更新检查模块测试：版本比对、GitHub 响应解析、失败静默、缓存。"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

import tempfile
from pathlib import Path

from checkin import updater
from checkin.updater import (UpdateInfo, _is_newer, _version_key, apply_update,
                             build_apply_script, check_update, clear_cache,
                             download_update)


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
        self.assertEqual(info.current, updater.__version__)

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


class ApplyUpdateTests(unittest.TestCase):
    """一键替换：下载校验、脚本生成、模式判断。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    @staticmethod
    def _resp(body: bytes):
        r = mock.MagicMock()
        r.read.side_effect = [body, b""]
        r.__enter__ = lambda self: self
        r.__exit__ = lambda self, *a: None
        return r

    def _info(self, url: str) -> UpdateInfo:
        return UpdateInfo(current="0.1.0", latest="9.9.9", has_update=True,
                          download_url=url)

    def test_rejects_foreign_download_url(self):
        """下载地址不属于本仓库时必须拒绝，避免更新被劫持。"""
        with self.assertRaises(RuntimeError):
            download_update(self._info("https://evil.example/x.exe"), self.tmp)

    def test_rejects_non_pe_content(self):
        url = f"https://github.com/{updater.GITHUB_REPO}/releases/download/v9.9.9/CreditCheckin.exe"
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=self._resp(b"<html>not a program</html>")):
            with self.assertRaises(RuntimeError):
                download_update(self._info(url), self.tmp)
        self.assertFalse((self.tmp / "CreditCheckin.new.exe").exists())

    def test_downloads_valid_pe(self):
        url = f"https://github.com/{updater.GITHUB_REPO}/releases/download/v9.9.9/CreditCheckin.exe"
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=self._resp(b"MZ" + b"\x00" * 512)):
            out = download_update(self._info(url), self.tmp)
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes()[:2], b"MZ")

    def test_falls_back_to_versioned_asset_name(self):
        """固定名 404 时（发版只传了带版本号的附件）应自动改用带版本名。"""
        info = self._info(
            f"https://github.com/{updater.GITHUB_REPO}/releases/download/v9.9.9/CreditCheckin.exe")
        calls = []

        def fake_urlopen(req, timeout=60):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            if url.endswith("/CreditCheckin.exe"):
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return self._resp(b"MZ" + b"\x00" * 256)

        with mock.patch.object(updater.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = download_update(info, self.tmp)
        self.assertTrue(out.is_file())
        self.assertTrue(calls[0].endswith("/CreditCheckin.exe"))
        self.assertTrue(calls[1].endswith("/CreditCheckin-9.9.9.exe"))

    def test_apply_script_waits_and_rolls_back(self):
        """脚本必须：等进程退出、备份、覆盖、失败回滚。"""
        target = self.tmp / "CreditCheckin.exe"
        target.write_bytes(b"MZ-old")
        new = self.tmp / "CreditCheckin.new.exe"
        new.write_bytes(b"MZ-new")
        script = build_apply_script(target, new, pid=4321)
        self.assertTrue(script.is_file())
        raw = script.read_bytes()
        # cmd 只认 CRLF：LF-only 会把命令拼成一行而静默失效
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        content = raw.decode("mbcs", "replace")
        # 靠重命名探测文件释放，比查 PID 可靠（bootloader 可能延后释放）
        self.assertIn(":waitfree", content)
        self.assertIn(":restore", content)
        self.assertIn(":giveup", content)
        self.assertIn("move /y", content)
        self.assertIn('start "" "%OLD%"', content)
        # 必须清除 PyInstaller 的 _MEI 环境变量，否则新进程继承已删除的解压目录会崩溃
        self.assertIn("_PYI_APPLICATION_HOME_DIR=", content)
        self.assertIn("_MEIPASS2=", content)
        # 每行独立（之前 LF 导致 set 与 rem 拼成一行）
        self.assertNotIn('.bak"if', content)

    def test_apply_update_rejects_source_mode(self):
        """源码模式不支持自动替换，必须明确告知而不是静默失败。"""
        from checkin import paths as paths_mod
        with mock.patch.object(paths_mod, "is_frozen", return_value=False):
            r = apply_update(self._info("https://x/y.exe"), self.tmp)
        self.assertFalse(r["ok"])
        self.assertIn("git pull", r["message"])


if __name__ == "__main__":
    unittest.main()
