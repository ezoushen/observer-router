# 2026-10-28-001 — the lane serves Gemma 4 E4B, and MBP-14 returns to the chain

## 09:20 — the cutover the measurement recommended

- Decision (owner): perform the cutover. Premise:
  [`docs/researches/2026-10-28-001-mbp14-lane-model-selection.md`](../researches/2026-10-28-001-mbp14-lane-model-selection.md)
  measured six candidates on MBP-14. On the same 36 observer inputs, Gemma 4 E4B skipped 4 whose
  observations production had recorded, against Qwen3.5-4B's 25; it was judged supported 19 times
  against 5, useful 29 against 15, preferred blind 25 to 10, and passed 11 of 12 summary cards
  against 9.
- Changed on MBP-14: the `com.ezou.locals.qwen4b.observer` LaunchAgent's `--model` is now
  `mlx-community/gemma-4-e4b-it-4bit`; the job was restarted with `launchctl bootout` then
  `bootstrap`, and serves `locals/gemma-4-e4b-it` on `:8030` with the same flags (`--slots 1`,
  32,768 context, 2,048 max tokens). The first edit used `plutil -replace ProgramArguments.8`,
  which inserted an element instead of replacing it; the plist was restored from the backup and
  edited with `plistlib`, and `plutil -lint` passes.
- Changed in the router config: `chain` is `gemini, openrouter, mbp14, cursor` again, the `mbp14`
  provider's `model` is `locals/gemma-4-e4b-it`, and `min_budget_s` is 25 (was 20), because the
  measured whole-reply p90 is 19.9 s and the maximum 41.5 s. The `ornith` stand-in block stays in
  the file, unreferenced by the chain. The service was restarted with `bootout`/`bootstrap`.
- Verification: `/health` reports `ok`, that chain and that model; one request through `:1244`
  returned content, answered by Gemini as it is by day. The `mbp14` tier was exercised through the
  router itself with the documented throwaway router
  (`OBSERVER_ROUTER_PORT=12944 OBSERVER_ROUTER_CHAIN=mbp14`): a real observation-style request
  returned an `<observation>` block from `locals/gemma-4-e4b-it` in 1.96 s, and the instance was
  stopped with its port confirmed closed. The lane accepts the router's
  `chat_template_kwargs = { enable_thinking = false }` and answers with and without it.
- Revert: restore `~/Library/LaunchAgents/com.ezou.locals.qwen4b.observer.plist.before-gemma-2026-10-28`
  and `~/.config/observer-router/config.toml.before-gemma-2026-10-28`, then `bootout`/`bootstrap`
  both jobs.
- Not changed: claude-mem's settings, the provider's 90 s budget, the `ornith` provider block
  (present but unused), and the lane's launchd label, which still reads `qwen4b`.
- References: [`docs/researches/2026-10-28-001-mbp14-lane-model-selection.md`](../researches/2026-10-28-001-mbp14-lane-model-selection.md),
  `/tmp/throwaway-router.log`, `launchctl list` on MBP-14 (pid 51690).
