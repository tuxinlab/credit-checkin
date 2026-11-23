"""配置与定时任务管理：供 Web 面板增删改账号、保存凭据、配置每日签到。

所有写操作都直接改 config.json（凭据不写这里，走 vault），
改完返回一个新的规范化 cfg，调用方负责热更新运行时上下文。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import DEFAULTS, load_config
from .credentials import default_vault
from .models import UNSUPPORTED_VERSIONS as _MODEL_UNSUPPORTED
from .models import Platform, Version
from .vault import CredentialVaultError


class ManageError(ValueError):
    pass


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# 这些平台版本本身没有签到活动：允许保留配置行，但拒绝启用，避免每天白跑刷失败
UNSUPPORTED_VERSIONS = dict(_MODEL_UNSUPPORTED)


def _reject_unsupported(platform: str, version: str) -> None:
    reason = UNSUPPORTED_VERSIONS.get((platform.lower(), version.lower()))
    if reason:
        raise ManageError(f"{reason}，该版本不可启用")


def read_raw(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ManageError(f"配置文件不存在: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception as exc:
        raise ManageError(f"配置不是合法 JSON: {type(exc).__name__}") from None


# 线程内重入锁 + 跨进程文件锁：Web 面板多线程 + CLI 可能同时改 config.json，
# 每次"读→改→写"必须整体互斥，否则后写覆盖先写、丢失账号改动。
_config_thread_locks: Dict[str, threading.RLock] = {}
_config_thread_locks_guard = threading.Lock()


def _thread_lock_for(path: str | Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _config_thread_locks_guard:
        return _config_thread_locks.setdefault(key, threading.RLock())


@contextmanager
def config_lock(path: str | Path):
    """配置写操作的互斥锁（进程内 RLock + 进程间文件锁）。"""
    p = Path(path).resolve()
    with _thread_lock_for(p):
        lock_path = p.with_name(p.name + ".lock")
        deadline = time.monotonic() + 5.0
        fd = None
        try:
            while True:
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    break
                except FileExistsError:
                    if time.monotonic() > deadline:
                        try:
                            os.unlink(str(lock_path))
                        except OSError:
                            pass
                        continue
                    time.sleep(0.02)
            yield
        finally:
            if fd is not None:
                os.close(fd)
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass


def locked_write(fn):
    """装饰写操作：用第一个参数（配置文件路径）加互斥锁，保证读改写整体原子。"""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        path = args[0] if args else kwargs.get("path")
        if path is None:
            return fn(*args, **kwargs)
        with config_lock(path):
            return fn(*args, **kwargs)
    return wrapper


def write_raw(path: str | Path, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 唯一临时文件名：即使异常路径下两个写者重叠，也不会互相覆盖 tmp
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_platform(raw: Dict[str, Any], platform: str) -> Dict[str, Any]:
    p = raw.setdefault("platforms", {}).setdefault(platform, {})
    p.setdefault("enabled", True)
    p.setdefault("versions", {})
    return p


def _ensure_version(raw: Dict[str, Any], platform: str, version: str) -> Dict[str, Any]:
    v = _ensure_platform(raw, platform)["versions"].setdefault(version, {})
    v.setdefault("enabled", True)
    v.setdefault("accounts", [])
    return v


def list_accounts(cfg: Dict[str, Any], raw: Optional[Dict[str, Any]] = None) -> list:
    """列出账号。cfg 里 enabled 是「有效状态」（已与平台/版本开关相与）。

    raw 为原始配置时，额外给出 version_enabled / platform_enabled，
    便于页面解释「为什么同一个按钮点了没变化」。
    """
    out = []
    for acc in cfg.get("accounts", []):
        item = {
            "id": acc.id, "name": acc.name or acc.id,
            "platform": acc.platform, "version": acc.version,
            "enabled": acc.enabled,
            "self_enabled": acc.enabled,
        }
        if raw:
            node = (((raw.get("platforms") or {}).get(acc.platform) or {})
                    .get("versions") or {}).get(acc.version) or {}
            item["version_enabled"] = bool(node.get("enabled", True))
            item["platform_enabled"] = bool(
                ((raw.get("platforms") or {}).get(acc.platform) or {}).get("enabled", True))
            for entry in node.get("accounts") or []:
                if str(entry.get("id", "")).strip() == acc.id:
                    item["self_enabled"] = bool(entry.get("enabled", True))
                    break
        out.append(item)
    return out


_ID_PREFIX = {"workbuddy": "wb", "trae": "trae", "qoder": "qoder"}
_VERSION_TAG = {"cn": "cn", "global": "gl"}


def _all_account_ids(raw: Dict[str, Any]) -> set:
    used = set()
    for pconf in (raw.get("platforms") or {}).values():
        for vconf in ((pconf or {}).get("versions") or {}).values():
            for item in (vconf or {}).get("accounts") or []:
                value = str(item.get("id", "")).strip()
                if value:
                    used.add(value)
    return used


def next_account_id(raw: Dict[str, Any], platform: str, version: str) -> str:
    """生成全局唯一的账号 ID，形如 `qoder-gl-01`（平台缩写-版本-序号）。"""
    prefix = f"{_ID_PREFIX.get(platform, platform)}-{_VERSION_TAG.get(version, version)}-"
    used = _all_account_ids(raw)
    index = 1
    while f"{prefix}{index:02d}" in used:
        index += 1
    return f"{prefix}{index:02d}"


@locked_write
def add_account(path: str | Path, platform: str, version: str,
                account_id: str = "", name: str = "") -> Dict[str, Any]:
    platform = str(platform).lower().strip()
    version = str(version).lower().strip()
    account_id = str(account_id or "").strip()
    if platform not in Platform.values():
        raise ManageError(f"未知平台: {platform}")
    if version not in Version.values():
        raise ManageError(f"未知版本: {version}")
    _reject_unsupported(platform, version)
    raw = read_raw(path)
    if account_id:
        if not _ID_RE.match(account_id):
            raise ManageError("账号 ID 只能是字母、数字、下划线、连字符（1-64 位）")
        if account_id in _all_account_ids(raw):
            raise ManageError(f"账号 ID 已存在: {account_id}")
    else:
        # 默认自动分配：ID 只是内部去重键，不需要用户手填
        account_id = next_account_id(raw, platform, version)
    node = _ensure_version(raw, platform, version)
    node["enabled"] = True
    node["accounts"].append({
        "id": account_id, "name": name or account_id,
        "enabled": True, "credential_env": f"{platform.upper()}_{version.upper()}_TOKEN_{account_id.upper()}",
    })
    _ensure_platform(raw, platform)["enabled"] = True
    write_raw(path, raw)
    return {"id": account_id, "platform": platform, "version": version, "name": name or account_id}


@locked_write
def remove_account(path: str | Path, platform: str, version: str, account_id: str) -> bool:
    raw = read_raw(path)
    node = raw.get("platforms", {}).get(platform, {}).get("versions", {}).get(version, {})
    accounts = node.get("accounts") or []
    before = len(accounts)
    node["accounts"] = [a for a in accounts if str(a.get("id", "")).strip() != account_id]
    if len(node["accounts"]) == before:
        return False
    write_raw(path, raw)
    # 同时删掉保险库里的凭据（尽力而为）
    try:
        default_vault().delete(platform, version, account_id)
    except CredentialVaultError:
        pass
    return True


@locked_write
def set_account_name(path: str | Path, platform: str, version: str,
                     account_id: str, name: str) -> bool:
    """修改账号备注名（只是标签，可留空表示清空）。"""
    raw = read_raw(path)
    node = raw.get("platforms", {}).get(platform, {}).get("versions", {}).get(version, {})
    for item in node.get("accounts") or []:
        if str(item.get("id", "")).strip() == account_id:
            item["name"] = str(name).strip()
            write_raw(path, raw)
            return True
    return False


@locked_write
def update_general(path: str | Path, *, timezone: str = "",
                   window_start: str = "", window_end: str = "") -> Dict[str, Any]:
    """更新签到窗口与时区；非法值直接报错。"""
    raw = read_raw(path)
    if timezone:
        timezone = timezone.strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception:
            # Windows 上 zoneinfo 可能缺 tzdata；放行 utils.get_tz 内置的别名
            aliases = ("Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "UTC+8", "UTC")
            if timezone not in aliases:
                raise ManageError(f"无效时区: {timezone}（示例: Asia/Shanghai / UTC）") from None
        raw["timezone"] = timezone
    for key, value in (("start", window_start), ("end", window_end)):
        if value:
            value = str(value).strip()
            if not _HHMM_RE.match(value):
                raise ManageError(f"时间格式应为 HH:MM，收到: {value}")
            raw.setdefault("checkin_window", {})[key] = value
    write_raw(path, raw)
    merged = load_config(path)
    return {"timezone": merged.get("timezone"),
            "checkin_window": merged.get("checkin_window")}


@locked_write
def set_account_enabled(path: str | Path, platform: str, version: str,
                        account_id: str, enabled: bool) -> bool:
    raw = read_raw(path)
    node = raw.get("platforms", {}).get(platform, {}).get("versions", {}).get(version, {})
    for item in node.get("accounts") or []:
        if str(item.get("id", "")).strip() == account_id:
            item["enabled"] = bool(enabled)
            if enabled:
                _reject_unsupported(platform, version)
                # 账号的上层开关（版本/平台）若关着，账号依旧不会被执行；
                # 用户点「启用」的意图是「让它跑起来」，因此这里一并打开。
                node["enabled"] = True
                raw.setdefault("platforms", {}).setdefault(platform, {})["enabled"] = True
            write_raw(path, raw)
            return True
    return False


@locked_write
def set_version_enabled(path: str | Path, platform: str, version: str, enabled: bool) -> bool:
    if enabled:
        _reject_unsupported(platform, version)
    raw = read_raw(path)
    node = raw.get("platforms", {}).get(platform, {}).get("versions", {}).get(version)
    if not node:
        return False
    node["enabled"] = bool(enabled)
    if enabled:
        raw["platforms"][platform]["enabled"] = True
    write_raw(path, raw)
    return True


def save_credential(platform: str, version: str, account_id: str, token: str,
                    fields: Optional[Dict[str, Any]] = None,
                    secrets: Optional[Dict[str, str]] = None) -> str:
    if not token or not token.strip():
        raise ManageError("token 不能为空")
    return default_vault().put(platform, version, account_id, token.strip(), fields,
                               secrets=secrets)


def delete_credential(platform: str, version: str, account_id: str) -> bool:
    return default_vault().delete(platform, version, account_id)


# ---------- 每日定时 ----------
def get_schedule(raw: Dict[str, Any]) -> Dict[str, Any]:
    s = raw.get("schedule") or {}
    return {
        "enabled": bool(s.get("enabled", False)),
        "time": str(s.get("time", "10:20")),
        "second_time": str(s.get("second_time", "15:00")),
        "task_name": str(s.get("task_name", "CreditCheckinDaily")),
    }


def validate_time(value: str) -> str:
    value = str(value).strip()
    if not _HHMM_RE.match(value):
        raise ManageError("时间格式应为 HH:MM（24 小时制），例如 10:20")
    return value


@locked_write
def set_schedule(path: str | Path, enabled: bool, time_str: str,
                 second_time: str = "") -> Dict[str, Any]:
    time_str = validate_time(time_str)
    raw = read_raw(path)
    raw["schedule"] = {"enabled": bool(enabled), "time": time_str,
                       "second_time": validate_time(second_time) if second_time else "",
                       "task_name": "CreditCheckinDaily"}
    write_raw(path, raw)
    return get_schedule(raw)


def _decode_console(raw: bytes) -> str:
    """PowerShell 在中文 Windows 上输出 GBK；按 utf-8 → gbk → mbcs 依次尝试。"""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _run_powershell(script: str, timeout: int = 40) -> tuple:
    """执行 PowerShell 脚本，返回 (returncode, stdout, stderr)，自动处理编码。"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (proc.returncode,
            _decode_console(proc.stdout or b""),
            _decode_console(proc.stderr or b""))


