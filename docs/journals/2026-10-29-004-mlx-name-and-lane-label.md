# The repository says MLX, and the lane job is renamed

2026-10-29, following [2026-10-29-003](2026-10-29-003-cmem-summarizer-rename.md). Two names were still
wrong after that rename.

## The Hugging Face repository now names its format

`ezoushen/lfm3-1.7b-cmem-summarizer` became `ezoushen/lfm3-1.7b-cmem-summarizer-mlx`. The repository
holds MLX weights — 6-bit `model.safetensors` at the root and 8-bit under `q8/` — loadable only by
`mlx-lm` with the unmerged `lfm3` module, plus a PEFT adapter under `adapter/`. A visitor expecting
transformers weights would have been misled. It stays private at 3.3 GB, and the previous name
redirects.

The card title reads "LFM3-1.7B MLX". It was not named `-mlx-4bit`: there is no 4-bit file in the
repository, and 4-bit was measured and rejected on the summary workload (0/12 cards passing).

## The launchd job is `com.ezou.locals.cmem.summarizer`

The lane job was still `com.ezou.locals.lfm3.observer`, which named a role the label no longer
describes. Renamed across the plist, its log paths (`/tmp/locals-cmem-summarizer.log`) and the
sparkDash launcher in `~/lanes/locals-observer/`, which drives the label. The old plist was removed
after bootout; the job bootstrapped and answered `/health` on 8030.

## Unchanged

The served model id stays `locals/lfm3-1.7b-cmem-summarizer-q6`, so the router's `mbp14` provider,
`serial = true`, the budgets and the shim URL needed no edit. The Gemma rollback job
(`com.ezou.locals.gemma.observer`) is untouched.

## Verified

`/v1/models` reports `locals/lfm3-1.7b-cmem-summarizer-q6` and its `-thinking` variant, `/health`
answers 200, the sparkDash launcher reads the new label, `LaunchAgents` holds three plists (lane,
shim, Gemma rollback), and the router reports the chain with the unchanged model id.
