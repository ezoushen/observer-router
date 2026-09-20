# 2026-09-20-005 — lint tooling pinned, repository public, seam filed upstream

## 11:35 — three owner decisions executed

- Outcome: (1) the linter is now a real, pinned, argument-free command — added `package.json`
  (`private`, dev-only), `package-lock.json` and `.markdownlint-cli2.jsonc`, with rules still in
  `.markdownlint.json` so editors and CI read one definition; `npm run lint:md` replaces the
  ad-hoc `npx` invocation, and `npm test` wraps the Python suite. (2) The repository was made
  **public**. (3) The provider-resolution proposal was filed as a comment on claude-mem **#2785**
  (plan-12, Providers & auth), citing this repository as the reference implementation.
- Findings: no credential-shaped string and no committed credential file exists in either the
  working tree or the full commit history; the only exposed path component is the owner's
  username. Deliberately documented deviation: `MD013` is 100 columns rather than Google's 80,
  because the journal entries are append-only and already wrapped at that width — rewrapping them
  would break the immutability rule, so 100 is the convention that can actually be enforced.
- Verification: `npm run lint:md` reports 0 errors across 21 files with no arguments, proving the
  config is picked up (the default would be 80 columns); `npm test` 8/8 pass; `node_modules/`
  confirmed ignored by git; anonymous `curl` to the repo API, README raw URL and repo page all
  return HTTP 200; the filed comment renders and contains the live reference link.
- Open: awaiting the maintainer's response on #2785. A PR is only worth scoping if the slice is
  accepted (plan decision point 3).
- References: `package.json`, `.markdownlint-cli2.jsonc`, `AGENTS.md`, `CHANGELOG.md`,
  `docs/plans/2026-09-20-claude-mem-provider-resolution-seam.md`,
  <https://github.com/thedotmack/claude-mem/issues/2785#issuecomment-5747347717>
