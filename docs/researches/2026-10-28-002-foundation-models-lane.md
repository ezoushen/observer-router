# Can macOS 27's Foundation Models serve a router lane?

Date: 2026-10-28

## Question

macOS 27 ships Apple's own on-device models behind the Foundation Models framework, and its "advanced"
model is widely described as reachable from a terminal. Two claims were tested: that the advanced
model can be accessed through Foundation Models, and that projects already wrap it as an
OpenAI-compatible server. If both hold, is it a better tier for the local lane than the Gemma 4 E4B
chosen on the same day?

## Method

1. Read the Framework's Swift interface in the macOS 27 SDK for any model-selection API.
2. Probed both Macs at runtime: `SystemLanguageModel.default.variant`, `contextSize`, availability,
   and `PrivateCloudComputeLanguageModel` availability.
3. Drove Apple's own `fm serve` Chat Completions endpoint with the lane's fixed sample — the same 36
   observer tool uses and 12 My Work cards measured in
   [the lane selection](2026-10-28-001-mbp14-lane-model-selection.md) — and scored the replies with
   claude-mem's own validators and the same record shape.
4. Surveyed published OpenAI-compatible wrappers against what `fm serve` already does.

Summaries were sent with `response_format: json_schema`, hand-built from each card's own workstream
ids (enum, exact `minItems`/`maxItems`, `uniqueItems`). That is a best case, not what claude-mem
sends: see the `json_object` finding below.

## Evidence

### The advanced model is reachable, but never selectable

| Machine | `SystemLanguageModel.default.variant` | `contextSize` |
| --- | --- | --- |
| M5 Max, 128 GB | AFM 3 Core Advanced | 8192 |
| MBP-14, M1 Pro, 16 GB | AFM 3 Core | 4096 |

The SDK declares `SystemLanguageModel.Variant.coreAdvanced3` and `.core3`, but exposes `variant` as a
read-only property. No initializer takes a variant, so an app cannot ask for Core Advanced; the OS
assigns it by hardware. `fm serve` reports one model id, `system`, whatever the machine got.

