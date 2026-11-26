"""Token 自动续期：鉴权失败时用 refresh_token（或本机客户端）换取新 token 并写回保险库。

各平台协议（来源标注，独立实现）：
- Trae CN: POST https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken
           body {"ClientID","RefreshToken","ClientSecret":"-","UserID":""}
           resp Result.Token / Result.RefreshToken（refreshToken 会轮换，必须回写）
- Qoder:   POST {base}/api/v1/deviceToken/refresh   body {"refresh_token": rt}
           resp data.device_token|deviceToken|token|access_token + refresh_token
           drt- 开头的 refresh 遇到媒体类型拒绝时改用表单再试一次
- WorkBuddy: 客户端自己会续期，这里直接借运行时重新解密最新 token（autofill）
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .models import Account

TRAE_CLIENT_ID = "en1oxy7wnw8j9n"
TRAE_EXCHANGE_URL = "https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken"
QODER_BASE = {
    "cn": "https://openapi.qoder.com.cn",
    "global": "https://openapi.qoder.sh",
}
QODER_REFRESH_PATH = "/api/v1/deviceToken/refresh"


def _default_post(method: str, url: str, headers: Dict[str, str],
                  body: Optional[bytes], timeout: int = 20) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class TokenRefresher:
    """按平台续期 token 并写回保险库；http_fn 与适配器共用，便于测试。"""

    def __init__(self, http_fn=None, vault=None):
        self.http_fn = http_fn
        self._vault = vault
        # 最近一次续期拿到的新 token。执行器用它在本次进程内直接替换凭据，
        # 避免"环境变量优先于保险库"时重新解析又拿回旧 token 导致续期白做。
        self.last_token: Optional[str] = None
        # 最近一次恢复后是否需要「页面登录授权」（Qoder 本机与刷新都失败时为 True）。
        # 计划任务无人值守时只能记录该信号；面板签到时前端会据此拉起授权弹窗。
        self.need_login: bool = False
        # 本机获取同时拿到的新字段（device_id/uid 等），执行器需在本次进程内合并进凭据
        self.last_fields: Optional[Dict[str, Any]] = None

    # ---------- 工具 ----------
    def _post(self, url: str, headers: Dict[str, str], payload: Dict[str, Any],
              form: bool = False) -> Tuple[int, str]:
        if form:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {**headers, "Content-Type": "application/json"}
        if self.http_fn:
            return self.http_fn("POST", url, headers, body, 20)
        return _default_post("POST", url, headers, body)

    def _vault_obj(self):
        if self._vault is not None:
            return self._vault
        from .credentials import default_vault
        return default_vault()

    def _save(self, account: Account, token: str, fields: Dict[str, Any],
              refresh_token: str) -> None:
        secrets = {"refresh_token": refresh_token} if refresh_token else {}
        self._vault_obj().put(account.platform, account.version, account.id,
                              token, fields=fields, secrets=secrets)

    # ---------- 各平台 ----------
    def _refresh_trae(self, refresh_token: str) -> Tuple[Optional[str], Optional[str], str]:
        status, text = self._post(TRAE_EXCHANGE_URL, {"Accept": "application/json"}, {
            "ClientID": TRAE_CLIENT_ID, "RefreshToken": refresh_token,
            "ClientSecret": "-", "UserID": "",
        })
        if status >= 400 or status <= 0:
            return None, None, f"续期请求 HTTP {status}"
        try:
            result = json.loads(text).get("Result") or {}
        except ValueError:
            return None, None, "续期响应不是 JSON"
        token = str(result.get("Token") or "")
        new_refresh = str(result.get("RefreshToken") or "")
        if not token:
            return None, None, "续期响应缺少 Token"
        return token, (new_refresh or refresh_token), "ok"

    def _refresh_qoder(self, version: str, refresh_token: str) -> Tuple[Optional[str], Optional[str], str]:
        base = QODER_BASE.get(version, QODER_BASE["cn"])
        url = base + QODER_REFRESH_PATH
        headers = {"Accept": "application/json", "User-Agent": "Qoder"}
        status, text = self._post(url, headers, {"refresh_token": refresh_token})
        if status >= 400 and refresh_token.startswith("drt-"):
            # drt- 的 refresh 在部分网关不接受 JSON，改用表单再试一次
            status, text = self._post(url, headers, {"refresh_token": refresh_token}, form=True)
        if status in (401, 403):
            return None, None, "refresh_token 已失效，请重新登录客户端"
        if status >= 400 or status <= 0:
            return None, None, f"续期请求 HTTP {status}"
        try:
            payload = json.loads(text)
        except ValueError:
            return None, None, "续期响应不是 JSON"
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = ""
        for key in ("device_token", "deviceToken", "token", "access_token"):
            if data.get(key):
                token = str(data[key])
                break
        new_refresh = str(data.get("refresh_token") or data.get("refreshToken") or refresh_token)
        if not token:
            return None, None, "续期响应缺少 access token"
        return token, new_refresh, "ok"

    # ---------- 本机自动获取 ----------
    def _local_detect(self, account: Account) -> Tuple[Optional[str], Optional[str], Dict[str, Any], str]:
        """从本机客户端登录态重新取凭据，返回 (token, refresh_token, fields, note)。

        本机一直登录过客户端时这是最可靠的来源：token 由客户端自己续期，
        保险库里存的可能是旧的。字段尽量保留原凭据里的（如 Qoder 设备身份）。
        """
        platform = account.platform
        try:
            if platform == "workbuddy":
                from .autofill import auto_detect
                result = auto_detect()
            elif platform == "trae":
                from .autofill_trae import auto_detect
                result = auto_detect()
            elif platform == "qoder":
                from .autofill_qoder_local import auto_detect
                result = auto_detect(account.version or "cn")
            else:
                return None, None, {}, f"平台 {platform} 不支持本机获取"
        except Exception as exc:
            return None, None, {}, f"本机获取异常: {type(exc).__name__}"

        if not result.get("ok") or not result.get("token"):
            return None, None, {}, f"本机未取到新凭据: {result.get('error') or '未知原因'}"

        token = str(result["token"])
        refresh = str(result.get("refresh_token") or "")
        fields: Dict[str, Any] = {}
        for key in ("uid", "user_id", "domain", "device_id", "account_name",
                    "phone", "email", "uin", "endpoint"):
            if result.get(key):
                fields[key] = result[key]
        return token, refresh, fields, f"已从{platform}本机客户端重新获取凭据"

    # ---------- 统一恢复入口 ----------
    def recover(self, account: Account, cred: Any) -> Tuple[bool, str]:
        """凭据失效后的分级恢复：本机获取 → refresh_token 续期 → 标记需页面授权。

        与 refresh() 的区别：refresh() 只做协议续期（保留兼容性），
        recover() 是先试本机登录态（用户本机常登录，token 更新），失败再续期。
        成功后 ``last_token`` 记下新 token，executor 直接用它替换内存凭据。
        """
        self.last_token = None
        self.last_fields = None
        self.need_login = False
        platform = account.platform

        # 第一级：本机自动获取（Trae/Qoder/WorkBuddy 都支持）
        token, refresh, fields, note = self._local_detect(account)
        if token:
            merged = dict(cred.fields or {})
            merged.update(fields)
            self._save(account, token, merged, refresh or "")
            self.last_token = token
            self.last_fields = fields
            return True, note

        local_note = note

        # 第二级：refresh_token 协议续期（Trae ExchangeToken / Qoder deviceToken/refresh）
        ok, refresh_note = self.refresh(account, cred)
        if ok:
            return True, refresh_note

        # 第三级：Qoder 有页面登录授权渠道，标记让前端弹窗；其它平台只能提示重新配置
        if platform == "qoder":
            self.need_login = True
            return False, f"{local_note}；{refresh_note}；请在页面用「设备授权登录」重新认证"
        return False, f"{local_note}；{refresh_note}"

    # ---------- 入口 ----------
    def refresh(self, account: Account, cred: Any) -> Tuple[bool, str]:
        """尝试续期；成功会把新 token（及轮换后的 refresh_token）写回保险库。

        成功时同时把新 token 记到 ``self.last_token``，供调用方在本次进程内
        直接替换内存凭据（环境变量来源时重新 resolve 会拿回旧值）。
        """
        self.last_token = None
        platform = account.platform
        refresh_token = str(cred.secrets.get("refresh_token")
                            or cred.fields.get("refresh_token") or "")

        if platform == "workbuddy":
            # 客户端自己会续期，直接重新解密最新登录态
            from .autofill import auto_detect
            result = auto_detect()
            if not result.get("ok"):
                return False, f"本机重取失败: {result.get('error')}"
            fields = dict(cred.fields)
            fields.update({"uid": result.get("uid", ""), "domain": result.get("domain", "")})
            self._save(account, result["token"], fields, "")
            self.last_token = result["token"]
            return True, "已从本机 WorkBuddy 客户端重新解密最新 token"

        if not refresh_token:
            return False, "没有可用的 refresh_token，无法自动续期"

        if platform == "trae":
            token, new_refresh, note = self._refresh_trae(refresh_token)
        elif platform == "qoder":
            token, new_refresh, note = self._refresh_qoder(account.version, refresh_token)
        else:
            return False, f"平台 {platform} 暂不支持自动续期"

        if not token:
            return False, note
        self._save(account, token, dict(cred.fields), new_refresh)
        self.last_token = token
        return True, "token 已续期并写回保险库"
