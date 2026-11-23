"""端口清理（--restart）相关测试。"""
from __future__ import annotations

import unittest
from unittest import mock

from checkin.cli import _parse_netstat_pids, _panel_pids_on_port


class ParseNetstatTests(unittest.TestCase):
    def test_finds_listening_pid(self):
        text = (
            "活动连接\r\n\r\n"
            "  协议  本地地址          外部地址        状态           PID\r\n"
            "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       21512\r\n"
            "  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       999\r\n"
        )
        self.assertEqual(_parse_netstat_pids(text, 8765), {21512})

    def test_ignores_established_and_foreign_port(self):
        text = (
            "  TCP    127.0.0.1:8765         127.0.0.1:50000        ESTABLISHED     111\r\n"
            "  TCP    127.0.0.1:18765        0.0.0.0:0              LISTENING       222\r\n"
        )
        # 18765 不应匹配 8765（按端口精确后缀判断）
        self.assertEqual(_parse_netstat_pids(text, 8765), set())

    def test_empty_and_malformed(self):
        self.assertEqual(_parse_netstat_pids("", 8765), set())
        self.assertEqual(_parse_netstat_pids("garbage line", 8765), set())


class PanelIdentityTests(unittest.TestCase):
    """_panel_pids_on_port 必须同时认 python 与 exe 两种启动方式，且不误杀他人。"""

    NETSTAT = (
        b"  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       12345\r\n"
    )

    def _run(self, image_name: str, cmdline: bytes = b""):
        """netstat → tasklist（进程名）→ 必要时 powershell（命令行）。"""
        netstat = mock.Mock(stdout=self.NETSTAT)
        tasklist = mock.Mock(stdout=f'"{image_name}","12345","Console","1","10,000 K"\r\n'.encode("mbcs"))
        shell = mock.Mock(stdout=cmdline)
        with mock.patch("subprocess.run", side_effect=[netstat, tasklist, shell]):
            return _panel_pids_on_port(8765)

    def test_recognizes_packaged_exe(self):
        """打包后进程名 CreditCheckin*.exe 足够独特，无需查命令行。"""
        got = self._run("CreditCheckin.exe")
        self.assertEqual(got, {12345})

    def test_recognizes_versioned_exe_name(self):
        """带版本号的 exe 名（CreditCheckin-0.1.6.exe）同样要认得。"""
        got = self._run("CreditCheckin-0.1.6.exe")
        self.assertEqual(got, {12345})

    def test_recognizes_python_module(self):
        """python.exe 是通用名，必须核对命令行才确认。"""
        got = self._run("python.exe", b"python.exe -m checkin web --port 8765\r\n")
        self.assertEqual(got, {12345})

    def test_ignores_unrelated_listener(self):
        """同端口的其它程序（如 node）绝不能被误杀。"""
        got = self._run("node.exe", b"node server.js\r\n")
        self.assertEqual(got, set())

    def test_ignores_python_without_checkin_cmdline(self):
        """别的 python 服务不能因为端口撞上就被杀。"""
        got = self._run("python.exe", b"python -m http.server 8765\r\n")
        self.assertEqual(got, set())


if __name__ == "__main__":
    unittest.main()
