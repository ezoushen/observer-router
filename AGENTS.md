# AGENTS.md — working in observer-router

Project instructions for coding agents. `README.md` is for humans and states what the router does
**today**; the durable decisions live in `docs/plans/` and `docs/researches/`.

## What this repository is

One standard-library Python process that serves an OpenAI-compatible API on `127.0.0.1:1244` and
fronts claude-mem's observer calls with a Gemini Flash → OpenRouter free → local MLX lane chain.
It exists because claude-mem resolves exactly one provider and has no cross-provider fallback, so
a dead local lane silently breaks observation.

It runs as production infrastructure (`com.ezou.observer-router`) on this machine. Treat changes
as production changes.

## Commands

| Purpose | Command |
| --- | --- |
| Install dev tooling | `npm install` — pinned, needed once |
| Tests | `npm test`, or `python3 -m unittest -v test_observer_router.py` |
| Markdown lint | `npm run lint:md` — no arguments; rules live in `.markdownlint.json` |
| Router health | `curl -fsS http://127.0.0.1:1244/health` |
| Router models | `curl -fsS http://127.0.0.1:1244/v1/models` |
| Local lane check | `curl -fsS http://127.0.0.1:1243/v1/models` |
| Restart the service | `launchctl bootout gui/$UID/com.ezou.observer-router` then `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ezou.observer-router.plist` |
| Service log | `/tmp/observer-router.log` |

Exercise one tier in isolation by starting a throwaway instance with the earlier tiers broken —
see the Operations section of `README.md`.

## Layout

| Path | Contents |
| --- | --- |
| `observer-router.py` | The router. Single file, no third-party imports. |
| `test_observer_router.py` | Unit tests for prompt compaction and the power gate. |
| `com.ezou.observer-router.plist` | launchd deployment, and the home of machine-specific values. |
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
- Every environment knob belongs in the README configuration table, with its default.
- Machine-specific values (paths, `OBSERVER_LOCAL_MODEL`) belong in the plist, not in code
  defaults.
- New behaviour needs a test in `test_observer_router.py`.

## Documentation conventions

- **`README.md` is current state only.** No dated verification sections, no revision history, no
  measurement narrative. A rule stays; the measurement that justified it moves to a research doc.
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
- Run the linter before committing; `.markdownlint.json` holds the rules.

## Production safety

- **Restart with `bootout`/`bootstrap`, never `launchctl stop`** — stopping leaves the job
  unregistered and `KeepAlive` cannot revive it.
- **Verify after any reload**: `/health`, then one real request that returns content.
- **Never weaken or bypass the prompt guard**, and do not raise its limits without reproducing the
  oversized request that motivated them. A single oversized prompt is what killed the lane.
- **The power gate must fail safe.** An unreadable power source keeps the local tier available; a
  failed probe must not take the observer offline.
- **Never commit credentials.** Settings keys are referenced by name; no value is ever echoed into
  a document.
- Touching the local lane, its launchd job, or claude-mem's settings is out of scope for this
  repository. Report, do not silently reconfigure.

## Open items — owner decisions

These are recorded, not started. Do not act on any of them without the owner's answer.

| # | Item | Status |
| --- | --- | --- |
| 1 | **Repository visibility.** `ezoushen/observer-router` is **public**. The working tree and full
history were scanned for credentials before the flip; both were clean. | Resolved |
| 2 | **LICENSE.** None added; choosing one is the owner's call. | Not added |
| 3 | **Project location.** It stays at `~/Workspace/local-llm/observer-router`, ignored by the parent `local-llm` repo. Relocating to a top-level path requires a launchd path change. | Deferred |
| 4 | **Quota strategy.** Both free remote tiers hit hard daily caps, leaving the local lane load-bearing. Options: paid Gemini/OpenRouter capacity, or a third provider. | Open |
| 5 | **Breaker-triggered lane restart.** A hung-but-alive MLX lane still defeats launchd `KeepAlive`; router-triggered `launchctl kickstart -k` is unimplemented. | Open |
| 6 | **Log noise.** claude-mem client disconnects raise `BrokenPipeError` / `ConnectionResetError` tracebacks. Cosmetic, not yet suppressed. | Open |
| 7 | **Upstream contribution.** Filed as a comment on claude-mem #2785 (plan-12, Providers &
auth), citing this repository as the reference implementation. Awaiting a response; a PR is only
worth scoping if the maintainer accepts the slice. | Filed 2026-09-20 |
| 8 | **Vendored research provenance.** The 2026-09-15 report is a copy from the wider workspace; the original file is untouched. Kept here as the premise evidence. | Recorded |

## Upstream facts worth knowing

- claude-mem has **no merged cross-provider fallback**; cross-provider routing is expected to
  happen in a gateway in front of it.
- In plugin **13.24.8** the observation queue is **in-memory**. `pending_messages` in the database
  is vestigial — nothing writes it. A worker restart discards queued work, so the retry cushion
  holds only while the worker process stays up.
- `--prompt-cache-bytes` is not enforced in mlx_lm's streaming insertion path; sequence count is
  the effective bound on the lane's cache.