def task_status(task_name: str = "CreditCheckinDaily") -> Dict[str, Any]:
    """查询 Windows 计划任务状态；非 Windows 或未注册时如实返回。"""
    if sys.platform != "win32":
        return {"supported": False, "registered": False, "reason": "非 Windows 系统"}
    try:
        ps = (
            f"$tn='{task_name}'; "
            "$t=Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue; "
            "if($t){$i=Get-ScheduledTaskInfo -TaskName $tn; "
            "Write-Output (\"REGISTERED|\"+$t.State+\"|\"+$i.NextRunTime+\"|\""
            "+$i.LastRunTime+\"|\"+$i.LastTaskResult)} "
            "else{Write-Output 'MISSING'}"
        )
        proc_rc, out, err = _run_powershell(ps, timeout=20)
        out = (out or "").strip()
        if out.startswith("REGISTERED|"):
            parts = out.split("|")
            info = {"supported": True, "registered": True,
                    "state": parts[1] if len(parts) > 1 else "",
                    "next_run": parts[2] if len(parts) > 2 else "",
                    "last_run": parts[3] if len(parts) > 3 else "",
                    "last_result": parts[4] if len(parts) > 4 else ""}
            # 任务从未跑过时 LastRunTime 是 1999-11-30（格式随区域变化），展示成 "-" 更直观
            if "1999" in info["last_run"] or not info["last_run"]:
                info["last_run"] = ""
                info["never_run"] = True
            return info
        return {"supported": True, "registered": False}
    except Exception as exc:
        return {"supported": True, "registered": False, "reason": f"{type(exc).__name__}: {exc}"}


