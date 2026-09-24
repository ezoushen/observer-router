#!/usr/bin/env python3
"""Observer router for claude-mem: Gemini Flash -> OpenRouter Free -> [Cursor] -> local lane.

claude-mem speaks OpenAI-compatible HTTP and resolves exactly one provider, so the
fallback chain lives here instead. This process exposes /v1/chat/completions and
/v1/models on 127.0.0.1:1244 and rewrites every request for whichever tier runs.

Why it exists (2026-09-18): the local observer lane (:1243) hit a Metal GPU OOM,
its generation thread died while the process kept answering /v1/models, and claude-mem
timed out on every call for ~2 hours. A healthy remote first hop plus a per-tier
budget removes that single point of failure.

Tier rules, measured rather than assumed:
  * openrouter/free rotates across free variants; roughly half of plain calls return
    EMPTY content because a reasoning model spends the whole max_tokens budget on
    reasoning. We inject {"reasoning":{"enabled":false}} and retry once with
    {"reasoning":{"exclude":true}} when an endpoint answers 400 "Reasoning is
    mandatory for this endpoint and cannot be disabled."
  * A tier that yields no content counts as a failed attempt and the next tier runs,
    so an observer call never returns an empty result.
  * Both free tiers hit daily quotas and say when they reset: Gemini names the quota in
    google.rpc.QuotaFailure (daily quotas reset at midnight Pacific), OpenRouter sends
    X-RateLimit-Reset. Such a 429 skips the tier until the reset rather than re-probing
    it every breaker interval.
  * The optional cursor tier is a local cursor-api-proxy bridge in front of Cursor's
    subscription models (Composer by default). It is remote compute, so it runs on
    battery and needs no lane lock; it is opt-in through OBSERVER_ROUTER_CHAIN because
    the bridge is a separate install.

Streaming contract: claude-mem sends stream=true. Response headers are only sent once
the first content-bearing delta arrives, which keeps failover possible until that
point. Reasoning deltas are dropped; only content is forwarded downstream.

Prompt contract: requests are sanitized once before tier selection. Image payloads and
large base64 blobs are replaced with markers, individual fields are folded, and older
messages are dropped only when the remaining conversation exceeds the configured character
budget. System messages and the newest conversation context receive priority. This bounds
KV allocation without depending on another model call during an outage.

Idle rule: with OBSERVER_LOCAL_IDLE_METRICS set, the local tier also waits until the listed
interactive lanes have been idle for OBSERVER_LOCAL_IDLE_SECONDS; see the idle gate section.

Power rule: with OBSERVER_LOCAL_AC_ONLY enabled (the default) the local tier is skipped
while the machine runs on battery, so an unplugged laptop does not start GPU inference as
a last resort. claude-mem answers a failed chain with a provider quota cooldown and retries
the same queued batch, so a skipped tier delays observations instead of dropping them. An
unreadable power state leaves the tier available: a failed probe must not take the observer
offline. Desktops report AC Power permanently, making this gate inert there.

Configuration is read from claude-mem's settings.json (re-read whenever it changes, so
keys edited in the claude-mem console are picked up without a restart), with env
overrides for every value.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

SETTINGS_PATH = os.environ.get(
    "CLAUDE_MEM_SETTINGS", os.path.expanduser("~/.claude-mem/settings.json")
)
HOST = os.environ.get("OBSERVER_ROUTER_HOST", "127.0.0.1")
LOG_PREFIX = "[observer-router]"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CURSOR_URL = os.environ.get(
    "OBSERVER_CURSOR_URL", "http://127.0.0.1:8765/v1/chat/completions"
)
LOCAL_URL = os.environ.get(
    "OBSERVER_LOCAL_URL", "http://127.0.0.1:1240/v1/chat/completions"
)

GEMINI_MODEL = os.environ.get("OBSERVER_GEMINI_MODEL", "gemini-flash-lite-latest")
OPENROUTER_MODEL = os.environ.get("OBSERVER_OPENROUTER_MODEL", "openrouter/free")
CURSOR_MODEL = os.environ.get("OBSERVER_CURSOR_MODEL", "composer-2.5")
# The bridge key lives in the bridge's own env file; reading it from there keeps one
# copy of the secret, so rotating it in the bridge cannot silently break this tier.
CURSOR_API_KEY = os.environ.get("OBSERVER_CURSOR_API_KEY", "").strip()
CURSOR_ENV_FILE = os.path.expanduser(os.environ.get("OBSERVER_CURSOR_ENV_FILE", "").strip())
# The local tier's model id must match what the local server advertises at /v1/models: a
# mismatch is a 404, which silently leaves the chain without a backstop. Machine-specific
# ids therefore belong in the deployment (the launchd plist here), not in this default.
LOCAL_MODEL = os.environ.get("OBSERVER_LOCAL_MODEL", "local-model")
# Empty string disables the parameter for lanes that reject it (mlx_lm rejects
# unknown fields). "none" is what Splash accepts to switch thinking off.
LOCAL_REASONING_EFFORT = os.environ.get("OBSERVER_LOCAL_REASONING_EFFORT", "none").strip()

# The local tier is one GPU lane. ThreadingHTTPServer will happily run two
# upstream calls at once, and two concurrent prefills on the same lane both
# slow down and inflate its KV pool -- the pool has to be sized for the
# concurrency, not for the request. claude-mem already sets
# CLAUDE_MEM_MAX_CONCURRENT_AGENTS=1, but nothing here enforced it, so any
# second caller on :1244 would have broken that assumption silently.
# Only the local tier is serialised; gemini and openrouter are remote and
# hold no GPU.
LOCAL_SERIAL = os.environ.get("OBSERVER_LOCAL_SERIAL", "1").strip() not in ("", "0", "false")
_LOCAL_LANE_LOCK = threading.Lock()


class _NullGuard:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def local_lane_guard(tier: str):
    """Hold the lane for the duration of a local-tier call."""
    if tier == "local" and LOCAL_SERIAL:
        return _LOCAL_LANE_LOCK
    return _NullGuard()

# Per-tier budget for reaching the first content delta. The default chain's sum stays
# under claude-mem's CLAUDE_MEM_API_TIMEOUT_MS (120s) with headroom for queuing; adding
# the cursor tier (measured 14-27s per observer call) needs that timeout raised.
BUDGETS = {"gemini": 18.0, "openrouter": 24.0, "cursor": 40.0, "local": 70.0}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _now() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


PORT = _env_int("OBSERVER_ROUTER_PORT", 1244)

# Circuit breaker: a tier that fails repeatedly is skipped briefly instead of
# consuming the budget on every call (free tiers rate-limit aggressively).
BREAK_AFTER = _env_int("OBSERVER_BREAK_AFTER", 3)
BREAK_SECONDS = _env_float("OBSERVER_BREAK_SECONDS", 60.0)

# The lane's native context is 262,144 tokens, but accepting that many tokens would leave
# no memory headroom for concurrent GPU clients. Character budgets are conservative token
# estimates; the router folds deterministically rather than recursively asking an LLM to
# summarize an already-oversized request.
MAX_PROMPT_CHARS = _env_int("OBSERVER_MAX_PROMPT_CHARS", 240_000)
MAX_FIELD_CHARS = _env_int("OBSERVER_MAX_FIELD_CHARS", 80_000)
MIN_RETAINED_CHARS = _env_int("OBSERVER_MIN_RETAINED_CHARS", 1_024)
BASE64_BLOB_CHARS = _env_int("OBSERVER_BASE64_BLOB_CHARS", 16_384)
MAX_ERROR_BODY_BYTES = 16_384

CHAIN = tuple(
    tier for tier in os.environ.get("OBSERVER_ROUTER_CHAIN", "gemini,openrouter,local").split(",")
    if tier
)


def log(message: str) -> None:
    print(f"{LOG_PREFIX} [{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# --- power source -------------------------------------------------------------

LOCAL_AC_ONLY = os.environ.get("OBSERVER_LOCAL_AC_ONLY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
POWER_CACHE_SECONDS = _env_float("OBSERVER_POWER_CACHE_SECONDS", 30.0)

_power_lock = threading.Lock()
_power_cache = {"checked_at": 0.0, "source": "unknown"}


def power_source() -> str:
    """Return "AC Power", "Battery Power", or "unknown", cached briefly.

    The probe shells out to pmset, so the answer is cached for POWER_CACHE_SECONDS to keep
    it off the hot path. Any failure (pmset absent, hung, or output the router cannot read)
    returns "unknown" rather than guessing.
    """
    with _power_lock:
        now = time.time()
        if now - _power_cache["checked_at"] < POWER_CACHE_SECONDS:
            return _power_cache["source"]
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
        except Exception:  # unreadable power state must not take the observer offline
            source = "unknown"
        if source != _power_cache["source"]:
            log(f"power source {_power_cache['source']} -> {source}")
        _power_cache["checked_at"] = now
        _power_cache["source"] = source
        return source


# --- idle gate ----------------------------------------------------------------

# The local tier shares the GPU with interactive lanes on the same machine. With
# OBSERVER_LOCAL_IDLE_METRICS naming those lanes' Prometheus /metrics URLs, the local tier
# runs only after every one of them has been idle for OBSERVER_LOCAL_IDLE_SECONDS, so an
# observer prefill never competes with a turn the user is waiting on. The grace period
# matters because an agent loop pauses between requests while its tools run.
#
# A background sampler, not a per-request probe, tracks activity: a request that starts and
# finishes between two observer calls still moves the submitted counter, but only a sampler
# can say when. An unreadable lane leaves its last reading in place and ages into idle, so a
# stopped or broken lane cannot keep the observer offline.
LOCAL_IDLE_METRICS = tuple(
    url.strip() for url in os.environ.get("OBSERVER_LOCAL_IDLE_METRICS", "").split(",")
    if url.strip()
)
LOCAL_IDLE_SECONDS = _env_float("OBSERVER_LOCAL_IDLE_SECONDS", 30.0)
IDLE_SAMPLE_SECONDS = _env_float("OBSERVER_IDLE_SAMPLE_SECONDS", 2.0)
_IN_FLIGHT_COUNTERS = {
    "splash_requests_submitted_total": 1,
    "splash_requests_completed_total": -1,
    "splash_requests_cancelled_total": -1,
    "splash_requests_failed_total": -1,
}

_idle_lock = threading.Lock()
# url -> {"submitted": last submitted count, "busy_at": last time activity was seen}
_lane_activity: dict[str, dict] = {}


def parse_lane_metrics(text: str) -> tuple[int, int] | None:
    """Return (submitted, in_flight) from a Splash /metrics body, or None when unreadable."""
    values = {}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name in _IN_FLIGHT_COUNTERS:
            try:
                values[name] = int(float(value))
            except ValueError:
                return None
    if len(values) != len(_IN_FLIGHT_COUNTERS):
        return None
    in_flight = sum(values[name] * sign for name, sign in _IN_FLIGHT_COUNTERS.items())
    return values["splash_requests_submitted_total"], in_flight


def record_lane_sample(url: str, sample: tuple[int, int] | None, now: float) -> None:
    """Fold one reading into the lane's activity: any new or in-flight request is activity."""
    if sample is None:
        return
    submitted, in_flight = sample
    with _idle_lock:
        state = _lane_activity.setdefault(url, {"submitted": submitted, "busy_at": 0.0})
        if in_flight > 0 or submitted != state["submitted"]:
            state["busy_at"] = now
        state["submitted"] = submitted


