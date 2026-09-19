# Working journal — observer router

Journal for this repository: the claude-mem provider chain, its tiers, and the lane outages
that motivated it. claude-mem's own state lives in `~/.claude-mem/`; the local lane the
router calls is a separate project.

Entries were written while the router lived inside a larger personal workspace, so some
references point at sibling paths (`../llm-runtime/…`) that are not part of this repository.
They are kept verbatim because entries are append-only: a correction is a new entry that
links back, never an edit.

Create one file per work item: `YYYY-MM-DD-###-topic.md`. Use the next zero-padded sequence
for that date.

```markdown
# 2026-09-18-001 — topic

## HH:MM — Short title

- Outcome: what changed, found, or decided.
- References: `path/to/file`, command, or report link.
```

Retrieve prior work with `rg "keyword" journal/`.

Journal topic files are append-only. Never edit, delete, or rename an existing entry. If an
earlier entry needs correction, create the next sequenced topic file and link back to the
original.
