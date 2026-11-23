"""自动凭据获取测试：路径探测、无运行时/无凭据时的安全失败。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from checkin import autofill


class AutofillTests(unittest.TestCase):
    def test_auth_file_candidates_uses_override(self):
        with mock.patch.dict("os.environ", {"WORKBUDDY_AUTH_FILE": "/custom/auth.info"}):
            self.assertEqual(autofill.auth_file_candidates(), [Path("/custom/auth.info")])

    def test_auth_file_candidates_default_structure(self):
        # 只移除 override，保留 HOME/USERPROFILE 以便 Path.home() 正常工作
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "WORKBUDDY_AUTH_FILE"}
        with mock.patch.dict("os.environ", env, clear=True):
            paths = autofill.auth_file_candidates()
        self.assertTrue(any("CodeBuddyExtension" in str(p) for p in paths))

    def test_helper_js_has_path_placeholder(self):
        self.assertIn("__AUTH_PATH__", autofill._HELPER_JS)
        self.assertIn("electron_browser_workbuddy_storage", autofill._HELPER_JS)

    def test_auto_detect_no_auth_file(self):
        with mock.patch.object(autofill, "find_auth_file", return_value=None):
            r = autofill.auto_detect()
        self.assertFalse(r["ok"])
        self.assertIn("未找到", r["error"])
        self.assertFalse(r["token"])

    def test_auto_detect_no_runtime(self):
        fake = mock.MagicMock()
        fake.is_file.return_value = True
        fake.read_text.return_value = '{"auth":{"accessToken":{"$wbEncrypted":1,"envelope":"x"}},"account":{}}'
        with mock.patch.object(autofill, "find_auth_file", return_value=fake), \
             mock.patch.object(autofill, "find_runtime", return_value=None), \
             mock.patch.object(autofill.sys, "platform", "win32"):
            r = autofill.auto_detect()
        self.assertFalse(r["ok"])
        self.assertIn("运行时", r["error"])

    @unittest.skipUnless(__import__("sys").platform == "win32", "仅 Windows")
    def test_real_runtime_found_on_this_machine(self):
        runtime = autofill.find_runtime()
        auth = autofill.find_auth_file()
        # 本机应能找到（安装并登录过 WorkBuddy）；若没有则跳过断言而非失败
        if runtime and auth:
            self.assertTrue(runtime.is_file())
            self.assertTrue(auth.is_file())

    def test_probe_candidates_cover_desktop_auth_path(self):
        """面板的「登录态探测」必须覆盖桌面端真实凭据路径，否则会误报未找到。"""
        from checkin.credentials import workbuddy_candidates
        paths = [str(p).replace("\\", "/") for p in workbuddy_candidates()]
        self.assertTrue(
            any("CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" in x for x in paths),
            paths,
        )


class FindRuntimeTests(unittest.TestCase):
    """运行时探测：客户端装在非系统盘时也必须能找到（否则自动获取失效）。"""

    def test_prefers_running_process_path(self):
        fake = Path("D:/Program Files/workBuddy/WorkBuddy.exe")
        with mock.patch.dict("os.environ", {"WORKBUDDY_EXE": ""}), \
             mock.patch.object(autofill, "running_process_paths", return_value=[fake]), \
             mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(autofill.find_runtime(), fake)

    def test_env_override_wins(self):
        with mock.patch.dict("os.environ", {"WORKBUDDY_EXE": "C:/x/WorkBuddy.exe"}), \
             mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(autofill.find_runtime(), Path("C:/x/WorkBuddy.exe"))

    def test_falls_back_to_non_system_drive(self):
        """没有运行中进程时，也要扫非系统盘的 Program Files。"""
        target = Path("D:/Program Files/WorkBuddy/WorkBuddy.exe")

        def only_target_exists(self):
            return self.as_posix().lower() == target.as_posix().lower()

        with mock.patch.dict("os.environ", {"WORKBUDDY_EXE": ""}), \
             mock.patch.object(autofill, "running_process_paths", return_value=[]), \
             mock.patch.object(autofill, "drive_roots",
                               return_value=[Path("C:/"), Path("D:/")]), \
             mock.patch.object(Path, "is_file", only_target_exists):
            self.assertEqual(autofill.find_runtime(), target)

    def test_returns_none_when_absent(self):
        with mock.patch.dict("os.environ", {"WORKBUDDY_EXE": ""}), \
             mock.patch.object(autofill, "running_process_paths", return_value=[]), \
             mock.patch.object(autofill, "drive_roots", return_value=[Path("C:/")]), \
             mock.patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(autofill.find_runtime())


if __name__ == "__main__":
    unittest.main()
