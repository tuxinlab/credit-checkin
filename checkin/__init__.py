"""credit-checkin: 个人多平台每日签到领积分管理工具（非商业用途）。"""
from .models import Account, DailyTask, RunRecord, RunStatus, Platform, Version
from .config import load_config
from .storage import Storage
from .plan import build_daily_plan
from .executor import Executor
from .notifier import Notifier

__version__ = "0.1.9"
__all__ = [
    "Account", "DailyTask", "RunRecord", "RunStatus", "Platform", "Version",
    "load_config", "Storage", "build_daily_plan", "Executor", "Notifier",
    "__version__",
]
