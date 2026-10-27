# Changelog

Notable changes to this repository, newest first. Dates are the day the change landed.
Documentation moves are listed here; behaviour changes are listed under the release that
carries them.

## 2026-10-27 — the router is a package

### Changed

- **`observer-router.py` is a thin entry point over `observer_router/`.** The 1,939-line file is
  split by concern: `settings` (environment knobs), `config` (the TOML schema), `power` and
  `idle` (the GPU-lane gates), `breakers` (failures, quota blocks, group strikes), `prompt` (the
  deterministic guard), `providers` (request rewriting) and `upstream` (the HTTP call). `runtime`
  owns the chain and all mutable state, `server` the HTTP surface, `app` start-up. Nothing about
  the HTTP API, the config schema or the environment knobs changed.
- **Tests are split per module.** `test_observer_router.py` is gone; `tests/` holds one file per
  module, driven through each module's interface, with the fake upstream and runtime helpers in
  `tests/support.py`. `python3 -m unittest -v` still runs everything.
- **Mutable state lives in one `Runtime`.** The module globals that `configure()` filled are gone;
  a test builds its own runtime, so two tests share no providers, breakers or samples. The
  power probe, lane fetch and clock are injected.

## 2026-10-27 — standard-library documentation check

### Changed

- **The markdown linter is gone; `docslint.py` replaces it.** The repository now needs nothing
  installed, not even to check its documentation. `python3 docslint.py`, and the test suite
  (`test_docslint.py`), enforce the style rules in `AGENTS.md`: 100-column prose with tables, code
  blocks, headings and references exempt, ATX headings, one H1, blank lines around headings, lists
  and fences, fences that name a language, no trailing whitespace or doubled blank lines, and no
  wrapped line starting with punctuation or `#`. It also checks that every relative link and anchor
  resolves, which the linters never did. `package.json`, `package-lock.json`, `.markdownlint.json`
  and `.markdownlint-cli2.jsonc` are removed, as is the short-lived pip-installed linter.
- **It found one real violation the linters missed**, an issue number opening a wrapped line inside
  a list item; that line was re-wrapped. Three more were fixed earlier, moving line breaks only.
  One append-only journal entry that has such lines is exempted by name in `docslint.py`.
- **Not checked any more:** a few generic rules that `AGENTS.md` never stated, such as list-marker
  style and undefined reference links.

## 2026-10-27 — idle gate for any server

### Added

- **`idle_signal`** on a provider names the `/metrics` counters (`submitted` and `finished`) or
  gauges (`in_flight`, summed over label sets) that mean a lane is busy, so the idle gate works
  for vLLM or any Prometheus-style server, not only Splash. Unset, the Splash counters apply.
  One URL read two ways by two providers is a config error.
- **A start-up warning for `ac_only` on a provider whose `url` names another host**: the power
  gate reads the router's own battery, so it cannot see that host's.

## 2026-10-27 — per-provider limits

### Added

- **`max_prompt_chars`, `break_after`, `break_seconds`** on any provider. They override the
  router-wide prompt guard and breaker settings for that provider alone, so a small-context lane
  can be given a smaller prompt without shrinking it for everyone, and a flaky lane can be skipped
  sooner. `max_prompt_chars` only tightens the router-wide guard. `/health` lists the overrides
  that are set. The config file is read at start-up: a change needs a restart, not a new release.
- **The newest request keeps up to half the prompt budget** when an oversized system message
  would otherwise use all of it (the request was dropped, and the provider answered anyway).
  This applies to the router-wide guard too, which only changes prompts that exceed it.
- **A group block logs each member's interval**, and start-up warns about a `max_prompt_chars`
  above the router-wide limit, which never acts.
- **Sizing a limit from the lane**: `docs/reference/prompt-guard.md` shows how to read a lane's
  context window from `/v1/models` and turn it into a character budget.

## 2026-10-27 — request guard

### Security

- **A request must carry a `Host` the router can trust.** `localhost`, an IP literal, or a name
  in the new `OBSERVER_ROUTER_ALLOWED_HOSTS`; anything else is answered `403`. A web page that
  rebinds its DNS name to `127.0.0.1` could otherwise read `/health` and spend the quotas.
- **A POST must declare `Content-Type: application/json`**, or it is answered `415`, so a
  cross-site form or text post cannot drive the router.
