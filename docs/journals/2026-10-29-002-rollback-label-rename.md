# The rollback job is now `com.ezou.locals.gemma.observer`

2026-10-29, correcting [2026-10-29-001](2026-10-29-001-mbp14-lfm3-cutover.md). That entry's rollback
command names `com.ezou.locals.qwen4b.observer`, which no longer exists.

## What changed

The Gemma rollback job was labelled after the model it replaced two cutovers earlier, so the rollback
instruction read as if it restored Qwen3.5-4B. It serves Gemma 4 E4B, and now says so:

- `~/Library/LaunchAgents/com.ezou.locals.qwen4b.observer.plist` was renamed to
  `com.ezou.locals.gemma.observer.plist`, and its `Label` value with it, because launchd refuses a
  plist whose filename and label disagree.
- Its log paths moved from `/tmp/locals-qwen4b.log` to `/tmp/locals-gemma.log`.
- `plutil -lint` passes, the label reads `com.ezou.locals.gemma.observer`, and the job's program
  arguments still load `mlx-community/gemma-4-e4b-it-4bit` on 8030.

The job is not loaded: the lane serves the fine-tune, and the Gemma plist stays on the machine as the
rollback.

## The corrected rollback

```sh
launchctl bootout gui/501/com.ezou.locals.lfm3.observer
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.ezou.locals.gemma.observer.plist
```

then restore `~/.config/observer-router/config.toml` from
`config.toml.before-lfm3-2026-10-29` and restart the router.

## What the same day removed

Qwen3.5-4B and its caches were deleted from MBP-14: four model directories (23.4 GB), two cache
entries, and the pre-Gemma plist backup that still named that model. The Qwen3.5-9B cache, the Bonsai
weights and an unrelated `tf4b` virtual environment went too, at the owner's instruction, which took
the machine from 715 GB to 741 GB free.
