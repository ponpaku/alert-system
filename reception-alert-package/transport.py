from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from config import HttpConfig
from models import AlertEvent


class DeadlineExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    text: str
    truncated: bool = False


class HttpTransport:
    def __init__(self, config: HttpConfig, session: requests.Session | None = None, *, owns_session: bool | None = None):
        self.config = config
        self._shared_session = session
        self._owns_shared_session = False if owns_session is None else owns_session
        self._shared_session_lock = threading.Lock()
        self._thread_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._owned_sessions: list[requests.Session] = []

    @property
    def verify(self) -> bool | str:
        if self.config.ca_bundle_path:
            return self.config.ca_bundle_path
        return self.config.verify_tls

    def close(self) -> None:
        if self._shared_session is not None:
            if self._owns_shared_session:
                self._shared_session.close()
            return
        with self._sessions_lock:
            for session in self._owned_sessions:
                session.close()
            self._owned_sessions.clear()

    def request(
        self,
        *,
        method: str,
        url: str,
        event: AlertEvent,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        auth: Any = None,
        deadline_monotonic: float | None = None,
    ) -> HttpResponse:
        timeout = self._resolve_timeout(deadline_monotonic)
        request_headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            "X-Reception-Alert-Event-Id": str(event.event_id),
        }
        if headers:
            request_headers.update(headers)
        logging.info("HTTP %s %s event_id=%s timeout=%.3f", method.upper(), url, event.event_id, timeout)
        session = self._session()
        if self._shared_session is not None:
            with self._shared_session_lock:
                response = session.request(
                    method=method.upper(),
                    url=url,
                    headers=request_headers,
                    json=json_body,
                    data=data,
                    auth=auth,
                    timeout=timeout,
                    verify=self.verify,
                    stream=True,
                )
        else:
            response = session.request(
                method=method.upper(),
                url=url,
                headers=request_headers,
                json=json_body,
                data=data,
                auth=auth,
                timeout=timeout,
                verify=self.verify,
                stream=True,
            )
        try:
            text, truncated = self._read_limited_text(response)
            return HttpResponse(response.status_code, {key: value for key, value in response.headers.items()}, text, truncated)
        finally:
            response.close()

    def _resolve_timeout(self, deadline_monotonic: float | None) -> float:
        timeout = self.config.request_timeout_seconds
        if deadline_monotonic is None:
            return timeout
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceededError("deadline exceeded before request start")
        return max(0.05, min(timeout, remaining))

    def _read_limited_text(self, response: requests.Response) -> tuple[str, bool]:
        limit = self.config.response_body_limit_bytes
        if limit <= 0:
            return "", False
        chunks: list[bytes] = []
        total = 0
        truncated = False
        chunk_size = min(4096, max(256, limit))
        for chunk in response.iter_content(chunk_size=chunk_size, decode_unicode=False):
            if not chunk:
                continue
            remaining = limit - total
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total += remaining
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        encoding = response.encoding or "utf-8"
        text = b"".join(chunks).decode(encoding, errors="replace")
        return text, truncated

    def _session(self) -> requests.Session:
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._owned_sessions.append(session)
        return session
