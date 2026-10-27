# Which model should MBP-14's local lane serve?

Date: 2026-10-28

## Question

Which model, served on MBP-14's 16 GB M1 Pro, does claude-mem's observation and My Work summary work
better than today's Qwen3.5-4B, while staying inside the machine's memory and the lane's latency
envelope? The owner also asked whether Gemma 4 E4B beats Qwen3.5-4B on this work, and asked for a new
LFM3 model to be measured as well.

## Why this was checked

The lane's problem is quality, not availability. At night the free tiers are quota-blocked, so MBP-14
is the only working tier, and about two thirds of its requests are not memory notes. They are the
My Work "human copy" summarizer, which reads as `stream=0` in the service log: the 4B returns valid
JSON but breaks the shape check `Oz` with "Every supplied workstream must appear exactly once", so
claude-mem retries the same card every 60 s forever — 209 qwen calls on one card — and real observer
requests are skipped at the request deadline. Two failure shapes were reproduced with claude-mem's own
code: an invented workstream id when the input has none, and a shortened id when the real id is a
JSON-array-looking string.

A model swap is the owner's decision, so this document measures and recommends; it changes no live
configuration.

## Method

The router is client-agnostic and the lane lives outside this repository, so the measurement sends
what claude-mem 15.3.0 actually sends and judges replies with claude-mem's own parser and validators.
Nothing here re-implements a prompt, a schema or a check.

### The harness rebuilds production requests

The harness slices the functions out of claude-mem's worker bundle
(`~/.claude/plugins/cache/thedotmack/claude-mem/15.3.0/scripts/worker-service.cjs`) and the active mode
file (`modes/code.json`), and calls them unmodified. It is Node.js, so it stays outside the repository,
which is standard-library Python only: `harness/slice.js` builds the code, `harness/repro-check.js` is
its acceptance check, and both live in this effort's `.scratch/` folder.

The acceptance check confirms the rebuild against the evidence already recorded: the two Work View
cards from the 2026-10-27 work rebuild to the fingerprints that work recorded
(`2f6f6e275e` for `g_892065667d7d`, `a097daddaa` for `g_85dfb1d9813b`), and the live lane reproduces
the recorded failure on both when judged by the harness.

### Observer inputs

Observer prompts are rebuilt from `tool_uses` rows in `~/.claude-mem/claude-mem.db` (86,393 rows) with
the bundle's own builder: `HQ` redacts and elides the fields to the context budget `L1` computes, `ib`
writes the observation turn, and `x1` writes the init prompt from the mode's sections. Each sampled
`tool_uses` row is replayed as a session's first observation — the init prompt and the turn in one user
message, which is the shape claude-mem itself uses when it prepends a queued prompt. Later turns also
carry prior conversation, which is excluded here so every model sees identical input; that is a stated
limit of the measurement, not of the prompt text.

### Summary inputs

Summary prompts are the live Work View cards (`http://127.0.0.1:37701/api/work-view`), rebuilt with
`MXe` and fingerprinted with `NXe`, and judged with `Oz` after `X$r` normalisation — the same functions
production uses.

### Candidates

Everything must fit 16 GB with 32k context; macOS lets the GPU use about 12 GB, so weights stay under
roughly 7 GB.

| Model | HF id | Weights | Engine |
| --- | --- | --- | --- |
| Qwen3.5-4B (today) | `mlx-community/Qwen3.5-4B-MLX-4bit` | 2.9 GB | `locals` |
| Gemma 4 E4B | `mlx-community/gemma-4-e4b-it-4bit` | 5.15 GB | `locals` |
| Qwen3.5-9B | `mlx-community/Qwen3.5-9B-MLX-4bit` | ~5.9 GB | `locals` |
| LFM3-1.7B | `LiquidAI/LFM3-1.7B` (GGUF `Q4_K_M`) | ~1.1 GB | `llama.cpp` `b11935` |
| Qwen3.5-4B OptiQ | `mlx-community/Qwen3.5-4B-OptiQ-4bit` | 2.9 GB | `locals` |
| Gemma 4 12B OptiQ | `mlx-community/gemma-4-12B-it-OptiQ-4bit` | ~6.7 GB | `locals` |
| MiniCPM5-2B | `mlx-community/MiniCPM5-2B-mlx-4Bit` | 1.3 GB | `locals` |

The last three are extensions, measured only where the core three leave a real gap. LFM3 has no MLX
loader: `mlx-lm` 0.32.2 and its upstream main hold no `lfm3` architecture, so it is served by llama.cpp,
whose LFM3 support merged on 2026-10-27 and whose release `b11935` published after it. Its speed is
therefore not comparable to `locals`; its quality on identical prompts is.

### Scoring

Acceptance is mechanical: the observer reply must pass `oFe` (the `<observation>` parser, the 9 types
and 7 concepts of the active mode) or produce a `<skip_summary />`, and the summary reply must pass
`Oz`. Objective counters on every reply: accepted, required fields non-empty, invalid type or concept,
an invented path (a `files_read`/`files_modified` entry absent from the input), truncation at the token
cap, and skip rate.

Fidelity and usefulness are then judged blind by Ornith 1.5 122B on `:1245`, which is not a candidate:
candidate identity is stripped and the order is randomized per sample.

### Speed

Measured on MBP-14 with the lane's own flags (`--slots 1 --max-context 32768 --max-tokens 2048`): TTFT
and decode rate at 2k, 8k and 24k-token prompts, whole-reply wall time over the sampled observer
prompts, cold load time and peak memory. The envelope is the one that already governs the lane: a whole
reply inside the router's 85 s request deadline and TTFT inside claude-mem's 90 s idle timeout.
