# 2026-09-29-001 — config file with named provider instances

## 01:00 — Credential audit before independent publication

- Outcome: gitleaks over all 8 commits on every branch, plus a pattern grep for Google, OpenRouter,
  OpenAI, GitHub, and Slack key shapes, private keys, and bearer tokens: no leaks. The only
  personal values in history are the owner's home path in the plist and the commit author email.
- References: `gitleaks git . --log-opts="--all"`.

## 01:10 — Providers move from environment variables to a TOML config

- Outcome: `[providers.<name>]` instances of type `gemini`, `openrouter`, or `openai`, ordered by
  `chain`; several instances of one type each keep their own breaker and quota block. Keys are
  named by `api_key_env` or `api_key_file` + `api_key_var`; inline keys are rejected. The router
  no longer reads claude-mem's settings, so it runs for any OpenAI-compatible client.
- Outcome: failover between two Gemini instances is tested through the real HTTP handler against
  fake upstreams; mutating the breaker key to the provider type, or sharing one model across
  instances, fails those tests.
- Outcome: a throwaway router on `:1247` with the migrated config authenticated to Gemini (from
  claude-mem's settings.json, got a daily-quota 429) and OpenRouter (quota 429 after the
  reasoning retry) and was served by the cursor bridge in 17.6 s.
- References: `config.example.toml`, `test_observer_router.py` (`ConfigTests`,
  `RequestBuildingTests`, `ChainTests`).

## 01:30 — Keys move into the router's own secrets file; the client timeout is corrected

- Outcome: the Gemini and OpenRouter keys were copied from claude-mem's settings into
  `~/.config/observer-router/secrets.env` (mode 600) by a script that never printed them, and the
  live config now points there. A comparison that printed only booleans confirmed both resolve to
  the same values. The cursor bridge key stays in the bridge's own file.
- Outcome: in claude-mem 13.28.0, `CLAUDE_MEM_API_TIMEOUT_MS` bounds calls to the worker's own
  HTTP API. The observer's LLM deadline is `CLAUDE_MEM_LLM_TIMEOUT_MS` (default 30 s, clamped to
  500 ms–300 s, read on each call). README and the example config named the wrong setting and were
  corrected.
- References: `worker-service.cjs` in the 13.28.0 plugin cache (`eIt`, `u0e`).
