"""The AC-power probe and its cache.

The probe shells out to `pmset`, so the answer is cached to keep it off the hot path. Any
failure (pmset absent, hung, or output this module cannot read) answers "unknown" rather than
guessing: an unreadable power state must not take the observer offline, so "unknown" keeps an
`ac_only` provider available.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence


def probe_power() -> str:
    """Return "AC Power", "Battery Power", or "unknown" from this machine's `pmset`."""
    source = "unknown"
    try:
        completed = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode == 0:
            headline = (completed.stdout or "").splitlines()[:1]
            if headline and "AC Power" in headline[0]:
                source = "AC Power"
            elif headline and "Battery Power" in headline[0]:
                source = "Battery Power"
    except Exception:  # an unreadable power state is not an outage
        source = "unknown"
    return source


def _no_log(message: str) -> None:
    return


class PowerGate:
    """The cached power source, and the decision an `ac_only` provider rests on.

    `probe` returns one of the three sentences above; `cache_seconds` bounds how often it is
    called. `log` receives a line when the answer changes.
    """

    def __init__(self, probe: Callable[[], str] = probe_power, cache_seconds: float = 30.0,
                 log: Callable[[str], None] = _no_log):
        self._probe = probe
        self._cache_seconds = cache_seconds
        self._log = log
        self._lock = threading.Lock()
        self._checked_at = 0.0
        self._source = "unknown"

    def source(self, now: float | None = None) -> str:
        """Return the current power source, re-probing only when the cache has expired."""
        now = time.time() if now is None else now
        with self._lock:
            if now - self._checked_at < self._cache_seconds:
                return self._source
            try:
                answer = self._probe()
            except Exception:  # a probe that raises is unreadable, not an outage
                answer = "unknown"
            source = answer if answer in ("AC Power", "Battery Power") else "unknown"
            if source != self._source:
                self._log(f"power source {self._source} -> {source}")
            self._checked_at = now
            self._source = source
            return source

    def snapshot(self, ac_only: Sequence[str], now: float | None = None) -> dict:
        """Describe the gate for `/health`: `ac_only` names the providers it holds back."""
        source = self.source(now)
        return {
            "source": source,
            "ac_only": list(ac_only),
            "ac_only_allowed": source != "Battery Power",
        }
