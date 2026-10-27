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
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from observer_router.config import (
    OVERRIDE_FIELDS,
    Config,
    ConfigError,
    Provider,
    chain_override,
    load_config,
    parse_config,
    provider_warnings,
    stale_environment,
)
from observer_router.breakers import Breakers, quota_reset
from observer_router.idle import (
    IdleMonitor,
    LaneLocks,
    read_lane,
)
from observer_router.failure import TierFailure
from observer_router.idle import parse_lane_metrics as _parse_lane_metrics
from observer_router.log import log
from observer_router.power import PowerGate, probe_power
from observer_router.providers import api_key, headers_for, payload_for
from observer_router.prompt import PromptGuard, PromptLimits
from observer_router.prompt import message_chars as _message_chars_of
from observer_router.settings import build_settings
from observer_router.upstream import iter_stream, nonstream


LANES = LaneLocks()


def lane_guard(provider):
    """Hold the provider's lane for the duration of a call when it is serial."""
    return LANES.guard(provider)


# Invalid OBSERVER_* settings are collected by build_settings and reported by main() before it
# starts: like a bad config file, a bad setting stops the router instead of silently using the
# default. These aliases keep the rest of this module unchanged until the split moves them into
# the runtime object.
SETTINGS, _ENV_ERRORS = build_settings(os.environ)
HOST = SETTINGS.host


def _now() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


PORT = SETTINGS.port
# DNS names, besides localhost, that may appear in a request's Host header. A web page reaches the
# router under a name an attacker's DNS first points here (rebinding); an IP literal cannot be
# rebound, so those are always accepted and LAN access by address keeps working.
ALLOWED_HOSTS = SETTINGS.allowed_hosts

# Circuit breaker: a tier that fails repeatedly is skipped briefly instead of
# consuming the budget on every call (free tiers rate-limit aggressively).
BREAK_AFTER = SETTINGS.break_after
BREAK_SECONDS = SETTINGS.break_seconds

# The lane's native context is 262,144 tokens, but accepting that many tokens would leave
# no memory headroom for concurrent GPU clients. Character budgets are conservative token
# estimates; the router folds deterministically rather than recursively asking an LLM to
# summarize an already-oversized request.
MAX_PROMPT_CHARS = SETTINGS.max_prompt_chars
MAX_FIELD_CHARS = SETTINGS.max_field_chars
MIN_RETAINED_CHARS = SETTINGS.min_retained_chars
BASE64_BLOB_CHARS = SETTINGS.base64_blob_chars


# --- power source -------------------------------------------------------------

POWER = PowerGate(probe_power, SETTINGS.power_cache_seconds, log)


def power_source() -> str:
    """Return "AC Power", "Battery Power", or "unknown", cached briefly."""
    return POWER.source()


# --- idle gate ----------------------------------------------------------------

IDLE = IdleMonitor(read_lane, SETTINGS.idle_sample_seconds)


def parse_lane_metrics(text: str, signal: dict | None = None) -> tuple[int | None, int] | None:
    return _parse_lane_metrics(text, signal)


def record_lane_sample(url: str, sample: tuple[int | None, int] | None, now: float) -> None:
    IDLE.record(url, sample, now)


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
    IDLE.sample_once(((url, lane_signal(url)) for url in watched_lanes()))


def busy_lanes(provider, now: float | None = None) -> list[str]:
    """Return the provider's watched lanes active within its last idle_seconds."""
    return IDLE.busy(provider, now)


def busy_reason(provider) -> str | None:
    return IDLE.reason(provider)


def idle_snapshot() -> dict:
    return IDLE.snapshot(PROVIDERS, chain_providers())


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
    return POWER.snapshot([name for name in chain_providers() if PROVIDERS[name].ac_only])


# --- config -------------------------------------------------------------------

