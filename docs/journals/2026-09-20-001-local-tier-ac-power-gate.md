# 2026-09-20-001 — local tier gated to AC power

## 00:15 — claude-mem's retry mechanism verified before relying on it

- Outcome: the requeue path is real and running. On a failed chain claude-mem logs
  `Observer failed {provider=openrouter, kind=quota_exhausted}` with the router's
  `502 {"error": "all tiers failed", "attempted": [...]}` body, then enters a provider quota
  cooldown and repeats `Skipping generator start while the provider quota cooldown is active
  {retryInMs=…}` until a retry succeeds. The 2026-09-18/19 worker log recorded 75 such events
  and continued storing observations.
- Contrary finding: `pending_messages` is vestigial in plugin 13.24.8 — every one of its 56
  references is a schema migration, dedupe, or diagnostic; no INSERT or UPDATE writes it. The
  live queue is the worker's in-memory buffer, whose `onPendingMutate` hook only broadcasts
  SSE status. Retry therefore holds while the worker process stays up.
- References: `worker-service.cjs` (`resetProcessingToPending`, `buffer.resetClaimed`,
  `getMessageIterator`), `~/.claude-mem/logs/claude-mem-2026-09-18.log`.

## 00:17 — power gate implemented and verified on both branches

- Outcome: `OBSERVER_LOCAL_AC_ONLY` (default on) skips the local tier while `pmset -g batt`
  reports `Battery Power`. The probe is cached 30 s; an unreadable state reports `unknown` and
  leaves the tier available, so a failed probe never takes the observer offline. `/health`
  reports `power.source`, `power.local_ac_only`, and `power.local_tier_allowed`, and a skipped
  tier is named in the `attempted` array so claude-mem's cooldown log explains the absence.
- Verification: a stubbed `pmset` on `PATH` drove a throwaway instance on `:1246` end-to-end —
  battery reported `502 {"attempted": ["local(skipped: on battery (AC-only))"]}` in 1 ms, while
  real AC served `POWER_AC` from the local lane. Production reloaded with `bootout`/`bootstrap`
  reports `power=AC Power, local_tier_allowed=true` and served `PROD_OK`.
- References: `observer-router.py`, `test_observer_router.py` (8 tests), `/tmp/observer-router.log`.
