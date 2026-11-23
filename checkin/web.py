"""本地 Web 面板后端：仅监听 127.0.0.1，标准库实现，零第三方依赖。

API：
  GET  /api/overview     今日状态 + 统计 + 凭据探测
  GET  /api/history      执行历史（支持过滤）
  GET  /api/drifts       契约漂移事件
  GET  /api/contracts    已加载契约
  GET  /api/config       当前配置（脱敏）
  POST /api/run          手动执行签到（支持 simulate/force/日期/平台过滤）
  GET  /                 单文件面板页面
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import DEFAULTS, load_config
from .credentials import default_vault, probe_local, resolve as resolve_credential, vault_path
from .models import RunStatus
from .plan import build_daily_plan
from .utils import RunBusy, now_local, parse_date, sanitize
from . import manage
from .manage import ManageError

from . import paths
STATIC_DIR = paths.static_dir()

# 后端能力版本：新增 API/路由时递增，页面用它检测"后端是否为最新代码"
API_VERSION = "2026-11-19.3"
API_CAPS = ["autofill.workbuddy", "autofill.trae", "qoder.device", "schedule.install",
            "run.simulate_memory_only", "autofill.qoder", "identity.detected", "account.enable_cascades_version", "version.unsupported", "qoder.device_headers", "account.auto_id", "token.refresh", "verify.endpoint", "trend.chart", "account.rename", "config.general", "run.lock"]


def default_vault_keys():
    from .credentials import default_vault
    try:
        return default_vault().list_keys()
    except Exception:
        return []


class WebState:
    """持有应用上下文，供各请求共享。"""

    def __init__(self, ctx: Dict[str, Any], *, allow_local_decrypt: bool = False):
        self.ctx = ctx
        self.allow_local_decrypt = allow_local_decrypt
        self.lock = threading.Lock()
        from .utils import now_local
        self.started_at = now_local(ctx.get("cfg", {}).get("timezone", "Asia/Shanghai")).isoformat(timespec="seconds")

    @property
    def cfg(self):
        return self.ctx["cfg"]

    @property
    def storage(self):
        return self.ctx["storage"]

    @property
    def registry(self):
        return self.ctx["registry"]

    @property
    def executor(self):
        return self.ctx["executor"]

    def reload_config(self) -> None:
        """配置被页面修改后，重新加载并热更新 executor 的账号表。"""
        from .executor import Executor
        path = self.ctx["config_path"]
        cfg = load_config(path)
        for key in ("database", "log_file", "contracts_dir"):
            from .config import DEFAULTS
            raw = cfg.get(key, DEFAULTS[key])
            p = Path(raw).expanduser()
            cfg[key] = str(p if p.is_absolute() else (self.ctx["home"] / p))
        if paths.is_frozen() and cfg.get("contracts_dir") == str(
                self.ctx["home"] / DEFAULTS["contracts_dir"]):
            cfg["contracts_dir"] = str(paths.contracts_dir())
        self.ctx["cfg"] = cfg
        executor = Executor(cfg, self.storage, self.registry,
                            self.ctx["notifier"], self.ctx["logger"])
        self.ctx["executor"] = executor


def _mask_phone(phone: str) -> str:
    s = str(phone or "").strip()
    if not s or "*" in s:
        return s
    if len(s) >= 8:
        return s[:3] + "****" + s[-4:]
    return s[:2] + "***"


def _vault_identity_map() -> Dict[str, Dict[str, str]]:
    """从保险库读出每个账号的真实身份（客户端登录态解析出的账号名/手机号）。"""
    from .credentials import default_vault
    out: Dict[str, Dict[str, str]] = {}
    try:
        vault = default_vault()
        for key in vault.list_keys():
            parts = key.split(":")
            if len(parts) != 3:
                continue
            item = vault.get(*parts) or {}
            f = item.get("fields") or {}
            out[key] = {"identity": f.get("account_name") or "",
                        "phone": _mask_phone(f.get("phone", ""))}
    except Exception:
        pass
    return out


def _trend(state: WebState, days: int = 14) -> Dict[str, Any]:
    """近 N 天每日积分（本次领取 + 今日已领），缺失日期记 0。"""
    from datetime import timedelta
    tz = state.cfg.get("timezone", "Asia/Shanghai")
    today = now_local(tz).date()
    series = []
    total = 0
    for back in range(days - 1, -1, -1):
        d = (today - timedelta(days=back)).isoformat()
        credits = sum(r.credits or 0 for r in state.storage.runs_for_date(d)
                      if r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value))
        total += credits
        series.append({"date": d, "credits": credits})
    return {"days": series, "total": total}


def _exit_soon(delay: float = 1.2) -> None:
    """留出发送 HTTP 响应的时间后退出进程，让更新替换脚本接管文件。"""
    import os
    import threading
    import time

    def _do() -> None:
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=_do, daemon=True).start()


def _run_overview(state: WebState) -> Dict[str, Any]:
    cfg = state.cfg
    tz = cfg.get("timezone", "Asia/Shanghai")
    today = now_local(tz).date()
    runs = state.storage.runs_for_date(today.isoformat())
    by_status: Dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    # 今日积分：成功领取 + 已签（服务端返回的今日额度）都算当天到手
    total_credits = sum(
        r.credits or 0 for r in runs
        if r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value)
    )

    identities = _vault_identity_map()
    accounts = []
    for acc in cfg.get("accounts", []):
        cred = resolve_credential(acc, allow_local_decrypt=False)
        ident = identities.get(f"{acc.platform}:{acc.version}:{acc.id}", {})
        accounts.append({
            "id": acc.id,
            "name": acc.name or acc.id,
            "platform": acc.platform,
            "version": acc.version,
            "enabled": acc.enabled,
            "credential_source": cred.source,
            "credential_masked": cred.masked(),
            "identity": ident.get("identity", ""),
            "identity_phone": ident.get("phone", ""),
            "device_identity": bool(cred.fields.get("machine_token")),
            "has_contract": state.registry.has(acc.platform, acc.version),
        })

    run_items = []
    for r in runs:
        item = _serialize_run(r)
        ident = identities.get(f"{r.platform}:{r.version}:{r.account_id}", {})
        item["account_identity"] = ident.get("identity", "")
        run_items.append(item)

    return {
        "today": today.isoformat(),
        "timezone": tz,
        "window": cfg.get("checkin_window", {}),
        "catchup_days": cfg.get("catchup_days", 0),
        "stats": {
            "total": len(runs),
            "by_status": by_status,
            "credits_today": total_credits,
            "failed_total": state.storage.failed_count(),
            "drift_total": state.storage.drift_count(),
        },
        "runs": run_items,
        "accounts": accounts,
        "probe": probe_local(),
        "contract_errors": list(state.registry.errors),
    }


def _serialize_run(r) -> Dict[str, Any]:
    return {
        "key": r.key,
        "account_id": r.account_id,
        "platform": r.platform,
        "version": r.version,
        "target_date": r.target_date,
        "status": r.status,
        "attempts": r.attempts,
        "credits": r.credits,
        "is_catchup": r.is_catchup,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "message": r.message,
    }


def _handle_run(state: WebState, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = state.cfg
    tz = cfg.get("timezone", "Asia/Shanghai")
    target = parse_date(payload.get("date"), tz)
    only = {
        "platform": str(payload.get("platform") or ""),
        "version": str(payload.get("version") or ""),
        "account_id": str(payload.get("account_id") or ""),
    }
    simulate = bool(payload.get("simulate", False))
    force = bool(payload.get("force", False))
    allow_local = bool(payload.get("allow_local_decrypt", state.allow_local_decrypt))

    with state.lock:
        tasks = build_daily_plan(
            cfg, state.storage, target_date=target, only=only,
            include_catchup=not bool(payload.get("no_catchup", False)),
            include_completed=force,
        )
        if not tasks:
            # 解释为什么没任务，而不是一句干巴巴的提示
            existing = state.storage.runs_for_date(target.isoformat())
            def _match(r):
                return (not only["platform"] or r.platform == only["platform"]) and \
                       (not only["version"] or r.version == only["version"]) and \
                       (not only["account_id"] or r.account_id == only["account_id"])
            existing = [r for r in existing if _match(r)]
            done = [r for r in existing if r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value)]
            drift = [r for r in existing if r.status == RunStatus.DRIFT.value]
            if done:
                msg = (f"今天这 {len(done)} 个账号已经签过了，无需重复执行。"
                       f"想再跑一遍可勾选「忽略去重强制执行」。")
            elif drift:
                msg = f"有 {len(drift)} 个账号因接口变更需人工处理，请到「异常与漂移」查看。"
            else:
                msg = "当前没有可执行的账号，请确认「账号配置」中已启用账号并配好凭据。"
            return {"ok": True, "records": [], "message": msg}
        try:
            records = state.executor.run_tasks(
                tasks, simulate=simulate, force=force, allow_local_decrypt=allow_local
            )
        except RunBusy as exc:
            return {"ok": False, "busy": True,
                    "records": [],
                    "message": f"已有签到进程在运行（{exc}），本次未执行。等它结束后再试。"}
    return {
        "ok": all(r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value,
                               RunStatus.SKIPPED.value) for r in records),
        "records": [_serialize_run(r) for r in records],
        "message": f"执行 {len(records)} 项",
    }


class WebHandler(BaseHTTPRequestHandler):
    state: WebState

    server_version = "CreditCheckinWeb/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # 静默访问日志，避免刷屏；异常由 do_* 捕获记录到项目日志
        return

    # ---------- 响应工具 ----------
    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(sanitize(data), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        if not path.is_file():
            self._send_json({"error": "面板页面缺失"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                self._send_html(STATIC_DIR / "index.html")
                return
            if path == "/api/health":
                import time as _t
                self._send_json({"ok": True, "api_version": API_VERSION,
                                 "capabilities": API_CAPS,
                                 "started_at": getattr(self.state, "started_at", ""),
                                 "pid": __import__("os").getpid()})
                return
            if path == "/api/version":
                from .updater import check_update
                qs = urllib.parse.parse_qs(parsed.query)
                force = str(qs.get("force", ["0"])[0]).lower() in ("1", "true", "yes")
                self._send_json(check_update(force=force).to_dict())
                return
            if path == "/api/overview":
                self._send_json(_run_overview(self.state))
                return
            if path == "/api/history":
                qs = urllib.parse.parse_qs(parsed.query)
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
                runs = self.state.storage.history(
                    limit,
                    platform=qs.get("platform", [None])[0],
                    account_id=qs.get("account_id", [None])[0],
                    status=qs.get("status", [None])[0],
                )
                self._send_json({"items": [_serialize_run(r) for r in runs]})
                return
            if path == "/api/drifts":
                qs = urllib.parse.parse_qs(parsed.query)
                limit = min(int(qs.get("limit", ["50"])[0]), 500)
                items = []
                for e in self.state.storage.drifts(limit):
                    items.append({
                        "id": e.id, "platform": e.platform, "version": e.version,
                        "contract_version": e.contract_version, "reason": e.reason,
                        "sample": e.sample, "detected_at": e.detected_at,
                    })
                self._send_json({"items": items})
                return
            if path == "/api/contracts":
                self._send_json({
                    "items": self.state.registry.summary(),
                    "errors": list(self.state.registry.errors),
                })
                return
            if path == "/api/config":
                cfg = self.state.cfg
                self._send_json({
                    "timezone": cfg.get("timezone"),
                    "checkin_window": cfg.get("checkin_window"),
                    "catchup_days": cfg.get("catchup_days"),
                    "retry": cfg.get("retry"),
                    "notify": {k: ("<set>" if k == "webhook" and v else v)
                               for k, v in (cfg.get("notify") or {}).items()},
                    "config_path": cfg.get("_config_path"),
                    "data_dir": str(paths.data_root()),
                })
                return
            if path == "/api/accounts":
                try:
                    raw_cfg = manage.read_raw(self.state.ctx["config_path"])
                except Exception:
                    raw_cfg = None
                accounts = manage.list_accounts(self.state.cfg, raw_cfg)
                from .credentials import default_vault
                from .credentials import qoder_device_identity
                try:
                    qdev = qoder_device_identity()
                except Exception:
                    qdev = {}
                try:
                    vault = default_vault()
                except Exception:
                    vault = None
                for a in accounts:
                    item = None
                    if vault is not None:
                        try:
                            item = vault.get(a["platform"], a["version"], a["id"])
                        except Exception:
                            item = None
                    f = (item or {}).get("fields") or {}
                    a["has_credential"] = bool((item or {}).get("token"))
                    # 真实账号身份（来自客户端登录态，非用户手填的标签）
                    a["identity"] = f.get("account_name") or f.get("nickname") or f.get("uid") or ""
                    a["identity_phone"] = _mask_phone(f.get("phone", ""))
                    a["identity_uid"] = f.get("uid", "")
                    # 该版本本身没有签到活动（如 Trae 国际版）：禁止启用，页面给出原因
                    contract = self.state.registry.contracts.get(
                        (a["platform"], a["version"])) or {}
                    a["unsupported"] = str(contract.get("status", "")).lower() in ("unsupported", "disabled")
                    a["note"] = str(contract.get("note", ""))
                    a["device_identity"] = bool(qdev.get("machine_token")) if a["platform"] == "qoder" else None
                self._send_json({"items": accounts,
                                 "encrypted": self.state.ctx.get("vault_encrypted", False)})
                return
            if path == "/api/guide":
                qs = urllib.parse.parse_qs(parsed.query)
                platform = qs.get("platform", [""])[0]
                self._send_json(manage.token_guide(platform))
                return
            if path == "/api/autofill":
                qs = urllib.parse.parse_qs(parsed.query)
                platform = qs.get("platform", [""])[0]
                version = qs.get("version", ["cn"])[0]
                account_id = qs.get("id", [""])[0]
                if platform == "workbuddy":
                    from .autofill import auto_detect
                    result = auto_detect()
                    fields = {"uid": result.get("uid", ""), "domain": result.get("domain", ""),
                              "account_name": result.get("nickname") or result.get("uin", ""),
                              "phone": result.get("phone", "")}
                    token = result.get("token", "")
                    label = "WorkBuddy"
                elif platform == "trae":
                    from .autofill_trae import auto_detect as trae_detect
                    result = trae_detect()
                    fields = {"device_id": result.get("device_id", ""),
                              "uid": result.get("user_id", ""),
                              "account_name": result.get("account_name", ""),
                              "phone": result.get("phone", "")}
                    token = result.get("token", "")
                    label = "Trae"
                elif platform == "qoder":
                    from .autofill_qoder_local import auto_detect as qoder_local
                    result = qoder_local(version)
                    fields = {"uid": result.get("user_id", ""),
                              "account_name": result.get("account_name", ""),
                              "phone": result.get("phone", "")}
                    token = result.get("token", "")
                    label = "Qoder"
                else:
                    self._send_json({"ok": False,
                                     "error": "不支持的平台"}, 400)
                    return
                if not result.get("ok") or not token:
                    self._send_json({"ok": False, "error": result.get("error", "自动获取失败"),
                                     "detail": result.get("detail", "")}, 400)
                    return
                # 直接存入保险库：token 不经浏览器，页面只拿到成功标记与脱敏信息
                enc = "none"
                if account_id:
                    # refresh_token 等敏感值走加密 secrets，不与普通字段混放
                    secrets = {}
                    if result.get("refresh_token"):
                        secrets["refresh_token"] = result["refresh_token"]
                    enc = manage.save_credential(platform, version, account_id, token, fields,
                                                 secrets=secrets)
                masked = token[:6] + "…" + token[-4:]
                who = fields.get("account_name") or ""
                safe_fields = dict(fields)
                safe_fields["phone"] = _mask_phone(fields.get("phone", ""))
                self._send_json({"ok": True, "masked": masked, "encoding": enc,
                                 "fields": safe_fields,
                                 "identity": who, "identity_phone": safe_fields["phone"],
                                 "message": (f"已从本机 {label} 自动获取并加密保存凭据"
                                             + (f"（真实账号：{who}）" if who else ""))})
                return
            if path == "/api/qoder/login/start":
                qs = urllib.parse.parse_qs(parsed.query)
                from .autofill_qoder import start as qoder_start
                try:
                    self._send_json({"ok": True, **qoder_start(qs.get("region", ["cn"])[0])})
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"发起授权失败: {exc}"}, 400)
                return
            if path == "/api/qoder/login/poll":
                qs = urllib.parse.parse_qs(parsed.query)
                session = qs.get("session", [""])[0]
                account_id = qs.get("id", [""])[0]
                version = qs.get("version", ["cn"])[0]
                from .autofill_qoder import poll as qoder_poll
                reply = qoder_poll(session)
                if not reply.get("ok"):
                    self._send_json(reply, 400)
                    return
                if reply.get("pending"):
                    self._send_json(reply)
                    return
                # 授权成功：存 vault（refresh_token 走加密 secrets，续期要用）
                enc = "none"
                if account_id:
                    secrets = {}
                    if reply.get("refresh_token"):
                        secrets["refresh_token"] = reply["refresh_token"]
                    enc = manage.save_credential(
                        "qoder", version, account_id, reply["token"],
                        {"uid": reply.get("user_id", "")}, secrets=secrets)
                masked = reply["token"][:6] + "…" + reply["token"][-4:]
                self._send_json({"ok": True, "pending": False, "masked": masked,
                                 "encoding": enc, "region": reply.get("region"),
                                 "message": "设备授权成功，凭据已加密保存"})
                return
            if path == "/api/schedule":
                raw = manage.read_raw(self.state.ctx["config_path"])
                info = manage.get_schedule(raw)
                info["task"] = manage.task_status(info["task_name"])
                info["supported"] = info["task"].get("supported", False)
                self._send_json(info)
                return
            if path == "/api/attempts":
                qs = urllib.parse.parse_qs(parsed.query)
                key = qs.get("key", [""])[0]
                attempts = self.state.storage.attempts_for(key)
                self._send_json({"items": [
                    {"attempt": a.attempt, "at": a.at, "status": a.status,
                     "message": a.message, "detail": a.detail}
                    for a in attempts
                ]})
                return
            if path.startswith("/api/trend"):
                qs = urllib.parse.parse_qs(parsed.query)
                days = max(1, min(60, int(qs.get("days", ["14"])[0])))
                self._send_json(_trend(self.state, days))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.state.ctx["logger"].warning(f"Web API 异常 {path}: {exc}")
            self._send_json({"error": f"请求失败: {type(exc).__name__}"}, 500)

    # ---------- POST ----------
    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/update/apply":
                self._read_json()
                from .updater import apply_update, check_update
                info = check_update(force=True)
                result = apply_update(info, paths.data_root())
                self._send_json(result)
                if result.get("ok"):
                    _exit_soon()  # 响应发完后退出，让替换脚本接管
                return
            if path == "/api/run":
                payload = self._read_json()
                self._send_json(_handle_run(self.state, payload))
                return
            if path == "/api/account":
                payload = self._read_json()
                action = str(payload.get("action", "add"))
                config_path = self.state.ctx["config_path"]
                if action == "add":
                    acc = manage.add_account(
                        config_path, payload.get("platform", ""), payload.get("version", ""),
                        payload.get("id", ""), payload.get("name", ""))
                    self.state.reload_config()
                    self._send_json({"ok": True, "account": acc,
                                     "message": f"已添加 {acc['id']}，接着点「配置凭据」自动识别真实账号"})
                elif action == "remove":
                    ok = manage.remove_account(
                        config_path, payload.get("platform", ""), payload.get("version", ""),
                        payload.get("id", ""))
                    self.state.reload_config()
                    self._send_json({"ok": ok, "message": "账号已删除" if ok else "未找到该账号"})
                elif action == "enable":
                    ok = manage.set_account_enabled(
                        config_path, payload.get("platform", ""), payload.get("version", ""),
                        payload.get("id", ""), bool(payload.get("enabled", True)))
                    self.state.reload_config()
                    self._send_json({"ok": ok, "message": "账号状态已更新" if ok else "未找到该账号"})
                elif action == "rename":
                    ok = manage.set_account_name(
                        config_path, payload.get("platform", ""), payload.get("version", ""),
                        payload.get("id", ""), str(payload.get("name", "")))
                    self.state.reload_config()
                    self._send_json({"ok": ok, "message": "备注名已更新" if ok else "未找到该账号"})
                elif action == "enable_version":
                    ok = manage.set_version_enabled(
                        config_path, payload.get("platform", ""), payload.get("version", ""),
                        bool(payload.get("enabled", True)))
                    self.state.reload_config()
                    self._send_json({"ok": ok, "message": "版本状态已更新" if ok else "未找到该版本"})
                else:
                    self._send_json({"error": "未知操作"}, 400)
                return
            if path == "/api/credential":
                payload = self._read_json()
                action = str(payload.get("action", "save"))
                p, v, aid = (str(payload.get(k, "")) for k in ("platform", "version", "id"))
                if action == "save":
                    enc = manage.save_credential(
                        p, v, aid, str(payload.get("token", "")),
                        payload.get("fields") if isinstance(payload.get("fields"), dict) else None)
                    self._send_json({"ok": True, "encoding": enc,
                                     "message": "凭据已加密保存" if enc == "dpapi" else "凭据已保存（明文）"})
                elif action == "delete":
                    ok = manage.delete_credential(p, v, aid)
                    self._send_json({"ok": ok, "message": "凭据已删除" if ok else "未找到凭据"})
                else:
                    self._send_json({"error": "未知操作"}, 400)
                return
            if path == "/api/verify":
                payload = self._read_json()
                p = str(payload.get("platform", "")).strip()
                v = str(payload.get("version", "")).strip()
                aid = str(payload.get("id", "")).strip()
                acc = next((a for a in self.state.cfg.get("accounts", [])
                            if a.id == aid and a.platform == p and a.version == v), None)
                if acc is None:
                    self._send_json({"ok": False, "error": "未找到该账号"}, 404)
                    return
                adapter = self.state.registry.get(p, v)
                if adapter is None or not hasattr(adapter, "check_status"):
                    self._send_json({"ok": False, "error": "该平台契约不支持在线验证"}, 400)
                    return
                cred = resolve_credential(acc)
                if not cred.token:
                    self._send_json({"ok": False, "health": "no_credential",
                                     "message": "没有可用凭据，请先在「配置凭据」里获取或粘贴 token"})
                    return
                result = adapter.check_status(
                    acc, credential={"token": cred.token, "fields": cred.fields,
                                     "headers": cred.headers})
                self._send_json({"ok": bool(result.get("ok")),
                                 "health": result.get("health"),
                                 "message": result.get("message"),
                                 "simulated": bool(result.get("simulated"))})
                return
            if path == "/api/schedule":
                payload = self._read_json()
                action = str(payload.get("action", "save"))
                config_path = self.state.ctx["config_path"]
                if action == "save":
                    info = manage.set_schedule(
                        config_path, bool(payload.get("enabled", False)),
                        str(payload.get("time", "10:20")),
                        second_time=str(payload.get("second_time", "")))
                    self._send_json({"ok": True, "schedule": info, "message": "定时配置已保存"})
                elif action == "install":
                    info = manage.set_schedule(
                        config_path, True, str(payload.get("time", "10:20")),
                        second_time=str(payload.get("second_time", "")))
                    result = manage.register_task(self.state.ctx["home"], info["time"],
                                                  second_time=info["second_time"])
                    msg = (f"已注册系统定时任务，每天 {info['time']}"
                           + (f" / {info['second_time']} 各执行一次" if info["second_time"] else " 执行"))
                    self._send_json({"ok": True, "schedule": info, "task": result,
                                     "message": msg})
                elif action == "uninstall":
                    ok = manage.unregister_task()
                    with manage.config_lock(config_path):
                        raw = manage.read_raw(config_path)
                        keep = manage.get_schedule(raw)
                        raw["schedule"] = {"enabled": False,
                                           "time": keep["time"],
                                           "second_time": keep["second_time"],
                                           "task_name": "CreditCheckinDaily"}
                        manage.write_raw(config_path, raw)
                    self._send_json({"ok": ok, "message": "已注销系统定时任务"})
                elif action == "save_general":
                    info = manage.update_general(
                        config_path,
                        timezone=str(payload.get("timezone", "")),
                        window_start=str(payload.get("window_start", "")),
                        window_end=str(payload.get("window_end", "")))
                    self.state.reload_config()
                    self._send_json({"ok": True, "config": info,
                                     "message": "签到窗口与时区已保存"})
                else:
                    self._send_json({"error": "未知操作"}, 400)
                return
            self._send_json({"error": "not found"}, 404)
        except ManageError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.state.ctx["logger"].warning(f"Web API 异常 {path}: {exc}")
            self._send_json({"error": f"请求失败: {type(exc).__name__}"}, 500)


def create_server(ctx: Dict[str, Any], *, host: str = "127.0.0.1", port: int = 8765,
                  allow_local_decrypt: bool = False) -> ThreadingHTTPServer:
    from .vault import CredentialVault
    ctx.setdefault("vault_encrypted", CredentialVault(vault_path()).is_encrypted())
    state = WebState(ctx, allow_local_decrypt=allow_local_decrypt)
    handler = type("BoundWebHandler", (WebHandler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)
