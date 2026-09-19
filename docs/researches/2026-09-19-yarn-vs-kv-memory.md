# Can a YaRN factor-2 config extend the lane's usable window enough to drop the character guard?

Date: 2026-09-19

## Expectation being tested

The local model advertises a 262,144-token native window. The guard forwards roughly 60k
tokens. If the lane's real limit were a positional-encoding limit, a YaRN config would raise
it and let the guard be relaxed.

## Method

Built a separate model view `~/.mlx-models/qwen3.5-4b-observer-yarn2`: weights symlinked to
the production copy, independent `config.json` with `rope_type: "yarn"`, `factor: 2.0`,
`original_max_position_embeddings: 262144`, and a 524,288-token target. Served it on `:1245`
as a foreground process — not under launchd and not in the production chain — and ran a short
smoke test.

## Result

MLX loaded the config and the lane returned `YARN_OK` in 1.62 s, identifying the YaRN model
path.

## Why it does not replace the guard

- **YaRN changes positional encoding, not KV memory.** A longer *valid* window does not
  reduce the per-token KV allocation, which is the quantity that actually blew up in
  `2026-09-19-observer-lane-oom-diagnosis.md`. The factor-2 run would still need the same
  ~92 GiB for a 3.25M-character prompt.
- **A smoke test proves config acceptance, not practicality.** It shows this MLX build accepts
  YaRN and loads it; it does not show a 524k-token request fits or is fast.
- **Static YaRN degrades short-context quality.** Observer traffic is overwhelmingly
  short-context, which is the common case the lane must stay good at. Trading that away for a
  window the guard never uses is a bad exchange.
- **The guard also works when every quota is exhausted**, because it is deterministic and
  never calls another model.

## Decision

Keep the production lane native. The YaRN view is created on demand for experiments, not
persisted, and the test lane is stopped after validation to avoid GPU contention. Any future
factor-2 evaluation should measure quality and memory at 300k–500k tokens before factor 4 is
considered.
