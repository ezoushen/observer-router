# 2026-09-30-001 — a remote Qwen lane over the tailnet

## 13:30 — Qwen 4B on a second laptop joins the chain before the local lane

- Outcome: the lane (mtplx, `qwen3.5-4b-mtplx-optimized-speed`) listened on `127.0.0.1:8030` only.
  It is exposed tailnet-only with `tailscale serve --bg --tcp 8030 tcp://127.0.0.1:8030` on that
  host; the server's own bind is unchanged.
- Outcome: mtplx ignores `reasoning_effort: "none"` and spends `max_tokens` thinking (empty
  content). `chat_template_kwargs: {enable_thinking: false}` answers directly, so providers gained
  a `chat_template_kwargs` field.
- Outcome: 20,000-token context. A 24k-token prompt is refused with HTTP 400
  `context_length_exceeded` in 0.4 s. A cold 19.8k-token prompt took about 60 s, and the same
  prompt cached reached first content in 4.8 s, so a 30 s budget covers cold prompts up to about
  10k tokens.

## 15:20 — An overloaded model blocks its whole group; stalled streams fail over

- Outcome: during the 2026-09-29 Gemini "high demand" outage, each request spent up to 60 s on
  five keys that all failed for the same reason. A 503 or a timeout from a group member now skips
  every member for the breaker interval; a quota 429 stays per member.
- Outcome: writing the timeout test exposed a pre-existing defect. An upstream that sent its headers
  and then stalled raised its read timeout out of the handler, so the client lost its connection
  instead of failing over. Errors before the first content delta are now failed attempts.
- References: `ChainTests.test_an_overloaded_member_blocks_its_whole_group`,
  `test_a_stream_that_stalls_after_its_headers_fails_over`.

## 15:30 — Review: one timeout is tail latency, not an outage

- Outcome: review found that blocking the group on any single timeout would idle all five keys
  for 60 s on about 1% of healthy calls, because the 12 s budget sits at Gemini's p99. A 503 still
  blocks at once; timeouts now need two members in a row, and only count when the member had its
  whole budget. The OpenRouter reasoning retry no longer drops the overload flag.
