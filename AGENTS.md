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
| Install dev tooling | `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` — pinned, needed once |
| Tests | `python3 -m unittest -v test_observer_router.py` |
| Markdown lint | `scripts/lint-md` — no arguments; rules live in `.pymarkdown` |
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
| `observer-router.py` | The router. Single file, no third-party imports. |
| `test_observer_router.py` | Tests for config loading, request building, chain failover over HTTP, cut-off replies and buffered streams, group blocks, compaction, the power and idle gates, and quota blocks. |
| `scripts/lint-md` | Runs the markdown linter over the repository, with no arguments. |
| `requirements-dev.txt` | The pinned markdown linter and its dependencies; development only. |
| `.pymarkdown` | The markdown rules. |
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

- **Standard library only.** No third-party runtime dependencies; the router must start when a
  lane or network is unhealthy and no package install is possible.
- **Keep prompt compaction deterministic.** It must never call another model, so it still works
  when every upstream quota is exhausted.
- **Treat an empty tier response as a failure**, never as a result.
- Every config field and environment knob belongs in the tables in
  `docs/reference/configuration.md`, with its default, and every config field in
  `config.example.toml`.
- Machine-specific values (URLs, model ids, key locations) belong in the local config file, not
  in code defaults or the committed example.
- New behaviour needs a test in `test_observer_router.py`.

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
- ATX headings, exactly one H1, a blank line around headings, lists, and fences.
- **No trailing whitespace.** Use a blank line instead of a two-space hard break.
- **Never let a wrapped line start with punctuation or `#`**: punctuation attaches to the previous
  token, and a line starting with `#` renders as a heading.
- Use reference links for long or repeated links, especially inside tables.
- Run the linter before committing; `.pymarkdown` holds the rules. It also refuses a wrapped line
  that starts with `#`, such as an issue number.

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
- Plugin **15.2.1** (running since at least 2026-10-26) asks observers for `max_tokens` 4096 at
  temperature 0.3 and streams them. Its stream idle timeout, `CLAUDE_MEM_LLM_STREAM_IDLE_TIMEOUT_MS`
  (90 s), runs from the request until the router's response headers arrive, so every failed
  attempt before the answering one counts against it. It stores the complete blocks of a reply cut
  off at `finish_reason: length`, which is why the router serves one when no other tier answers.
- `--prompt-cache-bytes` is not enforced in mlx_lm's streaming insertion path; sequence count is
  the effective bound on the lane's cache.
