# 2026-10-26-002 — unexplained 429s block a group

## 22:50 — Gemini's bare RESOURCE_EXHAUSTED is the model, not the key

- Question: should the router remember 429s itself, so it stops asking a limited key, and do
  Gemini, OpenRouter and the Cursor bridge say when a limit ends?
- Finding: it already does when the 429 says when. Gemini's `QuotaFailure` and `RetryInfo`, and
  OpenRouter's `X-RateLimit-Reset`, produced 71 and 91 quota blocks between 2026-10-21 and
  2026-10-26. The 429s that repeat are a different kind. 2,326 of Gemini's 2,397 429s carried only
  `"Resource has been exhausted (e.g. check quota)."`, with no quota and no delay, so there is no
  reset to wait for. In 155 of the 262 minutes they occurred, all five keys got one, and in a
  third of cases the same key also served within that minute. That points at shared capacity
  behind `gemini-flash-lite-latest`, not at a key's quota.
- Finding: OpenRouter's 87 other 429s read `"<model>:free is temporarily rate-limited
  upstream"`. They concern the free model `openrouter/free` routed to, not our key, so they are
  not blocked. The Cursor bridge (`cursor-api-proxy`) never answers 429: its `reportRateLimit` has
  no caller, and its handlers send 401, 403, 404 and 5xx only. There were no Cursor 429s.
- Outcome: a 429 that names no quota is now `overloaded="exhausted"`. Two group members failing
  that way in a row, or mixed with full-budget timeouts, block the whole group for the breaker
  interval. One alone still only counts toward that member's breaker.
- Not done: reading a `Retry-After` header. No logged response has shown one, and Gemini states
  its delays in `RetryInfo`, which is already parsed.
- References: `ChainTests.test_two_members_exhausted_in_a_row_block_the_group`,
  `test_one_member_exhausted_does_not_block_the_group`.

## 23:05 — Review corrections

- Correction to 22:50: the counts above came from `grep -c`, which also matched the breaker lines
  that repeat a failure's reason. Counted as failure events, Gemini had 1,686 bare
  `RESOURCE_EXHAUSTED` 429s and 71 with a quota, about 96%. The per-minute figures (155 of 262)
  were already counted per event.
- Correction to 22:50: the Cursor bridge can answer 429. `tool-session-registry.js` throws a 429
  `tool_session_limit` when too many tool sessions are parked. None has been logged, and the
  bridge is not in a group, so the change does not touch it.
- Accepted, not changed: the strike count does not expire, so one unexplained 429, hours of idle,
  then another can still block the group once for the breaker interval. Timeouts already behaved
  so, and any member's success resets the count, which at observer traffic happens within
  minutes.
