"""Repeated requests.

A client that sends a request it already got an answer to did not take that answer: a validator
on its side refused it, and it is asking again. The same chain would hand the identical request to
the same model, which tends to repeat its mistake, so the router remembers for a while which
chain entries answered each request and tries the others first.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict


def request_key(body: dict) -> str:
    """A digest of the request as the client sent it; field order does not matter."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class Repeats:
    """For each recent request, the chain entries that answered it and when they last did."""

    def __init__(self, window: float, limit: int = 1024):
        self._window = window
        self._limit = limit
        self._answers: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, key: str, entry: str, now: float | None = None) -> None:
        if self._window <= 0:
            return
        now = time.time() if now is None else now
        with self._lock:
            answers = self._answers.pop(key, {})
            answers.pop(entry, None)
            answers[entry] = now
            self._answers[key] = answers  # most recently answered request last
            while len(self._answers) > self._limit:
                self._answers.popitem(last=False)

    def answered(self, key: str, now: float | None = None) -> list[str]:
        """Entries that answered this request inside the window, least recent first."""
        if self._window <= 0:
            return []
        now = time.time() if now is None else now
        with self._lock:
            answers = self._answers.get(key, {})
            return [entry for entry, at in answers.items() if now - at <= self._window]
