# LFM3 fine-tune for the MBP-14 lane

Question: can a fine-tune of a small local model serve this lane's observer calls *and* its My Work
summaries well enough to replace Gemma 4 E4B?

Answer: no. The observer half is better than the live lane on recall, completeness and latency, but
the summarizer stays far below it, and one adapter has to serve both workloads.

## Why the question was worth asking

The lane answers two different requests from claude-mem, and Gemma 4 E4B is a general model that
was never trained on either. Both workloads have production ground truth on this machine: every
observation claude-mem stored came from a tool use that the plugin judged worth keeping, and every
summary card carries a human copy that the plugin itself accepted.

## Method

Corpus, from `~/.claude-mem/claude-mem.db`:

- Pairs built from `tool_uses` JOIN `observations`, partitioned by `t.id % 4` across four chunks.
  62,004 candidate rows, 54,265 kept after type, title/narrative, facts, concepts and path filters,
  deduped. Rebuilt with `tool_input`, `tool_response`, `project`, `session_id` and `tool_epoch` to
  54,405 pairs.
- Credential scrub runs to a fixpoint: gitleaks' 220-rule corpus plus structural checks for PEM,
  JWT, URL passwords and Basic auth. A hit drops the whole pair, never a partial redaction. The gate
  exits zero with 0 gitleaks, 0 structural and 0 invalid JSON findings.
- Prompts are assembled from claude-mem **15.4.0**'s own `modes/code.json` shapes, targets rendered
  to the `<observation>` block the plugin parses. 53,849 pairs kept: 52,772 train, 1,077 val, 36
  harness inputs held out, 145 cross-chunk duplicates removed, 375 dropped for an empty subtitle.

Training used bf16 base weights with an fp32 adapter, completion-only loss, gradient checkpointing
and per-step MPS cache release. One 2,962-token example peaks 24.9 GB forward; without
checkpointing a 30-step run exhausted 65 GiB. Calibration measured 5.26 M trainable parameters
(0.30% of the model) at 14.76 s/step.

The harness pins claude-mem 15.3.0 and replays a fixed 36-observer / 12-summary sample with the
bundle's own prompts and validators (`Oz` for summaries), so a model is scored by the same code
production uses.

## Evidence

Observer-only adapter, 623 steps on a 6k subset, final loss 0.706, val 0.804, merged to bf16 and
converted with `mlx-lm` at bf16/q8/q6/q4:

| Model | Accepted | Missed worthy | Complete | Genuinely invented | Truncated | p50 | p90 | TTFT | Summaries |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Gemma 4 E4B (live) | 36/36 | 4/36 | 32/32 | 0 | 0 | 13.08 s | 19.94 s | 5.66 s | 11/12 |
| FT q6 | 35/36 | 1/36 | 34/34 | 0 | 1 | 8.65 s | 13.91 s | 6.84 s | 2/12 |
| FT q8 | 33/36 | 1/36 | 32/32 | 1 | 3 | 8.26 s | 19.33 s | 6.25 s | 2/12 |
| FT q4 | 31/36 | 0/36 | 31/31 | 0 | 5 | 7.03 s | 14.66 s | 4.52 s | 0/12 |
| FT bf16 | 32/36 | 2/36 | 30/30 | 0 | 4 | 21.51 s | 40.58 s | 15.85 s | 2/12 |
| Base LFM3 q8 | 3/36 | 0/36 | 3/3 | 0 | 33 | 28.01 s | 43.37 s | 19.14 s | 0/12 |

The base-model row is the control that makes the rest meaningful: unmodified LFM3 runs to the
4096-token cap on 33 of 36 observer requests and never produces a passing summary.

Combined adapters, trained to serve both workloads from one model. The first used a 29% summary mix
and a fresh OneCycle at 1e-4; the second used a 16.9% mix and 3e-5, continuing from the observer
adapter:

| Model | Observers accepted | Genuinely invented | p50 / p90 / TTFT | Summaries |
| --- | --- | --- | --- | --- |
| Combined v1 q8 | 24/36 | 0 | 7.94 / 16.61 / 5.22 s | 5/12 |
| Combined v2 q8 | 31/36 | 0 | 6.50 / 9.77 / 4.72 s | 4/12 |
| Combined v2 q6 | 26/36 | 0 | 5.73 / 9.17 / 4.25 s | 1/12 |

## Two measurements that were wrong, and why

**Invented paths.** The harness checked each cited path against the tool input and response only.
Production's prompt also carries the session's `<user_request>`, and an observer that records a file
the user asked it to read is correct, not inventive. Recounted against the full prompt, Gemma falls
from 2 to 0, the observer-only adapter from 6–9 to 0–1, and both combined adapters to 0. The earlier
"worse than Gemma on path fidelity" reading was an artefact of a narrow comparison.

**The summary corpus.** Stored copies nest their payload under `copy`
(`{fingerprint, copy:{…}, model, generatedAt}`). Reading the outer object made every candidate fail
`Oz` with "workstreams must be an array", which nearly hid the corpus. Read correctly, the live Work
View offers **420 cards, 350 distinct inputs, 346 with a valid copy**, and **457 pairs** after
capping variants at three per input. A further 422 stored copies describe historical card states
whose inputs cannot be rebuilt: the card builder exists only inside the plugin's minified worker,
and no request body is retained. Provenance matters too — 271 of 525 valid copies came from
`locals/qwen3.5-4b`, the lane whose failures repeat rotation exists to absorb.

## Conclusion

- **The observer half is a genuine win**: 35/36 accepted at q6 against Gemma's 36/36, but 1/36
  missed against 4/36, more complete observations (34/34 against 32/32), equal path fidelity and a
  p90 of 13.91 s against 19.94 s. It fails only on the single truncated `grep` request.
- **The summarizer is the binding constraint.** One workload of 457 pairs cannot be taught to a
  1.7B model to Gemma's standard; the second run, with a lower learning rate and a smaller summary
  share, improved observers from 24 to 31 accepted while summaries fell from 5 to 4. Six-bit
  quantisation makes the JSON emission collapse to 1/12.
- **The observer win cannot ship alone.** The lane answers both request kinds with one model, and
  routing by request shape would put claude-mem-specific logic in a router that must stay
  client-agnostic.
- Therefore the lane stays on Gemma 4 E4B.

## What would change this answer

- A larger base model, or one already competent at strict JSON, so that capacity is not the limit.
- A summary corpus an order of magnitude larger, with inputs retained at request time so a card's
  history can be replayed.
- Serving two adapters side by side and letting a client choose, which the router cannot do without
  becoming client-aware.

## Reproducing it

The corpus, training, merge, conversion and harness scripts live in `.scratch/finetune-corpus/`,
outside the repository's shipped code; nothing here is part of the router. Provenance for the SFT set
is `sft/build-report.json`, which records the mode file's sha256 and the dataset id.