def register_task(project_root: str | Path, time_str: str,
                  task_name: str = "CreditCheckinDaily",
                  second_time: str = "") -> Dict[str, Any]:
    """注册/更新 Windows 计划任务。time_str 为主触发时间，second_time 为兜底触发（可选）。"""
    if sys.platform != "win32":
        raise ManageError("自动定时仅支持 Windows，请在其它系统使用 crontab 或常驻 schedule 命令")
    time_str = validate_time(time_str)
    if second_time:
        second_time = validate_time(second_time)
        if second_time == time_str:
            second_time = ""  # 与主触发相同则不建第二个
    root = Path(project_root).resolve()
    # 打包后 sys.executable 就是 exe 本身，不能再传 -m checkin（那套参数只有 Python 认识）。
    # exe 的 --run 分支会静默执行一次签到。
    from . import paths
    if paths.is_frozen():
        executable = str(Path(sys.executable).resolve())
        action = "--run"
    else:
        executable = sys.executable
        action = "-m checkin run"
    if second_time:
        trigger_lines = f"$t2=New-ScheduledTaskTrigger -Daily -At '{second_time}'"
        triggers = "@($t1,$t2)"
    else:
        trigger_lines = ""
        triggers = "$t1"
    ps = f"""
$ErrorActionPreference='Stop'
$tn='{task_name}'
$a=New-ScheduledTaskAction -Execute '{executable}' -Argument '{action}' -WorkingDirectory '{root}'
$t1=New-ScheduledTaskTrigger -Daily -At '{time_str}'
{trigger_lines}
$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
$old=Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
if($old){{Unregister-ScheduledTask -TaskName $tn -Confirm:$false}}
Register-ScheduledTask -TaskName $tn -Action $a -Trigger {triggers} -Settings $s -Description 'Credit-checkin personal daily check-in' | Out-Null
$i=Get-ScheduledTaskInfo -TaskName $tn
Write-Output ("OK|"+$i.NextRunTime)
"""
    try:
        rc, out, err = _run_powershell(ps, timeout=40)
    except Exception as exc:
        raise ManageError(f"注册计划任务失败: {exc}") from None
    out = (out or "").strip()
    err = (err or "").strip()
    if rc != 0 or not out.startswith("OK|"):
        raise ManageError(f"注册计划任务失败: {(out or err)[:300]}")
    return {"registered": True, "time": time_str,
            "next_run": out.split("|", 1)[1] if "|" in out else ""}