def _read_lane(url: str) -> tuple[int, int] | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=1.0)
    try:
        connection.request("GET", parts.path or "/metrics")
        response = connection.getresponse()
        if response.status != 200:
            return None
        return parse_lane_metrics(response.read().decode("utf-8", "ignore"))
    except Exception:  # an unreachable lane is not activity
        return None
    finally:
        connection.close()


def _sample_lanes_forever() -> None:
    while True:
        for url in LOCAL_IDLE_METRICS:
            record_lane_sample(url, _read_lane(url), time.time())
        time.sleep(IDLE_SAMPLE_SECONDS)


def busy_lanes(now: float | None = None) -> list[str]:
    """Return the watched lanes active within the last LOCAL_IDLE_SECONDS."""
    now = time.time() if now is None else now
    with _idle_lock:
        return [
            url for url in LOCAL_IDLE_METRICS
            if now - _lane_activity.get(url, {}).get("busy_at", 0.0) < LOCAL_IDLE_SECONDS
        ]


def local_busy_reason() -> str | None:
    busy = busy_lanes()
    if not busy:
        return None
    ports = ",".join(urllib.parse.urlsplit(url).netloc for url in busy)
    return f"skipped: lanes busy ({ports}, idle {LOCAL_IDLE_SECONDS:.0f}s required)"


