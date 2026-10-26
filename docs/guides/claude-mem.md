# Use with claude-mem

claude-mem resolves exactly one observer provider and has no cross-provider fallback, so when
that provider fails, observation silently stops. The router stands in as that one provider and
fails over behind it. Routing belongs here rather than in claude-mem: upstream reviewed a provider
chain, declined it as premature, and shipped OpenRouter-only model fallback that stops at the
OpenRouter boundary
([upstream research](../researches/2026-09-15-claude-mem-provider-fallback-upstream.md)).

claude-mem is optional. The router reads nothing from it; as a client it needs only the two
settings below.

## Point claude-mem at the router

`~/.claude-mem/settings.json` must point at the router, not at any one provider:

```text
CLAUDE_MEM_OPENROUTER_BASE_URL = http://127.0.0.1:1244/v1
CLAUDE_MEM_OPENROUTER_MODEL    = observer-router
```

The model id is an alias; the router rewrites it per instance. claude-mem records the tier's real
model in its own `OpenRouter API usage` lines, so the chain is observable from its log.

## Timeouts

For claude-mem (15.2.1) the per-attempt deadline is `CLAUDE_MEM_LLM_TIMEOUT_MS`, 180 s by default and
at most 300 s. A streamed call is bound tighter by `CLAUDE_MEM_LLM_STREAM_IDLE_TIMEOUT_MS`, 90 s
by default, which runs from the moment the request is sent until the response headers arrive,
and again between reads. The router withholds its headers until it commits, so every attempt
before the one that answers counts against those 90 s: an answer that comes later than that
reaches no one. `CLAUDE_MEM_API_TIMEOUT_MS` is unrelated; it bounds calls to claude-mem's own
worker API. claude-mem asks for `max_tokens` 4096 (`CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS`) at
temperature 0.3, and the router forwards both.

## When every provider fails

claude-mem retries instead of dropping work. On a failed chain it logs
`Observer failed {kind=quota_exhausted}`, enters a provider quota cooldown, and resumes when the
cooldown clears; the requeue path is `resetProcessingToPending()`. The queue is in memory
(plugin `13.24.8`), so that cushion holds only while the worker process stays up. See
[the retry research](../researches/2026-09-20-claude-mem-retry-and-quota-limits.md).
