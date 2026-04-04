from __future__ import annotations

import threading

from config import LineBotConfig
from models import AlertEvent, DispatchResult
from transport import HttpTransport

from .common import event_text, failure_result_from_exception, failure_result_from_response, preflight_not_attempted_result


class LineBotDestination:
    def __init__(self, config: LineBotConfig, transport: HttpTransport):
        self.name = config.name
        self.enabled = config.enabled
        self._config = config
        self._transport = transport

    def send(
        self,
        event: AlertEvent,
        *,
        stop_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> DispatchResult:
        preflight_result = preflight_not_attempted_result(
            self.name,
            stop_event=stop_event,
            deadline_monotonic=deadline_monotonic,
        )
        if preflight_result is not None:
            return preflight_result
        try:
            response = self._transport.request(
                method="POST",
                url="https://api.line.me/v2/bot/message/push",
                event=event,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._config.channel_access_token}",
                },
                json_body={"to": self._config.to, "messages": [{"type": "text", "text": event_text(event)}]},
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            return failure_result_from_exception(self.name, exc)
        if response.status_code == 200:
            return DispatchResult.success(self.name, status_code=response.status_code)
        return failure_result_from_response(destination_name=self.name, response=response)