def idle_snapshot() -> dict:
    now = time.time()
    with _idle_lock:
        lanes = {
            url: round(now - _lane_activity[url]["busy_at"], 1)
            if url in _lane_activity and _lane_activity[url]["busy_at"] else None
            for url in LOCAL_IDLE_METRICS
        }
    return {
        "idle_seconds_required": LOCAL_IDLE_SECONDS,
        "seconds_since_active": lanes,
        "local_tier_allowed": not busy_lanes(now),
    }


def allowed_tiers() -> tuple[list[str], dict[str, str]]:
    """Split CHAIN into the tiers worth trying and the ones skipped, with a reason each.

    Skipped tiers are reported in the failed-chain response so claude-mem's cooldown log
    explains why the local lane never ran.
    """
    allowed: list[str] = []
    skipped: dict[str, str] = {}
    on_battery = LOCAL_AC_ONLY and power_source() == "Battery Power"
    for name in CHAIN:
        if name == "local" and on_battery:
            skipped[name] = "skipped: on battery (AC-only)"
            continue
        busy = local_busy_reason() if name == "local" else None
        if busy:
            skipped[name] = busy
            continue
        allowed.append(name)
    return allowed, skipped


def power_snapshot() -> dict:
    """Describe the power gate for /health, reusing the cached probe."""
    source = power_source()
    return {
        "source": source,
        "local_ac_only": LOCAL_AC_ONLY,
        "local_tier_allowed": not (LOCAL_AC_ONLY and source == "Battery Power"),
    }


# --- settings -----------------------------------------------------------------

_settings_lock = threading.Lock()
_settings_cache = {"mtime": None, "data": {}}


