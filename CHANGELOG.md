# Changelog

Notable changes to this repository, newest first. Dates are the day the change landed.
Documentation moves are listed here; behaviour changes are listed under the release that
carries them.

## 2026-09-20 — documentation restructure

### Changed

- **`README.md` is now a current-state reference.** It describes what the router does now and
  no longer carries dated verification sections or a revision history.
- **Journals moved to `docs/journals/`** from the top-level `journal/`. The files were
  relocated, not rewritten; entry contents are unchanged and still append-only.
- **Behavior rules no longer carry measurement narrative.** The rule stayed in the README, the
  measurement moved to a research document.
- The `OBSERVER_LOCAL_MODEL` default documented as `local-model`, matching the code; the
  reference install sets its real id in the launchd plist.

### Added

- **`docs/researches/`** — six dated investigation records with their evidence and dead ends:
  the upstream claude-mem fallback review, the 2026-09-18 chain verification, the local-lane
  OOM diagnosis, the YaRN-vs-KV-memory result, the claude-mem retry and quota limits, and the
  router memory profile. Includes an index of questions answered.
- **`CHANGELOG.md`** — this file.
- A journal entry for the restructure, `docs/journals/2026-09-20-002-docs-restructure.md`.

### Removed

- **`## Verification — 2026-09-18`** and **`## Verification — 2026-09-19 prompt guard and YaRN
  experiment`** sections from the README. Their content is preserved in
  `docs/researches/2026-09-18-observer-chain-verification.md` and
  `docs/researches/2026-09-19-observer-lane-oom-diagnosis.md`.
- **`### Why YaRN does not replace this guard`** from the README, now
  `docs/researches/2026-09-19-yarn-vs-kv-memory.md`.

## 2026-09-20 — initial release

### Added

- **`observer-router.py`** — an OpenAI-compatible router that fronts claude-mem's observer
  calls with a Gemini Flash → OpenRouter free → local lane chain, serving
  `/v1/chat/completions` and `/v1/models` on `127.0.0.1:1244`. Includes per-tier budgets,
  failover on empty content, a circuit breaker, deterministic prompt compaction, and an
  AC-power gate on the local tier.
- **`test_observer_router.py`** — eight unit tests covering prompt compaction and the power
  gate.
- **`com.ezou.observer-router.plist`** — the launchd deployment, holding the machine-specific
  values (paths and the local model id) rather than leaving them in code defaults.
- **`journal/`** — four dated entries recording the chain build, the prompt guard and YaRN
  experiment, the post-validation cleanup, and the AC power gate. Moved to `docs/journals/`
  later the same day.
- **`README.md`** — project overview, chain, behaviour contracts, configuration, install,
  operations, and tests.
