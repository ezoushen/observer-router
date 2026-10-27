"""Breakers, quota blocks and group strikes.

A tier that fails repeatedly is skipped briefly instead of spending the whole budget on every
call. A 429 that names its quota and its reset blocks the tier until then rather than re-probing
it; a longer block is never shortened by a later failure. Members of a group are keys for one
model, so a capacity failure that repeats across members blocks the group instead of exhausting
every key in turn.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from observer_router.config import Provider

# A reset further out than this is treated as unreadable rather than trusted: a malformed
# header must not take a tier offline for days.
MAX_QUOTA_BLOCK_SECONDS = 26 * 3600
# Gemini's daily quotas reset at midnight Pacific time.
GEMINI_QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def _next_pacific_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now, GEMINI_QUOTA_TZ)
    midnight = datetime(local.year, local.month, local.day, tzinfo=GEMINI_QUOTA_TZ)
    return (midnight + timedelta(days=1)).timestamp()


def _duration_seconds(text) -> float | None:
    """Parse a protobuf Duration string such as "34s" or "34.94s"."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(text or ""))
    return float(match.group(1)) if match else None


def _gemini_quota(body: str, now: float) -> tuple[float, str] | None:
    """Read QuotaFailure and RetryInfo from a Gemini 429 body.

    RetryInfo's retryDelay is a per-minute hint that Google sends even when a per-day quota
    is the one exhausted, so any PerDay violation wins and blocks until the daily reset.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    details = ((payload or {}).get("error") or {}).get("details") or []
    quota_ids, retry_delay = [], None
    for detail in details:
        kind = str(detail.get("@type", ""))
        if kind.endswith("google.rpc.QuotaFailure"):
            quota_ids += [str(v.get("quotaId", "")) for v in detail.get("violations") or []]
        elif kind.endswith("google.rpc.RetryInfo"):
            retry_delay = _duration_seconds(detail.get("retryDelay"))
    daily = [quota for quota in quota_ids if "PerDay" in quota]
    if daily:
        return _next_pacific_midnight(now), daily[0]
    if retry_delay is not None:
        return now + retry_delay, quota_ids[0] if quota_ids else "retryDelay"
    return None


def _openrouter_quota(headers, body: str) -> tuple[float, str] | None:
    """Read X-RateLimit-Reset (epoch milliseconds) from the headers or the body's copy."""
    reset = headers.get("X-RateLimit-Reset") if headers is not None else None
    source = ""
    try:
        metadata = (json.loads(body).get("error") or {}).get("metadata") or {}
        reset = reset or (metadata.get("headers") or {}).get("X-RateLimit-Reset")
        source = str(metadata.get("limit_source") or "")
    except (ValueError, AttributeError):
        pass
    if reset is None:
        return None
    try:
        return float(reset) / 1000.0, source or "X-RateLimit-Reset"
    except (TypeError, ValueError):
        return None


def quota_reset(kind: str, code: int, headers, body: str,
                now: float | None = None) -> tuple[float, str] | None:
    """Return (reset_epoch, quota_name) when a response states its quota reset.

    `headers` is the response's mapping of headers (or None); `body` its text.
    """
    if code != 429:
        return None
    now = time.time() if now is None else now
    if kind == "gemini":
        found = _gemini_quota(body, now)
    elif kind == "openrouter":
        found = _openrouter_quota(headers, body)
    else:
        found = None
    if not found or not now < found[0] <= now + MAX_QUOTA_BLOCK_SECONDS:
        return None
    return found


