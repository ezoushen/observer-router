# 2026-09-19-002 — post-validation cleanup

## 10:49 — Experimental residue removed

- Outcome: stopped-state verification confirmed no listener on `:1245`; removed the
  generated `~/.mlx-models/qwen3.5-4b-observer-yarn2` view, router/runtime Python bytecode
  caches, the transient YaRN log, and the earlier throwaway-router log. The preparation
  script recreates the YaRN view on demand. Production router `:1244`, observer lane
  `:1243`, and source model weights remain intact.
- Correction: the transient `/tmp/mlx-qwen35-4b-yarn2.log` cited by
  `2026-09-19-001-prompt-guard-and-yarn-validation.md` was intentionally deleted during
  housekeeping. Durable verification commands and expected results live in `../README.md`;
  the scripts and prior journal outcome remain the retained evidence.
- References: `../README.md`, `../llm-runtime/prepare-qwen35-4b-yarn2.py`,
  `../llm-runtime/serve-mlx-qwen35-4b-yarn2.sh`.