Apple describes AFM 3 Core Advanced as a 20-billion-parameter sparse model activating 1 to 4 billion
parameters, with its weights held in flash and routed experts paged into DRAM
([Apple ML research](https://machinelearning.apple.com/research/introducing-third-generation-of-apple-foundation-models)).
The hardware bar is an M3-family chip and 12 GB of RAM
([Ars Technica review](https://arstechnica.com/gadgets/2026/09/macos-27-golden-gate-the-ars-technica-review/)),
which MBP-14 clears on neither count, and the runtime probe confirms what the machine actually gets.

`PrivateCloudComputeLanguageModel()` reports `available`, with
`QuotaUsage.Status.belowLimit(isApproachingLimit: false)` on both Macs, so AFM 3 Cloud is reachable
from a Swift process. `fm serve` does not list it.

### `fm serve` is the built-in server, and it has a shape

Measured against `http://127.0.0.1:1976` on the M5 Max:

| Behaviour | Result |
| --- | --- |
| `GET /health`, `GET /v1/models` | `{"status":"fm serve is running"}`, one model: `system` |
| `POST /v1/chat/completions` | works; `stream: false` honoured as JSON |
| Streaming | SSE when asked, and also SSE when not asked |
| `response_format: json_schema` | works; returns bare JSON with no code fence |
| `response_format: json_object` | 400, "Use 'json_schema' instead" |
| `json_schema` pinned to a card's ids | 500 "Failed to parse generated content." on 2 of 12 cards |

The `json_object` rejection is the blocker for claude-mem's summarizer, which sends
`response_format: {"type":"json_object"}`. The wrapper comparison published by
[OpenAppleModelsServer](https://spacecorps.github.io/open-apple-models/SERVER.md) adds that `fm serve`
also returns 400 for `stop`, `n > 1`, `reasoning_effort` and unknown models, ignores `max_tokens`
(only `max_completion_tokens` binds), serves one request at a time, and answers context overflow with
500. Those last items were not re-measured here.

### Measured against the lane's own sample

Observer notes, 36 inputs:

| Model | Accepted | Missed known-worthy | Observations complete | Replies inventing a path | Truncated | Wall p50 | Wall p90 | TTFT p50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Qwen3.5-4B (previous lane) | 36/36 | 25/36 | 11/11 | 1/36 | 0/36 | 7.04 s | 16.12 s | 5.79 s |
| Gemma 4 E4B (live lane) | 36/36 | 4/36 | 32/32 | 2/36 | 0/36 | 13.08 s | 19.94 s | 5.66 s |
| AFM 3 Core Advanced | 36/36 | 4/36 | 23/32 | 10/36 | 0/36 | 10.88 s | 18.41 s | 2.49 s |

My Work summaries, 12 cards:

| Model | Accepted | Workstream ids exact | Wall p50 |
| --- | --- | --- | --- |
| Qwen3.5-4B | 9/12 | 12/12 | 3.12 s |
| Gemma 4 E4B | 11/12 | 12/12 | 4.19 s |
| AFM 3 Core Advanced | 10/12 | 10/10 (schema-pinned) | 1.78 s |

What the failures look like: 9 of 32 observations omit a required field, and 10 replies invent a
path. `tu_86604` rewrote a Claude scratchpad path as `/tmp/scratchpad/…`; `tu_86596` invented two
`zloop` component files; `tu_86591` dropped `.scratch/` from real paths. The two summary 500s hit the
card whose id is a literal array (`["overall-request", "g_90d340249bf3"]`) and `g_85dfb1d9813b`, the
same card Gemma also fails.

Prompt size is not what blocked it: the largest sampled observer prompt is 2,566 tokens
(`fm count-tokens`), against the 8,192-token context on Core Advanced and 4,096 on Core.

### The wrappers exist, and they mostly fix the server, not the model

| Project | Transport | Documented fit |
| --- | --- | --- |
| [OpenAppleModelsServer](https://spacecorps.github.io/open-apple-models/SERVER.md) | Swift library | real `tool_calls`, `json_object` best-effort with fence stripping, concurrency 4 |
| [AFMBridge](https://github.com/kolohelios/afmbridge) | Swift/Vapor, OpenAI and Anthropic | MIT, notarized binary, streaming, tool calling |
| [apfel](https://github.com/hasit/apfel) | CLI and server on `:11434/v1` | MIT, `brew install apfel`, tool calling, context 4,096 |
| [Fameuse](https://github.com/p1atdev/Fameuse) | Swift, OpenAI-compatible | streaming; parts in TypeScript |
| [Foundation-Server](https://github.com/stoobit/Foundation-Server) | Swift/Vapor | small project |
| [apple-on-device-openai](https://github.com/gety-ai/apple-on-device-openai) | SwiftUI app | OpenAI-compatible, streaming |

None of them changes which model the machine gets, and none removes the need for a separate Swift
process beside a standard-library Python router.

## Conclusion

- **The advanced model is real and reachable, but only by hardware.** On an M3-or-newer Mac with 12 GB
  or more, `SystemLanguageModel.default` is AFM 3 Core Advanced with an 8,192-token context; on MBP-14
  it is AFM 3 Core at 4,096. No API requests Advanced, so a lane cannot choose it: the machine either
  has it or does not.
- **It is not a lane replacement on MBP-14.** The observed quality is below the live lane: 10 of 36
  replies invent file paths against Gemma's 2, and 9 of 32 observations are structurally incomplete
  against Gemma's 0. MBP-14 would run the smaller Core model, so this is the better of the two
  variants and still short of Gemma.
- **`fm serve` cannot take claude-mem's summaries at all.** `json_object` is a 400. A wrapper or a
  router-side translation to a permissive `json_schema` would be needed, and that translation is a
  generic OpenAI-compatibility quirk rather than router logic.
- **Kept as a negative result**: the most capable on-device model Apple ships is worse at file-path
  fidelity than every candidate measured for this lane, Qwen3.5-4B included. Its advantage is
  latency — TTFT p50 2.49 s and summary p50 1.78 s, the fastest measured — and speed alone does not
  make a successor.
- **Where it could fit**: an M3-or-newer Mac with 12 GB or more, exposing `fm serve` (or
  OpenAppleModelsServer) as an extra chain entry. That is a new process and a new dependency for a
  lane whose current failure modes are already covered by repeat rotation.

Records: 48 replies under `.scratch/mbp14-model-selection/reports/fm-serve.json`, produced by
`harness/fm-serve-run.js` and scored by `harness/fm-analyze.js`. The scratch is ignored by the
repository.