CONFIG_PATH = SETTINGS.config_path
# The active configuration. main() fills these; they stay empty until then so importing the
# module (the tests do) never reads a config file.
PROVIDERS: dict[str, Provider] = {}
GROUPS: dict[str, tuple[str, ...]] = {}
CHAIN: tuple[str, ...] = ()
_rotation_lock = threading.Lock()
_rotation: dict[str, int] = {}


def configure(config: Config, chain: tuple[str, ...] | None = None) -> None:
    global PROVIDERS, GROUPS, CHAIN, BREAKERS
    PROVIDERS = dict(config.providers)
    GROUPS = dict(config.groups)
    CHAIN = tuple(chain or config.chain)
    with _rotation_lock:
        _rotation.clear()
    BREAKERS = Breakers(GROUPS, PROVIDERS, BREAK_AFTER, BREAK_SECONDS,
                        GROUP_STRIKES_TO_BLOCK, log)


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

GUARD = PromptGuard(
    PromptLimits(SETTINGS.max_prompt_chars, SETTINGS.max_field_chars,
                 SETTINGS.min_retained_chars, SETTINGS.base64_blob_chars),
    log,
)


def _message_chars(messages: list[dict]) -> int:
    return _message_chars_of(messages)


def compact_messages(messages: list[dict], limit: int | None = None) -> tuple[list[dict], dict]:
    return GUARD.compact(messages, limit)


def fit_prompt(provider: Provider, body: dict) -> dict:
    return GUARD.fit(provider, body)


def record_compaction(stats: dict) -> None:
    GUARD.record(stats)


def compaction_snapshot() -> dict:
    return GUARD.snapshot()


# --- breakers and quota blocks ------------------------------------------------

BREAKERS = Breakers({}, {}, BREAK_AFTER, BREAK_SECONDS, SETTINGS.group_strikes, log)


def tier_skip_reason(name: str) -> str | None:
    """Return why the tier is skipped right now, or None when it may run."""
    return BREAKERS.skip_reason(name)


def tier_available(name: str) -> bool:
    return BREAKERS.available(name)


def tier_ok(name: str) -> None:
    BREAKERS.ok(name)


def tier_failed(name: str, reason: str) -> None:
    BREAKERS.failed(name, reason)


def tier_quota_exhausted(name: str, until: float, quota: str) -> None:
    BREAKERS.quota_exhausted(name, until, quota)


def breaker_snapshot() -> dict:
    return BREAKERS.snapshot(chain_providers())


# --- tier requests ------------------------------------------------------------

def record_failure(tier: str, failure: TierFailure) -> None:
    if failure.partial:
        return  # a cut-off depends on the prompt, not on the provider's health
    if failure.retry_at:
        tier_quota_exhausted(tier, failure.retry_at, failure.quota)
        return
    tier_failed(tier, str(failure))
    group = BREAKERS.group_of(tier)
    if group is None or not failure.overloaded:
        return
    if failure.overloaded in ("timeout", "exhausted"):
        # One timeout is ordinary tail latency (a budget sits near the p99), and one unexplained
        # 429 can be one key; two members failing so in a row, with no success between, is the
        # model.
        if not BREAKERS.strike(group):
            return
    BREAKERS.group_overloaded(group, str(failure))


# Consecutive member timeouts or unexplained 429s, per group, that block the group; any member's
# success resets it.
GROUP_STRIKES_TO_BLOCK = SETTINGS.group_strikes


def _group_of(tier: str) -> str | None:
    return BREAKERS.group_of(tier)


def group_overloaded(group: str, reason: str) -> None:
    BREAKERS.group_overloaded(group, reason)


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
                           **{key: getattr(PROVIDERS[name], key) for key in OVERRIDE_FIELDS
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
                generator = iter_stream(provider, fit_prompt(provider, body), deadline, LANES)
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
                payload, content = nonstream(provider, fit_prompt(provider, body), deadline, LANES)
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
    for warning in provider_warnings(PROVIDERS, MAX_PROMPT_CHARS):
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
        IDLE.start(lambda: ((url, lane_signal(url)) for url in watched_lanes()))
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
