# Changelog

Notable changes to this repository, newest first. Dates are the day the change landed.
Documentation moves are listed here; behaviour changes are listed under the release that
carries them.

## 2026-09-30 — chat template arguments

### Added

- **`chat_template_kwargs`** on `openai` providers, sent as-is. Qwen served by mtplx ignores
  `reasoning_effort` and returns empty content until `{ enable_thinking = false }` is passed here.

## 2026-09-29 — rotating provider groups

### Added

- **`[groups.<name>]`** with `members = [...]`. A chain entry naming a group rotates across its
  members: each request starts one member further on, and the others follow as in-group
  fallbacks before the chain moves on. `/health` reports `groups`. A provider may be reachable
  only once through the chain.

## 2026-09-29 — config file with named provider instances

### Changed

- **Providers come from a TOML config file**, `OBSERVER_ROUTER_CONFIG` (default
  `~/.config/observer-router/config.toml`); `config.example.toml` is the template. Each
  `[providers.<name>]` table is an instance of type `gemini`, `openrouter`, or `openai`, several
  instances may share a type, and `chain` orders them by name. Breakers and quota blocks are
  per instance. `OBSERVER_ROUTER_CHAIN` still overrides the chain, now by instance name.
- **Keys are referenced, never inlined**: `api_key_env`, or `api_key_file` + `api_key_var`
  (dotenv, or JSON when the path ends `.json`; in dotenv the last assignment wins). An inline
  `api_key`, unknown fields, and wrongly typed values are rejected at start-up; an invalid config
  exits with status 2.
- **The GPU-lane gates are per instance**: `serial`, `ac_only`, `idle_metrics`, and
  `idle_seconds` on `openai` instances. `/health` reports `providers`, `power.ac_only`, and
  `idle_gate.<name>`.

### Removed

- The router no longer reads claude-mem's `settings.json` for keys, nor the per-tier
  environment variables `OBSERVER_GEMINI_MODEL`, `OBSERVER_OPENROUTER_MODEL`, `OBSERVER_CURSOR_*`,
  `OBSERVER_LOCAL_URL`, `OBSERVER_LOCAL_MODEL`, `OBSERVER_LOCAL_REASONING_EFFORT`,
  `OBSERVER_LOCAL_SERIAL`, `OBSERVER_LOCAL_AC_ONLY`, `OBSERVER_LOCAL_IDLE_METRICS`,
  `OBSERVER_LOCAL_IDLE_SECONDS`, and `CLAUDE_MEM_SETTINGS`. Their values move into the config
  file; an `api_key_file` may still point at claude-mem's settings.

## 2026-09-24 — cursor tier, quota-reset blocks, and the idle gate

### Added

- **Idle gate for the local tier.** `OBSERVER_LOCAL_IDLE_METRICS` lists interactive lanes'
  `/metrics` URLs; the local tier runs only after all of them have been idle for
  `OBSERVER_LOCAL_IDLE_SECONDS` (30 s), read from Splash's request counters by a 2 s sampler.
  Off when unset. An unreadable lane ages into idle.

- **Quota-reset blocks.** A Gemini or OpenRouter 429 that states its reset skips the tier until
  then, instead of re-probing it every 60 s. Gemini `PerDay` quotas block until midnight
  Pacific, other Gemini quotas for `retryDelay`, and OpenRouter until `X-RateLimit-Reset`.
  `/health` reports the reason per tier. Error bodies are now read up to 16 KiB, because
  Gemini names its quota well past the old 300-character cut, and a body read for the
  reasoning-retry check no longer comes back empty for the log line.

- **`cursor` tier.** An OpenAI-compatible hop to a local cursor-api-proxy bridge
  (`OBSERVER_CURSOR_URL`, default `127.0.0.1:8765`) serving `composer-2.5`
  (`OBSERVER_CURSOR_MODEL`), with a 40 s budget. It is opt-in through `OBSERVER_ROUTER_CHAIN`
  and sits before the local lane. The bridge key comes from `OBSERVER_CURSOR_API_KEY` or the
  `CURSOR_BRIDGE_API_KEY` line of `OBSERVER_CURSOR_ENV_FILE`. It is remote compute, so it runs
  on battery and does not take the local lane lock.

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
