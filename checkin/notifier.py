"""通知层：控制台 / 日志 / 可选 Webhook。"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional

from .utils import sanitize


class Notifier:
    def __init__(self, cfg: Dict[str, Any], logger=None):
        self.cfg = cfg or {}
        self.channels = list(self.cfg.get("channels") or ["console", "log"])
        self.webhook = str(self.cfg.get("webhook") or "")
        self.logger = logger

    def notify(self, kind: str, title: str, message: str,
               data: Optional[Dict[str, Any]] = None, *, force: bool = False) -> None:
        """kind: info | success | failure | drift。failure/drift 受配置开关约束。"""
        if kind in ("failure", "drift") and not force:
            key = "on_failure" if kind == "failure" else "on_drift"
            if not self.cfg.get(key, True):
                return

        line = f"[{kind.upper()}] {title}: {message}"
        if "console" in self.channels:
            print(line)
        if self.logger:
            level = "error" if kind in ("failure", "drift") else "info"
            getattr(self.logger, level)(f"{title}: {message}")
        if self.webhook and "webhook" in self.channels:
            self._webhook(kind, title, message, data)

    def _webhook(self, kind: str, title: str, message: str, data: Optional[Dict[str, Any]]) -> None:
        payload = json.dumps({
            "kind": kind,
            "title": title,
            "message": message[:1000],
            "data": sanitize(data or {}),
        }, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.webhook, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:  # 通知失败不应影响签到主流程
            if self.logger:
                self.logger.warning(f"Webhook 通知失败: {exc}")
