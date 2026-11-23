"""从本机 Qoder CN 客户端登录态自动解密 token（Windows，纯标准库）。

机制（参考社区项目 88lin/workbuddy-auto-signin 的 Qoder 部分，独立实现）：
- 登录态在 %APPDATA%\\com.qodercn.app*\\auth.v1.dat：b"v10" + 12 字节 nonce + AES-256-GCM 密文(+tag)
- 密钥在 Local State 的 os_crypt.encrypted_key（base64，前置 "DPAPI"）：
  DPAPI(CryptUnprotectData) 解出 32 字节 AES 密钥
- AES-GCM 走 Windows 自带 CNG(bcrypt)，不引入 cryptography 依赖
- token 只在内存流转，不落盘、不打印
"""
from __future__ import annotations

import base64
import ctypes
import glob
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional

# 国内版与国际版是不同的产品，配置目录与账号体系都独立，必须按版本区分读取，
# 否则国际版会读不到（或误读国内版账号）。
AUTH_GLOB = "com.qodercn.app*"          # Qoder CN
AUTH_GLOB_GLOBAL = "com.qoder.app*"     # Qoder 国际版
AUTH_FILE = "auth.v1.dat"
LOCAL_STATE = "Local State"


class QoderAuthError(RuntimeError):
    pass


# ---------------- DPAPI ----------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    src = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
    )
    if not ok:
        raise QoderAuthError("DPAPI 解不开密钥（需同一 Windows 用户且已登录 Qoder 桌面端）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


# ---------------- AES-256-GCM（Windows CNG） ----------------
class _AEAD_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def _gcm_decrypt(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    if sys.platform != "win32":
        raise QoderAuthError("本地凭据解密目前仅支持 Windows")
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    ct, tag = blob[:-16], blob[-16:]

    def chk(res, what):
        if res != 0:
            raise QoderAuthError("CNG %s 失败 NTSTATUS=0x%08x" % (what, res & 0xFFFFFFFF))

    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    try:
        chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
        mode = "ChainingModeGCM"
        chk(bcrypt.BCryptSetProperty(
            h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0), "SetProperty")
        kbuf = ctypes.create_string_buffer(key, len(key))
        chk(bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0), "GenKey")
        nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _AEAD_INFO()
        info.cbSize = ctypes.sizeof(_AEAD_INFO)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)
        out_len = wintypes.ULONG()
        cbuf = (ctypes.c_ubyte * len(ct)).from_buffer_copy(ct)
        chk(bcrypt.BCryptDecrypt(h_key, cbuf, len(ct), ctypes.byref(info), None, 0,
                                 None, 0, ctypes.byref(out_len), 0), "DecryptSize")
        out = (ctypes.c_ubyte * out_len.value)()
        chk(bcrypt.BCryptDecrypt(h_key, cbuf, len(ct), ctypes.byref(info), None, 0,
                                 out, out_len.value, ctypes.byref(out_len), 0), "Decrypt")
        return bytes(out[:out_len.value])
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# ---------------- 凭据定位与解密 ----------------
def auth_dirs(version: str = "cn") -> List[Path]:
    override = os.environ.get("QODER_AUTH_DIR", "").strip()
    if override:
        return [Path(override).expanduser()]
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    pattern = AUTH_GLOB_GLOBAL if version == "global" else AUTH_GLOB
    try:
        roots = sorted(glob.glob(str(appdata / pattern)),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        roots = []
    return [Path(r) for r in roots]


def find_auth_dir(version: str = "cn") -> Optional[Path]:
    for d in auth_dirs(version):
        if (d / AUTH_FILE).is_file() and (d / LOCAL_STATE).is_file():
            return d
    return None


def _read_store(root: Path) -> Dict[str, Any]:
    raw = (root / AUTH_FILE).read_bytes()
    if raw[:3] != b"v10":
        raise QoderAuthError("auth.v1.dat 格式未知（前 3 字节 %r）" % raw[:3])
    if len(raw) < 3 + 12 + 16 + 2:
        raise QoderAuthError("auth.v1.dat 过短，可能正被客户端改写")
    state = json.loads((root / LOCAL_STATE).read_text(encoding="utf-8"))
    crypt = state.get("os_crypt") if isinstance(state, dict) else None
    if not isinstance(crypt, dict):
        raise QoderAuthError("Local State 缺少 os_crypt 配置")
    blob = base64.b64decode(crypt.get("encrypted_key") or "")
    if blob[:5] != b"DPAPI":
        raise QoderAuthError("Local State 的 encrypted_key 结构变了")
    key = _dpapi_unprotect(blob[5:])
    plain = _gcm_decrypt(key, raw[3:15], raw[15:])
    sess = json.loads(plain.decode("utf-8"))
    if not isinstance(sess, dict):
        raise QoderAuthError("会话内容不是 JSON 对象")
    return sess


def _read_retry(root: Path, attempts: int = 3) -> Dict[str, Any]:
    last = None
    for i in range(attempts):
        try:
            return _read_store(root)
        except (ValueError, OSError, QoderAuthError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.25)
    raise last  # type: ignore[misc]


def auto_detect(version: str = "cn") -> Dict[str, Any]:
    """返回 {ok, token, refresh_token, user_id, account_name, phone, email, error, detail}。

    version 为 ``cn``（国内版）或 ``global``（国际版），决定读取哪个客户端目录。
    """
    result = {"ok": False, "token": "", "refresh_token": "", "user_id": "",
              "account_name": "", "phone": "", "email": "", "error": "", "detail": ""}
    if sys.platform != "win32":
        result["error"] = "本地凭据解密目前仅支持 Windows"
        return result
    root = find_auth_dir(version)
    if not root:
        client = "Qoder 国际版" if version == "global" else "Qoder CN"
        result["error"] = f"未找到 {client} 登录态（请先在桌面端登录）"
        return result
    result["detail"] = str(root)
    try:
        sess = _read_retry(root)
    except QoderAuthError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = f"读取登录态失败: {type(exc).__name__}"
        return result

    token = sess.get("token") or sess.get("accessToken") or ""
    if not isinstance(token, str) or not token.strip():
        result["error"] = "解密成功但会话里没有 token，请重新登录 Qoder 桌面端"
        return result
    refresh = sess.get("refreshToken") or ""
    user = sess.get("user") or {}
    uid = user.get("id") or sess.get("userId") or sess.get("user_id") or sess.get("uid") or ""
    result.update({
        "ok": True, "token": token.strip(),
        "refresh_token": str(refresh), "user_id": str(uid),
        "account_name": str(user.get("name") or ""),
        "phone": str(user.get("phone") or ""),
        "email": str(user.get("email") or ""),
    })
    return result