- **Every error sent before the request body is read closes the connection** (`403`, `415`, the
  `404` for an unknown path, and a `400` for an unusable `Content-Length`), so the unread body is
  not parsed as the next request. Before, a page could post text to `http://127.0.0.1:1244/x`
  with a complete JSON POST as its body, and the router served that inner request.
- **`localhost.`** (a trailing dot) is the same host as `localhost`.
- **Each refusal is logged** with its status, the request line, and the offending header.
- **`docs/reference/security.md`** states the model, including that the log file is readable by
  every local account.

## 2026-10-26 — documentation split into docs/

### Documentation

- **`README.md` is a bootstrap page:** what the router does, a five-step quick start with a
  minimal config, and links.
- **`docs/guides/`** (build a chain, use with claude-mem, install, operate) and
  **`docs/reference/`** (configuration, routing and streaming, breakers and quotas, prompt guard,
  GPU-lane gates, limitations) hold what the README held, each folder with an index, under a new
  `docs/README.md`. Research links into the old README now point at the reference pages.
- **`AGENTS.md`** names the new folders and moves the "current state only" rule to them.

## 2026-10-26 — configuration consistency

### Changed

- **`buffer_stream` is accepted on every provider type**, not only `openai`: it is a stream
  policy, not a GPU-lane gate, and hosted tiers reach the token limit too. Default unchanged.
- **`OBSERVER_GROUP_STRIKES`** (default 2) sets how many group members failing in a row block the
  group; it was a fixed constant beside the configurable breaker settings.
- **Invalid `OBSERVER_*` settings stop the router at start-up** (exit status 2, the variable
  named), as an invalid config file does. A value that was not a number used to fall back to the
  default silently, and an out-of-range one, such as `OBSERVER_GROUP_STRIKES=0`, was accepted.

### Documentation

- **The README is about building a chain**, not the reference deployment's: providers, order,
  groups, per-instance behaviour and router-wide policy, with example chains.

## 2026-10-26 — unexplained 429s block a group

### Changed

- **A 429 that names no quota counts as an overloaded model in a group.** Two members in a row
  that get Gemini's bare `RESOURCE_EXHAUSTED` skip every member for the breaker interval, as two
  full-budget timeouts already did. Between 2026-10-21 and 2026-10-26 that 429 was 1,686 of
  Gemini's 1,757 429s and, in 155 of the 262 minutes it occurred, reached all five keys. A 429
  that names its quota still blocks only its own key, until the stated reset.

## 2026-10-26 — replies cut off at the token limit

### Added

- **`buffer_stream`** on `openai` providers holds a stream back until it ends, so a reply cut off
  at the token limit can still fail over. It costs first-byte latency, so it suits a small local
  model with a hard output cap.
- **A cut-off reply is logged** with its prompt and completion size and its first and last 80
  characters, to tell a looping model from a genuinely long answer.
- **A `buffer_stream` instance's budget covers its whole reply**, as a hard bound on every read, so
  a lane still generating when the budget runs out fails over instead of holding a `serial` lane
  for a client that left.
- **A cut-off reply is served when no later tier answers**, with `finish_reason: length`, since
  claude-mem stores the complete blocks of a cut-off reply; a 502 would lose them all.

### Fixed

- **The README's throwaway-router example** used port 1247, which another local service listens
  on; it now uses 12944 and says to check the port first.
- **A reply that ends `finish_reason: length` is no longer served as a success.** Non-streaming,
  and from a `buffer_stream` instance, the next tier answers; an unbuffered stream that was
  already forwarded is logged, ends with a `finish_reason: length` frame, and is not recorded as
  served. A cut-off does not count toward the breaker: it depends on the prompt, not on the
  provider's health.

## 2026-09-30 — group-wide overload block

### Added

- **An overloaded model blocks its whole group.** An HTTP 503 from a group member, or two members
  timing out in a row with their full budget, skips every member for `OBSERVER_BREAK_SECONDS`, so a
  Gemini "high demand" outage costs one or two budgets instead of one per key. Quota 429s stay per
  member.

### Fixed

- **A stream that stalls after its headers now fails over.** The read timeout used to escape
  the handler and drop the client connection; it is now a failed attempt before the first
  content delta, and the chain moves on.

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
