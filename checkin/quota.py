"""账号额度/积分余额与到期时间查询（只读）。

三个平台的计费接口都不在签到契约里，单独放在本模块：
- WorkBuddy CN/Global: POST {endpoint}/billing/meter/get-user-resource-summary
  再按汇总返回的 PackageCode 查 free/paid packages 拿到期时间。
- Trae CN: POST https://api.trae.cn/trae/api/v2/pay/web_user_ent_usage
  （社区验证的额度快照接口；本机登录态 token 可能已过期，如实返回原因。）
- Qoder CN/Global: POST https://openapi.qoder.com.cn/api/v2/quota/usage
  （Global 为 openapi.qoder.sh；响应 userQuota/addOnQuota。）

设计原则：
1. 只读查询，绝不调用签到/领取/登录/续期接口。
2. 凭据复用 credentials.resolve()，token 只在内存中使用，不打印、不落盘。
3. 每个账号独立查询，单个失败不影响其它账号；失败返回明确 reason。
4. 缓存 5 分钟（进程内），页面刷新命中缓存；force=True 跳过缓存。
5. 返回数据全部是可展示的聚合值（剩余/总额/套餐/到期），不含敏感字段。
"""
from __future__ import annotations

import json as _json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .credentials import resolve as resolve_credential
from .models import Account, UNSUPPORTED_VERSIONS

CACHE_TTL = 300  # 5 分钟，与 cc-switch 一致
_BEIJING = timezone(timedelta(hours=8))

# WorkBuddy 资源包分组，用于判断查 free 还是 paid 明细
_FREE_CODES = (
    "TCACA_code_001", "TCACA_code_008", "TCACA_code_035", "TCACA_code_006",
    "TCACA_code_039", "TCACA_code_040", "TCACA_code_007", "TCACA_code_028",
    "TCACA_code_037", "TCACA_code_029", "TCACA_code_030",
)


@dataclass
class QuotaInfo:
    """单个账号的额度快照（全部为可展示字段）。"""
    key: str = ""
    platform: str = ""
    version: str = ""
    account_id: str = ""
    ok: bool = False
    remaining: Optional[float] = None      # 剩余积分
    total: Optional[float] = None          # 总额度（可为 0）
    used: Optional[float] = None
    plan: str = ""                         # 套餐名，如「体验版」
    expires_at: str = ""                   # 最近一批积分的到期时间 YYYY-MM-DD HH:MM
    expires_credits: Optional[float] = None  # 该批到期积分数量
    reason: str = ""                       # 查不到时的原因（凭据/网络/不支持）
    queried_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        def _num(v: Optional[float]):
            if v is None:
                return None
            return int(v) if float(v).is_integer() else round(float(v), 2)

        return {
            "key": self.key, "platform": self.platform, "version": self.version,
            "account_id": self.account_id, "ok": self.ok,
            "remaining": _num(self.remaining), "total": _num(self.total),
            "used": _num(self.used), "plan": self.plan,
            "expires_at": self.expires_at, "expires_credits": _num(self.expires_credits),
            "reason": self.reason,
            "queried_at": datetime.fromtimestamp(self.queried_at, _BEIJING).isoformat(timespec="seconds")
            if self.queried_at else "",
        }


class QuotaCache:
    """进程内缓存：按账号 key 存 QuotaInfo，TTL 到期或 force 时重查。"""

    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._data: Dict[str, QuotaInfo] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[QuotaInfo]:
        with self._lock:
            item = self._data.get(key)
            if item and (time.time() - item.queried_at) < self.ttl:
                return item
            return None

    def put(self, item: QuotaInfo) -> QuotaInfo:
        with self._lock:
            self._data[item.key] = item
        return item

    def invalidate(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key:
                self._data.pop(key, None)
            else:
                self._data.clear()


def _post_json(url: str, body: Dict[str, Any], headers: Dict[str, str],
               timeout: int = 15) -> Dict[str, Any]:
    """发一个 JSON POST，返回解析后的 dict；非 2xx 抛 RuntimeError。"""
    req = urllib.request.Request(
        url, data=_json.dumps(body).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("凭据已失效，请到「账号配置」重新获取") from None
        raise RuntimeError(f"HTTP {exc.code}") from None
    except Exception as exc:
        raise RuntimeError(type(exc).__name__) from None
    try:
        data = _json.loads(raw)
    except ValueError:
        raise RuntimeError("响应不是 JSON") from None
    return data if isinstance(data, dict) else {}


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or value == "NaN":
            return None
        f = float(value)
        if f < 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _sum_packages(packages: Any) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """汇总 Packages[].CycleTotalCapacity / Remain / Used，返回 (total, remain, used)。"""
    if not isinstance(packages, list):
        return None, None, None
    total = remain = used = 0.0
    seen = False
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        t = _num(pkg.get("CycleTotalCapacity"))
        r = _num(pkg.get("CycleRemainCapacity"))
        u = _num(pkg.get("CycleUsedCapacity"))
        if t is None and r is None and u is None:
            continue
        seen = True
        total += t or 0
        remain += r or 0
        used += u or 0
    if not seen:
        return None, None, None
    return total, remain, used


def _parse_time(value: Any) -> Optional[datetime]:
    """解析毫秒/秒时间戳或北京时间字符串 YYYY-MM-DD HH:MM:SS。"""
    if isinstance(value, (int, float)) and value > 10 ** 11:
        try:
            return datetime.fromtimestamp(value / 1000, _BEIJING)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, (int, float)) and value > 10 ** 9:
        # Trae 的 end_time 是秒级时间戳
        try:
            return datetime.fromtimestamp(value, _BEIJING)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str) and value:
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:len(fmt) + 2], fmt).replace(tzinfo=_BEIJING)
            except ValueError:
                continue
    return None


