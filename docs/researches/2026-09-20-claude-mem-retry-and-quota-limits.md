# Are the free remote quotas sufficient, and does skipping the local tier lose work?

Date: 2026-09-20

Two questions had to be answered before the router could safely skip its backstop tier while
on battery.

## 1. Are the free remote quotas enough?

No. Both free tiers hit hard walls under real observer volume:

- **Gemini** — served 471 calls, then returned `HTTP 429` with
  `exceeded your current quota`.
- **OpenRouter** — returned `Rate limit exceeded: free-models-per-day`, its daily free-model
  cap.

Once both expire, the local lane becomes the only tier that can serve. That is why the local
lane is load-bearing, and why the router treats the remote tiers as capacity rather than
guarantees.

## 2. Does skipping the local tier lose observations?

No. claude-mem retries instead of dropping work, and this was verified in code and in logs
rather than assumed from documentation.

**Log evidence.** On a failed chain the router returns
`quota exhausted (status 502): {"error": "all tiers failed", "attempted": [...]}`. claude-mem
logs `Observer failed {provider=openrouter, kind=quota_exhausted}` and enters a provider quota
cooldown, then repeats:

```text
Skipping generator start while the provider quota cooldown is active {source=observation, provider=openrouter, probeInFlight=false, retryInMs=…}
```

until a retry succeeds. On 2026-09-19 the log recorded 75 such `502` events and observations
were still stored afterwards.

**Code evidence.** The requeue path is `resetProcessingToPending()`, which fires on
quota-limit prose, auth failure, and context overflow. `getMessageIterator` resets claimed
messages at each drain, and `buffer.resetClaimed` returns unprocessed messages to the queue.

## Contrary finding: the queue is in-memory, not durable

`pending_messages` in the database is **vestigial** in plugin **13.24.8**: all 56 references
are schema migrations, dedupe logic, or diagnostics, and nothing inserts or updates it. The
live queue is the worker's in-memory buffer, whose `onPendingMutate` hook only broadcasts SSE
status.

**Consequence:** the retry cushion holds only while the worker process stays up. A worker
restart discards queued work. This is upstream behaviour, not something the router can fix —
it is recorded here so nobody mistakes `pending_messages` for a durable queue when diagnosing
a gap in observations.

## Related measurement: `openrouter/free` returns empty content

This is what forced the router's reasoning handling:

- About half of `openrouter/free` calls returned empty content, because the auto-selected
  reasoning model spent the whole `max_tokens` budget on `reasoning`
  (`finish_reason: length`, `content: ""`).
- Injecting `{"reasoning":{"enabled":false}}` restored `content="OK", finish_reason=stop`.
- One endpoint refused the injection with
  `HTTP 400 "Reasoning is mandatory for this endpoint and cannot be disabled."`, so the router
  retries the same tier once with `{"reasoning":{"exclude":true}}`.
- Any tier that yields no content is treated as a failed attempt, so claude-mem never records
  an empty observation.
