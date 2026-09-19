# 2026-09-18-001 — observer router launched: Gemini → OpenRouter free → local Qwen

## 20:45 — Chain built and deployed on :1244; claude-mem repointed

- Outcome: `observer-router.py` (stdlib only, `http.client`) now fronts claude-mem's
  observer calls on `127.0.0.1:1244`, chaining `gemini-flash-lite-latest` (18 s) →
  `openrouter/free` (24 s) → local Qwen 3.5 4B on `:1243` (70 s). Installed as launchd
  `com.ezou.observer-router` with `KeepAlive`, logging to `/tmp/observer-router.log`.
  claude-mem's `CLAUDE_MEM_OPENROUTER_BASE_URL` moved from `:1243` to `:1244/v1` and
  `CLAUDE_MEM_OPENROUTER_MODEL` to the alias `observer-router`; its settings were backed up
  to `~/.claude-mem/settings.json.bak-pre-router-20260918-205350`.
- Why: claude-mem resolves one provider and has no cross-provider fallback (its `fallback`
  strings are the cmem.ai Pro tier and the chroma search strategy), so the 2026-09-18
  `:1243` outage was a single point of failure.
- References: `README.md`, `observer-router.py`, `com.ezou.observer-router.plist`,
  `curl -fsS http://127.0.0.1:1244/health`.

## 20:56 — Tier-3 test caught the observer lane OOM'ing again despite the cache cap

- Outcome: with tiers 1 and 2 pointed at bogus models, the local backstop timed out — `:1243`
  had died again with `[METAL] Insufficient Memory`, this time at prompt-cache evaluation
  (`mlx_lm/generate.py:1161 in prompt`), leaving the process alive at 0 % CPU. The
  `--prompt-cache-size 4` cap added earlier the same day reduced peak cache (8.5 → ~5 GB in
  a burst test) but did not prevent recurrence under claude-mem's 25–29k-token prompts.
  claude-mem was unaffected because the router served from Gemini and OpenRouter.
- Follow-ups recorded in `README.md`: tighten `--prompt-cache-size` to 2 or 1 in
  `../llm-runtime/serve-mlx-qwen35-4b.sh`, and let the router `launchctl kickstart -k` the
  lane once the tier-3 breaker trips (a hung-but-alive lane defeats launchd `KeepAlive`).
- References: `/tmp/mlx-qwen35-4b.log`, `../llm-runtime/serve-mlx-qwen35-4b.sh`.

## 21:05 — All three tiers verified; production traffic split across the chain

- Outcome: tier 1 (non-stream and stream), tier 2 failover (non-stream and stream, SSE
  intact), and tier 3 failover after the lane restart were each verified 200. End-to-end via
  claude-mem: memories `STORED`, `consecutiveFailures: 0`. Production mix so far: 15 calls
  served by Gemini, 9 by `openrouter/free`; Gemini's failures were 2 × HTTP 503 and
  2 × 18 s read timeouts — the reason tier 2 earns ~37 % of calls.
- Tier rules that were measured, not assumed: `openrouter/free` returns empty content about
  half the time (reasoning consumes `max_tokens`), fixed by injecting
  `{"reasoning":{"enabled":false}}`; one endpoint refused that with HTTP 400
  "Reasoning is mandatory", so the router retries with `{"reasoning":{"exclude":true}}`; any
  tier with no content counts as a failure and falls through.
- References: `/tmp/observer-router.log`, `~/.claude-mem/observer-health.json`,
  `~/.claude-mem/logs/claude-mem-*.log`.