def _earliest_expiry(accounts: Any) -> tuple[str, Optional[float]]:
    """从资源包明细中找最早到期的一批积分，返回 (时间字符串, 积分数量)。

    与 cc-switch 一致：按到期时间升序，合并同一时间的积分，
    跳过剩余 <= 0 和「按天切片」的包，超远时间视为永不过期。
    """
    if not isinstance(accounts, list):
        return "", None
    # 按「日期」合并：cc-switch 的展示逻辑把同一天到期的积分相加（688+100=788），
    # 日期取当天最早一笔的时间，数值取合计。
    groups: Dict[Any, Dict[str, Any]] = {}
    far_future = datetime.now(_BEIJING) + timedelta(days=365 * 10)
    for item in accounts:
        if not isinstance(item, dict):
            continue
        capacity_type = item.get("CapacityType")
        if capacity_type == 4 and item.get("SlicePeriodUsageDetails"):
            continue  # 按天切片，天天到期，没有展示意义
        credits = _num(item.get("CycleCapacityRemainPrecise"))
        if credits is None:
            credits = _num(item.get("CycleCapacityRemain"))
        if credits is None or credits <= 0:
            credits = _num(item.get("CapacityRemainPrecise"))
        if credits is None or credits <= 0:
            continue
        when = _parse_time(item.get("DeductionEndTime")) or _parse_time(item.get("ExpiredTime"))
        cycle = _parse_time(item.get("CycleEndTime"))
        if when and cycle and cycle < when:
            when = cycle
        when = when or cycle
        if when is None or when > far_future:
            continue
        day = when.date()
        group = groups.setdefault(day, {"when": when, "credits": 0.0})
        group["credits"] += credits
        if when < group["when"]:
            group["when"] = when
    if not groups:
        return "", None
    first = min(groups)
    group = groups[first]
    return group["when"].strftime("%Y-%m-%d %H:%M"), group["credits"]


