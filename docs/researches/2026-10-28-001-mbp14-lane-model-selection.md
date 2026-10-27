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
| Qwen3.5-9B | `mlx-community/Qwen3.5-9B-MLX-4bit` | 5.35 GB | `locals` |
| LFM3-1.7B | `LiquidAI/LFM3-1.7B` (GGUF `Q4_K_M`) | 1.1 GB | `llama.cpp` built `19ff817` |
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
at the largest prompts the lane actually receives, decode rate on a 256-token generation, the first
request after a load (which pays Metal kernel compilation), whole-reply wall time over the sampled
observer prompts, cold load time and memory.

The probe corrected an assumption: a single-turn prompt cannot be arbitrarily long. `HQ` elides a
turn's fields to the context budget `L1` computes — 13,107 characters at 32,768 tokens — and the
template adds roughly 9k more, so a production observer turn tops out around 22k characters. Prompts
of 24k tokens never reach the lane through this path. The envelope is still the one that governs the
lane: a whole reply inside the router's 85 s request deadline and TTFT inside claude-mem's 90 s idle
timeout.

## Results

### What loads on the target

Every candidate was served alone on a spare port on MBP-14, with the lane's own flags
(`--slots 1 --max-context 32768 --max-tokens 2048`), and probed with a real observer request built by
the harness. Load time is from process start to `/v1/models` answering; RSS is the server process's
resident size after the probe, which understates Metal memory.

| Candidate | Engine | Load s | Probe wall s | RSS MB | Outcome |
| --- | --- | --- | --- | --- | --- |
| qwen3.5-4b | `locals` | 4.6 | 5.1 | 1594 | loaded, `locals/qwen3.5-4b`, 32768 context |
| gemma-4-e4b | `locals` | 6.2 | 6.3 | 4250 | loaded, `locals/gemma-4-e4b-it`, 32768 context |
| qwen3.5-9b | `locals` | 6.1 | 13.5 | 2430 | loaded, `locals/qwen3.5-9b`, 32768 context |
| lfm3-1.7b | `llama.cpp` | 1.7 | 3.6 | 1644 | loaded, thinking disabled |
| minicpm5-2b | `locals` | 6.1 | 7.4 | 1286 | loaded, `locals/minicpm5-2b`, 32768 context |
| gemma-4-12b | `locals` | 12.2 | 24.0 | 477 | loaded, `locals/gemma-4-12b-it-optiq`, 32768 context |

Every model that answered was served with the same flags, and its record carries the requested id, the
id the server answered under, the argv and the seeds, so a record can always be traced to what ran.
Six facts decided how the candidates could be measured at all, three of them about LFM3:

- The `b11935` release binary predates the architecture it needs: it rejects the GGUF with
  `unknown model architecture: 'lfm3'`, although llama.cpp merged LFM3 support on 2026-10-27.
- Building llama.cpp's source at `19ff817fb25e` (Metal enabled, cmake from a private venv) produces a
  `llama-server` that loads the model; nothing was installed system-wide.
- The chat template defaults to thinking, so the probe returned only `reasoning_content` and empty
  content. Passing `chat_template_kwargs: {enable_thinking: false}` gives a direct answer, which is
  the same condition the lane runs Qwen under (`enable_thinking = false`).

### Baseline: today's lane

Qwen3.5-4B, served on MBP-14 with the lane's flags, over the fixed sample. The sample's 36 observer
rows all carry a stored observation in `tool_uses.observation_id`, so on this sample a skip is a missed
signal rather than sensible noise filtering.

| Measure | Result |
| --- | --- |
| observer replies accepted by the parser | 36/36 |
| observer replies that skipped (`<skip_summary />`) | 25/36 (69%) |
| observations produced | 11, all with every required field |
| observations with an invented path | 1/11 (9%) |
| replies truncated at the 2048-token cap | 0/36 |
| observer whole-reply wall | p50 7.0 s, p90 16.1 s |
| observer TTFT | p50 5.8 s |
| prompt size | 9.2k-20.8k characters, median 13.0k |
| summary cards passing `Oz` | 9/12 |
| summary whole-reply wall | p50 3.1 s |
| cold first-request TTFT (20.8k characters) | 10.6 s |
| warm TTFT at the three largest prompts | 10.6 s, 12.1 s, 10.6 s |
| decode | 55 tokens/s |

