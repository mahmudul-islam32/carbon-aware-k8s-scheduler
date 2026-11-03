"""Release staggering (F-26).

When many Jobs pick the same green window they would all be released in the
same reconcile tick and hit the cluster at once. ``ReleaseLimiter`` caps how
many *planned* releases happen per rolling minute. Releases that are close to
a Job's latest feasible start always bypass the limiter, so staggering can
never cause a missed deadline.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta


class ReleaseLimiter:
    """Sliding-window limiter for planned releases.

    Args:
        max_per_minute: Maximum releases granted in any 60-second window.
            ``0`` disables the limiter.
    """

    WINDOW = timedelta(minutes=1)

    def __init__(self, max_per_minute: int = 0) -> None:
        self.max_per_minute = max(0, int(max_per_minute))
        self._granted: deque[datetime] = deque()
        self._lock = threading.Lock()
        self.deferred_total = 0

    @property
    def enabled(self) -> bool:
        """Whether staggering is active."""
        return self.max_per_minute > 0

    def _prune(self, now: datetime) -> None:
        while self._granted and now - self._granted[0] >= self.WINDOW:
            self._granted.popleft()

    def try_acquire(self, now: datetime, critical: bool = False) -> bool:
        """Ask for permission to release one Job now.

        Args:
            now: Current time.
            critical: The Job is close to its latest start; always granted.

        Returns:
            True if the Job may be released now.
        """
        with self._lock:
            self._prune(now)
            if critical or not self.enabled or len(self._granted) < self.max_per_minute:
                self._granted.append(now)
                return True
            self.deferred_total += 1
            return False

    def in_flight(self, now: datetime) -> int:
        """Releases granted in the current window."""
        with self._lock:
            self._prune(now)
            return len(self._granted)


def is_deadline_critical(
    now: datetime,
    latest_start: datetime | None,
    reconcile_interval: timedelta,
) -> bool:
    """A Job is critical when the next reconcile tick could be past its latest start."""
    if latest_start is None:
        return False
    return now >= latest_start - 2 * reconcile_interval
