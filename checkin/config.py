"""配置加载：统一配置各平台 / 版本 / 账号，支持环境变量注入。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import UNSUPPORTED_VERSIONS, Account, Platform, Version

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULTS: Dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "checkin_window": {"start": "09:00", "end": "12:00"},
    "catchup_days": 3,
    "retry": {"max_attempts": 3, "backoff_seconds": [60, 300, 900]},
    "notify": {"channels": ["console", "log"], "webhook": "", "on_failure": True, "on_drift": True},
    "contracts_dir": "contracts",
    "database": "data/checkin.db",
    "log_file": "logs/checkin.log",
    "retention_days": 180,          # runs/attempt_logs/drift_events 保留天数（0 = 永不清理）
    "platforms": {},
}


class ConfigError(ValueError):
    pass


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(base))
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path, *, expand_env: bool = True) -> Dict[str, Any]:
    """读取并校验配置文件，返回规范化后的 dict。

    规范化结果包含 `accounts: List[Account]`，方便下游直接使用。
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {config_path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是对象")

    cfg = _deep_merge(DEFAULTS, raw)
    if expand_env:
        cfg = _expand_env(cfg)

    # ---- 校验 ----
    tz = str(cfg.get("timezone") or "Asia/Shanghai")
    cfg["timezone"] = tz

    try:
        cfg["catchup_days"] = max(0, int(cfg.get("catchup_days", 3)))
    except (TypeError, ValueError) as exc:
        raise ConfigError("catchup_days 必须是整数") from exc

    retry = cfg.get("retry") or {}
    try:
        max_attempts = max(1, int(retry.get("max_attempts", 3)))
    except (TypeError, ValueError) as exc:
        raise ConfigError("retry.max_attempts 必须是整数") from exc
    backoffs = []
    for b in (retry.get("backoff_seconds") or [60, 300, 900]):
        try:
            backoffs.append(max(0, int(b)))
        except (TypeError, ValueError):
            backoffs.append(60)
    cfg["retry"] = {"max_attempts": max_attempts, "backoff_seconds": backoffs}

    window = cfg.get("checkin_window") or {}
    cfg["checkin_window"] = {
        "start": str(window.get("start", "09:00")),
        "end": str(window.get("end", "12:00")),
    }

    notify = cfg.get("notify") or {}
    channels = notify.get("channels") or ["console", "log"]
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",") if c.strip()]
    cfg["notify"] = {
        "channels": list(channels),
        "webhook": str(notify.get("webhook", "") or ""),
        "on_failure": bool(notify.get("on_failure", True)),
        "on_drift": bool(notify.get("on_drift", True)),
    }

    # ---- 账号 ----
    accounts: List[Account] = []
    seen_ids = set()
    platforms = cfg.get("platforms") or {}
    if not isinstance(platforms, dict):
        raise ConfigError("platforms 必须是对象")
    for platform, pconf in platforms.items():
        p = str(platform).lower()
        if p not in Platform.values():
            raise ConfigError(f"未知平台: {platform}（可选: {', '.join(Platform.values())}）")
        pconf = pconf or {}
        if not isinstance(pconf, dict):
            raise ConfigError(f"platforms.{platform} 必须是对象")
        p_enabled = bool(pconf.get("enabled", True))
        versions = pconf.get("versions") or {}
        if not isinstance(versions, dict):
            raise ConfigError(f"platforms.{platform}.versions 必须是对象")
        for version, vconf in versions.items():
            v = str(version).lower()
            if v not in Version.values():
                raise ConfigError(f"未知版本: {platform}.{version}（可选: {', '.join(Version.values())}）")
            vconf = vconf or {}
            if not isinstance(vconf, dict):
                raise ConfigError(f"platforms.{platform}.versions.{version} 必须是对象")
            v_enabled = bool(vconf.get("enabled", True))
            for item in (vconf.get("accounts") or []):
                if not isinstance(item, dict):
                    raise ConfigError(f"{p}.{v}.accounts 的元素必须是对象")
                acc = Account.from_dict(p, v, item)
                if not acc.id:
                    raise ConfigError(f"{p}.{v} 存在缺少 id 的账号")
                if acc.id in seen_ids:
                    raise ConfigError(f"账号 id 重复: {acc.id}（id 需全局唯一）")
                seen_ids.add(acc.id)
                acc.enabled = acc.enabled and p_enabled and v_enabled
                # 该版本没有签到活动：无论配置怎么写都不执行（防止手改配置后被跑起来）
                if (p.lower(), v) in UNSUPPORTED_VERSIONS:
                    acc.enabled = False
                accounts.append(acc)

    cfg["accounts"] = accounts
    cfg["_config_path"] = str(config_path.resolve())
    return cfg


def config_summary(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """生成脱敏的配置摘要，便于 doctor 命令展示。"""
    rows = [("配置项", "值")]
    rows.append(("timezone", cfg["timezone"]))
    rows.append(("checkin_window", f"{cfg['checkin_window']['start']} ~ {cfg['checkin_window']['end']}"))
    rows.append(("catchup_days", str(cfg["catchup_days"])))
    rows.append(("retry", f"max_attempts={cfg['retry']['max_attempts']}, backoff={cfg['retry']['backoff_seconds']}"))
    rows.append(("accounts_total", str(len(cfg["accounts"]))))
    rows.append(("accounts_enabled", str(sum(1 for a in cfg["accounts"] if a.enabled))))
    return rows


def sample_config() -> Dict[str, Any]:
    """返回配置模板（不含任何真实凭据）。"""
    def acc(i, name, env):
        return {"id": i, "name": name, "enabled": True, "credential_env": env}

    return {
        "timezone": "Asia/Shanghai",
        "checkin_window": {"start": "09:00", "end": "12:00"},
        "catchup_days": 3,
        "retry": {"max_attempts": 3, "backoff_seconds": [60, 300, 900]},
        "notify": {
            "channels": ["console", "log"],
            "webhook": "",
            "on_failure": True,
            "on_drift": True,
        },
        "contracts_dir": "contracts",
        "database": "data/checkin.db",
        "log_file": "logs/checkin.log",
        "platforms": {
            "workbuddy": {
                "enabled": True,
                "versions": {
                    "cn": {"enabled": True, "accounts": [acc("wb-cn-01", "主号", "WORKBUDDY_CN_TOKEN_1")]},
                    "global": {"enabled": False, "accounts": [acc("wb-gl-01", "国际版", "WORKBUDDY_GLOBAL_TOKEN_1")]},
                },
            },
            "trae": {
                "enabled": True,
                "versions": {
                    "cn": {"enabled": True, "accounts": [acc("trae-cn-01", "主号", "TRAE_CN_REFRESH_TOKEN_1")]},
                    "global": {"enabled": False, "accounts": [acc("trae-gl-01", "国际版", "TRAE_GLOBAL_REFRESH_TOKEN_1")]},
                },
            },
            "qoder": {
                "enabled": True,
                "versions": {
                    "cn": {"enabled": True, "accounts": [acc("qoder-cn-01", "主号", "QODER_CN_PAT_1")]},
                    "global": {"enabled": False, "accounts": [acc("qoder-gl-01", "国际版", "QODER_GLOBAL_PAT_1")]},
                },
            },
        },
    }