def settings() -> dict:
    """Return claude-mem settings, reloading when the file changes on disk."""
    with _settings_lock:
        try:
            mtime = os.stat(SETTINGS_PATH).st_mtime
        except OSError:
            return _settings_cache["data"]
        if mtime != _settings_cache["mtime"]:
            try:
                with open(SETTINGS_PATH) as handle:
                    _settings_cache["data"] = json.load(handle)
                _settings_cache["mtime"] = mtime
            except Exception as error:
                log(f"settings reload failed: {type(error).__name__}: {error}")
        return _settings_cache["data"]


# --- prompt safety ------------------------------------------------------------

_DATA_URL = re.compile(r"data:image/[^;,\s]+;base64,[^\s\"']+", re.IGNORECASE)
_BASE64_BLOB = re.compile(
    rf"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{{{BASE64_BLOB_CHARS},}}={{0,2}}"
    rf"(?![A-Za-z0-9+/=])"
)
_compaction_lock = threading.Lock()
_compaction_totals = {
    "compacted_requests": 0,
    "input_chars": 0,
    "output_chars": 0,
    "dropped_messages": 0,
    "removed_image_parts": 0,
    "removed_blobs": 0,
}


def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_content_chars(item) for item in content)
    if isinstance(content, dict):
        return sum(len(str(key)) + _content_chars(value) for key, value in content.items())
    return len(str(content)) if content is not None else 0


def _message_chars(messages: list[dict]) -> int:
    return sum(_content_chars(message.get("content")) for message in messages)


def _fold_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    marker = "\n[... content folded by observer-router ...]\n"
    if len(marker) >= limit:
        return marker[:limit]
    available = limit - len(marker)
    head = max(1, available * 3 // 5)
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _clean_text(text: str, stats: dict) -> str:
    def omit_data_url(match: re.Match) -> str:
        stats["removed_blobs"] += 1
        return f"[image binary payload omitted: {len(match.group(0)):,} chars]"

    def omit_blob(match: re.Match) -> str:
        stats["removed_blobs"] += 1
        return f"[binary payload omitted: {len(match.group(0)):,} chars]"

    cleaned = _DATA_URL.sub(omit_data_url, text)
    cleaned = _BASE64_BLOB.sub(omit_blob, cleaned)
    if len(cleaned) > MAX_FIELD_CHARS:
        stats["folded_fields"] += 1
        cleaned = _fold_text(cleaned, MAX_FIELD_CHARS)
    return cleaned


def _clean_content(content, stats: dict) -> str:
    if isinstance(content, str):
        return _clean_text(content, stats)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {
                "image",
                "image_url",
                "input_image",
            }:
                stats["removed_image_parts"] += 1
                parts.append("[image omitted from observer prompt]")
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                parts.append(_clean_text(str(item.get("text") or ""), stats))
            else:
                parts.append(_clean_text(json.dumps(item, ensure_ascii=False), stats))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if content.get("type") in {"image", "image_url", "input_image"}:
            stats["removed_image_parts"] += 1
            return "[image omitted from observer prompt]"
        return _clean_text(json.dumps(content, ensure_ascii=False), stats)
    return _clean_text(str(content), stats) if content is not None else ""


def compact_messages(messages: list[dict]) -> tuple[list[dict], dict]:
    """Return a bounded conversation while preserving system and newest context."""
    stats = {
        "changed": False,
        "input_chars": _message_chars(messages),
        "output_chars": 0,
        "dropped_messages": 0,
        "folded_fields": 0,
        "removed_image_parts": 0,
        "removed_blobs": 0,
    }
    sanitized = []
    for message in messages:
        if not isinstance(message, dict):
            stats["dropped_messages"] += 1
            continue
        cleaned = dict(message)
        cleaned["content"] = _clean_content(message.get("content"), stats)
        sanitized.append(cleaned)

    if _message_chars(sanitized) <= MAX_PROMPT_CHARS:
        stats["output_chars"] = _message_chars(sanitized)
        stats["changed"] = sanitized != messages
        return sanitized, stats

    system_indexes = [
        index for index, message in enumerate(sanitized) if message.get("role") == "system"
    ]
    recent_indexes = [
        index for index in reversed(range(len(sanitized))) if index not in system_indexes
    ]
    # Give the primary system contract and newest non-system message first claim on the
    # budget; processing every system message first could otherwise discard the request
    # that the observer is meant to answer.
    priority = system_indexes[:1] + recent_indexes[:1] + system_indexes[1:] + recent_indexes[1:]
    selected = {}
    remaining = MAX_PROMPT_CHARS
    for index in priority:
        message = dict(sanitized[index])
        size = _content_chars(message.get("content"))
        if size <= remaining:
            selected[index] = message
            remaining -= size
        elif remaining >= MIN_RETAINED_CHARS:
            message["content"] = _fold_text(str(message.get("content") or ""), remaining)
            selected[index] = message
            stats["folded_fields"] += 1
            remaining = 0

    compacted = [selected[index] for index in sorted(selected)]
    stats["dropped_messages"] += len(sanitized) - len(selected)
    stats["output_chars"] = _message_chars(compacted)
    stats["changed"] = True
    return compacted, stats


def record_compaction(stats: dict) -> None:
    if not stats["changed"]:
        return
    with _compaction_lock:
        _compaction_totals["compacted_requests"] += 1
        for key in (
            "input_chars",
            "output_chars",
            "dropped_messages",
            "removed_image_parts",
            "removed_blobs",
        ):
            _compaction_totals[key] += stats[key]


def compaction_snapshot() -> dict:
    with _compaction_lock:
        return {
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "max_field_chars": MAX_FIELD_CHARS,
            **_compaction_totals,
        }


# --- circuit breaker ----------------------------------------------------------

_breaker_lock = threading.Lock()
_failures = dict.fromkeys(CHAIN, 0)
_skip_until = dict.fromkeys(CHAIN, 0.0)
# Why a tier is skipped: "breaker-open", or the quota the provider says is exhausted.
_skip_reason = dict.fromkeys(CHAIN, "")


def tier_skip_reason(name: str) -> str | None:
    """Return why the tier is skipped right now, or None when it may run."""
    with _breaker_lock:
        if time.time() >= _skip_until.get(name, 0.0):
            return None
        return _skip_reason.get(name) or "breaker-open"


def tier_available(name: str) -> bool:
    return tier_skip_reason(name) is None


def tier_ok(name: str) -> None:
    with _breaker_lock:
        _failures[name] = 0
        _skip_until[name] = 0.0
        _skip_reason[name] = ""


def _skip(name: str, until: float, reason: str) -> bool:
    """Extend a tier's skip; return False when a longer skip already covers it.

    A request in flight when a quota block opened can still fail afterwards, and its 60 s
    breaker must not cut the block short.
    """
    if until <= _skip_until.get(name, 0.0):
        return False
    _skip_until[name] = until
    _skip_reason[name] = reason
    return True


def tier_failed(name: str, reason: str) -> None:
    with _breaker_lock:
        _failures[name] = _failures.get(name, 0) + 1
        if _failures[name] >= BREAK_AFTER and _skip(
            name, time.time() + BREAK_SECONDS, "breaker-open"
        ):
            log(
                f"tier {name} breaker open for {BREAK_SECONDS:.0f}s "
                f"after {_failures[name]} failures ({reason})"
            )


def tier_quota_exhausted(name: str, until: float, quota: str) -> None:
    """Skip a tier until the provider's stated quota reset instead of re-probing it."""
    with _breaker_lock:
        _failures[name] = _failures.get(name, 0) + 1
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(until))
        reason = f"quota {quota} until {stamp}"
        if _skip(name, until, reason):
            log(f"tier {name} {reason} (in {(until - time.time()) / 3600:.1f}h)")