def _workbuddy(account: Account, cred, info: QuotaInfo) -> QuotaInfo:
    base = str(cred.fields.get("endpoint") or "https://copilot.tencent.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "X-User-Id": str(cred.fields.get("uid", "")),
        "X-Domain": str(cred.fields.get("domain", "")),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "WorkBuddy",
    }
    if cred.fields.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(cred.fields["enterprise_id"])
        headers["X-Tenant-Id"] = str(cred.fields["enterprise_id"])

    # 企业号走 enterprise usage，个人号走资源汇总
    if cred.fields.get("enterprise_id"):
        try:
            d = _post_json(base + "/v2/billing/meter/get-enterprise-user-usage", {}, headers)
            data = d.get("data") or {}
            if d.get("code") not in (0, None) and data:
                pass
            remain = _num(data.get("credit"))
            total = _num(data.get("limitNum"))
            if remain is not None or total is not None:
                info.ok = True
                info.remaining, info.total = remain, total
                info.used = (total - remain) if (total is not None and remain is not None) else None
                end = _parse_time(data.get("cycleEndTime"))
                if end:
                    info.expires_at = end.strftime("%Y-%m-%d %H:%M")
                return info
        except RuntimeError:
            pass  # 企业接口失败时继续尝试通用汇总

    summary = _post_json(base + "/billing/meter/get-user-resource-summary", {}, headers)
    if summary.get("code") not in (0, None):
        raise RuntimeError(str(summary.get("msg") or "接口返回错误"))
    data = summary.get("data") or {}
    # TotalCredits 虽然叫 Total，实际是「剩余积分」（与 CreditGroups.CycleRemainCapacity 一致），
    # 不能拿它当总额：总额要汇总 Packages / CreditGroups 的 CycleTotalCapacity。
    total, remain, used = _sum_packages(data.get("Packages"))
    if total is None:
        gtotal, gremain, gused = _sum_packages(data.get("CreditGroups"))
        if total is None:
            total = gtotal
        if remain is None:
            remain = gremain
        if used is None:
            used = gused
    direct_remaining = _num(data.get("TotalCredits"))
    if remain is None:
        remain = direct_remaining
    if total is None:
        total = direct_remaining  # 只有 TotalCredits 时的最后兜底
    info.total = total
    info.remaining = remain
    info.used = used if used is not None else (
        (total - remain) if (total is not None and remain is not None) else None)
    info.plan = str(data.get("SubscriptionPackageName") or "").strip()
    info.ok = True

    # 查资源包明细拿到期时间（只查汇总里出现的 PackageCode）
    codes = [str(p.get("PackageCode")) for p in (data.get("Packages") or [])
             if isinstance(p, dict) and p.get("PackageCode")]
    if codes:
        today = datetime.now(_BEIJING).strftime("%Y-%m-%d")
        free_codes = [c for c in codes if any(c.startswith(p) for p in _FREE_CODES)]
        paid_codes = [c for c in codes if c not in free_codes]
        for path, body in (
            ("/billing/meter/get-user-resource-free-packages", {
                "PageNumber": 1, "PageSize": 200, "PackageCodes": free_codes,
                "Status": [0, 3],
                "SlicePeriodStartTime": today + " 00:00:00",
                "SlicePeriodEndTime": today + " 23:59:59",
            }),
            ("/billing/meter/get-user-resource-paid-packages", {
                "PageNumber": 1, "PageSize": 200, "PackageCodes": paid_codes,
                "Status": [0, 3], "NeedRenewInfo": True,
            }),
        ):
            if not body["PackageCodes"]:
                continue
            try:
                detail = _post_json(base + path, body, headers)
                expires_at, credits = _earliest_expiry((detail.get("data") or {}).get("Accounts"))
                if expires_at:
                    # 取所有查询里最早的一批
                    if not info.expires_at or expires_at < info.expires_at:
                        info.expires_at = expires_at
                        info.expires_credits = credits
            except RuntimeError:
                pass  # 明细失败不影响总量展示
    return info


def _trae(account: Account, cred, info: QuotaInfo) -> QuotaInfo:
    headers = {
        "Authorization": f"Cloud-IDE-JWT {cred.token}",
        "x-device-id": str(cred.fields.get("device_id", "")),
        "Content-Type": "application/json",
        "X-User-Region": "CN",
        "x-device-type": "Windows",
        "x-app-version": str(cred.fields.get("app_version") or "1.107.1"),
        "User-Agent": "Trae/1.107.1",
        "Origin": "https://www.trae.cn",
        "Referer": "https://www.trae.cn/",
    }
    d = _post_json("https://api.trae.cn/trae/api/v2/pay/web_user_ent_usage", {}, headers)
    code = d.get("code")
    if code not in (0, None):
        raise RuntimeError("凭据已失效或额度接口不可用")
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    # 接口结构可能变化：尽量兼容常见的额度字段
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    remain = _num(data.get("remainingCredits") or data.get("remaining_credits")
                  or data.get("creditRemain") or data.get("remainCredits")
                  or usage.get("remaining"))
    total = _num(data.get("totalCredits") or data.get("total_credits")
                 or data.get("creditLimit") or data.get("creditTotal")
                 or usage.get("total"))
    if remain is None:
        remain = _num(data.get("credits"))
    # 实际响应：total_balance / balance_summary[].total_balance，总额在 usage_summary.total_amount
    if remain is None:
        remain = _num(data.get("total_balance"))
        if remain is None and isinstance(data.get("balance_summary"), list):
            for item in data["balance_summary"]:
                if isinstance(item, dict) and _num(item.get("total_balance")) is not None:
                    remain = _num(item["total_balance"])
                    break
    usage_summary = data.get("usage_summary") if isinstance(data.get("usage_summary"), dict) else {}
    if total is None:
        total = _num(usage_summary.get("total_amount"))
    if remain is None and total is None:
        raise RuntimeError("额度接口未返回可识别的积分字段")
    info.ok = True
    info.remaining, info.total = remain, total
    info.used = (total - remain) if (total is not None and remain is not None) else None
    used = _num(usage_summary.get("consumed_amount"))
    if used is not None:
        info.used = used
    for key in ("planName", "identityStr", "user_pay_identity_str"):
        if data.get(key):
            info.plan = str(data[key])
            break
    for key in ("expiresAt", "expireTime", "expiryTime", "endTime"):
        moment = _parse_time(data.get(key))
        if moment:
            info.expires_at = moment.strftime("%Y-%m-%d %H:%M")
            break
    if not info.expires_at:
        # 真实响应里到期时间在资源包的 entitlement_base_info.end_time（秒级）
        ends = []
        packs = data.get("user_entitlement_pack_list")
        if isinstance(packs, list):
            for pack in packs:
                if not isinstance(pack, dict):
                    continue
                base = pack.get("entitlement_base_info") if isinstance(pack.get("entitlement_base_info"), dict) else {}
                for raw in (base.get("end_time"), pack.get("expire_time")):
                    moment = _parse_time(raw)
                    if moment:
                        ends.append(moment)
        if ends:
            info.expires_at = min(ends).strftime("%Y-%m-%d %H:%M")
    return info


