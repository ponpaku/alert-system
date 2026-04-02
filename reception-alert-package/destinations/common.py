from __future__ import annotations

import logging
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


def failure_result_from_exception(destination_name: str, exc: Exception) -> DispatchResult:
    if isinstance(exc, DeadlineExceededError):
        return DispatchResult.not_attempted(destination_name, error_summary="deadline exceeded before request start")
    retryable = isinstance(exc, requests.RequestException)
    logging.warning("Destination %s failed with exception: %s", destination_name, exc)
    return DispatchResult.failed(
        destination_name,
        retryable=retryable,
        error_summary=str(exc)[:200] or exc.__class__.__name__,
    )
