"""Trust energy account.

The account answers a different question from a single verdict: not "did
this one run work?" but "how much should we trust this harness right now?".
It keeps a sliding window of recent verdicts and reports the share of
evidential outcomes that ended in PASS.

NOT RUN is the one verdict that carries no information about the harness,
so it occupies a slot in the window without entering the denominator. A
window with no evidential outcome at all reports a balance of 0.0: an
account with no evidence has not earned trust.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict

from .verdict import Verdict

__all__ = ["ConfidenceAccount"]


class ConfidenceAccount:
    """Sliding-window trust balance derived from verdict history."""

    DEFAULT_WINDOW = 100
    HIGH_THRESHOLD = 0.7
    MEDIUM_THRESHOLD = 0.5

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        if isinstance(window, bool) or not isinstance(window, int):
            raise TypeError("window must be an int, got {!r}".format(window))
        if window < 1:
            raise ValueError("window must be >= 1, got {}".format(window))
        self._window: Deque[Verdict] = deque(maxlen=window)

    # -- recording ----------------------------------------------------

    def record(self, verdict: Verdict) -> Verdict:
        """Append one verdict, evicting the oldest when the window is full.

        NOT RUN is stored (it is a real event) but excluded from the
        denominator by ``get_balance``.
        """
        if not isinstance(verdict, Verdict):
            raise TypeError(
                "expected a Verdict, got {!r}".format(type(verdict).__name__)
            )
        self._window.append(verdict)
        return verdict

    def clear(self) -> None:
        """Drop all history."""
        self._window.clear()

    # -- reading ------------------------------------------------------

    @property
    def window_size(self) -> int:
        """Configured capacity of the sliding window."""
        return self._window.maxlen or 0

    @property
    def counts(self) -> Dict[str, int]:
        """Counts of each verdict currently inside the window."""
        counts = {verdict.value: 0 for verdict in Verdict}
        for verdict in self._window:
            counts[verdict.value] += 1
        return counts

    def get_balance(self) -> float:
        """Return PASS / (PASS + FAIL + INVALID) over the window."""
        counts = self.counts
        denominator = (
            counts[Verdict.PASS.value]
            + counts[Verdict.FAIL.value]
            + counts[Verdict.INVALID.value]
        )
        if denominator == 0:
            return 0.0
        return counts[Verdict.PASS.value] / denominator

    def get_label(self) -> str:
        """Bucket the balance into high / medium / low."""
        balance = self.get_balance()
        if balance >= self.HIGH_THRESHOLD:
            return "high"
        if balance >= self.MEDIUM_THRESHOLD:
            return "medium"
        return "low"

    # -- dunder -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._window)

    def __repr__(self) -> str:
        return "ConfidenceAccount(window={}, events={}, balance={:.3f}, " "label={!r})".format(
            self.window_size, len(self), self.get_balance(), self.get_label()
        )
