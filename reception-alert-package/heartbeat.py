from __future__ import annotations

import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from config import HeartbeatConfig, HttpConfig
from transport import HttpTransport


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HeartbeatState:
    queue_depth: int | None
    worker_alive: bool | None
    worker_fatal: bool


@dataclass(frozen=True)
class _TransportEventRef:
    event_id: UUID


class HeartbeatSender:
    def __init__(
        self,
        config: HeartbeatConfig,
        http_config: HttpConfig,
        *,
        location_name: str,
        state_supplier: Callable[[], HeartbeatState],
        started_at: datetime,
        service_name: str = "reception-alert",
        transport: HttpTransport | None = None,
    ):
        self._config = config
        self._location_name = location_name
        self._state_supplier = state_supplier
        self._started_at = started_at
        self._service_name = service_name
        self._instance_id = config.instance_id or socket.gethostname()
        heartbeat_http_config = replace(http_config, request_timeout_seconds=config.timeout_seconds)
        self._transport = transport or HttpTransport(heartbeat_http_config)
        self._owns_transport = transport is None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False

    def start(self) -> None:
        if not self._config.enabled or self._thread is not None:
            return
        if self._config.send_on_startup:
            self.send_once("startup")
        self._thread = threading.Thread(target=self._run, name="heartbeat-worker", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._join_timeout_seconds())
        if self._config.enabled and self._config.send_on_shutdown:
            self.send_once("shutdown")
        if self._owns_transport:
            self._transport.close()

    def send_once(self, event_type: str) -> bool:
        if not self._config.enabled:
            return False
        state = self._safe_state()
        event_id = uuid4()
        headers = {
            "Content-Type": "application/json",
        }
        if self._config.shared_secret:
            headers["X-Reception-Alert-Heartbeat-Secret"] = self._config.shared_secret
        payload = self._build_payload(event_id=event_id, event_type=event_type, state=state)
        try:
            response = self._transport.request(
                method=self._config.method,
                url=self._config.url,
                event=_TransportEventRef(event_id=event_id),
                headers=headers,
                json_body=payload,
            )
        except Exception:
            logging.exception("Heartbeat send failed event=%s", event_type)
            return False
        if 200 <= response.status_code < 300:
            logging.info("Heartbeat delivered event=%s status=%s", event_type, response.status_code)
            return True
        logging.warning(
            "Heartbeat returned unexpected status event=%s status=%s body=%s",
            event_type,
            response.status_code,
            response.text,
        )
        return False

    def _run(self) -> None:
        delay_seconds = self._next_delay_seconds(success=True)
        while not self._stop_event.wait(delay_seconds):
            success = self.send_once("heartbeat")
            delay_seconds = self._next_delay_seconds(success=success)

    def _next_delay_seconds(self, *, success: bool) -> float:
        base_seconds = self._config.interval_seconds if success else min(
            self._config.interval_seconds,
            self._config.failure_backoff_seconds,
        )
        jitter_seconds = random.uniform(0.0, self._config.jitter_seconds) if self._config.jitter_seconds > 0 else 0.0
        return max(0.05, base_seconds + jitter_seconds)

    def _build_payload(self, *, event_id: UUID, event_type: str, state: HeartbeatState) -> dict[str, Any]:
        sent_at = _utcnow()
        uptime_seconds = max(0, int((sent_at - self._started_at).total_seconds()))
        payload: dict[str, Any] = {
            "event_id": str(event_id),
            "event": event_type,
            "status": "stopping" if event_type == "shutdown" else "alive",
            "service": self._service_name,
            "source": self._service_name,
            "instance_id": self._instance_id,
            "location_name": self._location_name,
            "sent_at": sent_at.isoformat(),
            "started_at": self._started_at.isoformat(),
            "uptime_seconds": uptime_seconds,
            "stale_after_seconds": self._config.stale_after_seconds,
        }
        if self._config.include_worker_alive:
            payload["worker_alive"] = state.worker_alive
            payload["worker_fatal"] = state.worker_fatal
        if self._config.include_queue_depth:
            payload["queue_depth"] = state.queue_depth
        return payload

    def _safe_state(self) -> HeartbeatState:
        try:
            supplied = self._state_supplier()
        except Exception:
            logging.exception("Heartbeat state supplier failed")
            return HeartbeatState(queue_depth=None, worker_alive=None, worker_fatal=True)
        if isinstance(supplied, HeartbeatState):
            return supplied
        return HeartbeatState(
            queue_depth=getattr(supplied, "queue_depth", None),
            worker_alive=getattr(supplied, "worker_alive", None),
            worker_fatal=bool(getattr(supplied, "worker_fatal", False)),
        )

    def _join_timeout_seconds(self) -> float:
        return max(self._config.timeout_seconds, self._config.interval_seconds) + self._config.jitter_seconds + 1.0
