"""本地凭据保险库：把页面配置的 token 加密存到 data/vault.json。

Windows 使用系统 DPAPI（ctypes 调用 crypt32，绑定当前用户账户），
非 Windows 降级为明文并明确警告。密钥不入仓库、不落配置文件。

存储格式：
{
  "version": 1,
  "encoding": "dpapi" | "plain",
  "items": {
    "<platform>:<version>:<account_id>": {
      "token": "<base64 密文 或 明文>",
      "fields": {...},           # uid / device_id 等非敏感附加字段（明文）
      "updated_at": "..."
    }
  }
}
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

APP_ENTROPY = b"credit-checkin-vault-v1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _is_windows() -> bool:
    return sys.platform == "win32"


def _dpapi_available() -> bool:
    if not _is_windows():
        return False
    try:
        ctypes.windll.crypt32
        return True
    except Exception:
        return False


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(text: str) -> Tuple[str, str]:
    """返回 (编码后的值, encoding)。"""
    raw = text.encode("utf-8")
    if _dpapi_available():
        try:
            entropy = _blob(APP_ENTROPY)
            plain = _blob(raw)
            out = _DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(plain), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(out)
            )
            if ok:
                return base64.b64encode(_bytes(out)).decode("ascii"), "dpapi"
        except Exception:
            pass
    return text, "plain"


def unprotect(value: str, encoding: str) -> str:
    if encoding == "plain":
        return value
    if encoding == "dpapi" and _dpapi_available():
        try:
            data = base64.b64decode(value)
            entropy = _blob(APP_ENTROPY)
            blob = _blob(data)
            out = _DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(out)
            )
            if ok:
                return _bytes(out).decode("utf-8")
        except Exception:
            pass
    raise CredentialVaultError("无法解密凭据：请在配置它的同一 Windows 用户下运行")


class CredentialVaultError(RuntimeError):
    pass


class CredentialVault:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # ---- 跨进程文件锁：避免多个面板/CLI 同时写导致条目互相覆盖 ----
    class _FileLock:
        def __init__(self, path: Path, timeout: float = 5.0):
            self.path = path
            self.timeout = timeout
            self.fd = None

        def __enter__(self):
            import time
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    return self
                except FileExistsError:
                    if time.monotonic() > deadline:
                        # 超时视为陈旧锁，强行接管，避免永久卡死
                        try:
                            os.unlink(self.path)
                        except OSError:
                            pass
                        continue
                    time.sleep(0.05)

        def __exit__(self, *exc):
            try:
                if self.fd is not None:
                    os.close(self.fd)
            finally:
                self.fd = None
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            return False

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "encoding": "dpapi" if _dpapi_available() else "plain", "items": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            data.setdefault("items", {})
            return data
        except Exception as exc:
            raise CredentialVaultError(f"凭据库损坏: {type(exc).__name__}") from None

    def _write(self, data: Dict[str, Any]) -> None:
        # 权限收紧（Windows 忽略 chmod，靠 DPAPI 保护；POSIX 用 0600）
        try:
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not _is_windows():
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise CredentialVaultError(f"写入凭据库失败: {exc}") from None

    @staticmethod
    def key(platform: str, version: str, account_id: str) -> str:
        return f"{platform}:{version}:{account_id}"

    def put(self, platform: str, version: str, account_id: str, token: str,
            fields: Optional[Dict[str, Any]] = None,
            secrets: Optional[Dict[str, str]] = None) -> str:
        """token 与 secrets（如 refresh_token）都会 DPAPI/加密存储；fields 保持明文。"""
        value, enc = protect(token)
        enc_secrets = {}
        for name, secret in (secrets or {}).items():
            if secret:
                enc_secrets[str(name)] = protect(str(secret))
        with self._FileLock(self._lock_path):
            data = self._read()
            # 每个条目记录自己的编码，兼容混合环境
            data["items"][self.key(platform, version, account_id)] = {
                "token": value,
                "encoding": enc,
                "fields": {k: str(v) for k, v in (fields or {}).items() if v is not None},
                "secrets": enc_secrets,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._write(data)
        return enc

    def get(self, platform: str, version: str, account_id: str) -> Optional[Dict[str, Any]]:
        data = self._read()
        item = data["items"].get(self.key(platform, version, account_id))
        if not item:
            return None
        try:
            token = unprotect(str(item.get("token", "")), str(item.get("encoding", "plain")))
        except CredentialVaultError:
            return {"error": "解密失败：请在配置时的同一 Windows 用户下运行", "source": "vault"}
        secrets: Dict[str, str] = {}
        for name, enc_secret in (item.get("secrets") or {}).items():
            try:
                secrets[str(name)] = unprotect(str(enc_secret), "dpapi")
            except CredentialVaultError:
                pass  # 单个 secret 解不开不影响主凭据
        return {
            "token": token,
            "fields": dict(item.get("fields") or {}),
            "secrets": secrets,
            "source": "vault",
        }

    def delete(self, platform: str, version: str, account_id: str) -> bool:
        key = self.key(platform, version, account_id)
        with self._FileLock(self._lock_path):
            data = self._read()
            if key not in data["items"]:
                return False
            del data["items"][key]
            self._write(data)
        return True

    def list_keys(self) -> list:
        return sorted(self._read()["items"].keys())

    def is_encrypted(self) -> bool:
        return _dpapi_available()

    def masked_list(self) -> list:
        out = []
        for key, item in sorted(self._read()["items"].items()):
            token = str(item.get("token", ""))
            masked = token[:4] + "…" + token[-4:] if len(token) > 10 else token[:2] + "***"
            out.append({"key": key, "masked": masked,
                        "encoding": item.get("encoding"),
                        "updated_at": item.get("updated_at")})
        return out