def breaker_snapshot() -> dict:
    with _breaker_lock:
        now = time.time()
        return {
            name: {
                "failures": _failures.get(name, 0),
                "skipped_for_s": round(max(0.0, _skip_until.get(name, 0.0) - now), 1),
                "reason": _skip_reason.get(name, "") if _skip_until.get(name, 0.0) > now else "",
            }
            for name in CHAIN
        }


# --- provider quota signals -----------------------------------------------------

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
    try:
        return float(reset) / 1000.0, source or "X-RateLimit-Reset"
    except (TypeError, ValueError):
        return None


def quota_reset(tier: str, error: HTTPError, now: float | None = None) -> tuple[float, str] | None:
    """Return (reset_epoch, quota_name) when a 429 states when the quota comes back."""
    if error.code != 429:
        return None
    now = time.time() if now is None else now
    body = _error_body(error)
    if tier == "gemini":
        found = _gemini_quota(body, now)
    elif tier == "openrouter":
        found = _openrouter_quota(error.headers, body)
    else:
        found = None
    if not found or not now < found[0] <= now + MAX_QUOTA_BLOCK_SECONDS:
        return None
    return found


# --- tier requests ------------------------------------------------------------

class TierFailure(Exception):
    """Raised when a tier cannot produce usable content.

    retry_at/quota are set when the provider said which quota ran out and when it resets.
    """

    def __init__(self, message: str, retry_at: float | None = None, quota: str = ""):
        super().__init__(message)
        self.retry_at = retry_at
        self.quota = quota


_TIER_URLS = {
    "gemini": GEMINI_URL,
    "openrouter": OPENROUTER_URL,
    "cursor": CURSOR_URL,
    "local": LOCAL_URL,
}
_TIER_MODELS = {
    "gemini": GEMINI_MODEL,
    "openrouter": OPENROUTER_MODEL,
    "cursor": CURSOR_MODEL,
    "local": LOCAL_MODEL,
}


