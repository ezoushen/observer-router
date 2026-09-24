# Observer router — claude-mem provider chain

OpenAI-compatible router that fronts claude-mem's observer calls, so an observer request
survives a dead local lane. One process, standard library only, on `127.0.0.1:1244`.

## Why this exists

claude-mem resolves exactly one provider and has no cross-provider fallback of its own, so a
dead lane silently breaks observation. The local lane can keep answering `/v1/models` after
its generation thread is gone, which means the process still looks alive to launchd and
`KeepAlive` never fires.

The router removes that single point of failure by putting two remote hops, plus an optional
Cursor bridge, in front of the lane, treating any tier that returns no content as a failed
attempt, and bounding every request before any provider sees it. The remote tiers are
capacity, not guarantees — see [Known limitations](#known-limitations).

Routing belongs here rather than in claude-mem: upstream reviewed a provider chain, declined
it as premature, and shipped OpenRouter-only model fallback that stops at the OpenRouter
boundary. See [the upstream research](docs/researches/2026-09-15-claude-mem-provider-fallback-upstream.md).

## Chain

| Order | Upstream | Model | Budget to first content | Notes |
| ---: | --- | --- | ---: | --- |
| 1 | `generativelanguage.googleapis.com/v1beta/openai` | `gemini-flash-lite-latest` | 18 s | Primary hop; fast when healthy |
| 2 | `openrouter.ai/api/v1` | `openrouter/free` (Free Models Router) | 24 s | Auto-selects among ~28 free variants; `prompt=0 completion=0`, 200k ctx |
| 3 (opt-in) | `127.0.0.1:8765/v1` ([cursor-api-proxy] bridge) | `composer-2.5` | 40 s | Cursor subscription models; remote compute, runs on battery |
| 4 | `127.0.0.1:1243/v1` | your local model id (`OBSERVER_LOCAL_MODEL`) | 70 s | Guaranteed-content backstop; a cache-capped local lane |

The default chain is `gemini,openrouter,local`; the cursor tier joins only when
`OBSERVER_ROUTER_CHAIN` names it, because the bridge is a separate install. Without it the
budgets sum to 112 s, under claude-mem's `CLAUDE_MEM_API_TIMEOUT_MS` (120 s). With it they sum
to 152 s, so a chain in which every tier hangs to its budget outlives that timeout; raise the
timeout to at least 160 s when enabling the cursor tier. While on battery the local tier is
skipped and the remaining budgets sum to 82 s.

[cursor-api-proxy]: https://github.com/anyrobert/cursor-api-proxy

## Tier behaviour

- **`openrouter/free` can return empty content.** It rotates across free variants, and a
  reasoning model can spend the whole `max_tokens` budget on `reasoning`
  (`finish_reason: length`, `content: ""`). The router injects
  `{"reasoning":{"enabled":false}}`.
- **That injection can be refused.** An endpoint may answer
  `HTTP 400 "Reasoning is mandatory for this endpoint and cannot be disabled."`, so the router
  retries the same tier once with `{"reasoning":{"exclude":true}}`.
- **A tier with no content is a failure, not a result.** Streaming or not, an empty answer
  falls through to the next tier, so claude-mem never records an empty observation.
- **Reasoning deltas are dropped.** Only `delta.content` is forwarded downstream.
- **The cursor tier authenticates to the bridge, not to Cursor.** The key is
  `OBSERVER_CURSOR_API_KEY`, or else the `CURSOR_BRIDGE_API_KEY` line of the bridge's own env
  file named by `OBSERVER_CURSOR_ENV_FILE`, so the secret has one copy. No key fails the tier.
- **An observer skip looks like a failure.** claude-mem's prompt asks for an empty response
  when a tool call is routine. A tier that obeys returns empty content, so the next tier runs
  and may record what the first one skipped.

## Streaming contract

claude-mem sends `stream=true`, so the router speaks SSE with chunked transfer encoding.
Response headers are deliberately withheld until the **first content-bearing delta** arrives;
that keeps failover possible right up to the moment the stream commits. Once committed, a
mid-stream failure cannot switch tiers — it is logged and the stream is closed with
`data: [DONE]`.

## Prompt safety and context folding

Every request is sanitized once before tier selection, so all three tiers receive the same
bounded conversation:

- OpenAI image parts and `data:image/...;base64,...` payloads become descriptive markers.
- Contiguous base64-like blobs of at least 16,384 characters become markers.
- Any remaining field over 80,000 characters keeps its beginning and end around a folding
  marker.
- If the conversation still exceeds 240,000 characters (roughly 60k tokens), the primary
  system message and newest non-system message receive first claim on the budget, followed by
  remaining system messages and recent history. Older messages are dropped last.

This is deterministic: compaction never calls another model, so it still works when every
upstream quota is exhausted. `/health` reports cumulative compaction counters and the active
limits. A log entry records input/output characters, dropped messages, folded fields, images,
and binary blobs for each changed request.

The guard exists because an unbounded prompt is what actually killed the lane — not a leak.
See [the diagnosis](docs/researches/2026-09-19-observer-lane-oom-diagnosis.md).

## Circuit breaker

Three consecutive failures on a tier skip it for 60 s (`OBSERVER_BREAK_AFTER`,
`OBSERVER_BREAK_SECONDS`). Every failed attempt is logged with its reason, so a failing tier
is visible in the log without reproduction.

A 429 that states when its quota resets skips the tier until then instead:

- **Gemini** names the exhausted quotas in `google.rpc.QuotaFailure`. Any `PerDay` quota
  blocks the tier until the next midnight Pacific, when Gemini's daily quotas reset.
  Otherwise the tier waits for `google.rpc.RetryInfo.retryDelay`, which Google sends even for a
  daily quota and so is only trusted without one.
- **OpenRouter** sends `X-RateLimit-Reset` in epoch milliseconds, in the headers and again in
  the body's `error.metadata`. The free tier's daily limit resets at 00:00 UTC.
- A reset that is unreadable, already past, or more than 26 hours out falls back to the
  ordinary breaker, so a malformed header cannot take a tier offline for days.
- A shorter breaker skip never cuts a quota block short, and the next success clears it.

The block is logged once with its reset time, named in the failed-chain `attempted` array, and
reported in `/health` as `breaker.<tier>.reason`.

## Power rule — the local tier runs only on AC

The local tier is a GPU workload. With the MacBook unplugged, starting the lane is a poor
trade, so the router skips it and names the reason in its failed-chain body:

```text
{"error": "all tiers failed", "attempted": ["local(skipped: on battery (AC-only))"]}
```

- `OBSERVER_LOCAL_AC_ONLY=1` (default) skips the local tier whenever `pmset -g batt` reports
  `Battery Power`. Desktops always report AC, so the gate is inert there.
- The probe is cached for `OBSERVER_POWER_CACHE_SECONDS` (30 s) and never runs per request.
- If `pmset` is missing, hung, or unreadable, the source reads `unknown` and the local tier
  **stays available** — a failed probe must not take the observer offline.
- `/health` exposes `power.source`, `power.local_ac_only`, and `power.local_tier_allowed`.

Skipping a tier is only safe because claude-mem retries instead of dropping work. On a failed
chain it logs `Observer failed {kind=quota_exhausted}`, enters a provider quota cooldown, and
resumes when the cooldown clears; the requeue path is `resetProcessingToPending()`. One
caveat: in plugin `13.24.8` the queue is in-memory, so that cushion holds only while the
worker process stays up. See
[the retry research](docs/researches/2026-09-20-claude-mem-retry-and-quota-limits.md).

## Idle rule — the local tier waits for interactive lanes

The local lane shares the GPU with any interactive lanes on the same machine. Set
`OBSERVER_LOCAL_IDLE_METRICS` to those lanes' Prometheus `/metrics` URLs, comma-separated, and
the local tier runs only once every one of them has been idle for
`OBSERVER_LOCAL_IDLE_SECONDS` (30 s). Unset, the gate is off.

- A lane is active while `submitted − completed − cancelled − failed` is above zero, or when
  its submitted counter moved since the previous sample. The second rule catches a request that
  started and finished between samples.
- A background thread samples every `OBSERVER_IDLE_SAMPLE_SECONDS` (2 s). The grace period
  covers the pause an agent loop takes between requests while its tools run.
- The gate is read when the request arrives and again just before the local tier runs, because
  the tiers ahead of it can take tens of seconds.
- An unreachable or unreadable lane is not activity: it ages into idle, so a stopped lane cannot
  keep the observer offline.
- A skipped tier is named in the failed-chain body, for example
  `local(skipped: lanes busy (127.0.0.1:1240, idle 30s required))`, and claude-mem retries
  after its cooldown, as with the power rule.
- A local call already running is not interrupted when a lane becomes active.
- `/health` reports `idle_gate.seconds_since_active` per lane and `idle_gate.local_tier_allowed`.

## Configuration

Read from claude-mem's settings at startup and re-read whenever the file changes, so keys
edited in the claude-mem console take effect without restarting the router:

- `CLAUDE_MEM_GEMINI_API_KEY` — required for tier 1
- `CLAUDE_MEM_OPENROUTER_API_KEY` — required for tier 2 (the free router still authenticates)
- `CLAUDE_MEM_OPENROUTER_SITE_URL` / `CLAUDE_MEM_OPENROUTER_APP_NAME` — optional OpenRouter
  attribution headers

Overrides (all optional):

| Variable | Default |
| --- | --- |
| `OBSERVER_ROUTER_HOST` / `OBSERVER_ROUTER_PORT` | `127.0.0.1` / `1244` |
| `OBSERVER_ROUTER_CHAIN` | `gemini,openrouter,local` |
| `OBSERVER_GEMINI_MODEL` | `gemini-flash-lite-latest` |
| `OBSERVER_OPENROUTER_MODEL` | `openrouter/free` |
| `OBSERVER_CURSOR_URL` | `http://127.0.0.1:8765/v1/chat/completions` |
| `OBSERVER_CURSOR_MODEL` | `composer-2.5` |
| `OBSERVER_CURSOR_API_KEY` | empty — the bridge's `CURSOR_BRIDGE_API_KEY` |
| `OBSERVER_CURSOR_ENV_FILE` | empty — env file read for `CURSOR_BRIDGE_API_KEY` |
| `OBSERVER_LOCAL_MODEL` | `local-model` — set this to your local server's advertised id |
| `OBSERVER_LOCAL_URL` | `http://127.0.0.1:1243/v1/chat/completions` |
| `OBSERVER_BREAK_AFTER` / `OBSERVER_BREAK_SECONDS` | `3` / `60` |
| `OBSERVER_LOCAL_AC_ONLY` | `1` — skip the local tier while on battery |
| `OBSERVER_POWER_CACHE_SECONDS` | `30` |
| `OBSERVER_LOCAL_IDLE_METRICS` | empty — gate off; comma-separated `/metrics` URLs of lanes to wait for |
| `OBSERVER_LOCAL_IDLE_SECONDS` / `OBSERVER_IDLE_SAMPLE_SECONDS` | `30` / `2` |
| `OBSERVER_MAX_PROMPT_CHARS` / `OBSERVER_MAX_FIELD_CHARS` | `240000` / `80000` |
| `OBSERVER_MIN_RETAINED_CHARS` | `1024` |
| `OBSERVER_BASE64_BLOB_CHARS` | `16384` |
| `CLAUDE_MEM_SETTINGS` | `~/.claude-mem/settings.json` |

### Consumer contract

`~/.claude-mem/settings.json` must point at the router, not the lane:

```text
CLAUDE_MEM_OPENROUTER_BASE_URL = http://127.0.0.1:1244/v1
CLAUDE_MEM_OPENROUTER_MODEL    = observer-router
```

The model id is an alias; the router rewrites it per tier. claude-mem records the tier's real
model in its own `OpenRouter API usage` lines, so the chain is observable from its log.

## Install

The router is standard-library only, so any Python 3.11+ works. The reference install uses a
dedicated venv so the service cannot be disturbed by unrelated package installs.

1. Copy `com.ezou.observer-router.plist` into `~/Library/LaunchAgents/`.
2. Edit `Label`, both `ProgramArguments` paths, and `PATH` for your machine. Set
   `OBSERVER_LOCAL_MODEL` here as well: it must match the id your local server advertises at
   `/v1/models`, or that tier answers 404 and the chain loses its backstop.
3. Point claude-mem at the router, per [Consumer contract](#consumer-contract) above.
4. Load it and check health:

```bash
launchctl bootout gui/$UID/com.ezou.observer-router   # "no such process" is fine on first install
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ezou.observer-router.plist
curl -fsS http://127.0.0.1:1244/health
```

Restart with `bootout`/`bootstrap`, never `launchctl stop`: stopping leaves the service
unloaded, and `KeepAlive` cannot bring back a job that is no longer registered.

## Operations

| Thing | Value |
| --- | --- |
| launchd label | `com.ezou.observer-router` |
| plist | `~/Library/LaunchAgents/com.ezou.observer-router.plist` (copy kept here) |
| log | `/tmp/observer-router.log` |
| health | `curl -fsS http://127.0.0.1:1244/health` — chain, budgets, breaker and quota blocks, prompt guard, power/AC gate, idle gate |
| models | `curl -fsS http://127.0.0.1:1244/v1/models` |

Exercise one tier in isolation by starting a throwaway instance with the earlier tiers broken:

```bash
OBSERVER_ROUTER_PORT=1247 OBSERVER_GEMINI_MODEL=definitely-not-a-model \
OBSERVER_OPENROUTER_MODEL=bogus/model-not-real \
OBSERVER_LOCAL_MODEL=<your local model id> \
  python3 observer-router.py
# then POST to :1247 — the log shows which tier ended up serving
```

## Tests

```bash
python3 -m unittest -v test_observer_router.py
```

The suite covers prompt compaction — unchanged small messages, image/base64 stripping, and
bounded history that keeps the system message plus the newest context — the power gate,
including that an unreadable power source keeps the local tier available, and the cursor tier's
key resolution, payload, and exemption from the power gate and lane lock — and quota-reset
parsing for both remote tiers, against the 429 shapes they actually return — and the idle gate:
in-flight parsing, the grace period, activity between samples, and unreadable lanes.

## Known limitations

1. **Free remote quotas are too small for daily observer volume.** Both remote tiers have hard
   daily caps, so after they expire the local lane is the only tier left unless the cursor
   tier is enabled.
2. **A hung-but-alive local lane still defeats launchd `KeepAlive`.** The prompt guard blocks
   the oversized-prompt trigger, but an unrelated MLX generation-thread failure would still
   need `launchctl kickstart -k`. Router-triggered restart is unimplemented.
3. **The byte cache cap is not enforced in mlx_lm's streaming insertion path.** Sequence count
   is the effective production bound on the local lane.
4. **Cosmetic log noise.** claude-mem client disconnects raise `BrokenPipeError` and
   `ConnectionResetError` tracebacks in the router log. They are harmless but loud.

## Documentation

| Where | What |
| --- | --- |
| [`docs/researches/`](docs/researches/) | Investigations and measurements behind these rules, including dead ends |
| [`docs/plans/`](docs/plans/) | Durable plans awaiting an owner decision or execution |
| [`docs/journals/`](docs/journals/) | Dated work log, append-only |
| [`AGENTS.md`](AGENTS.md) | Conventions for coding agents, plus the open owner decisions |
| [`CHANGELOG.md`](CHANGELOG.md) | Notable changes to the repository |

## Scope

- This repository owns the router, its launchd plist, its tests, and its documentation.
- The local tier is any OpenAI-compatible endpoint: the router sends the id
  `OBSERVER_LOCAL_MODEL` names, so a different model or server works unchanged. The reference
  lane is a cache-capped MLX server serving a 4B observer model on `:1243`.
- claude-mem is third-party. The router needs only two of its settings,
  `CLAUDE_MEM_OPENROUTER_BASE_URL` and `CLAUDE_MEM_OPENROUTER_MODEL`.
