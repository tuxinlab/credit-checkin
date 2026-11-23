"""存储与计划层测试。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from checkin.models import Account, DailyTask, DriftEvent, RunRecord, RunStatus
from checkin.plan import build_daily_plan, in_checkin_window
from checkin.storage import Storage


def make_cfg(tmp: Path, accounts):
    return {
        "timezone": "Asia/Shanghai",
        "checkin_window": {"start": "09:00", "end": "12:00"},
        "catchup_days": 3,
        "retry": {"max_attempts": 3, "backoff_seconds": [0, 0, 0]},
        "notify": {"channels": [], "on_failure": False, "on_drift": False},
        "contracts_dir": str(tmp / "contracts"),
        "database": str(tmp / "db.sqlite"),
        "log_file": "",
        "accounts": accounts,
    }


class StoragePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = Storage(self.root / "db.sqlite")
        self.acc = Account(id="a1", platform="workbuddy", version="cn",
                           name="主号", credential_env="TOKEN_A1")
        self.cfg = make_cfg(self.root, [self.acc])

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_plan_then_dedupe(self):
        """计划只含当天任务；已完成去重；force 允许重跑。"""
        today = date(2026, 11, 17)
        tasks = build_daily_plan(self.cfg, self.storage, target_date=today)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].target_date, today)
        self.assertFalse(tasks[0].is_catchup)

        task = tasks[0]
        self.storage.upsert_start(RunRecord.from_task(task))
        self.storage.finish(task.key, RunStatus.SUCCESS.value, attempts=1, credits=100)

        again = build_daily_plan(self.cfg, self.storage, target_date=today)
        self.assertFalse(any(t.key == task.key for t in again), "已完成任务应被去重")

        forced = build_daily_plan(self.cfg, self.storage, target_date=today,
                                  include_catchup=False, include_completed=True)
        self.assertTrue(any(t.key == task.key for t in forced), "include_completed 应允许 force 重跑")

    def test_no_cross_day_catchup(self):
        """跨天不再追补：过去日期无论什么状态都不会出现在计划里。

        平台活动按天结算，过期积分补不回来；漏签保障靠当天多次触发 + 关机恢复补跑。
        """
        today = date(2026, 11, 17)
        for status in (RunStatus.FAILED.value, RunStatus.SKIPPED.value):
            t = DailyTask("workbuddy", "cn", "a1", today - timedelta(days=1))
            self.storage.upsert_start(RunRecord.from_task(t))
            self.storage.finish(t.key, status, attempts=1)
        plan = build_daily_plan(self.cfg, self.storage, target_date=today)
        self.assertTrue(all(t.target_date == today for t in plan),
                        "计划不应包含任何过去日期")

    def test_history_and_attempts(self):
        today = date(2026, 11, 17)
        task = DailyTask("workbuddy", "cn", "a1", today)
        self.storage.upsert_start(RunRecord.from_task(task))
        self.storage.finish(task.key, RunStatus.FAILED.value, attempts=2, message="boom")
        from checkin.models import AttemptLog
        for i in (1, 2):
            self.storage.log_attempt(AttemptLog(task.key, i, "2026-11-17T10:00:00",
                                                RunStatus.FAILED.value, "boom"))
        recs = self.storage.history(10, status=RunStatus.FAILED.value)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].key, task.key)
        self.assertEqual(len(self.storage.attempts_for(task.key)), 2)

    def test_drift_event(self):
        self.storage.add_drift(DriftEvent("qoder", "global", "v1", "字段缺失", "{}"))
        events = self.storage.drifts(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].platform, "qoder")
        self.assertIn("字段缺失", events[0].reason)

    def test_window_cross_midnight(self):
        cfg = dict(self.cfg)
        cfg["checkin_window"] = {"start": "23:00", "end": "01:00"}
        from datetime import datetime
        self.assertTrue(in_checkin_window(cfg, datetime(2026, 11, 17, 23, 30)))
        self.assertTrue(in_checkin_window(cfg, datetime(2026, 11, 17, 0, 30)))
        self.assertFalse(in_checkin_window(cfg, datetime(2026, 11, 17, 12, 0)))


if __name__ == "__main__":
    unittest.main()