def _payload_for(tier: str, body: dict, reasoning_mode: str | None) -> dict:
    if tier not in _TIER_MODELS:
        raise TierFailure(f"unknown tier {tier}")
    payload = {
        "model": _TIER_MODELS[tier],
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "max_tokens", "top_p", "stop"):
        if body.get(key) is not None:
            payload[key] = body[key]
    if tier == "local" and LOCAL_REASONING_EFFORT:
        # Splash honours reasoning_effort and ignores chat_template_kwargs entirely.
        # Without this a reasoning model spends the whole max_tokens budget thinking
        # and returns content=None with finish_reason=length, which the router then
        # reports as "empty content" and the chain loses its backstop.
        payload["reasoning_effort"] = LOCAL_REASONING_EFFORT
    if tier == "openrouter" and reasoning_mode == "disabled":
        # Without this, reasoning models burn the whole budget on reasoning and
        # return empty content.
        payload["reasoning"] = {"enabled": False}
    elif tier == "openrouter" and reasoning_mode == "exclude":
        # Some endpoints reject enabled=false with HTTP 400; keep reasoning on but
        # hide it from the response.
        payload["reasoning"] = {"exclude": True}
    return payload


def cursor_api_key() -> str:
    """Return OBSERVER_CURSOR_API_KEY, else CURSOR_BRIDGE_API_KEY from OBSERVER_CURSOR_ENV_FILE."""
    if CURSOR_API_KEY:
        return CURSOR_API_KEY
    if not CURSOR_ENV_FILE:
        return ""
    try:
        with open(CURSOR_ENV_FILE) as handle:
            for line in handle:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == "CURSOR_BRIDGE_API_KEY":
                    return value.strip().strip("\"'")
    except OSError as error:
        log(f"cursor: cannot read OBSERVER_CURSOR_ENV_FILE: {type(error).__name__}")
    return ""


def _headers_for(tier: str) -> dict:
    config = settings()
    headers = {"Content-Type": "application/json"}
    if tier == "gemini":
        key = (config.get("CLAUDE_MEM_GEMINI_API_KEY") or "").strip()
        if not key:
            raise TierFailure("gemini: no CLAUDE_MEM_GEMINI_API_KEY configured")
        headers["Authorization"] = f"Bearer {key}"
    elif tier == "openrouter":
        key = (config.get("CLAUDE_MEM_OPENROUTER_API_KEY") or "").strip()
        if not key:
            raise TierFailure("openrouter: no CLAUDE_MEM_OPENROUTER_API_KEY configured")
        headers["Authorization"] = f"Bearer {key}"
        site = (config.get("CLAUDE_MEM_OPENROUTER_SITE_URL") or "").strip()
        app = (config.get("CLAUDE_MEM_OPENROUTER_APP_NAME") or "claude-mem").strip()
        if site:
            headers["HTTP-Referer"] = site
        headers["X-Title"] = app
    elif tier == "cursor":
        key = cursor_api_key()
        if not key:
            raise TierFailure(
                "cursor: no OBSERVER_CURSOR_API_KEY or CURSOR_BRIDGE_API_KEY in "
                "OBSERVER_CURSOR_ENV_FILE"
            )
        headers["Authorization"] = f"Bearer {key}"
    return headers


# Upstream traffic goes through http.client directly: only the HTTPS and HTTP
# constructors are ever used, so file:/custom schemes cannot resolve here.


class UpstreamResponse:
    """File-like wrapper that closes both the response and its connection."""

    def __init__(self, connection, response):
        self._connection = connection
        self._response = response

    def read(self, size: int | None = None):
        if size is None:
            return self._response.read()
        return self._response.read(size)

    def __iter__(self):
        return iter(self._response)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _connect(tier: str, timeout: float):
    """Open an http(s) connection for a tier, rejecting any other scheme."""
    parts = urllib.parse.urlsplit(_TIER_URLS[tier])
    host = parts.hostname
    if not host:
        raise TierFailure(f"{tier}: URL has no host")
    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout)
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    raise TierFailure(f"{tier}: refusing non-http(s) URL")


def _open(tier: str, body: dict, timeout: float, reasoning_mode: str | None):
    if tier not in _TIER_URLS:
        raise TierFailure(f"unknown tier {tier}")
    parts = urllib.parse.urlsplit(_TIER_URLS[tier])
    payload = json.dumps(_payload_for(tier, body, reasoning_mode)).encode()
    headers = _headers_for(tier)
    headers["Content-Length"] = str(len(payload))
    connection = _connect(tier, timeout)
    try:
        connection.request("POST", parts.path or "/", body=payload, headers=headers)
        response = connection.getresponse()
    except Exception as error:
        connection.close()
        raise TierFailure(f"{tier}: {type(error).__name__}: {error}") from error
    if response.status >= 400:
        try:
            # Bounded but whole: a Gemini 429 names its quota only past the first ~1.5 KB.
            detail = response.read(MAX_ERROR_BODY_BYTES).decode("utf-8", "ignore")
        except Exception:
            detail = ""
        connection.close()
        error = HTTPError(
            _TIER_URLS[tier], response.status, detail[:300], response.headers,
            BytesIO(detail.encode()),
        )
        error.body_text = detail
        raise error
    return UpstreamResponse(connection, response)


