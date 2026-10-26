#!/usr/bin/env python3
"""Observer router: an OpenAI-compatible fallback chain across named provider instances.

claude-mem speaks OpenAI-compatible HTTP and resolves exactly one provider, so the
fallback chain lives here instead. This process exposes /v1/chat/completions and
/v1/models on 127.0.0.1:1244 and rewrites every request for whichever provider runs.

Why it exists (2026-09-18): the local observer lane (:1243) hit a Metal GPU OOM,
its generation thread died while the process kept answering /v1/models, and claude-mem
timed out on every call for ~2 hours. A healthy remote first hop plus a per-provider
budget removes that single point of failure.

Providers come from a TOML config file (OBSERVER_ROUTER_CONFIG, default
~/.config/observer-router/config.toml; see config.example.toml). Each [providers.<name>] table
is one instance of a type -- gemini, openrouter, or openai (any OpenAI-compatible endpoint) --
and several instances may share a type. The chain is an ordered list of instance names, and the
breaker and quota blocks are kept per instance. Keys are named, never inlined: an environment
variable or a line in a dotenv/JSON file.

Provider rules, measured rather than assumed:
  * openrouter/free rotates across free variants; roughly half of plain calls return
    EMPTY content because a reasoning model spends the whole max_tokens budget on
    reasoning. We inject {"reasoning":{"enabled":false}} and retry once with
    {"reasoning":{"exclude":true}} when an endpoint answers 400 "Reasoning is
    mandatory for this endpoint and cannot be disabled."
  * A provider that yields no content counts as a failed attempt and the next one runs,
    so an observer call never returns an empty result.
  * Both free types hit daily quotas and say when they reset: Gemini names the quota in
    google.rpc.QuotaFailure (daily quotas reset at midnight Pacific), OpenRouter sends
    X-RateLimit-Reset. Such a 429 skips the instance until the reset rather than re-probing
    it every breaker interval.

Streaming contract: claude-mem sends stream=true. Response headers are only sent once
the first content-bearing delta arrives, which keeps failover possible until that
point. Reasoning deltas are dropped; only content is forwarded downstream.

Prompt contract: requests are sanitized once before provider selection. Image payloads and
large base64 blobs are replaced with markers, individual fields are folded, and older
messages are dropped only when the remaining conversation exceeds the configured character
budget. System messages and the newest conversation context receive priority. This bounds
KV allocation without depending on another model call during an outage.

GPU-lane gates, per openai provider: serial = true runs one request at a time per lane URL;
idle_metrics makes the provider wait until the listed interactive lanes have been idle for
idle_seconds (see the idle gate section); ac_only = true skips it while the machine runs on
battery, so an unplugged laptop does not start GPU inference as a last resort. claude-mem
answers a failed chain with a provider quota cooldown and retries the same queued batch, so a
skipped provider delays observations instead of dropping them. An unreadable power state
leaves the provider available: a failed probe must not take the observer offline. Desktops
report AC Power permanently, making that gate inert there.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

HOST = os.environ.get("OBSERVER_ROUTER_HOST", "127.0.0.1")
LOG_PREFIX = "[observer-router]"

# A GPU lane runs one request at a time. ThreadingHTTPServer will happily run two upstream calls
# at once, and two concurrent prefills on the same lane both slow down and inflate its KV pool --
# the pool has to be sized for the concurrency, not for the request. claude-mem already sets
# CLAUDE_MEM_MAX_CONCURRENT_AGENTS=1, but nothing here enforced it, so any second caller on
# :1244 would have broken that assumption silently. A provider with serial = true takes the
# lock of its URL, so two instances pointed at one lane share it; remote providers hold no GPU.
_lane_locks: dict[str, threading.Lock] = {}
_lane_locks_lock = threading.Lock()


class _NullGuard:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def lane_guard(provider):
    """Hold the provider's lane for the duration of a call when it is serial."""
    if not provider.serial:
        return _NullGuard()
    with _lane_locks_lock:
        return _lane_locks.setdefault(provider.url, threading.Lock())


# Invalid OBSERVER_* settings, collected at import and reported by main() before it starts:
# like a bad config file, a bad setting stops the router instead of silently using the default.
_ENV_ERRORS: list[str] = []


def _env_number(name: str, default, kind, minimum, maximum=math.inf):
    text = os.environ.get(name, "").strip()
    if not text:  # unset, or set empty (as a plist may leave it): the default
        return default
    try:
        value = kind(text)
    except ValueError:
        value = None
    finite = value is not None and (kind is int or math.isfinite(value))
    if not finite or not minimum <= value <= maximum:
        bound = f"at least {minimum}" if maximum == math.inf else f"{minimum} to {maximum}"
        _ENV_ERRORS.append(f"{name} must be {'an integer' if kind is int else 'a number'} {bound}")
        return default
    return value


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 1_000_000_000) -> int:
    return _env_number(name, default, int, minimum, maximum)


_HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def _env_hosts(name: str) -> tuple[str, ...]:
    """A comma-separated list of DNS names, lower-cased; an invalid entry stops the router."""
    names = tuple(part.strip().lower() for part in os.environ.get(name, "").split(",")
                  if part.strip())
    for entry in names:
        if not _HOST_NAME.fullmatch(entry):
            _ENV_ERRORS.append(f"{name} must be comma-separated DNS names")
            return ()
    return names


def _env_float(name: str, default: float, minimum: float = 0.0,
               maximum: float = math.inf) -> float:
    return _env_number(name, default, float, minimum, maximum)


def _now() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


PORT = _env_int("OBSERVER_ROUTER_PORT", 1244, 1, 65535)
# DNS names, besides localhost, that may appear in a request's Host header. A web page reaches the
# router under a name an attacker's DNS first points here (rebinding); an IP literal cannot be
# rebound, so those are always accepted and LAN access by address keeps working.
ALLOWED_HOSTS = _env_hosts("OBSERVER_ROUTER_ALLOWED_HOSTS")

# Circuit breaker: a tier that fails repeatedly is skipped briefly instead of
# consuming the budget on every call (free tiers rate-limit aggressively).
BREAK_AFTER = _env_int("OBSERVER_BREAK_AFTER", 3)
BREAK_SECONDS = _env_float("OBSERVER_BREAK_SECONDS", 60.0, 1.0)

# The lane's native context is 262,144 tokens, but accepting that many tokens would leave
# no memory headroom for concurrent GPU clients. Character budgets are conservative token
# estimates; the router folds deterministically rather than recursively asking an LLM to
# summarize an already-oversized request.
MAX_PROMPT_CHARS = _env_int("OBSERVER_MAX_PROMPT_CHARS", 240_000)
MAX_FIELD_CHARS = _env_int("OBSERVER_MAX_FIELD_CHARS", 80_000)
MIN_RETAINED_CHARS = _env_int("OBSERVER_MIN_RETAINED_CHARS", 1_024, 0)
BASE64_BLOB_CHARS = _env_int("OBSERVER_BASE64_BLOB_CHARS", 16_384)
MAX_ERROR_BODY_BYTES = 16_384

