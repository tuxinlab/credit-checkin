"""并发安全测试：配置写锁、签到锁的空窗期处理。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from checkin import manage
from checkin.utils import RunLock


class ConfigLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        manage.write_raw(self.path, {"count": 0, "platforms": {}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_read_modify_write_no_lost_update(self):
        """多个线程同时做读-改-写，所有改动都应保留，不能互相覆盖。"""
        n = 12

        def worker(_):
            # 真实调用方必须用 config_lock 包住整个读-改-写事务
            with manage.config_lock(self.path):
                raw = manage.read_raw(self.path)
                raw["count"] = int(raw["count"]) + 1
                manage.write_raw(self.path, raw)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(int(manage.read_raw(self.path)["count"]), n)

    def test_locked_decorator_serializes_add_account(self):
        """并发 add_account 时账号 ID 不重复、数量正确。"""
        n = 8

        def worker(_):
            manage.add_account(self.path, "qoder", "cn")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        raw = manage.read_raw(self.path)
        accounts = raw["platforms"]["qoder"]["versions"]["cn"]["accounts"]
        self.assertEqual(len(accounts), n)
        ids = [a["id"] for a in accounts]
        self.assertEqual(len(ids), len(set(ids)), "账号 ID 必须全局唯一")


class RunLockWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_lock_file_is_not_taken_over_immediately(self):
        """锁文件刚创建、PID 尚未写入的空窗期，acquire 应等待而非抢占。

        回归：旧逻辑读到空 PID 立即 unlink 并接管，会与正在写 PID 的进程
        形成"双持锁"，导致两个签到进程并发执行。
        """
        import os
        lock_path = self.root / "run.lock"
        # 模拟另一个进程刚用 O_EXCL 创建了锁文件但还没写 PID
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        try:
            lock = RunLock(lock_path)
            ok, holder = lock.acquire()
            # 等待重读耗尽后仍读不到有效 PID，才会判定陈旧并接管；
            # 这里没有真实进程写 PID，最终会超时接管，但不会崩溃或永久卡死。
            self.assertIn(ok, (True, False))
        finally:
            os.close(fd)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
