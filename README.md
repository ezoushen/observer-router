# Observer router — claude-mem provider chain

OpenAI-compatible router that fronts claude-mem's observer calls, so an observer request
survives a dead local lane. One process, standard library only, on `127.0.0.1:1244`.

## Why this exists

claude-mem resolves exactly one provider and has no cross-provider fallback in its own code
(its `fallback` strings are the cmem.ai Pro tier and the chroma search strategy). On
2026-09-18 the local observer lane `:1243` hit a Metal GPU OOM that killed its generation
thread while the HTTP layer kept answering `/v1/models`, so the process stayed "alive" to
launchd, `KeepAlive` never fired, and every observer call timed out for ~2 hours — 84
consecutive failures and no memories saved.

The router removes that single point of failure by putting two remote hops in front of the
lane, treating any tier that returns no content as a failed attempt, and bounding every
request before any provider sees it. The remote tiers are capacity, not guarantees: both
free quotas were exhausted under real observer traffic on 2026-09-19.

## Chain

| Order | Upstream | Model | Budget to first content | Notes |
| ---: | --- | --- | ---: | --- |
| 1 | `generativelanguage.googleapis.com/v1beta/openai` | `gemini-flash-lite-latest` | 18 s | Primary hop. Verified 200 in under 1 s when healthy |
| 2 | `openrouter.ai/api/v1` | `openrouter/free` (Free Models Router) | 24 s | Auto-selects among ~28 free variants; `prompt=0 completion=0`, 200k ctx |
| 3 | `127.0.0.1:1243/v1` | your local model id (`OBSERVER_LOCAL_MODEL`) | 70 s | Guaranteed-content backstop; a cache-capped local lane |

Budgets sum to 112 s, under claude-mem's `CLAUDE_MEM_API_TIMEOUT_MS` (120 s).

## Tier rules — measured, not assumed

- **`openrouter/free` returns empty content about half the time.** It rotates across free
  variants, and a reasoning model spends the whole `max_tokens` budget on `reasoning`
  (`finish_reason: length`, `content: ""`). The router injects
  `{"reasoning":{"enabled":false}}`, which restored `content="OK", finish_reason=stop` in
  testing.
- **That injection can be refused.** One endpoint answered
  `HTTP 400 "Reasoning is mandatory for this endpoint and cannot be disabled."`, so the
  router retries the same tier once with `{"reasoning":{"exclude":true}}`.
- **A tier with no content is a failure, not a result.** Either streaming or non-streaming,
  an empty answer falls through to the next tier, so claude-mem never records an empty
  observation.
- **Reasoning deltas are dropped.** Only `delta.content` is forwarded downstream.

## Streaming contract

claude-mem sends `stream=true`, so the router speaks SSE with chunked transfer encoding.
Response headers are deliberately withheld until the **first content-bearing delta** arrives;
that keeps failover possible right up to the moment the stream commits. Once committed, a
mid-stream failure cannot switch tiers — it is logged and the stream is closed with
`data: [DONE]`.

## Prompt safety and context folding

Every request is sanitized once before tier selection, so Gemini, OpenRouter, and the local
lane receive the same bounded conversation:

- OpenAI image parts and `data:image/...;base64,...` payloads become descriptive markers.
- Contiguous base64-like blobs of at least 16,384 characters become markers.
- Any remaining field over 80,000 characters keeps its beginning and end around a folding
  marker.
- If the conversation still exceeds 240,000 characters (roughly 60k tokens), the primary
  system message and newest non-system message receive first claim on the budget, followed
  by remaining system messages and recent history. Older messages are dropped last.

This is deterministic: compaction never calls another model, so it still works when every
upstream quota is exhausted. `/health` reports cumulative compaction counters and the active
limits. A log entry records input/output characters, dropped messages, folded fields, images,
and binary blobs for each changed request.

## Circuit breaker

Three consecutive failures on a tier skip it for 60 s (`OBSERVER_BREAK_AFTER`,
`OBSERVER_BREAK_SECONDS`). Every failed attempt is logged with its reason, which is how the
2026-09-18 lane outage was caught within minutes.

## Power rule — the local tier runs only on AC

The third tier is a GPU workload. With the MacBook unplugged, starting the 4B lane is a poor
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

