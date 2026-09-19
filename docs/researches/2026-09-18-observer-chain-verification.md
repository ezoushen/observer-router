# Does the three-tier chain actually serve, and how often is the primary hop insufficient?

Date: 2026-09-18

## Method

Each tier was exercised in isolation by running the router with the earlier tiers
deliberately broken (`OBSERVER_GEMINI_MODEL=definitely-not-a-model`,
`OBSERVER_OPENROUTER_MODEL=bogus/model-not-real`) and reading the router log to see which
tier ended up serving. Tier 1 was exercised directly. The chain was then exercised end to end
through claude-mem.

## Results

| Check | Result |
| --- | --- |
| Tier 1, non-streaming and streaming | 200, `gemini-flash-lite-latest`, 0.78–2.2 s |
| Tier 2 failover (tier 1 broken) | 200, `inclusionai/ling-3.0-flash-vl:free`, 1.45 s |
| Tier 2 streaming failover | SSE intact, content streamed from `dots-studio/dots-3-note-preview:free` |
| Tier 3 failover (tiers 1+2 broken) | 200, local Qwen, 0.22 s, streaming and non-streaming |
| End-to-end via claude-mem | `STORED` memories, `consecutiveFailures: 0` |
| Production tier mix | 15 served by Gemini, 9 by `openrouter/free` |
| Gemini failure reasons in production | 2 × HTTP 503 (overloaded), 2 × 18 s read timeout |

## Findings

- **Every tier serves, and failover between them works in both streaming and non-streaming
  modes.** The SSE path survives a tier switch because headers are withheld until the first
  content delta.
- **The primary hop is fast when healthy but not always available.** Roughly 37% of
  production requests fell through to tier 2 in this sample (9 of 24), from HTTP 503
  overload plus budget expiry. That rate is why tier 2 exists rather than treating Gemini as
  the whole story.
- **The tier-3 exercise caught the local lane OOM'ing again**, even with the prompt cache
  capped by sequence count. That result started the investigation in
  `2026-09-19-observer-lane-oom-diagnosis.md`, which ended in the prompt-size guard.

## Follow-up

The tier-mix numbers here are a snapshot of one afternoon, not a standing measurement. Free
quota behaviour later changed the picture entirely — see
`2026-09-20-claude-mem-retry-and-quota-limits.md`.
