"""端口清理（--restart）相关测试。"""
from __future__ import annotations

import unittest

from checkin.cli import _parse_netstat_pids


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


if __name__ == "__main__":
    unittest.main()
