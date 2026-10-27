"""Start-up: the settings, the config file, the runtime and the server.

Observer router: an OpenAI-compatible fallback chain across named provider instances.

claude-mem speaks OpenAI-compatible HTTP and resolves exactly one provider, so the fallback
chain lives here instead. This process exposes /v1/chat/completions and /v1/models on
127.0.0.1:1244 and rewrites every request for whichever provider runs.

Why it exists (2026-09-18): the local observer lane (:1243) hit a Metal GPU OOM, its generation
thread died while the process kept answering /v1/models, and claude-mem timed out on every call
for ~2 hours. A healthy remote first hop plus a per-provider budget removes that single point of
failure.

Providers come from a TOML config file (OBSERVER_ROUTER_CONFIG, default
~/.config/observer-router/config.toml; see config.example.toml). Each [providers.<name>] table is
one instance of a type -- gemini, openrouter, or openai (any OpenAI-compatible endpoint) -- and
several instances may share a type. The chain is an ordered list of instance names, and the
breaker and quota blocks are kept per instance. Keys are named, never inlined: an environment
variable or a line in a dotenv/JSON file.

Provider rules, measured rather than assumed:
  * openrouter/free rotates across free variants; roughly half of plain calls return EMPTY
    content because a reasoning model spends the whole max_tokens budget on reasoning. We inject
    {"reasoning":{"enabled":false}} and retry once with {"reasoning":{"exclude":true}} when an
    endpoint answers 400 "Reasoning is mandatory for this endpoint and cannot be disabled."
  * A provider that yields no content counts as a failed attempt and the next one runs, so an
    observer call never returns an empty result.
  * Both free types hit daily quotas and say when they reset: Gemini names the quota in
    google.rpc.QuotaFailure (daily quotas reset at midnight Pacific), OpenRouter sends
    X-RateLimit-Reset. Such a 429 skips the instance until the reset rather than re-probing it
    every breaker interval.

Streaming contract: claude-mem sends stream=true. Response headers are only sent once the first
content-bearing delta arrives, which keeps failover possible until that point. Reasoning deltas
are dropped; only content is forwarded downstream.

Prompt contract: requests are sanitized once before provider selection. Image payloads and large
base64 blobs are replaced with markers, individual fields are folded, and older messages are
dropped only when the remaining conversation exceeds the configured character budget. System
messages and the newest conversation context receive priority. This bounds KV allocation without
depending on another model call during an outage.

GPU-lane gates, per openai provider: serial = true runs one request at a time per lane URL;
idle_metrics makes the provider wait until the listed interactive lanes have been idle for
idle_seconds; ac_only = true skips it while the machine runs on battery, so an unplugged laptop
does not start GPU inference as a last resort. claude-mem answers a failed chain with a provider
quota cooldown and retries the same queued batch, so a skipped provider delays observations
instead of dropping them. An unreadable power state leaves the provider available: a failed probe
must not take the observer offline. Desktops report AC Power permanently, making that gate inert
there.
"""

from __future__ import annotations

import os

from observer_router.config import (
    ConfigError,
    chain_override,
    load_config,
    provider_warnings,
    stale_environment,
)
from observer_router.log import log
from observer_router.runtime import Runtime
from observer_router.server import serve
from observer_router.settings import build_settings


def main() -> None:
    settings, errors = build_settings(os.environ)
    if errors:
        log(f"environment error: {'; '.join(errors)}")
        raise SystemExit(2)
    try:
        config = load_config(settings.config_path, settings.min_retained_chars)
        chain = chain_override(config, os.environ.get("OBSERVER_ROUTER_CHAIN", ""))
    except ConfigError as error:
        log(f"config error: {error}")
        raise SystemExit(2) from None
    runtime = Runtime.build(settings, config, chain)
    for warning in provider_warnings(config.providers, settings.max_prompt_chars):
        log(f"warning: {warning}")
    stale = stale_environment(os.environ)
    if stale:
        log(f"ignoring removed environment variable(s), now config fields: {', '.join(stale)}")
    lanes = runtime.watched_lanes()
    log(
        f"starting on {settings.host}:{settings.port} config={settings.config_path} "
        f"chain={','.join(runtime.chain)} max_prompt_chars={settings.max_prompt_chars} "
        f"max_field_chars={settings.max_field_chars} power={runtime.power.source()} "
        f"idle_gate={','.join(lanes) or 'off'}"
    )
    if lanes:
        runtime.start_sampler()
    server = serve(runtime, settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
