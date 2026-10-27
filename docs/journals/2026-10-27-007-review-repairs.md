# 2026-10-27-007 — review repairs

## 10:40 — close the independent review's findings

- Outcome: the independent review of the module split found that the retained prompt floor
  (`OBSERVER_MIN_RETAINED_CHARS`) no longer reached config validation and that an empty
  `OBSERVER_ROUTER_CONFIG` had changed meaning. Commit `6eb2ba0` restored both and the test rules
  the split dropped (210 tests green, up from 200). A re-review then named one residual
  test-parity gap (missing gauge, gauge-only lane, a type with no quota parser), a missing test for
  the config-path repair, and documentation drift (empty-value semantics, the layout's module list,
  this journal). This entry lands with those fixes; the earlier entry stays as written.
- References: commits `3b852cc` and `6eb2ba0`, `tests/`, `docs/reference/configuration.md`,
  `AGENTS.md`, `.scratch/refactor-modules/reports/spec-review.md`. No earlier entry changed.