def log(message: str) -> None:
    print(f"{LOG_PREFIX} [{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# --- power source -------------------------------------------------------------

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

# A local provider shares the GPU with interactive lanes on the same machine. With idle_metrics
# naming those lanes' Prometheus /metrics URLs, the provider runs only after every one of them
# has been idle for its idle_seconds, so an observer prefill never competes with a turn the user
# is waiting on. The grace period matters because an agent loop pauses between requests while
# its tools run.
#
# A background sampler, not a per-request probe, tracks activity: a request that starts and
# finishes between two observer calls still moves the submitted counter, but only a sampler
# can say when. An unreadable lane leaves its last reading in place and ages into idle, so a
# stopped or broken lane cannot keep the observer offline.
IDLE_SAMPLE_SECONDS = _env_float("OBSERVER_IDLE_SAMPLE_SECONDS", 2.0, 0.1, 3600.0)
# What counts as busy when a provider sets no idle_signal: Splash's request counters.
SPLASH_SIGNAL = {
    "submitted": "splash_requests_submitted_total",
    "finished": ["splash_requests_completed_total", "splash_requests_cancelled_total",
                 "splash_requests_failed_total"],
}

_idle_lock = threading.Lock()
# url -> {"submitted": last submitted count, "busy_at": last time activity was seen}
_lane_activity: dict[str, dict] = {}


def parse_lane_metrics(text: str, signal: dict | None = None) -> tuple[int | None, int] | None:
    """Return (submitted, in_flight) from a Prometheus /metrics body, or None when a named metric
    is missing or unreadable.

    The signal is an idle_signal table: counters, {submitted, finished}, give in-flight as
    submitted minus the finished counters; gauges, {in_flight}, are summed (a labelled gauge
    adds up over its label sets) and have no submitted count, so None stands for it.
    """
    signal = signal or SPLASH_SIGNAL
    wanted = set(signal["in_flight"] if "in_flight" in signal
                 else [signal["submitted"], *signal["finished"]])
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rstrip().rpartition(" ")
        name = head.rstrip().partition("{")[0]
        if name in wanted:
            try:
                values[name] = values.get(name, 0.0) + float(value)
            except ValueError:
                return None
    if wanted - values.keys():
        return None
    try:
        if "in_flight" in signal:
            return None, int(sum(values.values()))
        submitted = int(values[signal["submitted"]])
        return submitted, submitted - int(sum(values[name] for name in signal["finished"]))
    except (ValueError, OverflowError):  # NaN, infinity: not a reading
        return None


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


def _read_lane(url: str, signal: dict | None = None) -> tuple[int | None, int] | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=1.0)
    try:
        connection.request("GET", parts.path or "/metrics")
        response = connection.getresponse()
        if response.status != 200:
            return None
        return parse_lane_metrics(response.read().decode("utf-8", "ignore"), signal)
    except Exception:  # an unreachable lane is not activity
        return None
    finally:
        connection.close()


def watched_lanes() -> tuple[str, ...]:
    """Every /metrics URL an active provider in the chain waits on."""
    return tuple(dict.fromkeys(
        url for name in chain_providers() for url in PROVIDERS[name].idle_metrics
    ))


def lane_signal(url: str) -> dict | None:
    """The idle_signal of the first provider in the chain that watches this URL (config loading
    rejects two providers reading one URL differently); None means the Splash counters."""
    return next((PROVIDERS[name].idle_signal for name in chain_providers()
                 if url in PROVIDERS[name].idle_metrics), None)


def sample_lanes_once() -> None:
    for url in watched_lanes():
        record_lane_sample(url, _read_lane(url, lane_signal(url)), time.time())


def _sample_lanes_forever() -> None:
    while True:
        sample_lanes_once()
        time.sleep(IDLE_SAMPLE_SECONDS)


def busy_lanes(provider, now: float | None = None) -> list[str]:
    """Return the provider's watched lanes active within its last idle_seconds."""
    now = time.time() if now is None else now
    with _idle_lock:
        return [
            url for url in provider.idle_metrics
            if now - _lane_activity.get(url, {}).get("busy_at", 0.0) < provider.idle_seconds
        ]


def busy_reason(provider) -> str | None:
    busy = busy_lanes(provider)
    if not busy:
        return None
    ports = ",".join(urllib.parse.urlsplit(url).netloc for url in busy)
    return f"skipped: lanes busy ({ports}, idle {provider.idle_seconds:.0f}s required)"


def idle_snapshot() -> dict:
    now = time.time()
    snapshot = {}
    for name in chain_providers():
        provider = PROVIDERS[name]
        if not provider.idle_metrics:
            continue
        with _idle_lock:
            lanes = {
                url: round(now - _lane_activity[url]["busy_at"], 1)
                if url in _lane_activity and _lane_activity[url]["busy_at"] else None
                for url in provider.idle_metrics
            }
        snapshot[name] = {
            "idle_seconds_required": provider.idle_seconds,
            "seconds_since_active": lanes,
            "allowed": not busy_lanes(provider, now),
        }
    return snapshot