Skipping a tier is only safe because claude-mem retries instead of dropping work, and that
behavior is verified rather than assumed. On a failed chain it logs
`Observer failed {kind=quota_exhausted}` and enters a provider quota cooldown, then repeats
`Skipping generator start while the provider quota cooldown is active {retryInMs=…}` until a
retry succeeds; the queued batch survives the whole interval. On 2026-09-19 it recorded 75
such `502` events and still stored observations afterwards. The requeue path is
`resetProcessingToPending()`, which fires on quota-limit prose, auth failure, and context
overflow.

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
| `OBSERVER_LOCAL_MODEL` / `OBSERVER_LOCAL_URL` | the 4B observer path / `http://127.0.0.1:1243/v1/chat/completions` |
| `OBSERVER_BREAK_AFTER` / `OBSERVER_BREAK_SECONDS` | `3` / `60` |
| `OBSERVER_LOCAL_AC_ONLY` | `1` — skip the local tier while on battery |
| `OBSERVER_POWER_CACHE_SECONDS` | `30` |
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
3. Point claude-mem at the router (see [Consumer contract](#consumer-contract) above).
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
| health | `curl -fsS http://127.0.0.1:1244/health` — chain, budgets, breaker, prompt guard, power/AC gate |
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
bounded history that keeps the system message plus the newest context — and the power gate,
including that an unreadable power source keeps the local tier available.

## Verification — 2026-09-18

| Check | Result |
| --- | --- |
| Tier 1, non-streaming and streaming | 200, `gemini-flash-lite-latest`, 0.78–2.2 s |
| Tier 2 failover (tier 1 broken) | 200, `inclusionai/ling-3.0-flash-vl:free`, 1.45 s |
| Tier 2 streaming failover | SSE intact, content streamed from `dots-studio/dots-3-note-preview:free` |
| Tier 3 failover (tiers 1+2 broken) | 200, local Qwen, 0.22 s, streaming and non-streaming |
| End-to-end via claude-mem | `STORED` memories, `consecutiveFailures: 0` |
| Production tier mix | 15 served by Gemini, 9 by `openrouter/free` |
| Gemini failure reasons in production | 2 × HTTP 503 (overloaded), 2 × 18 s read timeout |

The ~37% Gemini fallback rate is the reason tier 2 exists — the primary hop is fast when
healthy but not always available.

## Verification — 2026-09-19 prompt guard and YaRN experiment

| Check | Result |
| --- | --- |
| Unit contract | 3/3 pass: unchanged small messages, image/base64 stripping, bounded history preserving system + newest context |
| Production-sized failure replay | 3,250,938 input chars → 97 chars; one binary blob removed; HTTP 200 streaming in 2.04 s |
| Local lane after replay | 200 in 0.21 s; no generation-thread failure |
| Factor-2 YaRN model view | symlinked weights, independent config with 524,288-token target |
| MLX YaRN smoke test | `:1245` returned `YARN_OK` in 1.62 s and identified the YaRN model path |

### Why YaRN does not replace this guard

The local model can advertise a longer context than the router forwards, but YaRN changes
positional encoding, not KV memory: a longer *valid* window does not make the KV allocation
fit in RAM. A factor-2 YaRN config (`rope_type: yarn`, `factor: 2.0`,
`original_max_position_embeddings` 262144, 524288 target) was validated in the companion
runtime project and loaded successfully — but that demonstrates config compatibility only.
It does not show that a 524k-token request is practical, and static YaRN is known to reduce
short-context quality. The deterministic character guard described above is what actually
keeps one request inside the lane's memory budget, and it keeps working when every upstream
quota is exhausted.

## Known issues and follow-ups

1. **Free remote quotas are too small for daily observer volume.** On 2026-09-19 Gemini had
   served 471 calls before returning `exceeded your current quota`; OpenRouter returned
   `free-models-per-day`. The local lane becomes the only tier after both limits expire.
2. **A hung-but-alive local lane still defeats launchd `KeepAlive`.** The prompt guard blocks
   the observed multi-megabyte trigger, but an unrelated MLX generation-thread failure would
   still require `launchctl kickstart -k`. Router-triggered restart remains unimplemented.
3. **The byte cache cap is not enforced in mlx_lm's streaming insertion path.** Sequence
   count 4 is the effective production bound. The prompt guard prevents a single retained
   request from approaching the model's 262,144-token native window.

## Scope

- This repository owns the router, its launchd plist, its tests, and its journal.
- The local tier is any OpenAI-compatible endpoint: the router sends the id
  `OBSERVER_LOCAL_MODEL` names, so a different model or server works unchanged. The
  reference lane is a cache-capped MLX server serving a 4B observer model on `:1243`.
- claude-mem is third-party. The router needs only two of its settings,
  `CLAUDE_MEM_OPENROUTER_BASE_URL` and `CLAUDE_MEM_OPENROUTER_MODEL`.
- The router was extracted from a larger personal workspace. Journal entries were written
there and are append-only, so a few reference sibling paths that are not part of this
repository.
