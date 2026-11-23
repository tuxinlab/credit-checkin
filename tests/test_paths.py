"""路径层与 exe 双模式入口测试：源码模式行为不变，打包模式路径正确。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from checkin import paths


class PathLayerTests(unittest.TestCase):
    def tearDown(self):
        paths.reset_cache()

    def test_source_mode_paths(self):
        """源码模式：数据与资源都指向仓库根。"""
        root = Path(__file__).resolve().parents[1]
        self.assertFalse(paths.is_frozen())
        self.assertEqual(paths.data_root(), root)
        self.assertEqual(paths.contracts_dir(), root / "contracts")
        self.assertEqual(paths.static_dir(), root / "web")

    def test_checkin_home_override(self):
        """CHECKIN_HOME 指定时，数据目录跟随它，资源目录不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CHECKIN_HOME": tmp}):
                self.assertEqual(paths.data_root(), Path(tmp).resolve())
                self.assertTrue((Path(tmp) / "config.json").parent.exists())
            paths.reset_cache()

    def test_cache_invalidates_on_env_change(self):
        """缓存应随 CHECKIN_HOME 变化失效（避免测试/运行时拿到旧路径）。"""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {"CHECKIN_HOME": a}):
                self.assertEqual(paths.data_root(), Path(a).resolve())
            with mock.patch.dict(os.environ, {"CHECKIN_HOME": b}):
                self.assertEqual(paths.data_root(), Path(b).resolve())

    def test_init_data_dirs(self):
        """init_data_dirs 创建 data/ 与 logs/。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CHECKIN_HOME": tmp}):
                root = paths.init_data_dirs()
                self.assertTrue((root / "data").is_dir())
                self.assertTrue((root / "logs").is_dir())


class FrozenModeTests(unittest.TestCase):
    def tearDown(self):
        paths.reset_cache()

    def test_frozen_resource_from_meipass(self):
        """打包后契约/前端从 sys._MEIPASS 内置目录读取。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "exe" / "CreditCheckin.exe"
            fake_exe.parent.mkdir()
            fake_exe.write_bytes(b"")
            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(paths.sys, "executable", str(fake_exe)):
                self.assertTrue(paths.is_frozen())
                self.assertEqual(paths.contracts_dir(), Path(tmp) / "contracts")
                self.assertEqual(paths.static_dir(), Path(tmp) / "web")

    def test_frozen_data_dir_is_fixed_app_dir(self):
        """打包后数据目录固定为用户目录，与 exe 所在位置无关。"""
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as appdata:
            fake_exe = Path(tmp) / "CreditCheckin.exe"
            fake_exe.write_bytes(b"")
            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(paths.sys, "executable", str(fake_exe)), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": appdata,
                                              "CHECKIN_HOME": ""}, clear=False):
                os.environ.pop("CHECKIN_HOME", None)
                paths.reset_cache()
                root = paths.data_root()
                self.assertEqual(root.name, paths.APP_DIR_NAME)
                self.assertEqual(root.parent, Path(appdata).resolve())
                # 不在 exe 目录
                self.assertNotEqual(root, Path(tmp).resolve())

    def test_migrate_legacy_data(self):
        """首次启动应把 exe 同目录的旧数据复制到固定目录，且保留原文件。"""
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as appdata:
            fake_exe = Path(tmp) / "CreditCheckin.exe"
            fake_exe.write_bytes(b"")
            # 旧数据：exe 同目录
            (Path(tmp) / "config.json").write_text('{"timezone":"Asia/Shanghai"}', encoding="utf-8")
            (Path(tmp) / "data").mkdir()
            (Path(tmp) / "data" / "checkin.db").write_bytes(b"SQLite fake")
            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(paths.sys, "executable", str(fake_exe)), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": appdata}, clear=False):
                os.environ.pop("CHECKIN_HOME", None)
                paths.reset_cache()
                source = paths.migrate_legacy_data()
                self.assertEqual(source, Path(tmp).resolve())
                target = paths.data_root()
                self.assertTrue((target / "config.json").is_file())
                self.assertTrue((target / "data" / "checkin.db").is_file())
                # 原文件仍在（只复制不删除）
                self.assertTrue((Path(tmp) / "config.json").is_file())

    def test_migrate_skipped_when_target_has_data(self):
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as appdata:
            fake_exe = Path(tmp) / "CreditCheckin.exe"
            fake_exe.write_bytes(b"")
            (Path(tmp) / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "_MEIPASS", tmp, create=True), \
                 mock.patch.object(paths.sys, "executable", str(fake_exe)), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": appdata}, clear=False):
                os.environ.pop("CHECKIN_HOME", None)
                paths.reset_cache()
                (paths.data_root() / "config.json").write_text("{}", encoding="utf-8")
                self.assertIsNone(paths.migrate_legacy_data())

    def test_register_task_points_to_exe_when_frozen(self):
        """打包后计划任务应指向 exe + --run，而不是 python -m checkin。"""
        from checkin import manage
        with tempfile.TemporaryDirectory() as tmp:
            exe = str(Path(tmp) / "CreditCheckin.exe")
            captured = {}

            def fake_run(script, timeout=40):
                captured["script"] = script
                return 0, "OK|11/23/2026 15:00:00", ""

            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "executable", exe), \
                 mock.patch.object(manage, "_run_powershell", fake_run):
                manage.register_task(tmp, "10:20", second_time="15:00")
            script = captured["script"]
            self.assertIn(exe, script)
            self.assertIn("-Argument '--run'", script)
            self.assertNotIn("-m checkin", script)

    def test_exe_main_routes_no_args_to_web(self):
        """双击 exe（无参数）应启动面板；--run 应走签到。"""
        from checkin import __main__ as entry
        from checkin import cli
        calls = []
        with mock.patch.object(paths, "is_frozen", return_value=True), \
             mock.patch.object(cli, "ensure_initialized"), \
             mock.patch.object(entry, "cli_main", side_effect=lambda a: calls.append(a) or 0):
            entry._exe_main([])
            entry._exe_main(["--run"])
            entry._exe_main(["status"])
        self.assertEqual(calls[0], ["web", "--restart"])
        self.assertEqual(calls[1], ["run"])
        self.assertEqual(calls[2], ["status"])

    def test_frozen_fallback_when_exe_dir_readonly(self):
        """exe 目录不可写时降级到应用数据目录。"""
        with tempfile.TemporaryDirectory() as fallback_dir:
            with mock.patch.object(paths.sys, "frozen", True, create=True), \
                 mock.patch.object(paths.sys, "executable", str(Path(fallback_dir) / "CreditCheckin.exe")), \
                 mock.patch.object(paths, "_dir_writable", return_value=False), \
                 mock.patch.object(paths, "_app_dir_fallback", return_value=Path(fallback_dir)):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(paths.data_root(), Path(fallback_dir).resolve())


if __name__ == "__main__":
    unittest.main()
