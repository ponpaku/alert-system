from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading

from config import NextcloudBotConfig
from models import AlertEvent, DispatchResult
from transport import HttpTransport

from .common import event_text, failure_result_from_exception, failure_result_from_response, not_attempted_for_stop


class NextcloudBotDestination:
    def __init__(self, config: NextcloudBotConfig, transport: HttpTransport):
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
        if stop_event is not None and stop_event.is_set():
            return not_attempted_for_stop(self.name)
        payload = {"message": event_text(event), "silent": self._config.silent}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        random_value = secrets.token_hex(32)
        signature = hmac.new(
            self._config.shared_secret.encode("utf-8"),
            f"{random_value}{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self._config.base_url.rstrip('/')}/ocs/v2.php/apps/spreed/api/v1/bot/{self._config.conversation_token}/message"
        try:
            response = self._transport.request(
                method="POST",
                url=url,
                event=event,
                headers={
                    "OCS-APIRequest": "true",
                    "Content-Type": "application/json",
                    "X-Nextcloud-Talk-Bot-Random": random_value,
                    "X-Nextcloud-Talk-Bot-Signature": signature,
                },
                data=body.encode("utf-8"),
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            return failure_result_from_exception(self.name, exc)
        if response.status_code == 201:
            return DispatchResult.success(self.name, status_code=response.status_code)
        return failure_result_from_response(destination_name=self.name, response=response, retryable_status_codes={429})