def _error_body(error: HTTPError) -> str:
    """Return an upstream error body; the stream behind HTTPError can be read only once."""
    body = getattr(error, "body_text", None)
    if body is None:
        try:
            body = error.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        error.body_text = body
    return body


def _snippet(error: HTTPError) -> str:
    return _error_body(error)[:160]


def _http_failure(tier: str, error: HTTPError) -> TierFailure:
    quota = quota_reset(tier, error)
    if quota:
        retry_at, name = quota
        return TierFailure(f"{tier}: HTTP {error.code}: quota {name} exhausted", retry_at, name)
    return TierFailure(f"{tier}: HTTP {error.code}: {_snippet(error)}")


def _is_mandatory_reasoning_error(error: HTTPError) -> bool:
    if error.code != 400:
        return False
    detail = _error_body(error).lower()
    return "reasoning" in detail and "mandatory" in detail


def _should_retry_with_reasoning_excluded(tier: str, error: HTTPError) -> bool:
    """True when an OpenRouter endpoint refuses to run without reasoning enabled."""
    return tier == "openrouter" and _is_mandatory_reasoning_error(error)


def _open_with_reasoning_retry(tier: str, body: dict, timeout: float, reasoning_mode: str | None):
    """Open a tier, retrying once with reasoning.exclude when an endpoint demands it."""
    try:
        return _open(tier, body, timeout, reasoning_mode)
    except HTTPError as error:
        if _should_retry_with_reasoning_excluded(tier, error):
            log("openrouter: endpoint requires reasoning; retrying with reasoning.exclude")
            try:
                return _open(tier, body, timeout, "exclude")
            except HTTPError as retry_error:
                raise _http_failure(tier, retry_error) from retry_error
            except Exception as retry_error:
                raise TierFailure(
                    f"{tier}: {type(retry_error).__name__}: {retry_error}"
                ) from retry_error
        raise _http_failure(tier, error) from error
    except Exception as error:
        raise TierFailure(f"{tier}: {type(error).__name__}: {error}") from error


def _budget_left(budget: float, deadline: float) -> float:
    return max(1.0, min(budget, deadline - time.time()))


def iter_stream(tier: str, body: dict, budget: float, deadline: float):
    """Yield ('meta', served_model) once, then ('delta', text) frames.

    Raises TierFailure when the tier produces no content or errors before the first
    content delta, so the caller can still fall through to the next tier.
    """
    reasoning_mode = "disabled" if tier == "openrouter" else None
    with local_lane_guard(tier):
        yield from _iter_stream_locked(tier, body, budget, deadline, reasoning_mode)


def _iter_stream_locked(tier, body, budget, deadline, reasoning_mode):
    response = _open_with_reasoning_retry(
        tier, body, _budget_left(budget, deadline), reasoning_mode
    )

    served = None
    saw_content = False
    try:
        for raw in response:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            served = served or chunk.get("model")
            delta = ((chunk.get("choices") or [{}])[0].get("delta")) or {}
            piece = delta.get("content")
            if not piece:
                continue  # reasoning deltas are intentionally dropped
            if not saw_content:
                saw_content = True
                yield ("meta", served or f"{tier}:{reasoning_mode or 'default'}")
            yield ("delta", piece)
    finally:
        response.close()
    if not saw_content:
        raise TierFailure(f"{tier}: stream produced no content")


