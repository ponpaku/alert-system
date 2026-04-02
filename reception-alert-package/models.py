from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from message_constants import LOCATION_LABEL


DispatchOutcome = Literal["success", "failed", "not_attempted"]
DispatchSummary = Literal["success", "warning", "failure"]


@dataclass(frozen=True)
class AlertEvent:
    event_id: UUID
    button_name: str
    kind: str
    prefix: str
    message: str
    location_name: str
    occurred_at: datetime
    source: str = "reception-alert"

    def as_template_context(self) -> dict[str, str]:
        return {
            "event_id": str(self.event_id),
            "button_name": self.button_name,
            "prefix": self.prefix,
            "message": self.message,
            "location_name": self.location_name,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "text": render_event_text(self),
        }


@dataclass(frozen=True)
class DispatchResult:
    destination_name: str
    outcome: DispatchOutcome
    attempted: bool
    ok: bool
    status_code: int | None
    retryable: bool
    retry_after_seconds: float | None
    error_summary: str | None

    @classmethod
    def success(cls, destination_name: str, status_code: int | None = None) -> "DispatchResult":
        return cls(destination_name, "success", True, True, status_code, False, None, None)

    @classmethod
    def failed(
        cls,
        destination_name: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        error_summary: str | None = None,
    ) -> "DispatchResult":
        return cls(destination_name, "failed", True, False, status_code, retryable, retry_after_seconds, error_summary)

    @classmethod
    def not_attempted(
        cls,
        destination_name: str,
        *,
        error_summary: str | None = None,
    ) -> "DispatchResult":
        return cls(destination_name, "not_attempted", False, False, None, False, None, error_summary)


def build_alert_event(
    *,
    button_name: str,
    kind: str,
    prefix: str,
    message: str,
    location_name: str,
) -> AlertEvent:
    return AlertEvent(
        event_id=uuid4(),
        button_name=button_name,
        kind=kind,
        prefix=prefix,
        message=message,
        location_name=location_name,
        occurred_at=datetime.now(timezone.utc),
    )


def render_event_text(event: AlertEvent) -> str:
    prefix = event.prefix.strip()
    lead = f"{prefix} " if prefix else ""
    return f"{lead}{event.message}\n{LOCATION_LABEL}{event.location_name}"


def summarize_dispatch_results(results: list[DispatchResult]) -> DispatchSummary:
    if any(result.outcome == "failed" for result in results):
        return "failure"
    if any(result.outcome == "not_attempted" for result in results):
        return "warning"
    return "success"
