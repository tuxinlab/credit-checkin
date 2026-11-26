"""凭据提供器：统一从环境变量或本地客户端登录态解析凭据。

设计原则：
1. 绝不打印 / 落盘 token 原文；所有返回只带来源标记与脱敏信息。
2. 环境变量优先；本地文件仅用于探测与「有/无」判断，解密需显式 --allow-local-decrypt。
3. 平台差异集中在各 provider 函数，适配层只拿到 {token, fields, headers}。
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import Account
from .vault import CredentialVault


class CredentialError(RuntimeError):
    pass


@dataclass
class Credential:
    token: str = ""
    source: str = ""                       # env | inline | trae-storage | workbuddy-file | qoder-auth
    fields: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    secrets: Dict[str, str] = field(default_factory=dict)   # refresh_token 等（来自保险库，已解密）
    note: str = ""

    def masked(self) -> str:
        t = self.token or ""
        if not t:
            return "<none>"
        if len(t) <= 10:
            return t[:2] + "***"
        return t[:6] + "…" + t[-4:]

    def describe(self) -> str:
        return f"{self.source} ({self.masked()})"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def vault_path() -> Path:
    """凭据库位置：优先 CHECKIN_HOME，否则数据目录 data/vault.json。"""
    from . import paths
    return paths.data_root() / "data" / "vault.json"


def default_vault() -> CredentialVault:
    return CredentialVault(vault_path())


def _vault_for_account(account: Account) -> Optional[CredentialVault]:
    try:
        return default_vault()
    except Exception:
        return None


# ---------- 各平台本地凭据路径探测（不读取内容） ----------
def workbuddy_candidates() -> List[Path]:
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    xdg = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    env = _env("WORKBUDDY_AUTH_FILE")
    paths = []
    if env:
        paths.append(Path(env))
    paths += [
        # 桌面端实际落地位置（与 autofill 的探测保持一致）
        Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info",
        Path(local) / "workbuddy-desktop.info",
        home / "Library" / "Application Support" / "workbuddy-desktop.info",
        Path(xdg) / "codebuddy-desktop.info",
        home / ".workbuddy" / "auth" / "workbuddy-desktop.info",
    ]
    return paths


def trae_candidates() -> List[Path]:
    home = Path.home()
    appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    lib = home / "Library" / "Application Support"
    env = _env("TRAE_STORAGE_PATH")
    paths = [Path(env)] if env else []
    for root in (Path(appdata), lib, home, home / ".config"):
        for name in ("Trae CN", "TRAE SOLO CN", "TRAE SOLO", "Trae", ".trae-cn", ".trae"):
            paths.append(root / name / "User" / "globalStorage" / "storage.json")
    return paths


def qoder_candidates() -> List[Path]:
    home = Path.home()
    return [
        home / ".qoder" / ".auth",
        home / ".qoder-proxy",
        home / ".qoder" / "auth.json",
    ]


def probe_local() -> Dict[str, Any]:
    """仅检查本地凭据文件是否存在，不读取内容。用于 doctor / probe 命令。"""
    result: Dict[str, Any] = {}
    for platform, finder in (("workbuddy", workbuddy_candidates),
                             ("trae", trae_candidates),
                             ("qoder", qoder_candidates)):
        found = [str(p) for p in finder() if p.exists()]
        result[platform] = {
            "found": bool(found),
            "paths": found[:3],
        }
    result["cryptography"] = _has_cryptography()
    return result


def _has_cryptography() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


# ---------- 凭据解析 ----------
def resolve(account: Account, *, allow_local_decrypt: bool = False) -> Credential:
    """按优先级解析账号凭据。

    1. 行内 credential / 环境变量（永远优先）
    2. 平台本地登录态（仅 allow_local_decrypt=True 时尝试解密）
    """
    token = account.token()
    source = "inline" if account.credential else "env"
    vault_fields: Dict[str, Any] = {}
    cred_secrets: Dict[str, str] = {}
    if not token:
        # 保险库：页面配置的凭据（DPAPI 加密），优先级高于本地客户端探测
        vault = _vault_for_account(account)
        if vault:
            item = vault.get(account.platform, account.version, account.id)
            if item and item.get("token"):
                token = item["token"]
                source = "vault"
                vault_fields = dict(item.get("fields") or {})
                cred_secrets = dict(item.get("secrets") or {})
                if item.get("error"):
                    return Credential(source="error", note=item["error"])
    if token:
        cred = Credential(token=token, source=source, secrets=cred_secrets)
        cred.fields.update(vault_fields)
        cred.fields.update({k: v for k, v in account.metadata.items()
                            if not str(k).startswith("cred_")})
        headers = account.metadata.get("cred_headers")
        if isinstance(headers, dict):
            cred.headers.update({str(k): str(v) for k, v in headers.items()})
        _inject_common_env(cred, account)
        _apply_qoder_device(cred, account)
        return cred

    if not allow_local_decrypt:
        return Credential(source="none", note="未设置凭据环境变量；如需读取本地客户端登录态请加 --allow-local-decrypt")

    try:
        if account.platform == "workbuddy":
            return _workbuddy_local(account)
        if account.platform == "trae":
            return _trae_local(account)
        if account.platform == "qoder":
            cred = _qoder_local(account)
            _apply_qoder_device(cred, account)
            return cred
    except CredentialError as exc:
        return Credential(source="error", note=str(exc))
    return Credential(source="none", note="无可用凭据来源")


def qoder_device_identity(base_dir: Optional[Path] = None) -> Dict[str, str]:
    """读取本机 Qoder 的 UMID 设备风控身份（Cosy-MachineToken 等）。

    来源：`~/.qoder/umid-cache.json`（客户端生成）+ `~/.qoder/.auth/machine_id`，
    可用 QODER_MACHINE_TOKEN / QODER_MACHINE_CODE / QODER_MACHINE_TYPE 覆盖。

    为什么需要：国际版活动列表在缺少这套设备头时会被降级——只剩订阅推广活动，
    每日 100 Credits 的 CLAIM_BENEFIT 不会出现在 campaigns 里，看起来像"没有活动"。
    """
    out: Dict[str, str] = {}
    env_map = {
        "machine_token": "QODER_MACHINE_TOKEN",
        "machine_code": "QODER_MACHINE_CODE",
        "machine_type": "QODER_MACHINE_TYPE",
        "machine_id": "QODER_MACHINE_ID",
    }
    for key, env_name in env_map.items():
        value = _env(env_name)
        if value:
            out[key] = value

    home = Path(base_dir) if base_dir else (Path.home() / ".qoder")
    cache = home / "umid-cache.json"
    if "machine_token" not in out and cache.is_file():
        try:
            info = (json.loads(cache.read_text(encoding="utf-8")) or {}).get("info") or {}
            for src, dst in (("machineToken", "machine_token"),
                             ("machineCode", "machine_code"),
                             ("machineType", "machine_type")):
                if info.get(src) and dst not in out:
                    out[dst] = str(info[src])
        except Exception:
            pass
    mid_file = home / ".auth" / "machine_id"
    if "machine_id" not in out and mid_file.is_file():
        try:
            value = mid_file.read_text(encoding="utf-8").strip()
            if value:
                out["machine_id"] = value
        except Exception:
            pass

    if out:
        out.setdefault("machine_os", "windows" if sys.platform == "win32" else sys.platform)
        out.setdefault("machine_hostname", socket.gethostname())
        out.setdefault("client_version", _env("QODER_CLIENT_VERSION") or "1.0.0")
    return out


def _apply_qoder_device(cred: Credential, account: Account) -> None:
    if account.platform == "qoder":
        for key, value in qoder_device_identity().items():
            cred.fields.setdefault(key, value)


def _inject_common_env(cred: Credential, account: Account) -> None:
    p = account.platform.upper()
    for suffix in ("UID", "DEVICE_ID", "DOMAIN", "ENTERPRISE_ID", "MACHINE_ID",
                   "ACCESS_TOKEN", "REFRESH_TOKEN", "PAT", "ENDPOINT"):
        value = _env(f"{p}_{suffix}")
        if value:
            cred.fields[suffix.lower()] = value
    raw_headers = _env(f"{p}_EXTRA_HEADERS")
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
            if isinstance(parsed, dict):
                cred.headers.update({str(k): str(v) for k, v in parsed.items()})
        except ValueError:
            pass
    # 平台默认值：避免从保险库/本地登录态读到的凭据缺少 endpoint 导致相对 URL。
    # 只给国内版补默认网关：国际版账号用国内网关查会把国内额度错显示成国际版的，
    # 额度查询模块会对未配置 endpoint 的国际版给出明确提示（见 quota.py）。
    if (account.platform == "workbuddy" and account.version == "cn"
            and not cred.fields.get("endpoint")):
        cred.fields["endpoint"] = "https://copilot.tencent.com"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        raise CredentialError(f"读取本地凭据失败 {path.name}: {type(exc).__name__}") from None


def _workbuddy_local(account: Account) -> Credential:
    """WorkBuddy 桌面端登录态。明文 token 直接读；加密 envelope 需客户端运行时解密，
    这里给出明确指引而不是尝试复制解密算法（密钥在客户端内部，稳定复制不可行）。"""
    for path in workbuddy_candidates():
        if not path.is_file():
            continue
        data = _read_json(path)
        auth = data.get("auth") or {}
        token = auth.get("accessToken") or data.get("accessToken")
        if isinstance(token, str) and token and token.strip().startswith("{"):
            raise CredentialError("WorkBuddy 凭据为加密 envelope，需在客户端内解密后通过环境变量提供 token")
        if isinstance(token, str) and token.strip():
            cred = Credential(token=token.strip(), source="workbuddy-file")
            acc = data.get("account") or {}
            if acc.get("uid"):
                cred.fields["uid"] = str(acc["uid"])
            if auth.get("domain"):
                cred.fields["domain"] = str(auth["domain"])
            if acc.get("enterpriseId"):
                cred.fields["enterprise_id"] = str(acc["enterpriseId"])
            if auth.get("endpoint"):
                cred.fields["endpoint"] = str(auth["endpoint"])
            _inject_common_env(cred, account)
            return cred
    raise CredentialError("本机未找到 WorkBuddy 登录态文件（请确认客户端已登录）")


def _trae_local(account: Account) -> Credential:
    """Trae CN storage.json。icubeAuth 为 AES-128-CBC + SHA512 校验，
    解密需要 cryptography（可选依赖）。refreshToken 轮换需外部客户端，不在此处处理。"""
    for path in trae_candidates():
        if not path.is_file():
            continue
        data = _read_json(path)
        enc = str(data.get("iCubeAuthInfo://icube.cloudide") or "").strip()
        if not enc:
            continue
        if enc.startswith("{"):
            parsed = json.loads(enc)
            cred = Credential(source="trae-storage")
            cred.fields.update({k: v for k, v in parsed.items() if isinstance(v, (str, int, float))})
        else:
            if not _has_cryptography():
                raise CredentialError("Trae 本地凭据已加密，需安装可选依赖：pip install cryptography")
            cred = Credential(source="trae-storage",
                              note="加密凭据需显式解密流程，请改用 TRAE_ACCESS_TOKEN / TRAE_REFRESH_TOKEN 环境变量")
            return cred
        token = cred.fields.get("accessToken") or cred.fields.get("token")
        if not token:
            return Credential(source="error", note="storage.json 中未找到 accessToken，请先登录 Trae 客户端")
        cred.token = str(token)
        for key in ("iCubeAuthInfo://icube-dc", "deviceId"):
            if data.get(key):
                cred.fields["device_id"] = str(data[key])
        _inject_common_env(cred, account)
        return cred
    raise CredentialError("本机未找到 Trae storage.json（请确认客户端已登录）")


def _qoder_local(account: Account) -> Credential:
    """Qoder PAT / 本地 auth 目录。国际版需 PAT（pt- 开头），国内版可复用客户端登录态。"""
    pat = _env("QODER_PAT") or _env("QODER_GLOBAL_PAT")
    if pat:
        return Credential(token=pat, source="env")
    for path in qoder_candidates():
        if path.is_dir():
            for name in ("token", "access_token", "auth.json"):
                p = path / name
                if p.is_file():
                    raw = p.read_text(encoding="utf-8", errors="replace").strip()
                    if raw and not raw.startswith("{"):
                        return Credential(token=raw, source="qoder-auth")
        elif path.is_file():
            data = _read_json(path)
            token = data.get("token") or data.get("accessToken")
            if token:
                cred = Credential(token=str(token), source="qoder-auth")
                if data.get("machine_id"):
                    cred.fields["machine_id"] = str(data["machine_id"])
                return cred
    raise CredentialError("未找到 Qoder 凭据：请设置 QODER_PAT / QODER_GLOBAL_PAT 环境变量")
