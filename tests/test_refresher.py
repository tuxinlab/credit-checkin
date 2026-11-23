"""自动续期测试：Trae/Qoder 协议、表单回退、失效处理与 WorkBuddy 本机重取。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checkin.models import Account
from checkin.refresher import TokenRefresher, TRAE_EXCHANGE_URL


class FakeVault:
    """捕获写回内容，避免测试碰真实保险库。"""

    def __init__(self):
        self.saved = None

    def put(self, platform, version, account_id, token, fields=None, secrets=None):
        self.saved = {"key": f"{platform}:{version}:{account_id}", "token": token,
                      "fields": dict(fields or {}), "secrets": dict(secrets or {})}
        return "plain"


def make_http(responses):
    """按调用次数返回预设响应，并记录每次请求。"""
    calls = []

    def http(method, url, headers, body, timeout):
        payload = responses[min(len(calls), len(responses) - 1)]
        calls.append({"method": method, "url": url,
                      "body": (body or b"").decode("utf-8", "replace"),
                      "headers": dict(headers)})
        status, text = payload
        return status, text

    return http, calls


class RefresherTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault()

    def _account(self, platform="trae", version="cn"):
        return Account(id=f"{platform}-t", platform=platform, version=version)

    def _cred(self, refresh="rt-old"):
        from checkin.credentials import Credential
        cred = Credential(token="old-token", source="vault")
        cred.secrets["refresh_token"] = refresh
        cred.fields["device_id"] = "dev-1"
        return cred

    def test_trae_refresh_sends_client_id_and_rotates_refresh(self):
        http, calls = make_http([
            (200, json.dumps({"Result": {"Token": "new-jwt", "RefreshToken": "rt-new"}})),
        ])
        ok, note = TokenRefresher(http_fn=http, vault=self.vault).refresh(
            self._account(), self._cred())
        self.assertTrue(ok, note)
        self.assertEqual(calls[0]["url"], TRAE_EXCHANGE_URL)
        body = json.loads(calls[0]["body"])
        self.assertEqual(body["ClientID"], "en1oxy7wnw8j9n")
        self.assertEqual(body["RefreshToken"], "rt-old")
        self.assertEqual(self.vault.saved["token"], "new-jwt")
        self.assertEqual(self.vault.saved["secrets"]["refresh_token"], "rt-new",
                         "refreshToken 轮换后必须回写")
        self.assertEqual(self.vault.saved["fields"]["device_id"], "dev-1",
                         "原有 fields 不能丢")

    def test_qoder_refresh_json_then_form_fallback_for_drt(self):
        ok_body = json.dumps({"data": {"device_token": "dt-new", "refresh_token": "drt-new"}})
        http, calls = make_http([
            (400, '{"error":"unsupported_media_type"}'),
            (200, ok_body),
        ])
        ok, note = TokenRefresher(http_fn=http, vault=self.vault).refresh(
            self._account("qoder", "global"), self._cred("drt-old"))
        self.assertTrue(ok, note)
        self.assertIn("/api/v1/deviceToken/refresh", calls[0]["url"])
        self.assertIn("openapi.qoder.sh", calls[0]["url"])
        first_body = json.loads(calls[0]["body"])
        self.assertEqual(first_body["refresh_token"], "drt-old")
        # 第二次是表单回退
        self.assertIn("refresh_token=drt-old", calls[1]["body"])
        self.assertEqual(self.vault.saved["token"], "dt-new")
        self.assertEqual(self.vault.saved["secrets"]["refresh_token"], "drt-new")

    def test_qoder_refresh_dead_token(self):
        http, _ = make_http([(401, '{"error":"invalid refresh"}')])
        ok, note = TokenRefresher(http_fn=http, vault=self.vault).refresh(
            self._account("qoder", "cn"), self._cred("drt-dead"))
        self.assertFalse(ok)
        self.assertIn("已失效", note)

    def test_no_refresh_token_means_give_up(self):
        ok, note = TokenRefresher(vault=self.vault).refresh(
            self._account("trae"), self._cred(refresh=""))
        self.assertFalse(ok)
        self.assertIn("refresh_token", note)

    def test_workbuddy_refresh_reruns_local_decrypt(self):
        from checkin import autofill
        with mock.patch.object(autofill, "auto_detect",
                               return_value={"ok": True, "token": "wb-new",
                                             "uid": "u1", "domain": "d1"}):
            ok, note = TokenRefresher(vault=self.vault).refresh(
                self._account("workbuddy"), self._cred())
        self.assertTrue(ok, note)
        self.assertEqual(self.vault.saved["token"], "wb-new")
        self.assertEqual(self.vault.saved["fields"]["uid"], "u1")


if __name__ == "__main__":
    unittest.main()