class Breakers:
    """Failure counts, breaker skips, quota blocks and group strikes, guarded by one lock.

    `providers` supplies each tier's breaker overrides; `groups` names each group's members.
    `break_after`/`break_seconds` apply where a provider sets no override, and `strikes` is how
    many members must fail in a row with no success between before the group is blocked.
    """

    def __init__(self, groups: Mapping[str, tuple[str, ...]],
                 providers: Mapping[str, Provider], break_after: int, break_seconds: float,
                 strikes: int, log: Callable[[str], None],
                 clock: Callable[[], float] = time.time):
        self._clock = clock
        self._groups = groups
        self._providers = providers
        self._break_after = break_after
        self._break_seconds = break_seconds
        self._strikes = strikes
        self._log = log
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._skip_until: dict[str, float] = {}
        self._skip_reason: dict[str, str] = {}
        self._group_strikes: dict[str, int] = {}

    def _after(self, name: str) -> int:
        provider = self._providers.get(name)
        return (provider.break_after if provider else 0) or self._break_after

    def _seconds(self, name: str) -> float:
        provider = self._providers.get(name)
        return (provider.break_seconds if provider else 0.0) or self._break_seconds

    def _skip(self, name: str, until: float, reason: str) -> bool:
        """Extend a tier's skip; return False when a longer skip already covers it."""
        if until <= self._skip_until.get(name, 0.0):
            return False
        self._skip_until[name] = until
        self._skip_reason[name] = reason
        return True

    def skip_reason(self, name: str, now: float | None = None) -> str | None:
        """Return why the tier is skipped right now, or None when it may run."""
        now = self._clock() if now is None else now
        with self._lock:
            if now >= self._skip_until.get(name, 0.0):
                return None
            return self._skip_reason.get(name) or "breaker-open"

    def available(self, name: str, now: float | None = None) -> bool:
        return self.skip_reason(name, now) is None

    def ok(self, name: str) -> None:
        """Clear a tier's failures and skip, and any group strikes it had accumulated."""
        group = self.group_of(name)
        with self._lock:
            if group:
                self._group_strikes[group] = 0
            self._failures[name] = 0
            self._skip_until[name] = 0.0
            self._skip_reason[name] = ""

    def failed(self, name: str, reason: str) -> None:
        """Count one failure, opening the breaker when the tier's threshold is reached."""
        seconds = self._seconds(name)
        with self._lock:
            self._failures[name] = self._failures.get(name, 0) + 1
            if self._failures[name] >= self._after(name) and self._skip(
                    name, self._clock() + seconds, "breaker-open"):
                self._log(
                    f"tier {name} breaker open for {seconds:.0f}s "
                    f"after {self._failures[name]} failures ({reason})"
                )

    def quota_exhausted(self, name: str, until: float, quota: str) -> None:
        """Skip a tier until the provider's stated quota reset instead of re-probing it."""
        with self._lock:
            self._failures[name] = self._failures.get(name, 0) + 1
            stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(until))
            reason = f"quota {quota} until {stamp}"
            if self._skip(name, until, reason):
                self._log(f"tier {name} {reason} (in {(until - self._clock()) / 3600:.1f}h)")

    def snapshot(self, order: Iterable[str], now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            return {
                name: {
                    "failures": self._failures.get(name, 0),
                    "skipped_for_s": round(max(0.0, self._skip_until.get(name, 0.0) - now), 1),
                    "reason": (self._skip_reason.get(name, "")
                               if self._skip_until.get(name, 0.0) > now else ""),
                }
                for name in order
            }

    def group_of(self, name: str) -> str | None:
        return next((group for group, members in self._groups.items() if name in members), None)

    def strike(self, group: str) -> bool:
        """Count one capacity failure in the group; True when the group should be blocked."""
        with self._lock:
            self._group_strikes[group] = self._group_strikes.get(group, 0) + 1
            if self._group_strikes[group] < self._strikes:
                return False
            self._group_strikes[group] = 0
            return True

    def group_overloaded(self, group: str, reason: str, now: float | None = None) -> None:
        """Skip every member of the group for its own break interval.

        A quota block already in place is kept: extending a skip never shortens one.
        """
        now = self._clock() if now is None else now
        note = f"group {group} overloaded ({reason[:120]})"
        with self._lock:
            blocked = [member for member in self._groups[group]
                       if self._skip(member, now + self._seconds(member), note)]
        if blocked:
            skipped = ",".join(f"{member}({self._seconds(member):.0f}s)" for member in blocked)
            self._log(f"group {group} overloaded: skipping {skipped}")
