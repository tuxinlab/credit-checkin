"""核心数据模型。"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Optional


class Platform(str, Enum):
    WORKBUDDY = "workbuddy"
    TRAE = "trae"
    QODER = "qoder"

    @classmethod
    def values(cls):
        return [p.value for p in cls]


class Version(str, Enum):
    CN = "cn"          # 国内版
    GLOBAL = "global"  # 国际版 / 国外版

    @classmethod
    def values(cls):
        return [v.value for v in cls]


# 这些平台版本本身没有签到活动：保留配置行可以，但永远不执行（plan 会强制停用）
UNSUPPORTED_VERSIONS: Dict[tuple, str] = {
    ("trae", "global"): "Trae 国际版（trae.ai）没有每日签到 / 领取 Credits 活动",
    ("workbuddy", "global"): (
        "WorkBuddy 国际版没有每日签到 / 领取入口；其「每日活跃奖励」是按使用自动发放，"
        "无需也无法手动签到"
    ),
}


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"      # 本次成功领取
    ALREADY = "already"      # 今日已领取，幂等跳过
    FAILED = "failed"        # 失败（重试耗尽）
    SKIPPED = "skipped"      # 未配置 / 未启用
    DRIFT = "drift"          # 契约漂移，需人工介入


# 视为"今日已处理完"的状态。注意 SKIPPED 不在此列：
# 跳过通常意味着当时缺凭据/活动未开，之后补好凭据应当允许重跑。
TERMINAL_STATUSES = {
    RunStatus.SUCCESS.value,
    RunStatus.ALREADY.value,
    RunStatus.DRIFT.value,
}


@dataclass
class Account:
    """一个平台上某个版本下的一个账号。

    credential 与 credential_env 二选一：优先使用行内 credential，
    否则从环境变量读取。永远不要把真实凭据写进 config.json 提交到仓库。
    """
    id: str
    platform: str
    version: str
    name: str = ""
    enabled: bool = True
    credential: str = ""
    credential_env: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.platform = str(self.platform).lower()
        self.version = str(self.version).lower()
        self.id = str(self.id)

    def token(self) -> Optional[str]:
        if self.credential:
            return self.credential.strip() or None
        if self.credential_env:
            return os.environ.get(self.credential_env, "").strip() or None
        return None

    def has_credential_hint(self) -> bool:
        return bool(self.credential or self.credential_env)

    @property
    def key_prefix(self) -> str:
        return f"{self.platform}:{self.version}:{self.id}"

    @classmethod
    def from_dict(cls, platform: str, version: str, data: Dict[str, Any]) -> "Account":
        return cls(
            id=str(data.get("id", "")).strip(),
            platform=platform,
            version=version,
            name=str(data.get("name", "")).strip(),
            enabled=bool(data.get("enabled", True)),
            credential=str(data.get("credential", "")).strip(),
            credential_env=str(data.get("credential_env", "")).strip(),
            metadata=dict(data.get("metadata") or {}),
        )

    def safe_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("credential"):
            d["credential"] = "<set>"
        return d


@dataclass(frozen=True)
class DailyTask:
    """某账号在某一天的一次签到任务。"""
    platform: str
    version: str
    account_id: str
    target_date: date
    is_catchup: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.version}:{self.account_id}:{self.target_date.isoformat()}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "version": self.version,
            "account_id": self.account_id,
            "target_date": self.target_date.isoformat(),
            "is_catchup": self.is_catchup,
            "key": self.key,
        }


@dataclass
class RunRecord:
    """一次任务（按 key 唯一）的执行结果记录。"""
    key: str
    platform: str
    version: str
    account_id: str
    target_date: str
    status: str = RunStatus.PENDING.value
    attempts: int = 0
    credits: Optional[int] = None
    is_catchup: bool = False
    started_at: str = ""
    finished_at: str = ""
    message: str = ""
    detail: str = ""

    @classmethod
    def from_task(cls, task: DailyTask) -> "RunRecord":
        return cls(
            key=task.key,
            platform=task.platform,
            version=task.version,
            account_id=task.account_id,
            target_date=task.target_date.isoformat(),
            is_catchup=task.is_catchup,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AttemptLog:
    key: str
    attempt: int
    at: str
    status: str
    message: str = ""
    detail: str = ""


@dataclass
class DriftEvent:
    """契约漂移事件：接口/鉴权/响应结构与本地契约不一致。"""
    platform: str
    version: str
    contract_version: str = ""
    reason: str = ""
    sample: str = ""
    detected_at: str = ""
    id: Optional[int] = None
