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
