# Research records — observer router

Investigations and measurements behind the rules the [README](../../README.md) states. The
README describes what the router does **now**; these documents record what was measured to
justify it, including the dead ends.

A journal entry in `docs/journals/` records what was done in a work item. A research document
here records what was found. They cross-link.

| Date | Document | Question it answers |
| --- | --- | --- |
| 2026-09-15 | [claude-mem provider fallback discussions on GitHub](2026-09-15-claude-mem-provider-fallback-upstream.md) | Does claude-mem already have cross-provider fallback? (No — so routing belongs in a gateway.) |
| 2026-09-18 | [Observer chain verification](2026-09-18-observer-chain-verification.md) | Does each tier serve, and how often is the primary hop insufficient? |
| 2026-09-19 | [Observer lane OOM diagnosis](2026-09-19-observer-lane-oom-diagnosis.md) | Is the local lane leaking, or is a prompt simply too large? (Prompt too large.) |
| 2026-09-19 | [YaRN vs KV memory](2026-09-19-yarn-vs-kv-memory.md) | Would a longer positional window let the character guard be relaxed? (No.) |
| 2026-09-20 | [claude-mem retry and quota limits](2026-09-20-claude-mem-retry-and-quota-limits.md) | Are the free quotas enough, and does skipping the local tier lose work? (No, and no.) |
| 2026-09-20 | [Router memory profile](2026-09-20-observer-router-memory-profile.md) | Does the router leak under oversized payloads? (No.) |
| 2026-09-20 | [Provider and routing extensibility survey](2026-09-20-claude-mem-provider-extensibility-survey.md) | Does claude-mem expose a model selection policy, and is multi-account rotation already filed? |

## Provenance

The 2026-09-15 document is reproduced from a research report produced in the wider personal
workspace before this repository existed. Its wording is unchanged: the reproduced body matches
the source word-for-word (864 of 864 tokens, in order) and all 27 links are identical.

What was normalised, and nothing else: line wrapping to this repository's 100-column convention,
a trailing-space hard break replaced by a blank paragraph break, and a closing
"Why this matters here" section added to connect the finding to this project. The original file
in the wider workspace is untouched.

## What belongs here

- A question, the method used to answer it, the evidence, and the conclusion.
- Negative results and corrections, because they are the reason a rule exists.
- Dated snapshots, clearly labelled as snapshots. Quota and tier-mix numbers are observations
  of a moment, not live guarantees.

## What does not

- Current behaviour of the router — that is the README.
- Chronological work log — that is `docs/journals/`.