def unregister_task(task_name: str = "CreditCheckinDaily") -> bool:
    if sys.platform != "win32":
        return False
    try:
        rc, out, _err = _run_powershell(
            f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false -ErrorAction SilentlyContinue",
            timeout=20)
        return rc == 0
    except Exception:
        return False


def token_guide(platform: str) -> Dict[str, Any]:
    """各平台获取 token 的指引（纯文本，不含任何真实凭据）。"""
    guides = {
        "workbuddy": {
            "title": "WorkBuddy 国内版",
            "steps": [
                "最简单：点下方的「自动获取本机凭据」，会自动从已登录的 WorkBuddy 客户端解出 token 并加密保存。",
                "手动方式：从本地登录态导出 accessToken（88lin/workbuddy-auto-signin 的导出工具）。",
                "按需填写 UID / Domain / Endpoint（默认 copilot.tencent.com）。",
            ],
            "fields": ["uid", "domain", "endpoint"],
        },
        "trae": {
            "title": "Trae 国内版",
            "steps": [
                "最简单：点下方的「自动获取本机凭据」，会自动从已登录的 Trae CN 客户端解出 token 和设备号并加密保存。",
                "手动方式：运行社区工具 trae_get_token.py 导出 refreshToken/base64 凭据，或提供 accessToken（Cloud-IDE-JWT）。",
                "务必填写 16 位设备号，否则签到接口会返回 9074。",
            ],
            "fields": ["device_id"],
        },
        "qoder": {
            "title": "Qoder 国内版 / 国际版",
            "steps": [
                "最简单：点下方的「设备授权登录」，在打开的浏览器页面确认后自动拿到 token（无需手填）。",
                "手动方式：国内版在 qoder.com.cn「个人设置 → 服务集成 → 个人访问令牌」创建 PAT；国际版在 qoder.sh 的 Settings → Personal Access Tokens 创建。",
                "把 PAT（pt- 开头）填入下方即可，无需桌面端。",
            ],
            "fields": [],
        },
    }
    return guides.get(platform, {"title": platform, "steps": [], "fields": []})
