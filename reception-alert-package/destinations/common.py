from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from models import AlertEvent, DispatchResult, render_event_text
from transport import DeadlineExceededError, HttpResponse


def retry_after_seconds(response: HttpResponse) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, dt.timestamp() - datetime.now(timezone.utc).timestamp())


def event_text(event: AlertEvent) -> str:
    return render_event_text(event)


def failure_result_from_response(
    *,
    destination_name: str,
    response: HttpResponse,
    retryable_status_codes: set[int] | None = None,
) -> DispatchResult:
    retryable_status_codes = retryable_status_codes or set()
    body = response.text.strip().replace("\n", " ")
    error_summary = f"HTTP {response.status_code}"
    if body:
        error_summary = f"{error_summary} {body[:200]}"
        if response.truncated:
            error_summary = f"{error_summary} [truncated]"
    retryable = (
        response.status_code == 429
        or 500 <= response.status_code <= 599
        or response.status_code in retryable_status_codes
    )
    return DispatchResult.failed(
        destination_name,
        status_code=response.status_code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds(response),
        error_summary=error_summary,
    )


def not_attempted_for_stop(destination_name: str) -> DispatchResult:
    return DispatchResult.not_attempted(destination_name, error_summary="stopped before request start")


def not_attempted_for_deadline(destination_name: str) -> DispatchResult:
    return DispatchResult.not_attempted(destination_name, error_summary="deadline exceeded before request start")


def preflight_not_attempted_result(
    destination_name: str,
    *,
    stop_event: threading.Event | None,
    deadline_monotonic: float | None,
) -> DispatchResult | None:
    if stop_event is not None and stop_event.is_set():
        return not_attempted_for_stop(destination_name)
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        return not_attempted_for_deadline(destination_name)
    return None


def failure_result_from_exception(destination_name: str, exc: Exception) -> DispatchResult:
    if isinstance(exc, DeadlineExceededError):
        return not_attempted_for_deadline(destination_name)
    retryable = isinstance(exc, requests.RequestException)
    logging.warning("Destination %s failed with exception: %s", destination_name, exc)
    return DispatchResult.failed(
        destination_name,
        retryable=retryable,
        error_summary=str(exc)[:200] or exc.__class__.__name__,
    )
