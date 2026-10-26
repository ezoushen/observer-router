# 2026-10-26-001 — replies cut off at the token limit fail over

## 21:20 — A `finish_reason: length` reply is no longer served as a success

- Premise: the `mbp14` lane (Locals 0.2.1, Qwen3.5-4B 4-bit, server cap `--max-tokens 2048`)
  ended 86 of 6,529 requests since 2026-10-22 at exactly 2048 tokens, about 1.3%. Several were the
  same prompt repeated, and one growing session hit the cap on every turn. The router read only
  `delta.content`, committed a stream at its first content delta, and failed a non-streaming reply
  only when it was empty, so claude-mem stored the truncated text. Whether those replies looped is
  unproven: Locals keeps no bodies and the router logged none.
- Verified against the code before the change: the stream path ignored `finish_reason`, and
  `payload_for` forwards `max_tokens` only when the client sends one. claude-mem 15.2.1 does send
  one: `max_tokens` 4096 (`CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS`), temperature 0.3, so the 2048
  cut is the server's cap, not the client's. A live probe of `mbp14` showed its last frame carries
  `finish_reason` and `usage` (`prompt_tokens`, `completion_tokens`).
- Decision: non-streaming, a length cut fails over for every provider, at no cost. Streaming, an
  opt-in `buffer_stream` holds an instance's text until its stream ends. Buffering every provider
  was rejected: claude-mem's stream idle timeout (`CLAUDE_MEM_LLM_STREAM_IDLE_TIMEOUT_MS`, 90 s by
  default) runs from the request, and holding back fast hosted tiers adds latency where a cut-off
  is rare. `mbp14` takes the cost: its median reply is about 297 tokens.
- Decision: the cap stays at 2048 and sampling is unchanged. The new log line,
  `tier <name> cut off at the token limit: ...` with sizes and an 80-character head and tail, is the
  evidence that has to show looping before temperature or a presence penalty is touched.
- References: `ChainTests.test_a_reply_cut_off_at_the_token_limit_fails_over`,
  `test_a_buffered_stream_cut_off_at_the_token_limit_fails_over`,
  `test_a_buffered_stream_that_finishes_is_served_whole`,
  `test_a_cut_off_reply_is_logged_with_its_size_and_both_ends`,
  `test_an_unbuffered_stream_cut_off_is_still_logged_but_not_served_as_a_success`.

## 21:35 — Review: a buffered stream must end with a `finish_reason`

- Outcome: an independent review found that a buffered stream which closed cleanly without a
  `finish_reason` was served as whole, which contradicted the reason for buffering. It is now a
  failed attempt. A `usage` that is not an object made the cut-off log raise `AttributeError`,
  which escaped failover; the log line now ignores it. The unbuffered test now also asserts that
  the tier is recorded as failed.
- Outcome: accepted as designed. A cut-off counts toward the breaker, like an empty reply, and the
  README now says so. A buffered reply holds a `serial` lane until it is fully generated, even
  after claude-mem has given up; the server's 2048-token cap bounds that.
- References: `test_a_buffered_stream_that_ends_without_a_finish_reason_fails_over`,
  `test_a_cut_off_reply_with_malformed_usage_still_fails_over`.

## 22:10 — Second pass: salvage, breaker, a hard budget, and the hosted-tier evidence

- Correction to the 21:20 premise: claude-mem 15.2.1 does not store a cut-off reply's text
  wholesale. It stores the complete `<observation>` blocks and logs that the unfinished tail is
  lost. Failing every cut-off to a 502 would have thrown those blocks away, so the first cut-off
  reply is now served, with `finish_reason: length`, when no later tier answers.
- Reversal of the 21:35 decision: a cut-off no longer counts toward the breaker. claude-mem
  retries the same prompt, so one deterministic loop on `mbp14` would have opened its breaker and
  pushed unrelated requests onto the next tier, which is often down.
- Reversal of the 21:35 decision: the lane is no longer held after the client leaves. A buffered
  instance's `budget_s` is now a hard bound on the whole reply, with every read limited to what is
  left of it. Live `mbp14` went from `budget_s = 100` to 90, matching claude-mem's 90 s stream
  idle timeout, which starts at its request: a reply finishing later reaches no one.
- Evidence: in its first 30 minutes the new log line recorded six cut-offs on hosted tiers, out of
  about 120 served. Gemini and OpenRouter reached claude-mem's `max_tokens` of 4096 while echoing
  the source file being observed (heads such as `import hashlib, json`). Those streams are
  unbuffered, so they were forwarded, and claude-mem kept what blocks it could. Buffering them is
  left open: Gemini members already time out at their 12 s budget for first content, and a
  whole-reply bound would fail many more of them over.
- Outcome: the README's throwaway-router port moved from 1247 to 12944. Port 1247 is
  `com.ezou.loop-guard`, and the earlier verification run killed it by mistake; launchd restarted
  it. Its 1246 route answers, but its 1247 upstream, a remote host, was unreachable before
  the kill and still is.
- References: `test_a_cut_off_reply_is_served_when_no_later_tier_answers`,
  `test_a_buffered_cut_off_stream_is_served_when_no_later_tier_answers`,
  `test_a_buffered_budget_holds_even_while_each_read_beats_the_read_timeout`,
  `test_an_unbuffered_stream_is_not_cut_at_its_budget_once_committed`,
  `test_a_cut_off_non_streaming_reply_is_logged_with_its_size_and_both_ends`.

## 22:25 — Third review: the bound also covers the wait for headers

- Outcome: the re-review found that the whole-reply bound started limiting reads only after the
  first line, so a lane holding its headers through prefill could overrun the budget by one read
  timeout. The first read is now bounded too
  (`test_a_buffered_budget_includes_the_wait_for_headers`). An unbuffered stream that was cut off
  now ends with a `finish_reason: length` frame, so claude-mem logs its own cut-off warning.
- Decision recorded: cut-offs stay out of the breaker. A looping prompt costs its tier one attempt
  each time it is sent; serving the cut-off reply when nothing else answers is what stops
  claude-mem from resending it.