Counters are reproducible: with the seed derived from the input id, a second run produced the same
accepted/skipped/observation and summary verdicts for all 36 and 12 inputs. Timings vary between runs
(decode measured 51 tokens/s in one and 55 in the next), so they are reported as measurements, not as
constants.

The three failing summary cards are the two pinned ones and one live card carrying a single
workstream: the zero-workstream card invents a workstream, the array-looking id card returns
`workstreams` as something that is not an array, and the one-workstream card returns a set that is not
"exactly once".

### The core candidates

Each candidate was measured on the baseline's own 36 observer rows and 12 summary cards, at the same
temperatures, token caps and seeds, served alone on its own port with the lane's flags.

| Model | Missed known-worthy | Observations | Complete | Invented paths | Truncated | Wall p50 | Wall p90 | TTFT p50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| qwen3.5-4b (today) | 25/36 | 11 | 100% | 9% | 0% | 7.0 s | 16.1 s | 5.8 s |
| gemma-4-e4b | 4/36 | 32 | 100% | 6% | 0% | 13.1 s | 19.9 s | 5.7 s |
| qwen3.5-9b | 29/36 | 7 | 100% | 0% | 0% | 15.0 s | 32.2 s | 13.6 s |
| lfm3-1.7b | 10/36 | 41 | 73% | 22% | 5.6% | 9.2 s | 25.2 s | 3.0 s |

| Model | Summary `Oz` pass | Workstream ids exact | Summary wall p50 | Cold TTFT | Warm TTFT | Decode |
| --- | --- | --- | --- | --- | --- | --- |
| qwen3.5-4b (today) | 9/12 | 9/9 accepted | 3.1 s | 10.6 s | 10.6 s | 55 tok/s |
| gemma-4-e4b | 11/12 | 11/11 accepted | 4.2 s | 7.9 s | 7.9-20.8 s | 43 tok/s |
| qwen3.5-9b | 11/12 | 11/11 accepted | 6.5 s | 25.8 s | 20.0-22.8 s | 16 tok/s |
| lfm3-1.7b | 0/12 | — | 3.6 s | — | 0.6-0.8 s | 96 tok/s |

What the counters say:

- **Gemma 4 E4B recovers signal the lane drops.** It skips 4 of the 36 rows production judged worth
  an observation, against today's 25, writes every observation with every required field, and keeps
  invented paths at 6% of observations. It also passes the zero-workstream summary card that stalls
  today's lane; only the array-looking-id card still defeats it. Its cost is latency: whole-reply p50
  13.1 s against 7.0 s, still well inside the 85 s deadline.
- **Qwen3.5-9B is worse at this job than the smaller sibling.** It skips 29 of 36 known-worthy rows,
  and it is slow: decode 16 tokens/s against 55, warm TTFT around 20 s on 20.8k-character prompts.
  Scale did not buy recall here.
- **LFM3-1.7B is the fastest and the least faithful.** It prefills 20.8k characters in under a second
  and decodes at 96 tokens/s, but it records almost everything: 41 observations from 36 rows, 22% of
  them carrying a path the input never contained, 27% missing a required field, and two replies cut off
  at the 4096-token cap (one produced 13 observations in one reply). Its `json_object` summaries are
  valid JSON but invent workstream ids on every card, so it fails all 12.

### The extension candidates

Two more were measured because the core comparison left two live questions: whether a much smaller,
faster model keeps the recall, and whether a larger Gemma is more faithful than the E4B.

| Model | Missed known-worthy | Observations | Complete | Invented paths | Truncated | Wall p50 | Summary `Oz` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| minicpm5-2b | 0/36 | 34 | 100% | 41% | 0% | 4.6 s | 8/12 |
| gemma-4-12b | 9/36 | 27 | 100% | 26% | 0% | 32.1 s | 10/12 |

- **MiniCPM5-2B is the fastest and the least accurate.** It never skips and answers with TTFT of
  0.4-0.7 s at 68 tokens/s, but 41% of its observations carry a path the input never contained, and
  two of its twelve summaries fail on shape. Speed alone does not make it a successor.
- **Gemma 4 12B is not better than the E4B at this work.** It skips more (9 against 4), invents more
  (26% against 6%), passes fewer summaries (10 against 11), and is far slower: cold TTFT 37 s, warm
  TTFT up to 43 s, decode 16.8 tokens/s. Its whole-reply times approach the router's 85 s deadline on
  the largest prompts, so it would also compress the lane's headroom.