def allowed_tiers(order: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Split a request's provider order (by default, the next expand_chain()) into the providers
    worth trying and the ones skipped, with a reason each.

    Skipped providers are reported in the failed-chain response so claude-mem's cooldown log
    explains why a local lane never ran.
    """
    order = expand_chain() if order is None else order
    allowed: list[str] = []
    skipped: dict[str, str] = {}
    on_battery = any(PROVIDERS[name].ac_only for name in order) and power_source() == "Battery Power"
    for name in order:
        provider = PROVIDERS[name]
        if provider.ac_only and on_battery:
            skipped[name] = "skipped: on battery (AC-only)"
            continue
        busy = busy_reason(provider)
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
        "ac_only": [name for name in chain_providers() if PROVIDERS[name].ac_only],
        "ac_only_allowed": source != "Battery Power",
    }


# --- config -------------------------------------------------------------------

CONFIG_PATH = os.path.expanduser(
    os.environ.get("OBSERVER_ROUTER_CONFIG", "~/.config/observer-router/config.toml")
)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-type defaults. "openai" is any OpenAI-compatible endpoint (a local lane, a bridge), so it
# has no default URL and needs no key; the hosted types always authenticate.
_TYPE_DEFAULTS = {
    "gemini": {"url": GEMINI_URL, "budget_s": 18.0},
    "openrouter": {"url": OPENROUTER_URL, "budget_s": 24.0},
    "openai": {"url": "", "budget_s": 40.0},
}
# Field -> kind. Every value is type-checked at start-up: TOML "false" is a string, and a
# dataclass would store it and read it as true.
_COMMON_FIELDS = {
    "type": "str", "url": "url", "model": "str", "budget_s": "seconds",
    "api_key_env": "name", "api_key_file": "str", "api_key_var": "name",
    "buffer_stream": "bool",
    # Per-provider overrides of router-wide settings; unset, the router-wide value applies.
    "max_prompt_chars": "count", "break_after": "count", "break_seconds": "seconds",
}
_TYPE_FIELDS = {
    "gemini": {},
    "openrouter": {"site_url": "str", "app_name": "str"},
    # The GPU-lane gates only make sense for a lane on this machine, so only openai has them.
    "openai": {"reasoning_effort": "str", "chat_template_kwargs": "table",
               "serial": "bool", "ac_only": "bool",
               "idle_metrics": "urls", "idle_seconds": "seconds", "idle_signal": "table"},
}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """The config file is missing, unreadable, or describes an unusable chain."""


# Provider fields that override a router-wide setting; /health shows the ones that are set.
_OVERRIDES = ("max_prompt_chars", "break_after", "break_seconds")


@dataclass(frozen=True)
class Provider:
    """One named upstream instance. Several instances may share a type."""

    name: str
    type: str
    url: str
    model: str
    budget: float
    api_key_env: str = ""
    api_key_file: str = ""
    api_key_var: str = ""
    site_url: str = ""
    app_name: str = "observer-router"
    reasoning_effort: str = ""
    chat_template_kwargs: dict | None = None
    serial: bool = False
    ac_only: bool = False
    buffer_stream: bool = False
    max_prompt_chars: int = 0  # 0: only the router-wide guard
    break_after: int = 0  # 0: OBSERVER_BREAK_AFTER
    break_seconds: float = 0.0  # 0: OBSERVER_BREAK_SECONDS
    idle_metrics: tuple[str, ...] = ()
    idle_seconds: float = 30.0
    idle_signal: dict | None = None  # which /metrics names mean busy; None: the Splash counters


@dataclass(frozen=True)
class Config:
    providers: dict[str, Provider]
    # Entries name a provider or a group; a group rotates its members (see expand_chain).
    chain: tuple[str, ...]
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _http_url(value) -> bool:
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _check_field(where: str, field: str, kind: str, value) -> None:
    """Raise ConfigError when value is not of kind. Messages never include the value: a key
    pasted into the wrong field must not be copied into the log."""
    valid = {
        "str": lambda v: isinstance(v, str),
        "name": lambda v: isinstance(v, str) and _NAME.fullmatch(v) is not None,
        "bool": lambda v: isinstance(v, bool),
        "count": lambda v: (isinstance(v, int) and not isinstance(v, bool)
                            and 1 <= v <= 1_000_000_000),
        "table": lambda v: isinstance(v, dict),
        "seconds": lambda v: (isinstance(v, (int, float)) and not isinstance(v, bool)
                              and math.isfinite(v) and v > 0),
        "url": lambda v: isinstance(v, str) and _http_url(v),
        # The idle sampler reads plain http only.
        "urls": lambda v: isinstance(v, list) and all(
            isinstance(u, str) and _http_url(u) and u.startswith("http://") for u in v
        ),
    }[kind](value)
    if not valid:
        expected = {
            "str": "a string", "bool": "true or false", "count": "a whole number from 1", "table": "a table, e.g. {k = v}", "seconds": "a positive number",
            "name": "a variable name (letters, digits, underscore), not the key itself",
            "url": "an http(s) URL with a host", "urls": "a list of http:// URLs",
        }[kind]
        raise ConfigError(f"{where}: {field} must be {expected}")


def _provider(name: str, table) -> Provider:
    where = f"providers.{name}"
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    kind = table.get("type")
    if kind not in _TYPE_DEFAULTS:
        raise ConfigError(f"{where}: type must be one of {', '.join(_TYPE_DEFAULTS)}")
    if "api_key" in table:
        # The value is never echoed: this message may land in a log.
        raise ConfigError(
            f"{where}: inline api_key is not allowed; use api_key_env or api_key_file"
        )
    kinds = {**_COMMON_FIELDS, **_TYPE_FIELDS[kind]}
    unknown = set(table) - set(kinds)
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) for type {kind}: {', '.join(sorted(unknown))}")
    for field, value in table.items():
        _check_field(where, field, kinds[field], value)
    fields = {key: value for key, value in table.items() if key != "budget_s"}
    fields["url"] = table.get("url") or _TYPE_DEFAULTS[kind]["url"]
    if not fields["url"]:
        raise ConfigError(f"{where}: type openai needs a url")
    if not table.get("model"):
        raise ConfigError(f"{where}: model is required")
    if bool(table.get("api_key_file")) != bool(table.get("api_key_var")):
        raise ConfigError(f"{where}: api_key_file and api_key_var go together (the file, and the "
                          "key's name in it)")
    if kind != "openai" and not (table.get("api_key_env") or table.get("api_key_file")):
        raise ConfigError(f"{where}: type {kind} needs api_key_env or api_key_file")
    if "api_key_file" in fields:
        fields["api_key_file"] = os.path.expanduser(fields["api_key_file"])
    if "idle_metrics" in fields:
        fields["idle_metrics"] = tuple(str(url) for url in fields["idle_metrics"])
    if "idle_seconds" in fields:
        fields["idle_seconds"] = float(fields["idle_seconds"])
    if "break_seconds" in fields:
        fields["break_seconds"] = float(fields["break_seconds"])
    # The guard keeps a message only if it can keep MIN_RETAINED_CHARS of it, and a prompt needs
    # room for a system message and the request it carries.
    if "idle_signal" in fields:
        if not fields.get("idle_metrics"):
            raise ConfigError(f"{where}: idle_signal needs idle_metrics, the lanes it is read from")
        fields["idle_signal"] = _idle_signal(where, fields["idle_signal"])
    floor = max(2 * MIN_RETAINED_CHARS, 1)
    if fields.get("max_prompt_chars", floor) < floor:
        raise ConfigError(f"{where}: max_prompt_chars must be at least {floor}, enough to keep "
                          "a system message and the request")
    budget = float(table.get("budget_s", _TYPE_DEFAULTS[kind]["budget_s"]))
    return Provider(name=name, budget=budget, **fields)


_METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")


def _idle_signal(where: str, value: dict) -> dict:
    """Check an idle_signal table: counters {submitted, finished}, or gauges {in_flight}."""
    counters, gauges = {"submitted", "finished"}, {"in_flight"}
    unknown = set(value) - counters - gauges
    if unknown:
        raise ConfigError(f"{where}: idle_signal has unknown key(s): {', '.join(sorted(unknown))}")
    if ("in_flight" in value) == bool(set(value) & counters):
        raise ConfigError(f"{where}: idle_signal must set exactly one of in_flight, or "
                          "submitted with finished")
    if set(value) & counters and set(value) != counters:
        raise ConfigError(f"{where}: idle_signal counters need both submitted and finished")

    def names(key: str, listed) -> list[str]:
        items = [listed] if key == "submitted" else listed
        if not isinstance(items, list) or not items or not all(
                isinstance(item, str) and _METRIC_NAME.fullmatch(item) for item in items):
            kind = "a metric name" if key == "submitted" else "a non-empty list of metric names"
            raise ConfigError(f"{where}: idle_signal {key} must be {kind}")
        return items

    result = {key: names(key, value[key]) for key in value}
    listed = [*result.get("finished", []), *result.get("in_flight", [])]
    if len(set(listed)) != len(listed):
        raise ConfigError(f"{where}: idle_signal names a metric more than once")
    if "submitted" in result:
        result["submitted"] = result["submitted"][0]
    return result


def parse_config(data: dict) -> Config:
    unknown = set(data) - {"chain", "providers", "groups"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    tables = data.get("providers") or {}
    if not isinstance(tables, dict) or not tables:
        raise ConfigError("config defines no [providers.<name>] tables")
    providers = {name: _provider(name, table) for name, table in tables.items()}
    _check_lane_signals(providers)
    groups = _groups(data.get("groups", {}), providers)
    chain = data.get("chain", list(providers))
    if not isinstance(chain, list) or not chain or not all(isinstance(n, str) for n in chain):
        raise ConfigError("chain must be a non-empty list of provider or group names")
    return Config(providers, _checked_chain(chain, providers, groups, "chain"), groups)


def _check_lane_signals(providers: dict) -> None:
    """One /metrics URL is sampled once, so every provider that watches it must name the same
    signal."""
    seen: dict[str, tuple[str, dict | None]] = {}
    for name, provider in providers.items():
        for url in provider.idle_metrics:
            other = seen.setdefault(url, (name, provider.idle_signal))
            if other[1] != provider.idle_signal:
                raise ConfigError(f"idle_metrics URL {url} is read two ways, by providers "
                                  f"{other[0]} and {name}: give both the same idle_signal")


def _names(where: str, names, providers: dict) -> tuple[str, ...]:
    unknown = [name for name in names if name not in providers]
    if unknown:
        raise ConfigError(f"{where} names undefined provider(s): {', '.join(unknown)}")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ConfigError(f"{where} repeats provider(s): {', '.join(repeated)}")
    return tuple(names)


def _groups(tables, providers: dict) -> dict[str, tuple[str, ...]]:
    """Parse [groups.<name>] tables. Members are providers only; a group cannot nest."""
    if not isinstance(tables, dict):
        raise ConfigError("groups must be [groups.<name>] tables")
    groups = {}
    for name, table in tables.items():
        where = f"groups.{name}"
        if name in providers:
            raise ConfigError(f"{where}: the name is already a provider")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = set(table) - {"members"}
        if unknown:
            raise ConfigError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
        members = table.get("members")
        if not isinstance(members, list) or not members or not all(
            isinstance(member, str) for member in members
        ):
            raise ConfigError(f"{where}: members must be a non-empty list of provider names")
        groups[name] = _names(where, members, providers)
    return groups


def _checked_chain(names, providers: dict, groups: dict, source: str) -> tuple[str, ...]:
    unknown = [name for name in names if name not in providers and name not in groups]
    if unknown:
        raise ConfigError(
            f"{source} names undefined provider(s) or group(s): {', '.join(unknown)}"
        )
    expanded = [member for name in names for member in groups.get(name, (name,))]
    repeated = sorted({name for name in expanded if expanded.count(name) > 1})
    if repeated:
        raise ConfigError(
            f"{source} would try provider(s) more than once, directly or through a group: "
            f"{', '.join(repeated)}"
        )
    return tuple(names)


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"cannot read config {path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from None
    return parse_config(data)


def chain_override(config: Config, text: str) -> tuple[str, ...]:
    """Apply OBSERVER_ROUTER_CHAIN: comma-separated provider or group names, or empty."""
    names = [name.strip() for name in text.split(",") if name.strip()]
    if not names:
        return config.chain
    return _checked_chain(names, config.providers, config.groups, "OBSERVER_ROUTER_CHAIN")


# Environment variables that configured providers before the config file existed. Setting one
# now does nothing, so start-up names them rather than ignoring them silently.
_REMOVED_ENV_PREFIXES = ("OBSERVER_LOCAL_", "OBSERVER_CURSOR_", "OBSERVER_GEMINI_",
                         "OBSERVER_OPENROUTER_")


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def provider_warnings(providers: dict) -> list[str]:
    """Settings that parse but never act, so /health must not be read as if they did."""
    warnings = [
        f"providers.{name}: max_prompt_chars {provider.max_prompt_chars} is above the router-wide "
        f"OBSERVER_MAX_PROMPT_CHARS {MAX_PROMPT_CHARS}, which already bounds every prompt first"
        for name, provider in providers.items() if provider.max_prompt_chars > MAX_PROMPT_CHARS
    ]
    for name, provider in providers.items():
        if provider.ac_only and not _is_loopback(urllib.parse.urlsplit(provider.url).hostname):
            warnings.append(
                f"providers.{name}: ac_only reads the battery of the machine running the "
                "router, but url names another host, so it cannot see that host's power"
            )
    return warnings


def stale_environment(env) -> list[str]:
    return sorted(
        name for name in env
        if name.startswith(_REMOVED_ENV_PREFIXES) or name == "CLAUDE_MEM_SETTINGS"
    )


# The active configuration. main() fills these; they stay empty until then so importing the
# module (the tests do) never reads a config file.
PROVIDERS: dict[str, Provider] = {}
GROUPS: dict[str, tuple[str, ...]] = {}
CHAIN: tuple[str, ...] = ()
_rotation_lock = threading.Lock()
_rotation: dict[str, int] = {}


def configure(config: Config, chain: tuple[str, ...] | None = None) -> None:
    global PROVIDERS, GROUPS, CHAIN
    PROVIDERS = dict(config.providers)
    GROUPS = dict(config.groups)
    CHAIN = tuple(chain or config.chain)
    with _rotation_lock:
        _rotation.clear()
    with _breaker_lock:
        _group_strikes.clear()


def chain_providers() -> list[str]:
    """Every provider the chain can reach, in configured order, groups flattened."""
    return [member for name in CHAIN for member in GROUPS.get(name, (name,))]


def expand_chain() -> list[str]:
    """Return this request's provider order.

    Each group starts one member further on than it did for the previous request, so load
    rotates across its keys; the remaining members follow in order, so a failing or quota-blocked
    member still falls over inside the group before the chain moves on.
    """
    order = []
    with _rotation_lock:
        for name in CHAIN:
            members = GROUPS.get(name)
            if members is None:
                order.append(name)
                continue
            start = _rotation.get(name, 0) % len(members)
            _rotation[name] = start + 1
            order += members[start:] + members[:start]
    return order


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


def compact_messages(messages: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    """Return a bounded conversation while preserving system and newest context.

    limit is the character budget; by default the router-wide OBSERVER_MAX_PROMPT_CHARS.
    """
    limit = MAX_PROMPT_CHARS if limit is None else limit
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

    if _message_chars(sanitized) <= limit:
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
    remaining = limit
    # The newest non-system message is the request the model must answer, so an oversized
    # system message may not take the whole budget: up to half is held back for it.
    newest = recent_indexes[0] if recent_indexes else None
    reserved = 0
    if newest is not None and system_indexes:
        reserved = min(_content_chars(sanitized[newest].get("content")), remaining // 2)
    for index in priority:
        message = dict(sanitized[index])
        size = _content_chars(message.get("content"))
        if index == newest:
            reserved = 0
        available = remaining - reserved
        if size <= available:
            selected[index] = message
            remaining -= size
        elif available >= MIN_RETAINED_CHARS:
            message["content"] = _fold_text(str(message.get("content") or ""), available)
            selected[index] = message
            stats["folded_fields"] += 1
            remaining -= available

    compacted = [selected[index] for index in sorted(selected)]
    stats["dropped_messages"] += len(sanitized) - len(selected)
    stats["output_chars"] = _message_chars(compacted)
    stats["changed"] = True
    return compacted, stats


def fit_prompt(provider: Provider, body: dict) -> dict:
    """Return the request body sized for this provider: its max_prompt_chars can only tighten the
    router-wide guard, which has already run, so a provider without one gets the body as is."""
    limit = provider.max_prompt_chars
    messages = body.get("messages") or []
    if not limit or _message_chars(messages) <= limit:
        return body
    compacted, stats = compact_messages(messages, limit)
    log(
        f"compacted prompt for tier {provider.name} chars={stats['input_chars']}->"
        f"{stats['output_chars']} dropped_messages={stats['dropped_messages']} "
        f"folded_fields={stats['folded_fields']}"
    )
    return {**body, "messages": compacted}


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
# Keyed by provider name, so two instances of one type break and block independently.
_failures: dict[str, int] = {}
_skip_until: dict[str, float] = {}
# Why a provider is skipped: "breaker-open", or the quota the provider says is exhausted.
_skip_reason: dict[str, str] = {}


def tier_skip_reason(name: str) -> str | None:
    """Return why the tier is skipped right now, or None when it may run."""
    with _breaker_lock:
        if time.time() >= _skip_until.get(name, 0.0):
            return None
        return _skip_reason.get(name) or "breaker-open"


def tier_available(name: str) -> bool:
    return tier_skip_reason(name) is None


def tier_ok(name: str) -> None:
    group = _group_of(name)
    with _breaker_lock:
        if group:
            _group_strikes[group] = 0
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


def _break_after(name: str) -> int:
    provider = PROVIDERS.get(name)
    return (provider.break_after if provider else 0) or BREAK_AFTER


def _break_seconds(name: str) -> float:
    provider = PROVIDERS.get(name)
    return (provider.break_seconds if provider else 0.0) or BREAK_SECONDS


def tier_failed(name: str, reason: str) -> None:
    seconds = _break_seconds(name)
    with _breaker_lock:
        _failures[name] = _failures.get(name, 0) + 1
        if _failures[name] >= _break_after(name) and _skip(
            name, time.time() + seconds, "breaker-open"
        ):
            log(
                f"tier {name} breaker open for {seconds:.0f}s "
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
            for name in chain_providers()
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


def quota_reset(kind: str, error: HTTPError, now: float | None = None) -> tuple[float, str] | None:
    """Return (reset_epoch, quota_name) when a 429 from a provider of this type states its reset."""
    if error.code != 429:
        return None
    now = time.time() if now is None else now
    body = _error_body(error)
    if kind == "gemini":
        found = _gemini_quota(body, now)
    elif kind == "openrouter":
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

    def __init__(self, message: str, retry_at: float | None = None, quota: str = "",
                 overloaded: str = "", partial: tuple[str, str] | None = None):
        super().__init__(message)
        self.retry_at = retry_at
        self.quota = quota
        # (served model, text) of a reply cut off at the token limit: served when no later tier
        # answers, since claude-mem keeps a cut-off reply's complete blocks.
        self.partial = partial
        # Why the model itself, not this key, looks out of capacity: "unavailable" (HTTP 503),
        # "exhausted" (an HTTP 429 that names no quota), or "timeout" (a socket read timeout that
        # used the provider's whole budget). Every key in the provider's group would fail the
        # same way. Empty otherwise.
        self.overloaded = overloaded


def payload_for(provider: Provider, body: dict, reasoning_mode: str | None) -> dict:
    payload = {
        "model": provider.model,
        "messages": body.get("messages") or [],
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "max_tokens", "top_p", "stop"):
        if body.get(key) is not None:
            payload[key] = body[key]
    if provider.reasoning_effort:
        # Splash honours reasoning_effort ("none" switches thinking off) and ignores
        # chat_template_kwargs entirely. Without it a reasoning model spends the whole
        # max_tokens budget thinking and returns content=None with finish_reason=length, which
        # the router then reports as "empty content". Unset for lanes that reject unknown
        # fields (mlx_lm does).
        payload["reasoning_effort"] = provider.reasoning_effort
    if provider.chat_template_kwargs:
        # For servers that ignore reasoning_effort but render the template themselves (mtplx,
        # vLLM): {enable_thinking = false} is what turns Qwen thinking off there.
        payload["chat_template_kwargs"] = provider.chat_template_kwargs
    if provider.type == "openrouter" and reasoning_mode == "disabled":
        # Without this, reasoning models burn the whole budget on reasoning and
        # return empty content.
        payload["reasoning"] = {"enabled": False}
    elif provider.type == "openrouter" and reasoning_mode == "exclude":
        # Some endpoints reject enabled=false with HTTP 400; keep reasoning on but
        # hide it from the response.
        payload["reasoning"] = {"exclude": True}
    return payload


_QUOTED_VALUE = re.compile(r"""(["'])(.*)\1\s*(?:#.*)?""")


def _key_from_file(path: str, var: str) -> str:
    """Read one key from a dotenv-style file, or from a JSON object when the path ends .json.

    Reading the key where it already lives (a bridge's env file, claude-mem's settings) keeps
    one copy of the secret, so rotating it there cannot silently break the provider. The file
    is re-read on every call, so a rotation takes effect without a restart.
    """
    with open(path) as handle:
        if path.endswith(".json"):
            value = json.load(handle).get(var)  # top-level keys only
            return str(value).strip() if value else ""
        found = ""
        for line in handle:  # the last assignment wins, as when the file is sourced
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if not sep or name.strip() != var:
                continue
            quoted = _QUOTED_VALUE.fullmatch(value.strip())
            found = quoted.group(2) if quoted else value.split(" #", 1)[0].strip()
    return found


def api_key(provider: Provider) -> str:
    """Return the provider's key: api_key_env first, then api_key_var in api_key_file."""
    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env, "").strip()
        if key:
            return key
    if provider.api_key_file:
        path = provider.api_key_file
        try:
            return _key_from_file(path, provider.api_key_var)
        except OSError as error:
            raise TierFailure(f"{provider.name}: cannot read {path}: {error.strerror}") from None
        except (ValueError, AttributeError) as error:
            # The decoder's message is not echoed: it can quote the file's contents.
            raise TierFailure(
                f"{provider.name}: cannot parse {path}: {type(error).__name__}"
            ) from None
    return ""


def headers_for(provider: Provider) -> dict:
    headers = {"Content-Type": "application/json"}
    wants_key = provider.api_key_env or provider.api_key_file
    if wants_key:
        key = api_key(provider)
        if not key:
            sources = " or ".join(filter(None, (
                provider.api_key_env and f"${provider.api_key_env}",
                provider.api_key_file and f"{provider.api_key_var} in {provider.api_key_file}",
            )))
            raise TierFailure(f"{provider.name}: no API key in {sources}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
            # http.client would reject the header and quote the whole key in its error.
            raise TierFailure(f"{provider.name}: API key contains control characters")
        headers["Authorization"] = f"Bearer {key}"
    if provider.type == "openrouter":
        if provider.site_url:
            headers["HTTP-Referer"] = provider.site_url
        headers["X-Title"] = provider.app_name
    return headers


# Upstream traffic goes through http.client directly: only the HTTPS and HTTP
# constructors are ever used, so file:/custom schemes cannot resolve here.


class UpstreamResponse:
    """File-like wrapper that closes both the response and its connection."""

    def __init__(self, connection, response, sock=None):
        self._connection = connection
        self._response = response
        self._sock = sock

    def read(self, size: int | None = None):
        if size is None:
            return self._response.read()
        return self._response.read(size)

    def __iter__(self):
        return iter(self._response)

    def set_timeout(self, seconds: float) -> None:
        """Bound the next reads; the socket keeps this until it is set again."""
        if self._sock is not None:
            self._sock.settimeout(seconds)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _connect(provider: Provider, timeout: float):
    """Open an http(s) connection for a provider, rejecting any other scheme."""
    parts = urllib.parse.urlsplit(provider.url)
    host = parts.hostname
    if not host:
        raise TierFailure(f"{provider.name}: URL has no host")
    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout)
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    raise TierFailure(f"{provider.name}: refusing non-http(s) URL")


def _timeout_kind(provider: Provider, error: BaseException, timeout: float) -> str:
    """ "timeout" for a read timeout that had the provider's whole budget; a timeout the request
    deadline cut short says nothing about the provider."""
    return "timeout" if isinstance(error, TimeoutError) and timeout >= provider.budget else ""


def _open(provider: Provider, body: dict, timeout: float, reasoning_mode: str | None):
    parts = urllib.parse.urlsplit(provider.url)
    payload = json.dumps(payload_for(provider, body, reasoning_mode)).encode()
    headers = headers_for(provider)
    headers["Content-Length"] = str(len(payload))
    connection = _connect(provider, timeout)
    try:
        connection.request("POST", parts.path or "/", body=payload, headers=headers)
        sock = connection.sock  # getresponse drops it from a connection that will close
        response = connection.getresponse()
    except Exception as error:
        connection.close()
        raise TierFailure(
            f"{provider.name}: {type(error).__name__}: {error}",
            overloaded=_timeout_kind(provider, error, timeout),
        ) from error
    if response.status >= 400:
        try:
            # Bounded but whole: a Gemini 429 names its quota only past the first ~1.5 KB.
            detail = response.read(MAX_ERROR_BODY_BYTES).decode("utf-8", "ignore")
        except Exception:
            detail = ""
        connection.close()
        error = HTTPError(
            provider.url, response.status, detail[:300], response.headers,
            BytesIO(detail.encode()),
        )
        error.body_text = detail
        raise error
    return UpstreamResponse(connection, response, sock)


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


def _http_failure(provider: Provider, error: HTTPError) -> TierFailure:
    quota = quota_reset(provider.type, error)
    name = provider.name
    if quota:
        retry_at, quota_name = quota
        return TierFailure(
            f"{name}: HTTP {error.code}: quota {quota_name} exhausted", retry_at, quota_name
        )
    # A 429 that names no quota (Gemini's bare RESOURCE_EXHAUSTED) has no reset to wait for, and
    # has been seen on every key of a group within the same minute: shared model capacity.
    overloaded = {503: "unavailable", 429: "exhausted"}.get(error.code, "")
    return TierFailure(f"{name}: HTTP {error.code}: {_snippet(error)}", overloaded=overloaded)


def _is_mandatory_reasoning_error(error: HTTPError) -> bool:
    if error.code != 400:
        return False
    detail = _error_body(error).lower()
    return "reasoning" in detail and "mandatory" in detail


def _open_with_reasoning_retry(
    provider: Provider, body: dict, timeout: float, reasoning_mode: str | None
):
    """Open a provider, retrying an OpenRouter endpoint once with reasoning.exclude when it
    refuses to run with reasoning disabled."""
    name = provider.name
    try:
        return _open(provider, body, timeout, reasoning_mode)
    except HTTPError as error:
        if provider.type == "openrouter" and _is_mandatory_reasoning_error(error):
            log(f"{name}: endpoint requires reasoning; retrying with reasoning.exclude")
            try:
                return _open(provider, body, timeout, "exclude")
            except HTTPError as retry_error:
                raise _http_failure(provider, retry_error) from retry_error
            except TierFailure:
                raise
            except Exception as retry_error:
                raise TierFailure(
                    f"{name}: {type(retry_error).__name__}: {retry_error}"
                ) from retry_error
        raise _http_failure(provider, error) from error
    except TierFailure:
        raise
    except Exception as error:
        raise TierFailure(f"{name}: {type(error).__name__}: {error}") from error


def _budget_left(budget: float, deadline: float) -> float:
    return max(1.0, min(budget, deadline - time.time()))


def _reasoning_mode(provider: Provider) -> str | None:
    return "disabled" if provider.type == "openrouter" else None


def iter_stream(provider: Provider, body: dict, deadline: float):
    """Yield ('meta', served_model) once, then ('delta', text) frames.

    Raises TierFailure when the provider produces no content or errors before the first
    content delta, so the caller can still fall through to the next provider. A buffer_stream
    provider yields nothing until its stream ends, so a reply cut off at the token limit
    (finish_reason=length) fails over too; any other provider's cut-off reply raises only after
    its text was forwarded.
    """
    with lane_guard(provider):
        yield from _iter_stream_locked(provider, body, deadline, _reasoning_mode(provider))


def _iter_stream_locked(provider, body, deadline, reasoning_mode):
    timeout = _budget_left(provider.budget, deadline)
    # A buffered reply reaches the client only once it is whole, so the budget to first content
    # covers all of it: each read may wait only for what is left of it.
    whole_by = time.time() + timeout
    response = _open_with_reasoning_retry(provider, body, timeout, reasoning_mode)

    served = None
    finish = None
    usage = {}
    pieces: list[str] = []
    committed = False

    def bound_next_read():
        left = whole_by - time.time()
        if left <= 0:
            raise TierFailure(f"{provider.name}: reply not finished within {timeout:.0f}s",
                              overloaded=_timeout_kind(provider, TimeoutError(), timeout))
        response.set_timeout(left)

    try:
        if provider.buffer_stream:
            bound_next_read()
        for raw in _lines_until_content(provider, response, timeout, lambda: committed):
            if provider.buffer_stream:
                bound_next_read()
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
            usage = chunk.get("usage") or usage
            choice = (chunk.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            piece = (choice.get("delta") or {}).get("content")
            if not piece:
                continue  # reasoning deltas are intentionally dropped
            pieces.append(piece)
            if provider.buffer_stream:
                continue  # held back until finish_reason says whether the reply is whole
            if not committed:
                committed = True
                yield ("meta", served or provider.model)
            yield ("delta", piece)
    finally:
        response.close()
    if not pieces:
        raise TierFailure(f"{provider.name}: stream produced no content")
    if finish == "length":
        raise _cut_off(provider, body, "".join(pieces), usage, served or provider.model)
    if not committed and not finish:
        raise TierFailure(f"{provider.name}: stream ended without a finish_reason")
    if not committed:
        yield ("meta", served or provider.model)
        yield ("delta", "".join(pieces))


def _lines_until_content(provider: Provider, response, timeout: float, committed):
    """Yield the response's lines. A read error before the stream commits -- an upstream that
    sent its headers and then stalled -- becomes a TierFailure, so the chain can still fail over;
    after it, the error propagates to the committed stream's handler."""
    lines = iter(response)
    while True:
        try:
            raw = next(lines)
        except StopIteration:
            return
        except (OSError, http.client.HTTPException) as error:
            if committed():
                raise
            raise TierFailure(
                f"{provider.name}: {type(error).__name__} before first content: {error}",
                overloaded=_timeout_kind(provider, error, timeout),
            ) from error
        yield raw


def nonstream(provider: Provider, body: dict, deadline: float):
    """Return (response_dict, content) for a provider, or raise TierFailure."""
    with lane_guard(provider):
        timeout = _budget_left(provider.budget, deadline)
        response = _open_with_reasoning_retry(provider, body, timeout, _reasoning_mode(provider))
        try:
            payload = json.load(response)
        except Exception as error:
            raise TierFailure(
                f"{provider.name}: unreadable response: {error}",
                overloaded=_timeout_kind(provider, error, timeout),
            ) from error
        finally:
            response.close()
    choice = (payload.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        raise TierFailure(
            f"{provider.name}: empty content (finish_reason={choice.get('finish_reason')})"
        )
    if choice.get("finish_reason") == "length":
        raise _cut_off(provider, body, content, payload.get("usage") or {},
                       payload.get("model") or provider.model)
    return payload, content


CUT_OFF_PREVIEW_CHARS = 80


def _cut_off(provider: Provider, body: dict, text: str, usage: dict, model: str) -> TierFailure:
    """Log a reply that hit the token limit, then return its failure.

    The head and tail are logged because no body is kept anywhere else: they are what tells a
    model stuck in a loop from a genuinely long answer.
    """
    usage = usage if isinstance(usage, dict) else {}
    log(
        f"tier {provider.name} cut off at the token limit: "
        f"prompt_tokens={usage.get('prompt_tokens', '?')} "
        f"prompt_chars={_message_chars(body.get('messages') or [])} "
        f"completion_tokens={usage.get('completion_tokens', '?')} chars={len(text)} "
        f"head={text[:CUT_OFF_PREVIEW_CHARS]!r} tail={text[-CUT_OFF_PREVIEW_CHARS:]!r}"
    )
    return TierFailure(f"{provider.name}: reply cut off at the token limit", partial=(model, text))


def record_failure(tier: str, failure: TierFailure) -> None:
    if failure.partial:
        return  # a cut-off depends on the prompt, not on the provider's health
    if failure.retry_at:
        tier_quota_exhausted(tier, failure.retry_at, failure.quota)
        return
    tier_failed(tier, str(failure))
    group = _group_of(tier)
    if group is None or not failure.overloaded:
        return
    if failure.overloaded in ("timeout", "exhausted"):
        # One timeout is ordinary tail latency (a budget sits near the p99), and one unexplained
        # 429 can be one key; two members failing so in a row, with no success between, is the
        # model.
        with _breaker_lock:
            _group_strikes[group] = _group_strikes.get(group, 0) + 1
            if _group_strikes[group] < GROUP_STRIKES_TO_BLOCK:
                return
            _group_strikes[group] = 0
    group_overloaded(group, str(failure))


# Consecutive member timeouts or unexplained 429s, per group, that block the group; any member's
# success resets it.
GROUP_STRIKES_TO_BLOCK = _env_int("OBSERVER_GROUP_STRIKES", 2)
_group_strikes: dict[str, int] = {}


def _group_of(tier: str) -> str | None:
    return next((name for name, members in GROUPS.items() if tier in members), None)


def group_overloaded(group: str, reason: str) -> None:
    """Skip every member of the group for its own break interval (BREAK_SECONDS by default).

    The members are keys for one model, so a model that is out of capacity fails them all; trying
    each in turn only spends every member's budget before the chain moves on. A quota block
    already in place is kept (_skip never shortens one).
    """
    now = time.time()
    note = f"group {group} overloaded ({reason[:120]})"
    with _breaker_lock:
        blocked = [member for member in GROUPS[group]
                   if _skip(member, now + _break_seconds(member), note)]
    if blocked:
        skipped = ",".join(f"{member}({_break_seconds(member):.0f}s)" for member in blocked)
        log(f"group {group} overloaded: skipping {skipped}")


# --- HTTP surface --------------------------------------------------------------


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        return  # access lines would drown the tier log

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # an IPv6 literal, with or without a port
            return True
        name = (host.rpartition(":")[0] if host.count(":") == 1 else host).removesuffix(".")
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return not name or name == "localhost" or name in ALLOWED_HOSTS

    def _refuse(self, status: int, message: str, header: str = "") -> None:
        """Answer an error before the request body is read, then end the connection: an unread
        body would otherwise be parsed as the next request on a kept-alive connection, which lets
        a page that may only post text smuggle in a POST that looks like JSON."""
        self.close_connection = True
        shown = f": {header}={(self.headers.get(header) or '')[:80]!r}" if header else ""
        log(f"refused {status} {self.command} {self.path[:80]}{shown}")
        self._json(status, {"error": message}, close=True)

    def _refuse_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self._refuse(403, "host not allowed", "Host")
        return True

    def _json(self, status: int, payload: dict, close: bool = False) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if close:
                self.send_header("Connection", "close")
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
    def _sse_delta(model: str, text: str, finish_reason: str | None = None) -> str:
        chunk = {
            "id": "chatcmpl-router",
            "object": "chat.completion.chunk",
            "created": _now(),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {},
                         "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def do_GET(self):
        if self._refuse_foreign_host():
            return
        route = self.path.rstrip("/")
        if route == "/health":
            self._json(200, {
                "status": "ok",
                "chain": list(CHAIN),
                "groups": {name: list(GROUPS[name]) for name in CHAIN if name in GROUPS},
                "providers": {
                    name: {"type": PROVIDERS[name].type, "model": PROVIDERS[name].model,
                           "budget_s": PROVIDERS[name].budget,
                           **{key: getattr(PROVIDERS[name], key) for key in _OVERRIDES
                              if getattr(PROVIDERS[name], key)}}
                    for name in chain_providers()
                },
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
        if self._refuse_foreign_host():
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._refuse(404, "not found")
            return
        # A cross-site form or text post cannot declare JSON without a CORS preflight, which the
        # router never answers.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() \
                != "application/json":
            self._refuse(415, "Content-Type must be application/json", "Content-Type")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            self._refuse(400, "invalid Content-Length")
            return
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

        order = expand_chain()  # once per request: every expansion advances the group rotation
        allowed, _ = allowed_tiers(order)
        deadline = time.time() + sum(PROVIDERS[name].budget for name in allowed) + 5.0
        if body.get("stream"):
            self._stream(body, order, deadline)
        else:
            self._once(body, order, deadline)

    # --- streaming path -------------------------------------------------------

    def _stream(self, body: dict, order: list[str], deadline: float) -> None:
        attempted = []
        salvage = None  # (tier, (model, text)) of the first reply cut off at the token limit
        allowed, skipped = allowed_tiers(order)
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier providers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep a local provider out.
            provider = PROVIDERS[tier]
            skip_reason = tier_skip_reason(tier) or busy_reason(provider)
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                generator = iter_stream(provider, fit_prompt(provider, body), deadline)
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
                salvage = salvage or (failure.partial and (tier, failure.partial))
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
                cut_off = bool(getattr(error, "partial", None))
                if not cut_off:
                    tier_failed(tier, f"mid-stream {type(error).__name__}")
                try:
                    if cut_off:  # tell the client, which keeps a cut-off reply's complete blocks
                        self._chunk(self._sse_delta(served, "", "length"))
                    self._chunk("data: [DONE]\n\n")
                    self._end_chunks()
                except Exception as error:
                    log(f"failed to close stream for tier {tier}: {type(error).__name__}")
            return

        if salvage:
            tier, (served, text) = salvage
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                self._chunk(self._sse_delta(served, text))
                self._chunk(self._sse_delta(served, "", "length"))
                self._chunk("data: [DONE]\n\n")
                self._end_chunks()
                log(f"served tier={tier} model={served} cut off, no later tier answered stream=1")
            except Exception as error:
                log(f"failed to send the cut-off reply of tier {tier}: {type(error).__name__}")
            return
        self._json(502, {"error": "all tiers failed", "attempted": attempted})

    # --- non-streaming path ---------------------------------------------------

    def _once(self, body: dict, order: list[str], deadline: float) -> None:
        attempted = []
        salvage = None  # (tier, (model, text)) of the first reply cut off at the token limit
        allowed, skipped = allowed_tiers(order)
        for tier, reason in skipped.items():
            attempted.append(f"{tier}({reason})")
        for tier in allowed:
            # The idle gate is re-read here: earlier providers can take tens of seconds, and a
            # lane that became busy meanwhile must still keep a local provider out.
            provider = PROVIDERS[tier]
            skip_reason = tier_skip_reason(tier) or busy_reason(provider)
            if skip_reason:
                attempted.append(f"{tier}({skip_reason})")
                continue
            started = time.time()
            try:
                payload, content = nonstream(provider, fit_prompt(provider, body), deadline)
            except TierFailure as failure:
                record_failure(tier, failure)
                attempted.append(f"{tier}({failure})")
                log(f"tier {tier} failed: {failure}")
                salvage = salvage or (failure.partial and (tier, failure.partial))
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
        if salvage:
            tier, (served, text) = salvage
            log(f"served tier={tier} model={served} cut off, no later tier answered stream=0")
            self._json(200, {
                "id": "chatcmpl-router",
                "object": "chat.completion",
                "created": _now(),
                "model": served,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "length",
                }],
                "usage": {},
            })
            return
        self._json(502, {"error": "all tiers failed", "attempted": attempted})


def main() -> None:
    if _ENV_ERRORS:
        log(f"environment error: {'; '.join(_ENV_ERRORS)}")
        raise SystemExit(2)
    try:
        config = load_config(CONFIG_PATH)
        configure(config, chain_override(config, os.environ.get("OBSERVER_ROUTER_CHAIN", "")))
    except ConfigError as error:
        log(f"config error: {error}")
        raise SystemExit(2) from None
    for warning in provider_warnings(PROVIDERS):
        log(f"warning: {warning}")
    stale = stale_environment(os.environ)
    if stale:
        log(f"ignoring removed environment variable(s), now config fields: {', '.join(stale)}")
    lanes = watched_lanes()
    log(
        f"starting on {HOST}:{PORT} config={CONFIG_PATH} chain={','.join(CHAIN)} "
        f"max_prompt_chars={MAX_PROMPT_CHARS} max_field_chars={MAX_FIELD_CHARS} "
        f"power={power_source()} idle_gate={','.join(lanes) or 'off'}"
    )
    if lanes:
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
