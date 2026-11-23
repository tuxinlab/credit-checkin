"""执行器：重试、退避、状态落库、漂移记录与通知。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from datetime import date
from typing import Any, Dict, List, Optional

from .adapters import ContractDriftError
from .adapters.registry import AdapterRegistry
from .credentials import resolve as resolve_credential
from .models import Account, AttemptLog, DailyTask, DriftEvent, RunRecord, RunStatus, TERMINAL_STATUSES
from .notifier import Notifier
from .storage import Storage
from .utils import RunBusy, RunLock, compact_json, now_local, sanitize


class Executor:
    def __init__(self, cfg: Dict[str, Any], storage: Storage, registry: AdapterRegistry,
                 notifier: Optional[Notifier] = None, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.storage = storage
        self.registry = registry
        self.notifier = notifier or Notifier(cfg.get("notify") or {}, logger)
        self.logger = logger or logging.getLogger("credit-checkin")
        self.accounts: Dict[str, Account] = {a.id: a for a in cfg.get("accounts", [])}
        self.max_attempts = int(cfg.get("retry", {}).get("max_attempts", 3))
        self.backoffs = list(cfg.get("retry", {}).get("backoff_seconds", [60, 300, 900]))
        self.tz = cfg.get("timezone", "Asia/Shanghai")
        from .refresher import TokenRefresher
        self.refresher = TokenRefresher(http_fn=getattr(registry, "http_fn", None))

    def run_tasks(self, tasks: List[DailyTask], *, simulate: bool = False,
                  force: bool = False, allow_local_decrypt: bool = False) -> List[RunRecord]:
        if not tasks:
            return []
        # 跨进程签到锁：计划任务与面板手动执行同时触发时，只允许一个在跑
        lock = RunLock(getattr(self.storage, "db_path", "data/checkin.db").parent / "run.lock")
        ok, holder = lock.acquire()
        if not ok:
            self.logger.warning(f"已有签到进程在运行（{holder}），本次跳过")
            raise RunBusy(holder)
        try:
            records = []
            for i, task in enumerate(tasks):
                if i > 0:
                    time.sleep(1)
                records.append(self.run_task(task, simulate=simulate, force=force,
                                             allow_local_decrypt=allow_local_decrypt))
            self._notify_summary(records, simulate=simulate)
            return records
        finally:
            lock.release()

    def _notify_summary(self, records: List[RunRecord], *, simulate: bool = False) -> None:
        """一轮执行完推一条汇总（info 级别，不受失败/漂移开关限制）。"""
        if not records or simulate:
            return
        ok = sum(1 for r in records if r.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value))
        skipped = sum(1 for r in records if r.status == RunStatus.SKIPPED.value)
        failed = sum(1 for r in records if r.status in (RunStatus.FAILED.value, RunStatus.DRIFT.value))
        credits = sum(r.credits or 0 for r in records)
        self.notifier.notify(
            "info", "签到汇总",
            f"本次领取 {sum(1 for r in records if r.status == RunStatus.SUCCESS.value)} · "
            f"今日已领 {sum(1 for r in records if r.status == RunStatus.ALREADY.value)} · "
            f"跳过 {skipped} · 失败 {failed} · 积分 {credits}",
            data={"records": [{"account": r.account_id, "status": r.status,
                               "credits": r.credits} for r in records]})

    def run_task(self, task: DailyTask, *, simulate: bool = False,
                 force: bool = False,
                 allow_local_decrypt: bool = False) -> RunRecord:
        # 去重：已完成的任务默认不重复执行
        existing = self.storage.get_run(task.key)
        if existing and existing.status in TERMINAL_STATUSES and not force:
            return existing
        if existing and existing.status == RunStatus.RUNNING.value and not force:
            return existing

        account = self.accounts.get(task.account_id)
        if not account:
            # 该账号不在配置里，库里也不应留记录；直接返回 SKIPPED
            rec = RunRecord.from_task(task)
            rec.status = RunStatus.SKIPPED.value
            rec.message = "配置中找不到该账号"
            return rec

        record = RunRecord.from_task(task)
        # 模拟运行绝不写入真实数据库：否则会占用去重位，导致之后"立即签到"显示没有待执行
        persist = not simulate
        finish = self._finish if persist else self._memory_finish
        # 必须在 upsert_start 之前取旧记录：重跑会把状态重置为 running
        previous = self.storage.get_run(task.key) if persist else None
        if persist:
            self.storage.upsert_start(record)

        def finish_guarded(status: str, attempts: int, **kwargs):
            """已有成功/已签记录时，不让后续的「跳过/失败」把它覆盖掉（保住已到账积分）。"""
            if (previous
                    and previous.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value)
                    and status in (RunStatus.SKIPPED.value, RunStatus.FAILED.value)):
                self.logger.info(
                    f"{task.key} 保留已有成功记录（本次结果 {status} 未覆盖）")
                # upsert_start 已把库里状态改成 running，这里必须写回原结果，
                # 否则会留下永远 running 的僵死记录。
                detail_obj: Any = None
                if previous.detail:
                    try:
                        detail_obj = json.loads(previous.detail)
                    except (TypeError, ValueError):
                        detail_obj = None
                restored = self.storage.finish(
                    task.key, previous.status, attempts=previous.attempts,
                    credits=previous.credits, message=previous.message,
                    detail=detail_obj)
                return restored or previous
            return finish(task, status, attempts, **kwargs)

        # 追补的历史日期无法补领过期积分：直接登记跳过，不产生请求。
        # 否则会把「今天」的额度错领并记到过去日期名下（统计错位）。
        if task.target_date < now_local(self.tz).date():
            self.logger.info(f"{task.key} 日期已过期，登记跳过（不补领过期积分）")
            return finish_guarded(
                RunStatus.SKIPPED.value, 0,
                message="该日期已过期，平台不允许补领（仅登记，不产生请求）",
                detail={"expired_date": task.target_date.isoformat()})

        adapter = self.registry.get(task.platform, task.version)
        if adapter is None:
            return finish(task, RunStatus.DRIFT.value, 0,
                          message=f"缺少契约 {task.platform}.{task.version}",
                          detail={"contract": f"{task.platform}.{task.version}"},
                          drift=True)

        # 解析凭据（只注入内存，不落日志）；失败只作为 skipped 记录，不视为接口漂移
        cred = resolve_credential(account, allow_local_decrypt=allow_local_decrypt)
        if not cred.token and not simulate:
            return finish_guarded(RunStatus.SKIPPED.value, 0,
                                  message=f"凭据不可用：{cred.note or '未设置环境变量'}",
                                  detail={"credential_source": cred.source})

        cred_dict = {"token": cred.token, "fields": cred.fields, "headers": cred.headers}

        last_status = RunStatus.FAILED.value
        last_message = "未知错误"
        last_detail: Any = {}
        refreshed = False  # 每次运行最多续期一次，避免死循环

        for attempt in range(1, self.max_attempts + 1):
            try:
                outcome = adapter.checkin(account, simulate=simulate,
                                          target_date=task.target_date,
                                          credential=cred_dict)
                last_status = outcome.status
                last_message = outcome.message
                last_detail = {
                    "response": outcome.response,
                    "steps": outcome.steps,
                    "simulated": outcome.simulated,
                }
                if persist:
                    self.storage.log_attempt(AttemptLog(
                        task.key, attempt, now_local(self.tz).isoformat(timespec="seconds"),
                        outcome.status, outcome.message[:500], compact_json(last_detail),
                    ))

                if outcome.status in (RunStatus.SUCCESS.value, RunStatus.ALREADY.value,
                                      RunStatus.SKIPPED.value):
                    do_finish = (finish_guarded if outcome.status == RunStatus.SKIPPED.value
                                 else (lambda st, at, **kw: finish(task, st, at, **kw)))
                    rec = do_finish(outcome.status, attempt,
                                    message=outcome.message,
                                    credits=outcome.credits,
                                    detail=last_detail)
                    if outcome.status == RunStatus.SUCCESS.value:
                        self.notifier.notify("success", "签到成功",
                                             f"{task.key}: {outcome.message}"
                                             + ("（模拟）" if outcome.simulated else ""))
                    return rec

                if outcome.status == RunStatus.DRIFT.value:
                    return finish(task, RunStatus.DRIFT.value, attempt,
                                        message=outcome.message, detail=last_detail, drift=True)

                # 鉴权类确定性失败：先尝试自动续期（每轮一次），成功则用新凭据重试
                if (outcome.status == RunStatus.FAILED.value
                        and outcome.retryable is False
                        and outcome.auth_failure
                        and not refreshed and not simulate
                        and attempt < self.max_attempts):
                    refreshed = True
                    ok, note = self.refresher.refresh(account, cred)
                    if ok:
                        self.logger.info(f"{task.key} token 已自动续期，用新凭据重试: {note}")
                        new_token = getattr(self.refresher, "last_token", None)
                        if new_token:
                            # 直接用续期返回的新 token 替换内存凭据。不要重新 resolve()：
                            # 当凭据来自环境变量时，环境变量优先级高于保险库，
                            # 重新解析会拿回旧的过期 token，导致本次续期白做。
                            cred.token = new_token
                            cred.source = "refreshed"
                            cred_dict["token"] = new_token
                        else:
                            # 兼容自定义 refresher（如测试替身）未暴露 last_token 的情况
                            cred = resolve_credential(account, allow_local_decrypt=allow_local_decrypt)
                            cred_dict = {"token": cred.token, "fields": cred.fields,
                                         "headers": cred.headers}
                        continue
                    self.logger.warning(f"{task.key} 自动续期失败: {note}；请重新配置凭据")

                # 确定性失败（如凭据失效）：重试无意义，立即收尾并提示用户处理
                if outcome.status == RunStatus.FAILED.value and not outcome.retryable:
                    rec = finish_guarded(RunStatus.FAILED.value, attempt,
                                         message=outcome.message, detail=last_detail)
                    self.notifier.notify("failure", "签到失败（不可重试）",
                                         f"{task.key}: {outcome.message[:300]}",
                                         data={"key": task.key})
                    return rec

            except ContractDriftError as exc:
                last_status = RunStatus.DRIFT.value
                last_message = exc.reason
                last_detail = {"sample": sanitize(exc.sample)}
                if persist:
                    self.storage.log_attempt(AttemptLog(
                        task.key, attempt, now_local(self.tz).isoformat(timespec="seconds"),
                        RunStatus.DRIFT.value, exc.reason[:500], compact_json(last_detail),
                    ))
                return finish(task, RunStatus.DRIFT.value, attempt,
                                    message=exc.reason, detail=last_detail, drift=True)
            except Exception as exc:  # 网络/实现异常：进入重试
                last_status = RunStatus.FAILED.value
                last_message = f"{type(exc).__name__}: {exc}"
                last_detail = {"error": str(exc)[:500]}
                if persist:
                    self.storage.log_attempt(AttemptLog(
                        task.key, attempt, now_local(self.tz).isoformat(timespec="seconds"),
                        RunStatus.FAILED.value, last_message[:500], compact_json(last_detail),
                    ))
                self.logger.warning(f"签到异常 {task.key} 第 {attempt} 次: {last_message}")

            if attempt < self.max_attempts:
                delay = self.backoffs[min(attempt - 1, len(self.backoffs) - 1)] if self.backoffs else 0
                if delay:
                    time.sleep(delay)

        self.notifier.notify("failure", "签到失败",
                             f"{task.key}: {last_message[:300]}",
                             data={"key": task.key, "detail": last_detail})
        return finish_guarded(last_status, self.max_attempts,
                              message=last_message, detail=last_detail)

    def _finish(self, task: DailyTask, status: str, attempts: int, *,
                message: str = "", credits: Optional[int] = None,
                detail: Any = None, drift: bool = False) -> RunRecord:
        rec = self.storage.finish(task.key, status, attempts=attempts,
                                  credits=credits, message=message[:1000],
                                  detail=sanitize(detail or {}))
        if drift:
            contract_version = ""
            c = self.registry.contracts.get((task.platform, task.version)) or {}
            contract_version = str(c.get("contract_version", ""))
            self.storage.add_drift(DriftEvent(
                platform=task.platform, version=task.version,
                contract_version=contract_version, reason=message[:1000],
                sample=compact_json(detail or {}, limit=2000),
                detected_at=now_local(self.tz).isoformat(timespec="seconds"),
            ))
            self.notifier.notify("drift", "契约漂移",
                                 f"{task.key}: {message[:300]}",
                                 data={"key": task.key, "contract_version": contract_version})
        return rec or RunRecord.from_task(task)

    def _memory_finish(self, task: DailyTask, status: str, attempts: int, *,
                       message: str = "", credits: Optional[int] = None,
                       detail: Any = None, drift: bool = False) -> RunRecord:
        """模拟模式：只构造运行结果，不写数据库、不写漂移、不推通知。"""
        rec = RunRecord.from_task(task)
        rec.status = status
        rec.attempts = attempts
        rec.credits = credits
        # 明确标注「模拟」，避免把假积分/假状态当成真实结果
        rec.message = (f"【模拟】{message}")[:1000]
        rec.finished_at = now_local(self.tz).isoformat(timespec="seconds")
        return rec
