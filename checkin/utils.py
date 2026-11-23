"""通用工具：时区、时间解析、脱敏与日志辅助。"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_SENSITIVE = re.compile(r"(token|secret|password|authorization|cookie|pat|credential)", re.I)
# 这些键虽然含敏感词，但值是非敏感的元数据（来源标记/掩码/布尔），前端需要展示
_SAFE_KEYS = re.compile(
    r"^(credential_source|credential_masked|credential_env|has_credential|"
    r"encoding|encrypted|token_days_left)$", re.I
)


def get_tz(name: str = "Asia/Shanghai"):
    """优先使用 zoneinfo，缺少 tzdata 时回退到固定 UTC+8。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        if name in ("Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "UTC+8"):
            return timezone(timedelta(hours=8), "UTC+8")
        return timezone.utc


def now_local(tz_name: str = "Asia/Shanghai") -> datetime:
    return datetime.now(get_tz(tz_name))


def parse_date(value: str | date | None, tz_name: str = "Asia/Shanghai") -> date:
    if value is None or value == "":
        return now_local(tz_name).date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def expand_env(value: Any) -> Any:
    import os
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def sanitize(obj: Any) -> Any:
    """递归脱敏，避免把凭据写进日志/历史明细。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # 只有字符串值才需要脱敏；布尔/数字（如 has_credential）是安全的元数据
            key_s = str(k)
            if (_SENSITIVE.search(key_s) and not _SAFE_KEYS.match(key_s)
                    and isinstance(v, str)):
                out[k] = "<redacted>"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str):
        # 常见 token 形态
        obj = re.sub(r"\b(dt|drt|pt)-[A-Za-z0-9._\-]{8,}\b", "<redacted>", obj)
        obj = re.sub(r"\beyJ[A-Za-z0-9._\-]{20,}", "<redacted>", obj)
        return obj[:1000]
    return obj


def compact_json(obj: Any, limit: int = 500) -> str:
    try:
        s = json.dumps(sanitize(obj), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + "..."


def format_dt(value: Optional[str]) -> str:
    return value or "-"


def setup_logger(cfg: dict):
    """配置根日志器：控制台 + 文件。返回 logger。"""
    import logging
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    logger = logging.getLogger("credit-checkin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    log_file = cfg.get("log_file")
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(p, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


class RunBusy(RuntimeError):
    """已有另一个签到进程在运行。"""


class RunLock:
    """跨进程签到锁：与 vault 锁不同，不做超时抢占——持锁期间其它运行直接拒绝。

    锁文件写入 PID；若持有者进程已死则视为陈旧锁并接管。
    """

    def __init__(self, path):
        import os
        self._os = os
        self.path = str(path)
        self._acquired = False

    def acquire(self, _retries: int = 0) -> tuple:
        """尝试获取锁，返回 (成功?, 持有者描述)。"""
        import time
        os = self._os
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            pid = self._read_pid()
            if pid and _pid_alive(pid):
                return False, f"PID {pid}"
            if not pid and _retries < 10:
                # 锁刚被创建、PID 还没写入的空窗期：短暂等待后重读，
                # 不能立刻 unlink，否则会与正在写 PID 的进程形成"双持锁"。
                time.sleep(0.02)
                return self.acquire(_retries + 1)
            # 陈旧锁：持有者已死，接管（限制递归深度）
            try:
                os.unlink(self.path)
            except OSError:
                pass
            if _retries >= 10:
                return False, "锁状态异常，请稍后重试"
            return self.acquire(_retries + 1)
        try:
            os.write(fd, f"{os.getpid()}|{stamp}".encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._acquired = True
        return True, ""

    def release(self) -> None:
        if self._acquired:
            try:
                self._os.unlink(self.path)
            except OSError:
                pass
            self._acquired = False

    def _read_pid(self):
        os = self._os
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return 0
        try:
            data = os.read(fd, 64)
        finally:
            os.close(fd)
        try:
            return int(data.decode("utf-8", "replace").split("|")[0].strip() or 0)
        except ValueError:
            return 0


def _pid_alive(pid: int) -> bool:
    import sys
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
