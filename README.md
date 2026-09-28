# Observer router — an OpenAI-compatible provider chain

OpenAI-compatible router that fronts claude-mem's observer calls, so an observer request
survives a dead local lane. One process, standard library only, on `127.0.0.1:1244`. The chain
is a config file of named provider instances, so it works for any OpenAI-compatible client and
needs nothing from claude-mem.

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

The chain is an ordered list of **provider instances** defined in a TOML file (see
[Configuration](#configuration)). Each instance has one of three types, and several instances
may share a type — two Gemini keys, say, each with its own breaker and quota block:

| Type | Upstream | Default budget to first content | Notes |
| --- | --- | ---: | --- |
| `gemini` | `generativelanguage.googleapis.com/v1beta/openai` | 18 s | Daily-quota 429s block the instance until midnight Pacific |
| `openrouter` | `openrouter.ai/api/v1` | 24 s | Reasoning disabled per request; `X-RateLimit-Reset` blocks the instance |
| `openai` | any OpenAI-compatible `url` | 40 s | Local lanes and bridges; key optional; the GPU-lane gates live here |

The reference deployment runs `gemini` (`gemini-flash-lite-latest`) → `openrouter`
(`openrouter/free`) → `cursor` (`composer-2.5` through a [cursor-api-proxy] bridge on `:8765`, 40
s) → `local` (an MLX/Splash lane, 70 s). Keep the budgets' sum under the client's timeout:
claude-mem's `CLAUDE_MEM_API_TIMEOUT_MS` defaults to 120 s, and that four-instance chain sums to
152 s, so a chain in which every instance hangs to its budget outlives it. Raise the timeout to at
least 160 s for that chain.

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
- **A bridge instance authenticates to the bridge, not to the service behind it.** Point its
  `api_key_file` at the bridge's own env file (for cursor-api-proxy, `CURSOR_BRIDGE_API_KEY`), so
  the secret has one copy. A configured key source that yields no key fails the instance.
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

Every request is sanitized once before provider selection, so every provider receives the same
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

Breakers and quota blocks are kept per instance. Three consecutive failures on an instance skip
it for 60 s (`OBSERVER_BREAK_AFTER`,
`OBSERVER_BREAK_SECONDS`). Every failed attempt is logged with its reason, so a failing tier
is visible in the log without reproduction.

A 429 that states when its quota resets skips the instance until then instead:

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
reported in `/health` as `breaker.<name>.reason`.

## Power rule — GPU lanes run only on AC

A local lane is a GPU workload. With the MacBook unplugged, starting it is a poor trade, so an
`openai` instance with `ac_only = true` is skipped and the failed-chain body names the reason:

```text
{"error": "all tiers failed", "attempted": ["local(skipped: on battery (AC-only))"]}
```

- The instance is skipped whenever `pmset -g batt` reports `Battery Power`. Desktops always
  report AC, so the gate is inert there. Instances without `ac_only` run on battery.
- The probe is cached for `OBSERVER_POWER_CACHE_SECONDS` (30 s) and never runs per request.
- If `pmset` is missing, hung, or unreadable, the source reads `unknown` and the instance
  **stays available** — a failed probe must not take the observer offline.
- `/health` exposes `power.source`, `power.ac_only` (the gated instances), and
  `power.ac_only_allowed`.

Skipping an instance is only safe because claude-mem retries instead of dropping work. On a failed
chain it logs `Observer failed {kind=quota_exhausted}`, enters a provider quota cooldown, and
resumes when the cooldown clears; the requeue path is `resetProcessingToPending()`. One
caveat: in plugin `13.24.8` the queue is in-memory, so that cushion holds only while the
worker process stays up. See
[the retry research](docs/researches/2026-09-20-claude-mem-retry-and-quota-limits.md).

## Idle rule — a GPU lane waits for interactive lanes

A local lane shares the GPU with any interactive lanes on the same machine. Give its instance
`idle_metrics`, a list of those lanes' Prometheus `/metrics` URLs, and it runs only once every
one of them has been idle for `idle_seconds` (30 s). Without `idle_metrics` the gate is off.

- A lane is active while `submitted − completed − cancelled − failed` is above zero, or when
  its submitted counter moved since the previous sample. The second rule catches a request that
  started and finished between samples.
- A background thread samples every `OBSERVER_IDLE_SAMPLE_SECONDS` (2 s), covering the lanes of
  every instance in the active chain. The grace period covers the pause an agent loop takes
  between requests while its tools run.
- The gate is read when the request arrives and again just before the instance runs, because
  the instances ahead of it can take tens of seconds.
- An unreachable or unreadable lane is not activity: it ages into idle, so a stopped lane cannot
  keep the observer offline.
- A skipped instance is named in the failed-chain body, for example
  `local(skipped: lanes busy (127.0.0.1:1240, idle 30s required))`, and claude-mem retries
  after its cooldown, as with the power rule.
- A call already running is not interrupted when a lane becomes active.
- `/health` reports `idle_gate.<name>.seconds_since_active` per lane and
  `idle_gate.<name>.allowed`.

## Configuration

The router reads one TOML file: `OBSERVER_ROUTER_CONFIG`, default
`~/.config/observer-router/config.toml`. Start from [`config.example.toml`](config.example.toml),
which documents every field. A missing or invalid file stops the router at start-up with the
reason in the log (exit status 2); it never starts with a partial chain.

```toml
chain = ["gemini-a", "gemini-b", "local"]

[providers.gemini-a]
type = "gemini"
model = "gemini-flash-lite-latest"
api_key_env = "GEMINI_API_KEY_A"

[providers.gemini-b]
type = "gemini"
model = "gemini-flash-lite-latest"
api_key_file = "~/.config/observer-router/secrets.env"
api_key_var = "GEMINI_API_KEY_B"

[providers.local]
type = "openai"
url = "http://127.0.0.1:1240/v1/chat/completions"
model = "local-model"
serial = true
ac_only = true
```

| Field | Types | Default | Meaning |
| --- | --- | --- | --- |
| `type` | all | required | `gemini`, `openrouter`, or `openai` |
| `model` | all | required | Model id sent upstream; for a local server, the id it advertises at `/v1/models` |
| `url` | all | per type; required for `openai` | Chat-completions URL, `http` or `https` |
| `budget_s` | all | 18 / 24 / 40 | Seconds allowed to reach the first content delta |
| `api_key_env` | all | none | Environment variable holding the key; checked first |
| `api_key_file`, `api_key_var` | all | none | A dotenv file (`NAME=value`), or a JSON object when the path ends `.json`, and the key's name in it; re-read on every request |
| `site_url`, `app_name` | `openrouter` | empty, `observer-router` | `HTTP-Referer` and `X-Title` attribution |
| `reasoning_effort` | `openai` | unset | Sent as-is (`"none"` turns Splash thinking off); leave unset for servers that reject unknown fields |
| `serial` | `openai` | `false` | One request at a time per lane URL |
| `ac_only` | `openai` | `false` | Skip while on battery |
| `idle_metrics`, `idle_seconds` | `openai` | none, `30` | Wait for these lanes to be idle first |

`chain` lists instance names in order; without it the tables' order is used. `gemini` and
`openrouter` instances must name a key source. **Keys never go in the file**: an inline `api_key`
is rejected, as is any field the instance's type does not know, so a typo cannot be ignored
silently. Every value is type-checked at start-up (`serial = "false"` is an error, not true), and
a key that turns out to contain control characters fails its instance without being echoed.
Environment variables from before the config file (`OBSERVER_LOCAL_*`, `OBSERVER_CURSOR_*`, and
the like) are ignored, and start-up logs their names. `config.toml` and `*.env` are git-ignored
in this repository.

Environment overrides (all optional):

| Variable | Default |
| --- | --- |
| `OBSERVER_ROUTER_CONFIG` | `~/.config/observer-router/config.toml` |
| `OBSERVER_ROUTER_CHAIN` | the file's `chain` — comma-separated instance names, e.g. to drop a local lane during a benchmark |
| `OBSERVER_ROUTER_HOST` / `OBSERVER_ROUTER_PORT` | `127.0.0.1` / `1244` |
| `OBSERVER_BREAK_AFTER` / `OBSERVER_BREAK_SECONDS` | `3` / `60` |
| `OBSERVER_POWER_CACHE_SECONDS` | `30` |
| `OBSERVER_IDLE_SAMPLE_SECONDS` | `2` |
| `OBSERVER_MAX_PROMPT_CHARS` / `OBSERVER_MAX_FIELD_CHARS` | `240000` / `80000` |
| `OBSERVER_MIN_RETAINED_CHARS` | `1024` |
| `OBSERVER_BASE64_BLOB_CHARS` | `16384` |

### Consumer contract

To use the router as claude-mem's observer, `~/.claude-mem/settings.json` must point at the
router, not the lane:

```text
CLAUDE_MEM_OPENROUTER_BASE_URL = http://127.0.0.1:1244/v1
CLAUDE_MEM_OPENROUTER_MODEL    = observer-router
```

The model id is an alias; the router rewrites it per instance. claude-mem records the tier's real
model in its own `OpenRouter API usage` lines, so the chain is observable from its log.

## Install

The router is standard-library only, so any Python 3.11+ works. The reference install uses a
dedicated venv so the service cannot be disturbed by unrelated package installs.

1. Copy `com.ezou.observer-router.plist` into `~/Library/LaunchAgents/`.
2. Edit `Label`, both `ProgramArguments` paths, and `PATH` for your machine.
3. Copy `config.example.toml` to `~/.config/observer-router/config.toml` and edit it. A local
   instance's `model` must match the id the server advertises at `/v1/models`, or it answers 404
   and the chain loses its backstop.
4. Point claude-mem at the router, per [Consumer contract](#consumer-contract) above.
5. Load it and check health:

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
| config | `~/.config/observer-router/config.toml` |
| health | `curl -fsS http://127.0.0.1:1244/health` — chain, instances, breaker and quota blocks, prompt guard, power/AC gate, idle gate |
| models | `curl -fsS http://127.0.0.1:1244/v1/models` |

Exercise one instance in isolation by starting a throwaway router whose chain names only it:

```bash
OBSERVER_ROUTER_PORT=1247 OBSERVER_ROUTER_CHAIN=cursor python3 observer-router.py
# then POST to :1247 — the log shows which instance served
```

## Tests

```bash
python3 -m unittest -v test_observer_router.py
```

The suite covers config loading and validation (named instances, type defaults, the chain and
its override, and rejection of inline keys, unknown fields and undefined names), per-instance
request building (key sources, attribution headers, reasoning fields, lane locks), failover
between two instances of one type through the real HTTP handler against fake upstreams, prompt
compaction, the power and idle gates, and quota-reset parsing against the 429 shapes Gemini and
OpenRouter actually return.

## Known limitations

1. **Free remote quotas are too small for daily observer volume.** Both remote tiers have hard
   daily caps, so after they expire the local lane is the only instance left unless another
   instance (a second key, or a bridge) is configured.
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

- This repository owns the router, its launchd plist, the example config, its tests, and its
  documentation.
- An `openai` instance is any OpenAI-compatible endpoint: the router sends the `model` its table
  names, so a different model or server works unchanged.
- claude-mem is third-party and optional. The router reads nothing from it; as a client it needs
  only `CLAUDE_MEM_OPENROUTER_BASE_URL` and `CLAUDE_MEM_OPENROUTER_MODEL` pointed here.
