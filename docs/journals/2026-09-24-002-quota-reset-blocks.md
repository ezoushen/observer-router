# 2026-09-24-002 — quota-reset blocks

## 15:25 — both free tiers state their reset

- Outcome: an OpenRouter 429 carries `X-RateLimit-Limit: 50`, `X-RateLimit-Remaining: 0`, and
  `X-RateLimit-Reset` in epoch milliseconds (00:00 UTC), repeated in `error.metadata` with
  `limit_source: "openrouter_free_tier_daily"`. A Gemini 429, captured by calling a model with a
  zero free-tier quota, lists `google.rpc.QuotaFailure` violations by `quotaId` (for example
  `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) and a `google.rpc.RetryInfo` of `34s`.
  That 34 s appears even though a per-day quota is violated, so it cannot be trusted alone.
- Outcome: the router log agrees with a midnight-Pacific Gemini reset. Service resumes each day
  at 15:00 GMT+8, serves roughly 375 to 460 calls in that hour, then returns 429 for the rest of
  the day, 500 to 600 times a day.
- Open: the flash-lite daily 429 itself was not captured, because the quota was fresh. If it
  lists a `PerDay` violation for a per-minute burst, the tier would block until midnight
  Pacific early; the `quota ... until` log line would show that at the start of the burst.

## 15:30 — blocks implemented and verified live

- Outcome: a throwaway instance on `:1247` with `gemini-pro-latest` and the exhausted
  OpenRouter key blocked Gemini until 2026-09-25 15:00 GMT+8 and OpenRouter until 08:00 GMT+8.
  The second request skipped both without a network call and named the quotas in `attempted`.
  Production was reloaded and served `PROD_OK` from Gemini.
- References: `observer-router.py` (`quota_reset`, `tier_quota_exhausted`),
  `test_observer_router.py` (23 tests).

## 15:34 — first live blocks in production

- Outcome: OpenRouter blocked at 15:30:50 until 2026-09-25 08:00 GMT+8. Gemini answered a
  per-minute burst at 15:33:50 with only `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`
  in `QuotaFailure` and a 9 s `retryDelay`, so the block lasted 9 s and Gemini served again.
  A per-minute 429 therefore does not list a `PerDay` violation, which narrows the open item
  from 15:25 to confirming what the daily 429 lists.
- References: `/tmp/observer-router.log`.

## 16:05 — the flash-lite daily 429 lists its PerDay quota

- Outcome: Gemini served 477 calls after its 15:00 reset, then at 15:42:35 answered with
  `GenerateRequestsPerDayPerProjectPerModel-FreeTier`; the tier blocked until 2026-09-25 15:00
  GMT+8. Together with the per-minute 429 at 15:33, which listed only its per-minute quota, this
  closes the open item from 15:25: the PerDay rule matches what Gemini sends.
- References: `/tmp/observer-router.log`.
