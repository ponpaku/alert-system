from __future__ import annotations

import threading
from typing import Protocol

from models import AlertEvent, DispatchResult


class Destination(Protocol):
    """Alert dispatch destination contract.

    Hard contract:
    - return promptly with ``not_attempted`` when ``stop_event`` is already set
    - return promptly with ``not_attempted`` when ``deadline_monotonic`` is already expired
    - pass ``deadline_monotonic`` through to any blocking transport call so request timeout
      enforcement remains aligned with dispatcher deadlines

    Guidance:
    - keep non-transport work small and deterministic
    - avoid introducing new blocking operations outside helpers that already honor
      ``stop_event`` / ``deadline_monotonic``
    """

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
