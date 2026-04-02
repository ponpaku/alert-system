from __future__ import annotations

import logging
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


class HttpTransport:
    def __init__(self, config: HttpConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    @property
    def verify(self) -> bool | str:
        if self.config.ca_bundle_path:
            return self.config.ca_bundle_path
        return self.config.verify_tls

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
        response = self.session.request(
            method=method.upper(),
            url=url,
            headers=request_headers,
            json=json_body,
            data=data,
            auth=auth,
            timeout=timeout,
            verify=self.verify,
        )
        return HttpResponse(response.status_code, {key: value for key, value in response.headers.items()}, response.text)

    def _resolve_timeout(self, deadline_monotonic: float | None) -> float:
        timeout = self.config.request_timeout_seconds
        if deadline_monotonic is None:
            return timeout
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceededError("deadline exceeded before request start")
        return max(0.05, min(timeout, remaining))
