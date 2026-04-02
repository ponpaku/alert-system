from __future__ import annotations

import threading
from typing import Any

from config import GenericWebhookAuthConfig, GenericWebhookConfig
from models import AlertEvent, DispatchResult
from transport import HttpTransport

from .common import failure_result_from_exception, failure_result_from_response, not_attempted_for_stop


class GenericWebhookDestination:
    def __init__(self, config: GenericWebhookConfig, transport: HttpTransport):
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
        headers = dict(self._config.headers)
        if self._config.content_type == "json":
            headers.setdefault("Content-Type", "application/json")
            json_body = render_template_value(self._config.payload, event)
            data = None
        elif self._config.content_type == "form":
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            raw_form = self._config.payload or {}
            json_body = None
            data = {str(key): stringify_template_value(value, event) for key, value in raw_form.items()}
        else:
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            json_body = None
            data = stringify_template_value(self._config.payload or "{{ text }}", event)
        auth = apply_auth_config(self._config.auth, headers)
        try:
            response = self._transport.request(
                method=self._config.method,
                url=self._config.url,
                event=event,
                headers=headers,
                json_body=json_body,
                data=data,
                auth=auth,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            return failure_result_from_exception(self.name, exc)
        success_codes = set(self._config.success_status_codes or range(200, 300))
        if response.status_code in success_codes:
            return DispatchResult.success(self.name, status_code=response.status_code)
        return failure_result_from_response(destination_name=self.name, response=response)


def render_template_value(value: Any, event: AlertEvent) -> Any:
    context = event.as_template_context()
    if isinstance(value, dict):
        return {str(key): render_template_value(inner, event) for key, inner in value.items()}
    if isinstance(value, list):
        return [render_template_value(item, event) for item in value]
    if isinstance(value, str):
        return render_template_string(value, context)
    return value


def stringify_template_value(value: Any, event: AlertEvent) -> str:
    rendered = render_template_value(value, event)
    return "" if rendered is None else str(rendered)


def render_template_string(template: str, context: dict[str, str]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{ {key} }}}}", value)
    return rendered


def apply_auth_config(auth: GenericWebhookAuthConfig, headers: dict[str, str]) -> tuple[str, str] | None:
    if auth.type == "none":
        return None
    if auth.type == "bearer":
        headers["Authorization"] = f"Bearer {auth.token or ''}"
        return None
    if auth.type == "basic":
        return (auth.username or "", auth.password or "")
    if auth.type == "header":
        if auth.header_name:
            headers[auth.header_name] = auth.header_value or ""
        return None
    return None
