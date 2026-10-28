# AGENTS.md — working in observer-router

Project instructions for coding agents. `README.md` gets a human from zero to a running router;
`docs/guides/` and `docs/reference/` state what the router does **today**; the durable decisions
live in `docs/plans/` and `docs/researches/`.

## What this repository is

One standard-library Python process that serves an OpenAI-compatible API on `127.0.0.1:1244` and
fronts claude-mem's observer calls with a fallback chain of named provider instances (types
`gemini`, `openrouter`, `openai`) read from a TOML config file. This machine's live chain is
whatever `~/.config/observer-router/config.toml` says; `/health` shows it.
It exists because claude-mem resolves exactly one provider and has no cross-provider fallback, so
a dead local lane silently breaks observation.

It runs as production infrastructure (`com.ezou.observer-router`) on this machine. Treat changes
as production changes.

## Commands

| Purpose | Command |
| --- | --- |
| Tests, and the documentation check | `python3 -m unittest -v` |
| Documentation check alone | `python3 docslint.py` — no arguments; exit status 1 on a finding |
| Router health | `curl -fsS http://127.0.0.1:1244/health` |
| Router models | `curl -fsS http://127.0.0.1:1244/v1/models` |
| Local lane check | `curl -fsS http://127.0.0.1:1243/v1/models` |
| Restart the service | `launchctl bootout gui/$UID/com.ezou.observer-router` then `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ezou.observer-router.plist` |
| Service log | `/tmp/observer-router.log` |

Exercise one provider in isolation with a throwaway router on another port whose
`OBSERVER_ROUTER_CHAIN` names only it — see `docs/guides/operations.md`.

## Layout

| Path | Contents |
| --- | --- |
| `observer-router.py` | The launchd entry point: imports the package and calls `main()`. |
| `observer_router/` | The router, one module per concern: `settings`, `config`, `power`, `idle`, `breakers`, `prompt`, `repeats`, `providers`, `upstream`, `runtime`, `server`, `app`, plus `failure` and `log`. Standard library only. |
| `tests/` | Tests per module, driven through each module's interface; `tests/support.py` holds the fake upstream and the runtime helpers. |
| `docslint.py` | Checks the Markdown against the style rules below and that relative links resolve. Standard library only. |
| `test_docslint.py` | Tests for `docslint.py`, and the check of this repository's own documentation. |
| `config.example.toml` | The documented config template. The live config is `~/.config/observer-router/config.toml`, outside the repository. |
| `com.ezou.observer-router.plist` | launchd deployment: interpreter and script paths. |
| `docs/README.md` | Index of all documentation. |
| `docs/guides/` | Task-oriented pages: building a chain, claude-mem, install, operations. |
| `docs/reference/` | Current behaviour, rule by rule: configuration, routing, breakers, prompt guard, gates, limitations. |
| `docs/researches/` | Investigations: question, method, evidence, conclusion. |
| `docs/plans/` | Durable plans awaiting or undergoing execution. |
| `docs/journals/` | Dated work log. Append-only. |
| `CHANGELOG.md` | Notable repository changes, newest first. |

## Code conventions

- **Standard library only, and nothing to install.** No third-party dependency of any kind, for the
  router, its tests, or its documentation check. The router must start when a lane or network is
  unhealthy and no package install is possible, and a contributor needs only Python.
- **Keep prompt compaction deterministic.** It must never call another model, so it still works
  when every upstream quota is exhausted.
- **Treat an empty tier response as a failure**, never as a result.
- Every config field and environment knob belongs in the tables in
  `docs/reference/configuration.md`, with its default, and every config field in
  `config.example.toml`.
- Machine-specific values (URLs, model ids, key locations) belong in the local config file, not
  in code defaults or the committed example.
- New behaviour needs a test beside the module it exercises, in `tests/`.

## Documentation conventions

- **`README.md` is a bootstrap page:** what the router is, a quick start, and links. Detail
  belongs in `docs/`.
- **`docs/guides/` and `docs/reference/` are current state only.** No dated verification sections,
  no revision history, no measurement narrative. A rule stays; the measurement that justified it
  moves to a research doc. Keep each folder's `README.md` index and `docs/README.md` in step when
  adding or renaming a page.
- **`docs/researches/`** — one document per question, with method, evidence, and conclusion.
  **Keep negative results**: they are why a rule exists rather than a rejected alternative.
  Update the index in `docs/researches/README.md` when adding one.
- **`docs/plans/`** — objective, success criteria, explicit non-goals, options with a
  recommendation, evidence, and decision points. Update a plan's status when it is executed or
  abandoned; never delete it.
- **`docs/journals/` is append-only.** Never edit, delete, or rename an entry. A correction is the
  next sequenced file that links back. Filename: `YYYY-MM-DD-###-topic.md`.
