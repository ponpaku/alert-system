from __future__ import annotations

import threading
from typing import Protocol

from models import AlertEvent, DispatchResult


class Destination(Protocol):
    name: str
    enabled: bool

    def send(
        self,
        event: AlertEvent,
        *,
        stop_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> DispatchResult:
        ...
