"""SQLite 存储层：状态、历史、尝试日志、契约漂移事件。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import AttemptLog, DriftEvent, RunRecord, RunStatus


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Storage:
    """轻量线程安全存储。所有写操作加锁，SQLite 使用 WAL。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    version TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    credits INTEGER,
                    is_catchup INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS attempt_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempt_key ON attempt_logs(key)")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS drift_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    version TEXT NOT NULL,
                    contract_version TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    sample TEXT NOT NULL DEFAULT '',
                    detected_at TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
            """)

    # ---------- runs ----------
    def get_run(self, key: str) -> Optional[RunRecord]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM runs WHERE key=?", (key,)).fetchone()
            return self._row_to_run(row) if row else None

    def upsert_start(self, record: RunRecord) -> None:
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT INTO runs (key, platform, version, account_id, target_date, status,
                                  attempts, credits, is_catchup, started_at, finished_at, message, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status,
                    attempts=excluded.attempts,
                    is_catchup=excluded.is_catchup,
                    started_at=excluded.started_at,
                    finished_at=NULL,
                    message=excluded.message,
                    detail=excluded.detail
            """, (
                record.key, record.platform, record.version, record.account_id,
                record.target_date, RunStatus.RUNNING.value, record.attempts,
                record.credits, int(record.is_catchup), record.started_at or _now(),
                "", "", "",
            ))

    def finish(self, key: str, status: str, *, attempts: int, credits: Optional[int] = None,
               message: str = "", detail: Any = None) -> Optional[RunRecord]:
        detail_s = json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else ""
        with self._lock, self.conn:
            cur = self.conn.execute("""
                UPDATE runs SET status=?, attempts=?, credits=?, finished_at=?,
                                message=?, detail=?
                WHERE key=?
            """, (status, attempts, credits, _now(), message or "", detail_s, key))
            if cur.rowcount == 0:
                return None
        return self.get_run(key)

    def log_attempt(self, entry: AttemptLog) -> None:
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT INTO attempt_logs (key, attempt, at, status, message, detail)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (entry.key, entry.attempt, entry.at or _now(), entry.status,
                  entry.message or "", entry.detail or ""))

    def attempts_for(self, key: str) -> List[AttemptLog]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempt_logs WHERE key=? ORDER BY attempt ASC", (key,)
            ).fetchall()
            return [AttemptLog(r["key"], r["attempt"], r["at"], r["status"],
                               r["message"], r["detail"]) for r in rows]

    def runs_for_date(self, target_date: str) -> List[RunRecord]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE target_date=? ORDER BY platform, version, account_id",
                (target_date,),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def first_run_date(self, platform: str, version: str, account_id: str) -> Optional[str]:
        """该账号最早一次执行的目标日期；无记录返回 None（新账号不做历史补签）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT MIN(target_date) FROM runs WHERE platform=? AND version=? AND account_id=?",
                (platform, version, account_id),
            ).fetchone()
            return row[0] if row and row[0] else None

    def prune(self, keep_days: int) -> Dict[str, int]:
        """清理超过 keep_days 天的历史数据；keep_days<=0 表示不清理。

        runs / attempt_logs / drift_events 一起清，返回各类删除条数。
        """
        if keep_days is None or int(keep_days) <= 0:
            return {"runs": 0, "attempt_logs": 0, "drift_events": 0}
        keep_days = int(keep_days)
        cutoff = (datetime.now() - timedelta(days=keep_days)).date().isoformat()
        with self._lock, self.conn:
            keys = [r[0] for r in self.conn.execute(
                "SELECT key FROM runs WHERE target_date < ?", (cutoff,))]
            n_runs = self.conn.execute(
                "DELETE FROM runs WHERE target_date < ?", (cutoff,)).rowcount
            n_logs = 0
            for k in keys:
                n_logs += self.conn.execute(
                    "DELETE FROM attempt_logs WHERE key = ?", (k,)).rowcount
            n_drifts = self.conn.execute(
                "DELETE FROM drift_events WHERE detected_at < ?", (cutoff,)).rowcount
        return {"runs": n_runs, "attempt_logs": n_logs, "drift_events": n_drifts}

    def history(self, limit: int = 50, *, platform: Optional[str] = None,
                account_id: Optional[str] = None, status: Optional[str] = None,
                since: Optional[str] = None) -> List[RunRecord]:
        clauses, params = [], []
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if since:
            clauses.append("finished_at>=?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM runs{where} ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def failed_count(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status IN (?, ?)",
                (RunStatus.FAILED.value, RunStatus.DRIFT.value),
            ).fetchone()["n"]

    def drift_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) AS n FROM drift_events").fetchone()["n"]

    # ---------- drift ----------
    def add_drift(self, event: DriftEvent) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute("""
                INSERT INTO drift_events (platform, version, contract_version, reason, sample, detected_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event.platform, event.version, event.contract_version,
                  event.reason, event.sample, event.detected_at or _now()))
            return int(cur.lastrowid)

    def drifts(self, limit: int = 50) -> List[DriftEvent]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM drift_events ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [DriftEvent(r["platform"], r["version"], r["contract_version"],
                               r["reason"], r["sample"], r["detected_at"], r["id"])
                    for r in rows]

    # ---------- meta ----------
    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v)
            )

    def get_meta(self, k: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else default

    # ---------- helpers ----------
    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            key=row["key"], platform=row["platform"], version=row["version"],
            account_id=row["account_id"], target_date=row["target_date"],
            status=row["status"], attempts=row["attempts"], credits=row["credits"],
            is_catchup=bool(row["is_catchup"]), started_at=row["started_at"] or "",
            finished_at=row["finished_at"] or "", message=row["message"] or "",
            detail=row["detail"] or "",
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