- **`CHANGELOG.md`** records notable repository changes, newest first.
- **Copied documents keep their wording.** If one is reproduced from elsewhere, note its
  provenance and what was normalised, in the index rather than in the document.

### Markdown style

- Wrap prose at **100 columns**. Tables, code blocks, headings, and links are exempt.
- ATX headings, exactly one H1, a blank line around headings, lists, and fences, and a language
  on every code fence. No doubled blank lines, and exactly one newline at the end of a file.
- **No trailing whitespace.** Use a blank line instead of a two-space hard break.
- **Never let a wrapped line start with punctuation or `#`**: punctuation attaches to the previous
  token, and a line starting with `#` renders as a heading.
- Use reference links for long or repeated links, especially inside tables.
- Run `python3 docslint.py` (the test suite runs it too) before committing. It enforces the rules
  above, which includes refusing a wrapped line that starts with `#`, such as an issue number, and
  it checks that every relative link and anchor resolves. It knows fenced code only: write code
  blocks with fences, not by indenting.

## Production safety

- **Restart with `bootout`/`bootstrap`, never `launchctl stop`** — stopping leaves the job
  unregistered and `KeepAlive` cannot revive it.
- **Verify after any reload**: `/health`, then one real request that returns content.
- **Never weaken or bypass the prompt guard**, and do not raise its limits without reproducing the
  oversized request that motivated them. A single oversized prompt is what killed the lane.
- **The power gate must fail safe.** An unreadable power source keeps the local tier available; a
  failed probe must not take the observer offline.
- **Never commit credentials.** The config names where a key lives (`api_key_env`,
  `api_key_file` + `api_key_var`) and rejects inline keys; no value is ever echoed into a document
  or a log line.
- Touching the local lane, its launchd job, or claude-mem's settings is out of scope for this
  repository. Report, do not silently reconfigure.

## Open items — owner decisions

These are recorded, not started. Do not act on any of them without the owner's answer.

| # | Item | Status |
| --- | --- | --- |
| 1 | **Repository visibility.** `ezoushen/observer-router` is **public**. The working tree and the full commit history were scanned for credentials before the flip; both were clean, and the only exposed path component is the owner's username. | Resolved |
| 2 | **LICENSE.** None added; choosing one is the owner's call. | Not added |
| 3 | **Project location.** It stays at `~/Workspace/local-llm/observer-router`, ignored by the parent `local-llm` repo. Relocating to a top-level path requires a launchd path change. | Deferred |
| 4 | **Quota strategy.** Both free remote tiers hit hard daily caps, leaving the local lane load-bearing. A third provider, the `cursor` bridge, landed 2026-09-24; since 2026-09-29 any number of instances per type (for example several Gemini keys) can be chained. | Multi-instance chain |
| 5 | **Breaker-triggered lane restart.** A hung-but-alive MLX lane still defeats launchd `KeepAlive`; router-triggered `launchctl kickstart -k` is unimplemented. | Open |
| 6 | **Log noise.** claude-mem client disconnects raise `BrokenPipeError` / `ConnectionResetError` tracebacks. Cosmetic, not yet suppressed. | Open |
| 7 | **Upstream contribution.** Filed as a comment on claude-mem #2785 (plan-12, Providers & auth), citing this repository as the reference implementation. Awaiting a response; a PR is only worth scoping if the maintainer accepts the slice. | Filed 2026-09-20 |
| 8 | **Vendored research provenance.** The 2026-09-15 report is a copy from the wider workspace; the original file is untouched. Kept here as the premise evidence. | Recorded |

## Upstream facts worth knowing

- claude-mem has **no merged cross-provider fallback**; cross-provider routing is expected to
  happen in a gateway in front of it.
- In plugin **13.24.8** the observation queue is **in-memory**. `pending_messages` in the database
  is vestigial — nothing writes it. A worker restart discards queued work, so the retry cushion
  holds only while the worker process stays up.
- Plugin **15.4.0** (updated 2026-10-28 12:12) asks observers for `max_tokens` 4096 (default of
  `CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS`) at temperature 0.3 and streams them. Its stream idle
  timeout, `CLAUDE_MEM_LLM_STREAM_IDLE_TIMEOUT_MS` (90 s), runs from the request until the router's
  response headers arrive, so every failed attempt before the answering one counts against it. It
  stores the complete blocks of a reply cut off at `finish_reason: length`, which is why the router
  serves one when no other tier answers.
- The same plugin's My Work summarizer is **not streamed**: temperature 0.15, `max_tokens` 5000,
  reasoning disabled, `response_format: {"type":"json_object"}`, under a 90 s abort. It keeps two
  cards in flight and retries a card every 60 s with an identical body and no backoff, which is the
  loop repeat rotation exists to absorb.
- `--prompt-cache-bytes` is not enforced in mlx_lm's streaming insertion path; sequence count is
  the effective bound on the lane's cache.
