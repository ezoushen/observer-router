# 2026-09-20-003 — markdown linted and agent conventions written

## 01:40 — README and docs brought to a documented markdown standard; AGENTS.md added

- Outcome: researched the community and academic guidance rather than guessing — Google's
  Markdown style guide (ATX headings, a single H1, no trailing whitespace, reference links for
  long or repeated links especially in tables), the CommonMark spec, `markdownlint`'s rule set,
  and the empirical README literature (arXiv 1802.06997, 4,226 manually annotated README
  sections across 393 repositories, which found READMEs are strong on "What/How" and often weak
  on purpose and status). Added `.markdownlint.json` (100-column prose; tables, code blocks and
  headings exempt) and `AGENTS.md` carrying the project's commands, layout, code and
  documentation conventions, markdown style, production-safety rules, and the eight open owner
  decisions.
- Findings: the first lint run reported 15 errors, all line-length, all in the copied
  2026-09-15 report — the rest of the repository already conformed. Wrapping that file exposed
  two real hazards in my own first attempt: `textwrap` broke inside link text and produced a
  line beginning `#3829`, which renders as a heading (caught by MD018), and a second pass left
  commas orphaned at line starts. Both attempts were discarded in favour of a wrapper that keeps
  links and code spans atomic and attaches punctuation to the preceding token.
- Verification: `markdownlint-cli2` 0 errors across 18 files; `python3 -m unittest` 8/8 pass;
  every relative link and anchor resolves; the reproduced document matches its source
  word-for-word (864 of 864 tokens in order) with all 27 links identical.
- References: `AGENTS.md`, `.markdownlint.json`, `docs/researches/README.md`,
  `docs/researches/2026-09-15-claude-mem-provider-fallback-upstream.md`, `CHANGELOG.md`
