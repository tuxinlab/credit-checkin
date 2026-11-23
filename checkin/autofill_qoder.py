"""Qoder 设备授权登录：生成授权链接，轮询换取 token（标准库实现）。

参考社区项目 L0NE-6/Qoder-Daily 的 RFC8628 风格设备码流程，独立实现。
用法：start() 返回的 auth_url 让用户在浏览器打开并确认；随后 poll() 轮询，
用户确认后返回 access_token / refresh_token。token 只走内存，不打印。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

REGIONS = {
    "cn": {"web": "https://qoder.com.cn", "open_api": "https://openapi.qoder.com.cn", "label": "中国版"},
    "global": {"web": "https://qoder.com", "open_api": "https://openapi.qoder.sh", "label": "国际版"},
}
DEVICE_LOGIN_PATH = "/device/selectAccounts"
DEVICE_POLL_PATH = "/api/v1/deviceToken/poll"

_sessions: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _machine_id() -> str:
    for p in (Path.home() / ".qoder" / ".auth" / "machine_id",
              Path.home() / ".qoder-proxy" / "machine_id"):
        if p.is_file():
            try:
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
            except Exception:
                pass
    return str(uuid.uuid4())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _request(method: str, url: str, headers: Optional[Dict[str, str]] = None,
             timeout: int = 15) -> tuple:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def start(region: str = "cn") -> Dict[str, Any]:
    """发起设备授权，返回 {session, auth_url, poll_url}。"""
    region = region if region in REGIONS else "cn"
    cfg = REGIONS[region]
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    nonce = uuid.uuid4().hex
    machine = _machine_id()
    auth_url = cfg["web"] + DEVICE_LOGIN_PATH + "?" + urllib.parse.urlencode({
        "challenge": challenge, "challenge_method": "S256",
        "machine_id": machine, "nonce": nonce,
    })
    poll_url = cfg["open_api"] + DEVICE_POLL_PATH + "?" + urllib.parse.urlencode({
        "nonce": nonce, "verifier": verifier, "challenge_method": "S256",
    })
    session = uuid.uuid4().hex
    with _lock:
        _sessions[session] = {"poll_url": poll_url, "region": region,
                              "created": time.time()}
    return {"session": session, "auth_url": auth_url, "region": region,
            "message": "请在浏览器打开授权链接并确认登录，然后点「我已授权」"}


def poll(session: str) -> Dict[str, Any]:
    """轮询一次授权结果。返回 {ok, pending, token, refresh_token, ...}。"""
    with _lock:
        info = _sessions.get(session)
    if not info:
        return {"ok": False, "error": "会话不存在或已过期，请重新发起授权"}
    if time.time() - info["created"] > 300:
        return {"ok": False, "error": "授权超时（5 分钟），请重新发起"}

    status, raw = _request("GET", info["poll_url"], headers={
        "Cosy-Version": "1.0.1", "Cosy-ClientType": "5",
        "User-Agent": "qoder-local-proxy",
    })
    if status in (202, 404):
        return {"ok": True, "pending": True, "message": "等待你在浏览器确认授权…"}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"ok": False, "error": f"授权响应异常（HTTP {status}）"}
    if status >= 400:
        msg = payload.get("message") or payload.get("errorMessage") or payload.get("msg") or ""
        return {"ok": False, "error": f"授权失败（HTTP {status}）{msg}"}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = data.get("token") or data.get("access_token") or data.get("device_token")
    if not token:
        code = payload.get("code")
        if code not in (None, 0, "0"):
            return {"ok": False, "error": f"授权失败（code={code}）"}
        return {"ok": True, "pending": True, "message": "等待你在浏览器确认授权…"}
    with _lock:
        _sessions.pop(session, None)
    return {
        "ok": True, "pending": False,
        "token": str(token),
        "refresh_token": str(data.get("refresh_token") or data.get("refreshToken") or ""),
        "user_id": str(data.get("user_id") or data.get("userId") or ""),
        "region": info["region"],
    }
