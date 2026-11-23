"""每日计划生成：去重 + 补执行（catchup）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .models import Account, DailyTask, RunStatus, TERMINAL_STATUSES
from .storage import Storage
from .utils import get_tz, now_local, parse_date


def in_checkin_window(cfg: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """判断是否处于配置的每日签到窗口内（用于守护模式）。"""
    now = now or now_local(cfg.get("timezone", "Asia/Shanghai"))
    window = cfg.get("checkin_window") or {}
    start = _parse_hhmm(window.get("start", "09:00"))
    end = _parse_hhmm(window.get("end", "12:00"))
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end  # 跨零点窗口


def _parse_hhmm(value: str) -> int:
    try:
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _matches_filter(task: DailyTask, only: Optional[Dict[str, str]]) -> bool:
    if not only:
        return True
    if only.get("platform") and only["platform"] != task.platform:
        return False
    if only.get("version") and only["version"] != task.version:
        return False
    if only.get("account_id") and only["account_id"] != task.account_id:
        return False
    return True


def _stale_running(record, tz_name: str, minutes: int = 10) -> bool:
    """判断一条 running 记录是否已僵死（进程被中断留下的），可安全重试。"""
    if not record or record.status != RunStatus.RUNNING.value:
        return False
    try:
        from datetime import datetime
        started = datetime.fromisoformat(record.started_at)
        now = now_local(tz_name)
        if started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo)
        return (now - started).total_seconds() > minutes * 60
    except Exception:
        return True  # 时间无法解析时按僵死处理，避免永久卡住


def build_daily_plan(
    cfg: Dict[str, Any],
    storage: Storage,
    *,
    target_date: Optional[date | str] = None,
    only: Optional[Dict[str, str]] = None,
    include_catchup: bool = True,
    include_completed: bool = False,
) -> List[DailyTask]:
    """生成待执行任务列表。

    - 当天：每个启用的账号/版本生成一条，已完成（success/already/skipped/drift）则去重跳过。
    - 补执行：过去 catchup_days 天内失败或从未执行的日期，生成 is_catchup 任务。
    """
    tz_name = cfg.get("timezone", "Asia/Shanghai")
    today = target_date if isinstance(target_date, date) else parse_date(target_date, tz_name)
    accounts: List[Account] = [a for a in cfg.get("accounts", []) if a.enabled]
    tasks: List[DailyTask] = []

    # ---- 当天 ----
    for acc in accounts:
        task = DailyTask(acc.platform, acc.version, acc.id, today, is_catchup=False)
        if not _matches_filter(task, only):
            continue
        existing = storage.get_run(task.key)
        # 当天：SUCCESS/ALREADY/DRIFT 视为已处理；SKIPPED 允许重跑（通常是之前缺凭据）
        if not include_completed and existing and existing.status in TERMINAL_STATUSES:
            continue
        if existing and existing.status == RunStatus.RUNNING.value and not _stale_running(existing, tz_name):
            continue
        tasks.append(task)

    tasks.sort(key=lambda t: (t.target_date, t.platform, t.version, t.account_id))
    return tasks

# 说明：不再做跨天「追补」——平台的活动按天结算，过期积分补不回来；
# 漏签保障依赖「当天多次触发（10:20/15:00）+ StartWhenAvailable 关机恢复补跑 + 失败自动重试」。
# include_catchup / catchup_days 参数保留仅为兼容旧调用与配置，已不起作用。
