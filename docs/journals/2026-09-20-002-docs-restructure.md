# 2026-09-20-002 — documentation restructured into docs/

## 01:16 — README reduced to current state; research and journals relocated

- Outcome: `README.md` now describes only current behaviour — the two dated
  `## Verification — …` sections, the YaRN rationale, and the dated outage narrative moved out,
  and their rules kept. Investigations became six documents in `docs/researches/` (questions,
  method, evidence, conclusion, including negative results), with an index; the upstream
  claude-mem fallback review from the wider workspace was copied in verbatim as the earliest
  record. `journal/` moved to `docs/journals/` via `git mv` so history follows the renames;
  entry contents are untouched because entries are append-only. Added `CHANGELOG.md`,
  including the initial-release entry.
- Findings: no journal entry referenced a `journal/` path internally, so the move left no
  stale links. Entries do reference `../llm-runtime/…` and `~/.claude-mem/…`; those stay
  verbatim and are explained in `docs/journals/README.md`.
- Verification: `python3 -m unittest` 8/8 pass; markdown lint clean; router, lane, and worker
  health unchanged; production `:1244` reloaded from the plist and serving.
- References: `README.md`, `CHANGELOG.md`, `docs/researches/README.md`,
  `docs/journals/README.md`, `docs/researches/2026-09-20-claude-mem-retry-and-quota-limits.md`
