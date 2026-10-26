# 2026-10-27-002 — per-provider prompt and breaker limits

## 01:30 — Two router-wide settings become provider fields

- Request: the prompt guard and the breaker thresholds were router-wide only, but a lane with a
  32k-token context and a hosted API need different values. Make them tunable without a code
  release, and find out what the lane can say about itself.
- What `mbp14` (Locals 0.2.1) exposes: `GET /v1/models` lists `context_length` and
  `max_model_len`, 32768 tokens, per model. Its `/health` shows only the version; `/props`,
  `/metrics` and `/info` answer 404. The 2048-token reply cap is not listed; it appears only as
  `completion_tokens=2048` in the router's cut-off log line. Its 400 for an oversized prompt
  states the window and the prompt's token count, and it happened twice in about 8,000 requests
  (34,316 and 32,597 tokens). Observer prompts measured 3.5 to 4.4 characters per token over 13
  cut-off samples.
- Outcome: `max_prompt_chars`, `break_after` and `break_seconds` on any provider. A provider's
  `max_prompt_chars` re-folds the already bounded prompt for that provider only and can only
  tighten the router-wide guard, so the rule against loosening it holds. The breaker values
  apply to its own breaker and to group blocks. `/health` lists the overrides that are set.
  The config file is read at start-up, so tuning is an edit and a restart.
- Not applied to the live config: `max_prompt_chars = 96000` on `mbp14` would fold prompts over
  that size for it instead of letting it answer a 400 and fall to the next provider, which sees
  the full prompt. Twice in about 8,000 requests, with a possible loss in observation quality,
  so it waits for an owner decision.

## 01:50 — Review: an oversized system message starved the request

- Outcome: an independent review found that the guard's priority loop gave the system message
  first claim on the whole budget. With a per-provider limit below the system message's size, the
  request was dropped and the provider still answered, which an empty-reply check cannot catch.
  The same loop serves the router-wide guard, which only changes prompts that exceed it, so the
  fix is a strengthening: the newest non-system message now keeps up to half the budget.
- Outcome: because a message is kept only if 1,024 characters of it fit, a provider limit must be
  at least twice `OBSERVER_MIN_RETAINED_CHARS` (2,048 by default). A group block now logs each
  member's own interval, and start-up warns about a `max_prompt_chars` above the router-wide
  limit, which parses but never acts.
- Not changed: per-provider folds are logged but not added to the router-wide compaction
  counters in `/health`, and some tests assume the default values of `OBSERVER_MIN_RETAINED_CHARS`
  and `OBSERVER_BREAK_AFTER`, as the existing prompt-guard tests already do.
- References: `ChainTests.test_each_provider_gets_the_prompt_sized_for_it`,
  `test_a_provider_breaker_threshold_overrides_the_router_default`,
  `PromptCompactionTests.test_an_oversized_system_message_does_not_starve_the_newest_request`.
