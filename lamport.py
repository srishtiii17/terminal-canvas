"""Lamport logical clock for ordering protocol v2 events.

Each peer keeps one clock. It ticks before every local DRAW or CLEAR, and
moves past every timestamp it receives, so an edit made after seeing
another peer's edit always carries a larger timestamp than that edit.
Ties between peers are broken by peer ID (see canvas.py).

The clock stops at 2**32 - 1, the largest value the v2 header can carry; a
session would need over four billion edits to get there.
"""

from __future__ import annotations

import threading

MAX_TIME = 2**32 - 1


class LamportClock:
    def __init__(self, start: int = 0) -> None:
        self._time = start
        self._lock = threading.Lock()

    @property
    def time(self) -> int:
        return self._time

    def tick(self) -> int:
        """Advance for a local event and return its timestamp."""
        with self._lock:
            self._time = min(self._time + 1, MAX_TIME)
            return self._time

    def observe(self, received: int) -> int:
        """Move past a received timestamp: time = max(time, received) + 1."""
        with self._lock:
            self._time = min(max(self._time, received) + 1, MAX_TIME)
            return self._time
