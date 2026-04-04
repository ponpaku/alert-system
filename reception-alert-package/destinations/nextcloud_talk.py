from __future__ import annotations

import threading

from config import NextcloudTalkConfig
from models import AlertEvent, DispatchResult
from transport import HttpTransport

from .common import event_text, failure_result_from_exception, failure_result_from_response, preflight_not_attempted_result


class NextcloudTalkDestination:
    def __init__(self, config: NextcloudTalkConfig, transport: HttpTransport):
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
        url = f"{self._config.base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/chat/{self._config.room_token}"
        try:
            response = self._transport.request(
                method="POST",
                url=url,
                event=event,
                headers={"OCS-APIRequest": "true", "Content-Type": "application/json"},
                json_body={"message": event_text(event)},
                auth=(self._config.username, self._config.app_password),
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            return failure_result_from_exception(self.name, exc)
        if response.status_code == 201:
            return DispatchResult.success(self.name, status_code=response.status_code)
        return failure_result_from_response(destination_name=self.name, response=response)