def _qoder(account: Account, cred, info: QuotaInfo) -> QuotaInfo:
    base = "https://openapi.qoder.sh" if account.version == "global" else "https://openapi.qoder.com.cn"
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    d = _post_json(base + "/api/v2/quota/usage", {}, headers)
    if d.get("code") in ("TOKEN_EXPIRE", "TOKEN_INVALID") or d.get("errorCode"):
        raise RuntimeError("凭据已失效，请重新获取")
    user_quota = d.get("userQuota") if isinstance(d.get("userQuota"), dict) else {}
    addon = d.get("addOnQuota") if isinstance(d.get("addOnQuota"), dict) else {}
    remain = _num(user_quota.get("remaining")) or 0.0
    addon_remain = _num(addon.get("remaining")) or 0.0
    total = _num(user_quota.get("total"))
    addon_total = _num(addon.get("total"))
    if total is not None or addon_total is not None:
        info.total = (total or 0.0) + (addon_total or 0.0)
    info.remaining = remain + addon_remain
    info.used = _num(user_quota.get("used"))
    info.ok = True
    user_type = str(d.get("userType") or "")
    info.plan = user_type.replace("personal_", "").replace("_", " ").strip()
    end = _parse_time(d.get("expiresAt"))
    if end and end.year < 9000:  # 253402214400000 = 远端占位「永不过期」
        info.expires_at = end.strftime("%Y-%m-%d %H:%M")
    return info


def _fetch_one(account: Account, allow_local_decrypt: bool) -> QuotaInfo:
    info = QuotaInfo(
        key=f"{account.platform}:{account.version}:{account.id}",
        platform=account.platform, version=account.version, account_id=account.id,
        queried_at=time.time(),
    )
    try:
        # 没有签到活动的版本（如 WorkBuddy/Trae 国际版）不查额度：
        # 它们的凭据往往就是国内账号那份，拿国内接口查会把国内额度错显示成国际版的。
        if (account.platform, account.version) in UNSUPPORTED_VERSIONS:
            raise RuntimeError("该版本无签到活动，不查询额度")
        cred = resolve_credential(account, allow_local_decrypt=allow_local_decrypt)
        if not cred.token:
            raise RuntimeError("未配置凭据，请先到「账号配置」获取")
        if account.platform == "workbuddy":
            _workbuddy(account, cred, info)
        elif account.platform == "trae":
            if account.version != "cn":
                raise RuntimeError("该版本暂无额度查询接口")
            _trae(account, cred, info)
        elif account.platform == "qoder":
            _qoder(account, cred, info)
        else:
            raise RuntimeError("不支持的平台")
    except Exception as exc:
        info.ok = False
        info.remaining = info.total = info.used = None
        detail = str(exc).strip()
        if len(detail) > 120:
            detail = detail[:120] + "…"
        info.reason = detail or type(exc).__name__
    return info


def query_quotas(accounts: List[Account], *, cache: Optional[QuotaCache] = None,
                 force: bool = False, allow_local_decrypt: bool = False) -> List[QuotaInfo]:
    """查询所有账号的额度，单个失败不影响整体。"""
    cache = cache or _DEFAULT_CACHE
    out: List[QuotaInfo] = []
    for account in accounts:
        key = f"{account.platform}:{account.version}:{account.id}"
        item = None if force else cache.get(key)
        if item is None:
            item = cache.put(_fetch_one(account, allow_local_decrypt))
        out.append(item)
    return out


_DEFAULT_CACHE = QuotaCache()
