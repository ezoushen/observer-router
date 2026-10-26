# Breakers, quota blocks, and group blocks

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
  of a group within the same minute, so in a group it is treated as an overloaded model ([below](#groups-and-overloaded-models)).
  So is OpenRouter's "temporarily rate-limited upstream", which names the free model behind
  `openrouter/free`, not this key. The Cursor bridge answers 429 only for its own cap on parked
  tool sessions, and has not been seen to.

The block is logged once with its reset time, named in the failed-chain `attempted` array, and
reported in `/health` as `breaker.<name>.reason`.

## Groups and overloaded models

A group's members are usually keys for one model, so an **overloaded model** blocks the whole
group for the breaker interval (`OBSERVER_BREAK_SECONDS`, 60 s) and the chain moves on at once,
instead of spending each member's budget in turn:

- an HTTP 503 from any member blocks the group immediately;
- a socket read timeout, or a 429 that names no quota, blocks it only when two members
  (`OBSERVER_GROUP_STRIKES`) fail so in a row, since one timeout is ordinary tail latency and one
  unexplained 429 can be one key. Any member's success resets the count, and a timeout counts only
  when the member had its whole budget, not one the request deadline had already cut short;
- a quota 429 still blocks only the member whose key ran out.
