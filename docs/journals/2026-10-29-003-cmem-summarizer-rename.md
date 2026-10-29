# The lane model is `lfm3-1.7b-cmem-summarizer`

2026-10-29, correcting the model id in [2026-10-29-001](2026-10-29-001-mbp14-lfm3-cutover.md). That
entry and the router config named `locals/lfm3-observer-q6`, which no longer exists.

## What changed

The model was named after the observer half of what it does, while its adapter and merged weights
already carried the other half. One name now covers the Hugging Face repository, the lane directory,
the model id, the adapter, the merged weights and the converted quants:

| | Before | After |
| --- | --- | --- |
| Hugging Face repository | `ezoushen/lfm3-1.7b-claude-mem-observer` | `ezoushen/lfm3-1.7b-cmem-summarizer` |
| Lane directory on MBP-14 | `~/models/lfm3-observer-q6` | `~/models/lfm3-1.7b-cmem-summarizer-q6` |
| Model id served | `locals/lfm3-observer-q6` | `locals/lfm3-1.7b-cmem-summarizer-q6` |
| Adapter | `lfm3-observer-summary-lora-v2` | `lfm3-1.7b-cmem-summarizer-lora-v2` |
| Merged weights | `lfm3-observer-summary-merged-v2` | `lfm3-1.7b-cmem-summarizer-merged-v2` |

The Hugging Face repository kept its private visibility and now reports 3.3 GB. The rename there
was a move, so the old URL redirects.

## Verification

The lane reports both ids from `/v1/models` (`locals/lfm3-1.7b-cmem-summarizer-q6` and its
`-thinking` variant), the router's `mbp14` provider carries the new model, `/health` reports the chain
intact, and a request through a throwaway router whose chain named only `mbp14` returned content with
`finish_reason: stop`.

That request's content was prose rather than `<observation>` XML, because it was hand-built: the real
observer prompt comes from the pinned claude-mem bundle, which was removed when the workspace was
cleaned up. The format check therefore rests on the earlier harness runs, not on this request.

## Unchanged

`serial = true`, `budget_s = 90`, `min_budget_s = 20`, the shim URL and the Gemma rollback job all
stay as they were. `~/.config/observer-router/config.toml.before-cmem-rename` holds the previous copy
of the config.
