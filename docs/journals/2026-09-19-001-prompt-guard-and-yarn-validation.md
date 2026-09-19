# 2026-09-19-001 — prompt guard deployed and factor-2 YaRN validated

## 01:45 — Local lane failure identified as oversized input, not a leak

- Outcome: controlled testing found no resident-memory leak: 20 unique streaming requests
  moved RSS from 2.71 to 2.73 GB (+20 MB, flat after request 5), with four cached sequences
  using 0.22 GB and idle CPU returning to 0 %. The lane nevertheless lost its generation
  thread after claude-mem supplied fields as large as 3,250,882 characters. Qwen3.5-4B's
  native context is 262,144 tokens; MLX attempted a 98,784,247,808-byte (92 GiB) allocation
  and rejected it above the 86,586,540,032-byte maximum buffer. The HTTP listener remained
  alive, so launchd did not restart it.
- References: `/tmp/mlx-qwen35-4b.log`, `observer-router.py`,
  `~/.mlx-models/qwen3.5-4b-observer/config.json`.

## 10:40 — Deterministic prompt guard deployed and production-sized trigger replayed

- Outcome: the router now strips image/base64 payloads, folds fields above 80,000
  characters, and bounds the complete conversation at 240,000 characters while preserving
  the primary system instruction and newest context. Compaction runs once before tier
  selection and never calls another model. A replay matching the largest observed field
  reduced 3,250,938 input characters to 97, returned HTTP 200 streaming in 2.04 seconds,
  and left the local lane serving in 0.21 seconds. `/health` exposes guard limits and
  cumulative counters.
- References: `observer-router.py`, `test_observer_router.py`, `/tmp/observer-router.log`,
  `curl -fsS http://127.0.0.1:1244/health`.

## 10:35 — Factor-2 YaRN accepted by the installed MLX implementation

- Outcome: a separate model view with symlinked weights and an independent config sets
  static YaRN factor 2 and a 524,288-token target. The foreground test lane on `:1245`
  loaded successfully and returned `YARN_OK` in 1.62 seconds. It was stopped after the
  smoke test to avoid GPU contention. This proves config compatibility, not that a 524k
  inference fits memory or latency requirements; the production guard remains active.
- References: `../llm-runtime/prepare-qwen35-4b-yarn2.py`,
  `../llm-runtime/serve-mlx-qwen35-4b-yarn2.sh`, `/tmp/mlx-qwen35-4b-yarn2.log`.
