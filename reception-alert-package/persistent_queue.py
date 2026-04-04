from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from models import AlertEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QueueFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistedAlert:
    record_id: int
    event: AlertEvent
    target_names: tuple[str, ...]
    attempt_count: int


class PersistentQueue:
    def __init__(
        self,
        path: str,
        *,
        capacity: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        recover_processing_rows: bool = True,
    ):
        self._path = path
        self._capacity = capacity
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._lock = threading.Lock()
        self._closed = False
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._initialize_schema()
        if recover_processing_rows:
            self._recover_processing_rows()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def enqueue(self, event: AlertEvent, target_names: tuple[str, ...]) -> int:
        self._ensure_open()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active_count = self._conn.execute(
                    "SELECT COUNT(*) FROM pending_alerts WHERE status = 'queued'"
                ).fetchone()[0]
                if active_count >= self._capacity:
                    raise QueueFullError("queue capacity reached")
                cursor = self._conn.execute(
                    """
                    INSERT INTO pending_alerts (
                        status,
                        event_json,
                        target_names_json,
                        created_at,
                        next_attempt_at,
                        processing_started_at,
                        attempt_count,
                        last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "queued",
                        _serialize_event(event),
                        json.dumps(list(target_names)),
                        _utcnow().isoformat(),
                        _utcnow().isoformat(),
                        None,
                        0,
                        None,
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                self._conn.rollback()
                raise

    def claim_next_ready(self) -> PersistedAlert | None:
        self._ensure_open()
        now = _utcnow().isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT record_id, event_json, target_names_json, attempt_count
                    FROM pending_alerts
                    WHERE status = 'queued' AND next_attempt_at <= ?
                    ORDER BY next_attempt_at ASC, record_id ASC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                self._conn.execute(
                    """
                    UPDATE pending_alerts
                    SET status = 'processing', processing_started_at = ?
                    WHERE record_id = ? AND status = 'queued'
                    """,
                    (now, row["record_id"]),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return PersistedAlert(
            record_id=int(row["record_id"]),
            event=_deserialize_event(row["event_json"]),
            target_names=tuple(json.loads(row["target_names_json"])),
            attempt_count=int(row["attempt_count"]),
        )

    def complete_success(self, record_id: int) -> None:
        self._ensure_open()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending_alerts WHERE record_id = ?", (record_id,))

    def mark_processed_destination(
        self,
        record_id: int,
        *,
        destination_name: str,
        keep_for_retry: bool,
        error_summary: str | None,
    ) -> tuple[str, ...]:
        self._ensure_open()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT target_names_json FROM pending_alerts WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return ()
            target_names = tuple(json.loads(row["target_names_json"]))
            if keep_for_retry:
                self._conn.execute(
                    "UPDATE pending_alerts SET last_error = ? WHERE record_id = ?",
                    (error_summary, record_id),
                )
                return target_names
            remaining_targets = tuple(name for name in target_names if name != destination_name)
            if not remaining_targets:
                self._conn.execute("DELETE FROM pending_alerts WHERE record_id = ?", (record_id,))
                return ()
            self._conn.execute(
                """
                UPDATE pending_alerts
                SET target_names_json = ?,
                    last_error = ?
                WHERE record_id = ?
                """,
                (json.dumps(list(remaining_targets)), error_summary, record_id),
            )
            return remaining_targets

    def requeue(
        self,
        record_id: int,
        *,
        target_names: tuple[str, ...],
        error_summary: str | None,
        delay_seconds: float,
    ) -> None:
        self._ensure_open()
        next_attempt = _utcnow() + timedelta(seconds=max(0.0, delay_seconds))
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE pending_alerts
                SET status = 'queued',
                    target_names_json = ?,
                    next_attempt_at = ?,
                    processing_started_at = NULL,
                    attempt_count = attempt_count + 1,
                    last_error = ?
                WHERE record_id = ?
                """,
                (json.dumps(list(target_names)), next_attempt.isoformat(), error_summary, record_id),
            )

    def current_targets(self, record_id: int) -> tuple[str, ...]:
        self._ensure_open()
        with self._lock:
            row = self._conn.execute(
                "SELECT target_names_json FROM pending_alerts WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return ()
            return tuple(json.loads(row["target_names_json"]))

    def compute_retry_delay_seconds(self, next_attempt_count: int) -> float:
        if self._retry_max_seconds <= 0:
            return 0.0
        if self._retry_base_seconds <= 0:
            return 0.0
        exponent = max(0, next_attempt_count - 1)
        delay = self._retry_base_seconds * (2**min(exponent, 16))
        return min(delay, self._retry_max_seconds)

    def pending_count(self) -> int:
        self._ensure_open()
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM pending_alerts WHERE status IN ('queued', 'processing')"
                ).fetchone()[0]
            )

    def _initialize_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_alerts (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    target_names_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    processing_started_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS pending_alerts_ready_idx
                ON pending_alerts (status, next_attempt_at, record_id)
                """
            )

    def _recover_processing_rows(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE pending_alerts
                SET status = 'queued',
                    processing_started_at = NULL,
                    next_attempt_at = created_at
                WHERE status = 'processing'
                """
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("persistent queue is closed")


def _serialize_event(event: AlertEvent) -> str:
    payload = {
        "event_id": str(event.event_id),
        "button_name": event.button_name,
        "kind": event.kind,
        "prefix": event.prefix,
        "message": event.message,
        "location_name": event.location_name,
        "occurred_at": event.occurred_at.isoformat(),
        "source": event.source,
    }
    return json.dumps(payload, separators=(",", ":"))


def _deserialize_event(raw: str) -> AlertEvent:
    payload = json.loads(raw)
    return AlertEvent(
        event_id=UUID(payload["event_id"]),
        button_name=payload["button_name"],
        kind=payload["kind"],
        prefix=payload["prefix"],
        message=payload["message"],
        location_name=payload["location_name"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        source=payload.get("source", "reception-alert"),
    )
