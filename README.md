# Observer router — an OpenAI-compatible provider chain

One OpenAI-compatible endpoint in front of any providers you choose. You name the provider
instances, put them in the order to try, and the router answers each request from the first one
that returns usable content. It was built for claude-mem's observer calls, but any
OpenAI-compatible client works. One process, standard library only, listening on
`127.0.0.1:1244` by default.

## Why this exists

claude-mem resolves exactly one provider and has no cross-provider fallback of its own, so when
that provider fails, observation silently stops. A local lane can even keep answering
`/v1/models` after its generation thread is gone, so the process still looks alive to launchd.

The router removes that single point of failure. Point the client at the router, and put as many
providers behind it as you like: hosted APIs, several keys for one API, bridges, local lanes. A
provider that errors, times out, returns no content, or stops at the token limit is a failed
attempt, and the next one answers. Every request is bounded before any provider sees it.

Routing belongs here rather than in claude-mem: upstream reviewed a provider chain, declined
it as premature, and shipped OpenRouter-only model fallback that stops at the OpenRouter
boundary. See [the upstream research](docs/researches/2026-09-15-claude-mem-provider-fallback-upstream.md).

## Build your chain

Everything about the chain lives in one TOML file (see [Configuration](#configuration) for every
field):

- **Providers.** Each `[providers.<name>]` table is one instance: a type, a model, where its key
  lives, and how long it may take. Define as many as you want, several of one type included —
  three Gemini keys are three instances, each with its own breaker and quota block.
- **Order.** `chain` lists provider names in the order they are tried. Reorder it, drop an
  entry, or add one; nothing else changes. `OBSERVER_ROUTER_CHAIN` overrides it for one run
  without editing the file.
- **Groups.** A chain entry may name a `[groups.<name>]` of providers instead. A group rotates:
  each request starts at the next member and the rest follow, so load spreads across several keys
  and a blocked member still falls over to its siblings before the chain moves on.
- **Per-provider behaviour.** Budgets, request fields (`reasoning_effort`,
  `chat_template_kwargs`), stream buffering, and the GPU-lane gates (`serial`, `ac_only`,
  `idle_metrics`) are set per instance, so one chain can mix fast hosted tiers with a slow local
  lane.
- **Router-wide policy.** Breaker thresholds, the prompt guard, and sampling intervals are
  environment variables with defaults; see the table under [Configuration](#configuration).

Three provider types cover the upstreams:

| Type | Upstream | Default budget | What the type adds |
| --- | --- | ---: | --- |
| `gemini` | `generativelanguage.googleapis.com/v1beta/openai` | 18 s | Reads Gemini's quota errors and blocks a key until its quota resets |
| `openrouter` | `openrouter.ai/api/v1` | 24 s | Disables reasoning per request; reads `X-RateLimit-Reset`; attribution headers |
| `openai` | any OpenAI-compatible `url` | 40 s | Local lanes and bridges; key optional; the GPU-lane gates and request fields |

For example, a pool of keys first, then a paid fallback, then a local lane as the backstop:

```toml
chain = ["keys", "paid", "local"]

[groups.keys]
members = ["gemini-a", "gemini-b"]
```

or a local lane first, with hosted capacity only when it is busy or down:

```toml
chain = ["local", "gemini-a", "openrouter"]
```

**Size the budgets to the client's deadline.** The router waits up to each instance's `budget_s`
in turn, so the budgets of the instances that can fail before the one that answers add up. For
claude-mem (15.2.1) the per-attempt deadline is `CLAUDE_MEM_LLM_TIMEOUT_MS`, 180 s by default and
at most 300 s. A streamed call is bound tighter by `CLAUDE_MEM_LLM_STREAM_IDLE_TIMEOUT_MS`, 90 s
by default, which runs from the moment the request is sent until the response headers arrive,
and again between reads. The router withholds its headers until it commits, so every attempt
before the one that answers counts against those 90 s: an answer that comes later than that
reaches no one. `CLAUDE_MEM_API_TIMEOUT_MS` is unrelated; it bounds calls to claude-mem's own
worker API. claude-mem asks for `max_tokens` 4096 (`CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS`) at
temperature 0.3, and the router forwards both.

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
- **A reply cut off at the token limit is a failure too.** `finish_reason: length` means the text
  stopped mid-thought, or a model looped until its cap. Non-streaming, the next tier answers. A
  stream can only fail over from an instance with `buffer_stream = true` (see the streaming
  contract). When no later tier answers, the first cut-off reply is served after all, with
  `finish_reason: length`: claude-mem keeps the complete blocks of a cut-off reply, so part of an
  answer beats a 502. Either way one log line, `tier <name> cut off at the token limit: ...`,
  records `prompt_tokens`, `prompt_chars`, `completion_tokens`, and the first and last 80
  characters, so a loop can be told from a genuinely long answer; those characters are model
  output, kept in the service log. A cut-off depends on the prompt, not the provider's health, so
  it does not count toward the breaker: a prompt that makes a model loop costs that tier's attempt
  each time it is sent, but cannot block the tier for other prompts. An unbuffered stream that was
  cut off ends with a `finish_reason: length` frame, so the client knows. Raising the server's
  token cap is not the fix: a loop only runs longer.
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
that keeps failover possible right up to the moment the stream commits. An upstream that sends its
own headers and then stalls past its budget is a failed attempt, like one that never answers.
Once committed, a mid-stream failure cannot switch tiers — it is logged and the stream is closed
with `data: [DONE]`.

An instance with `buffer_stream = true` commits only when its upstream stream has ended. Its text
is held back until `finish_reason` shows the reply is whole, so a reply cut off at the token
limit, a stream that ends without a `finish_reason`, a read error at any point, or a reply still
unfinished when its `budget_s` runs out, still fails over. The budget is a hard bound on the whole
reply, from the moment the request is sent, so it also frees a `serial` lane that would otherwise
keep generating for a client that has given up; size it from that client's 90 s idle timeout, not
from prefill time alone. The cost is latency: claude-mem receives its first byte only once the
whole reply is generated, and that must happen inside its 90 s stream idle timeout. Turn it on for
a small local model with a hard output cap, where a cut-off is common and a full reply takes
seconds; leave it off for fast hosted tiers. Without it, a cut-off stream has already been
forwarded: it is logged and ends with `finish_reason: length`, and is never recorded as served.

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
- A 429 that states no reset gets the ordinary breaker. Gemini's bare `RESOURCE_EXHAUSTED`
  ("Resource has been exhausted (e.g. check quota).") is this kind; it has been seen on every key
  of a group within the same minute, so in a group it is treated as an overloaded model (below).
  So is OpenRouter's "temporarily rate-limited upstream", which names the free model behind
  `openrouter/free`, not this key. The Cursor bridge answers 429 only for its own cap on parked
  tool sessions, and has not been seen to.

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
  started and finished between samples. The counters read are Splash's
  `splash_requests_{submitted,completed,cancelled,failed}_total`; a lane on another server
  exposes different names and cannot be gated yet.
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
| `budget_s` | all | 18 / 24 / 40 | Seconds allowed to reach the first content delta; with `buffer_stream`, to finish the whole reply |
| `api_key_env` | all | none | Environment variable holding the key; checked first |
| `api_key_file`, `api_key_var` | all | none | A dotenv file (`NAME=value`), or a JSON object when the path ends `.json`, and the key's name in it; re-read on every request |
| `site_url`, `app_name` | `openrouter` | empty, `observer-router` | `HTTP-Referer` and `X-Title` attribution |
| `reasoning_effort` | `openai` | unset | Sent as-is (`"none"` turns Splash thinking off); leave unset for servers that reject unknown fields |
| `chat_template_kwargs` | `openai` | unset | Sent as-is, e.g. `{ enable_thinking = false }` for Qwen on mtplx or vLLM, which ignore `reasoning_effort` |
| `serial` | `openai` | `false` | One request at a time per lane URL |
| `ac_only` | `openai` | `false` | Skip while on battery |
| `buffer_stream` | all | `false` | Hold a stream back until it ends, so a reply cut off at the token limit fails over |
| `idle_metrics`, `idle_seconds` | `openai` | none, `30` | Wait for these lanes to be idle first |

`chain` lists provider or group names in order; without it the providers' order is used, and
groups then go unused. A group
is a `[groups.<name>]` table with one field, `members`, a list of provider names; its name must not
also be a provider's, groups do not nest, and no provider may be reachable twice through the chain.

```toml
chain = ["gemini", "openrouter", "local"]

[groups.gemini]
members = ["gemini-a", "gemini-b", "gemini-c"]   # request 1 starts at a, request 2 at b, ...
```

A group's members are usually keys for one model, so an **overloaded model** blocks the whole
group for the breaker interval (`OBSERVER_BREAK_SECONDS`, 60 s) and the chain moves on at once,
instead of spending each member's budget in turn:

- an HTTP 503 from any member blocks the group immediately;
- a socket read timeout, or a 429 that names no quota, blocks it only when two members
  (`OBSERVER_GROUP_STRIKES`) fail so in a row, since one timeout is ordinary tail latency and one
  unexplained 429 can be one key. Any member's success resets the count, and a timeout counts only
  when the member had its whole budget, not one the request deadline had already cut short;
- a quota 429 still blocks only the member whose key ran out.

The rotation position is in memory and starts again at the first member after a restart. `/health`
lists each group's members under `groups`, and every member's breaker under `breaker`. `gemini` and
`openrouter` instances must name a key source. **Keys never go in the file**: an inline `api_key`
is rejected, as is any field the instance's type does not know, so a typo cannot be ignored
silently. Every value is type-checked at start-up (`serial = "false"` is an error, not true), and
a key that turns out to contain control characters fails its instance without being echoed.
Environment variables from before the config file (`OBSERVER_LOCAL_*`, `OBSERVER_CURSOR_*`, and
the like) are ignored, and start-up logs their names. `config.toml` and `*.env` are git-ignored
in this repository.

Environment overrides, all optional. An unset or empty variable takes the default. A value that is
not a number, or is out of range, stops the router at start-up with the variable named in the
log (exit status 2), as an invalid config file does; it never falls back to the default silently.
Integer settings stop at 1,000,000,000.

| Variable | Default | Accepts |
| --- | --- | --- |
| `OBSERVER_ROUTER_CONFIG` | `~/.config/observer-router/config.toml` | a path |
| `OBSERVER_ROUTER_CHAIN` | the file's `chain` | comma-separated provider or group names, e.g. to drop a local lane during a benchmark |
| `OBSERVER_ROUTER_HOST` / `OBSERVER_ROUTER_PORT` | `127.0.0.1` / `1244` | an address / an integer 1–65535 |
| `OBSERVER_BREAK_AFTER` / `OBSERVER_BREAK_SECONDS` | `3` / `60` | an integer ≥ 1 / seconds ≥ 1 |
| `OBSERVER_GROUP_STRIKES` | `2` | an integer ≥ 1: group members failing in a row, by timeout or unexplained 429, that block the group |
| `OBSERVER_POWER_CACHE_SECONDS` | `30` | seconds ≥ 0 |
| `OBSERVER_IDLE_SAMPLE_SECONDS` | `2` | seconds, 0.1–3600 |
| `OBSERVER_MAX_PROMPT_CHARS` / `OBSERVER_MAX_FIELD_CHARS` | `240000` / `80000` | integers ≥ 1 |
| `OBSERVER_MIN_RETAINED_CHARS` | `1024` | an integer ≥ 0 |
| `OBSERVER_BASE64_BLOB_CHARS` | `16384` | an integer ≥ 1 |

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
| health | `curl -fsS http://127.0.0.1:1244/health` — chain, groups, instances, breaker and quota blocks, prompt guard, power/AC gate, idle gate |
| models | `curl -fsS http://127.0.0.1:1244/v1/models` |

Exercise one instance in isolation by starting a throwaway router whose chain names only it. Use
a port nothing listens on: ports next to 1244 may belong to other local services, so check with
`lsof -nP -iTCP:<port> -sTCP:LISTEN` first, and stop only the process you started.

```bash
OBSERVER_ROUTER_PORT=12944 OBSERVER_ROUTER_CHAIN=<provider-name> python3 observer-router.py
# then POST to :12944 — the log shows which instance served
```

## Tests

```bash
python3 -m unittest -v test_observer_router.py
```

The suite covers config loading and validation (named instances, type defaults, groups and their
rotation, the chain and its override, and rejection of inline keys, unknown fields, undefined
names, and providers reachable twice), per-instance
request building (key sources, attribution headers, reasoning fields, lane locks), failover
between two instances of one type and rotation across a group's members, through the real HTTP
handler against fake upstreams, replies cut off at the token limit (failover, buffered streams and
their whole-reply budget, the last-resort cut-off reply, the log line), group-wide blocks for
overloaded models, prompt compaction, the power and idle gates, and quota-reset parsing against
the 429 shapes Gemini and OpenRouter actually return.

## Known limitations

1. **Free hosted quotas run out.** Free Gemini and OpenRouter keys have hard daily caps, so a
   chain of free tiers alone goes dark for the rest of the day; end it with an instance that has
   no such cap (a local lane, a paid key, or a bridge).
2. **A hung-but-alive local lane still defeats launchd `KeepAlive`.** The router fails over
   around it, but cannot restart it; that still needs `launchctl kickstart -k` on the lane's job.
3. **Cosmetic log noise.** claude-mem client disconnects raise `BrokenPipeError` and
   `ConnectionResetError` tracebacks in the router log. They are harmless but loud.
4. **A buffered instance answers only when its reply is whole.** Under load, the wait for a
   `serial` lane plus a whole reply can pass claude-mem's 90 s stream idle timeout, which a
   streamed first token would have reset. Sending headers and keep-alive comments early would
   avoid it, at the cost of the failed-chain 502.
5. **Unbuffered tiers still forward a cut-off reply.** Hosted tiers that hit claude-mem's
   `max_tokens` (seen while a model echoes the file it observes) cannot fail over; the reply is
   logged, ends with `finish_reason: length`, and claude-mem keeps its complete blocks.
6. **Breakers and quota blocks live in memory.** A restart forgets them, so the first requests
   after one meet each exhausted quota again before it is blocked anew.

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
