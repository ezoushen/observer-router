# Changelog

Notable changes to this repository, newest first. Dates are the day the change landed.
Documentation moves are listed here; behaviour changes are listed under the release that
carries them.

## 2026-09-20 — upstream filing, public repository, pinned lint tooling

### Added

- **Pinned lint tooling.** `package.json` (private, dev-only), `package-lock.json`, and
  `.markdownlint-cli2.jsonc`. Lint is now one argument-free command, `npm run lint:md`, pinned to
  markdownlint-cli2 `0.17.2` instead of an ad-hoc `npx` invocation. The rules still live in
  `.markdownlint.json`, so editors and CI read the same definition. The router itself remains
  standard-library Python and imports nothing from this tooling.
- **`npm test`** as a shortcut for the Python suite, so lint and tests share one entry point.
- `.gitignore` now ignores `node_modules/`.

### Changed

- **The repository is now public.** Before flipping, both the working tree and the full commit
  history were scanned for credential-shaped strings and committed credential files; both were
  clean, and the only exposed path component is the owner's username.
- **The provider-resolution plan is filed.** Posted as a comment on claude-mem
  [#2785](https://github.com/thedotmack/claude-mem/issues/2785#issuecomment-5747347717)
  (plan-12, Providers & auth), citing this repository as the reference implementation. The plan's
  status moved from "proposed" to "filed, awaiting response", and its first two decision points
  are resolved.
- **`AGENTS.md`** commands table and open-items table updated to match: visibility resolved, the
  upstream contribution filed, and lint reachable as a single pinned command.

### Note on the 100-column rule

`MD013` is set to **100 columns**, not the 80 of Google's style guide. Tables, code blocks, and
headings are exempt, exactly as Google exempts them. 100 is the enforced convention because the
existing journal entries are append-only and already wrapped at that width — rewrapping them
would violate the immutability rule that governs the journal.

## 2026-09-20 — agent conventions and upstream research

### Added

- **`AGENTS.md`** — conventions for coding agents: commands, layout, code and documentation
  rules, the markdown style, production-safety rules, and the eight open owner decisions
  recorded so they are not rediscovered or acted on unreviewed.
- **`docs/plans/`** — durable plans, with an index. First plan: a proposal for one declared
  provider-resolution seam in claude-mem, including a ready-to-file draft, its evidence pack,
  risks, and the decision points that block it.
- **`docs/researches/2026-09-20-claude-mem-provider-extensibility-survey.md`** — the seventh
  investigation record. It surveys all 42 open issues and 183 open pull requests for a model
  selection policy, and finds the override point largely already exists while multi-account
  rotation is already in flight as PR #3941.
- **`.markdownlint.json`** — the repository's markdown rules: 100-column prose, tables, code
  blocks and headings exempt, sibling-only duplicate headings.

### Changed

- **`README.md`** documentation table now lists `docs/plans/` and `AGENTS.md`.
- **The vendored 2026-09-15 report was rewrapped** to the repository's 100-column convention.
  Its wording is unchanged — the reproduced body matches the source word-for-word, 864 of 864
  tokens in order, with all 27 links identical.
- **The provenance note for that document was corrected.** It previously claimed the file was
  byte-for-byte identical, which stopped being true once the wrapping was normalised.

### Fixed

- **Markdown lint is now clean.** `markdownlint-cli2` reported 15 errors, all line-length, all
  in the unwrapped vendored document. Wrapping them fixed the file and, in the process, exposed
  two real bugs in an earlier wrapping attempt: `textwrap` broke inside link text, making a
  line begin with `#3829` and render as a heading, and a first pass left commas orphaned at line
  starts. Both are now guarded by checking that the word sequence and link sequence survive
  wrapping unchanged.

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

- **`docs/researches/`** — dated investigation records with their evidence and dead ends:
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