def nonstream(tier: str, body: dict, budget: float, deadline: float):
    """Return (response_dict, content) for a tier, or raise TierFailure."""
    reasoning_mode = "disabled" if tier == "openrouter" else None
    with local_lane_guard(tier):
        response = _open_with_reasoning_retry(
            tier, body, _budget_left(budget, deadline), reasoning_mode
        )
        try:
            payload = json.load(response)
        except Exception as error:
            raise TierFailure(f"{tier}: unreadable response: {error}") from error
        finally:
            response.close()
    choice = (payload.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        raise TierFailure(
            f"{tier}: empty content (finish_reason={choice.get('finish_reason')})"
        )
    return payload, content


def record_failure(tier: str, failure: TierFailure) -> None:
    if failure.retry_at:
        tier_quota_exhausted(tier, failure.retry_at, failure.quota)
    else:
        tier_failed(tier, str(failure))


# --- HTTP surface --------------------------------------------------------------


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        return  # access lines would drown the tier log

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")

    @staticmethod
    def _sse_delta(model: str, text: str) -> str:
        chunk = {
            "id": "chatcmpl-router",
            "object": "chat.completion.chunk",
            "created": _now(),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def do_GET(self):
        route = self.path.rstrip("/")
        if route == "/health":
            self._json(200, {
                "status": "ok",
                "chain": list(CHAIN),
                "models": dict(_TIER_MODELS),
                "budgets_s": BUDGETS,
                "breaker": breaker_snapshot(),
                "prompt_guard": compaction_snapshot(),
                "power": power_snapshot(),
                "idle_gate": idle_snapshot(),
            })
        elif route == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [{"id": "observer-router", "object": "model", "owned_by": "local"}],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            self._json(400, {"error": "messages must be an array"})
            return

        body = dict(body)
        body["messages"], compacted = compact_messages(body["messages"])
        record_compaction(compacted)
        if compacted["changed"]:
            log(
                "compacted prompt "
                f"chars={compacted['input_chars']}->{compacted['output_chars']} "
                f"dropped_messages={compacted['dropped_messages']} "
                f"folded_fields={compacted['folded_fields']} "
                f"removed_images={compacted['removed_image_parts']} "
                f"removed_blobs={compacted['removed_blobs']}"
            )

        allowed, _ = allowed_tiers()
        deadline = time.time() + sum(BUDGETS.get(t, 0.0) for t in allowed) + 5.0
        if body.get("stream"):
            self._stream(body, deadline)
        else:
            self._once(body, deadline)

    # --- streaming path -------------------------------------------------------

    def _stream(self, body: dict, deadline: float) -> None:
        attempted = []
        allowed, skipped = allowed_tiers()
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier tiers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep the local tier out.
            skip_reason = tier_skip_reason(tier) or (
                local_busy_reason() if tier == "local" else None
            )
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                generator = iter_stream(tier, body, BUDGETS.get(tier, 30.0), deadline)
                _, served = next(generator)  # blocks until the first content delta
            except StopIteration:
                tier_failed(tier, "no frames")
                attempted.append(f"{tier}(no frames)")
                log(f"tier {tier} failed: stream ended with no frames")
                continue
            except TierFailure as failure:
                record_failure(tier, failure)
                attempted.append(f"{tier}({failure})")
                log(f"tier {tier} failed: {failure}")
                continue

            # First content delta is in hand: commit to this tier and stop buffering.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _, piece in generator:
                    self._chunk(self._sse_delta(served, piece))
                self._chunk("data: [DONE]\n\n")
                self._end_chunks()
                tier_ok(tier)
                log(
                    f"served tier={tier} model={served} "
                    f"latency={time.time() - started:.2f}s stream=1"
                )
            except Exception as error:  # mid-stream failure: the connection is committed
                log(f"tier {tier} failed mid-stream: {type(error).__name__}: {error}")
                tier_failed(tier, f"mid-stream {type(error).__name__}")
                try:
                    self._chunk("data: [DONE]\n\n")
                    self._end_chunks()
                except Exception as error:
                    log(f"failed to close stream for tier {tier}: {type(error).__name__}")
            return

        self._json(502, {"error": "all tiers failed", "attempted": attempted})

    # --- non-streaming path ---------------------------------------------------

    def _once(self, body: dict, deadline: float) -> None:
        attempted = []
        allowed, skipped = allowed_tiers()
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier tiers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep the local tier out.
            skip_reason = tier_skip_reason(tier) or (
                local_busy_reason() if tier == "local" else None
            )
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                payload, content = nonstream(tier, body, BUDGETS.get(tier, 30.0), deadline)
            except TierFailure as failure:
                record_failure(tier, failure)
                attempted.append(f"{tier}({failure})")
                log(f"tier {tier} failed: {failure}")
                continue
            served = payload.get("model") or tier
            choice = (payload.get("choices") or [{}])[0]
            tier_ok(tier)
            log(
                f"served tier={tier} model={served} "
                f"latency={time.time() - started:.2f}s stream=0"
            )
            self._json(200, {
                "id": payload.get("id") or "chatcmpl-router",
                "object": "chat.completion",
                "created": payload.get("created") or _now(),
                "model": served,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": choice.get("finish_reason") or "stop",
                }],
                "usage": payload.get("usage") or {},
            })
            return
        self._json(502, {"error": "all tiers failed", "attempted": attempted})


def main() -> None:
    log(
        f"starting on {HOST}:{PORT} chain={','.join(CHAIN)} "
        f"max_prompt_chars={MAX_PROMPT_CHARS} max_field_chars={MAX_FIELD_CHARS} "
        f"local_ac_only={LOCAL_AC_ONLY} power={power_source()} "
        f"idle_gate={','.join(LOCAL_IDLE_METRICS) or 'off'}"
    )
    if LOCAL_IDLE_METRICS:
        threading.Thread(target=_sample_lanes_forever, name="idle-sampler", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), RouterHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
