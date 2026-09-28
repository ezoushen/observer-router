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
