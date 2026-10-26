# 2026-10-27-005 — the documentation check is standard-library Python

## 04:40 — `docslint.py` replaces the pip-installed linter

- Request: no editor-specific tooling, and pure Python throughout; the markdown lint had just
  moved from npm to a pip-installed linter (2026-10-27-004), which still left a third-party
  dependency and a virtual environment to set up.
- Decision (owner): a project whose router is "standard library only" should need nothing
  installed to check its documentation either. This supersedes the previous entry, which also
  mentioned an editor extension; that line is history and is left as written.
- Outcome: `docslint.py`, about 200 lines of standard library, enforces the rules `AGENTS.md`
  states: 100-column prose with tables, code blocks, headings and references exempt, ATX headings,
  one H1, blank lines around headings, lists and fences, fences that name a language, no trailing
  whitespace or doubled blank lines, and no wrapped line that starts with punctuation or `#`. It
  also checks that every relative link and anchor resolves, which no earlier tool did. It runs
  as `python3 docslint.py` and inside `python3 -m unittest`. `.pymarkdown`, `requirements-dev.txt`,
  `scripts/lint-md` and the `.venv` are removed.
- Verification: 28 tests, 26 of them one rule each on a small document and 22 mutations of the
  checker, each caught by a test. On the real documents it found one violation both earlier
  tools had missed, an issue number opening a wrapped line inside a list item; the line was
  re-wrapped without changing a word. The one journal entry that cannot be edited to meet a rule
  is exempted by name, and a test fails if that exemption ever stops being needed.
- Dropped on purpose: generic rules that `AGENTS.md` never stated, such as list-marker style,
  emphasis style and undefined reference links.
- References: `docslint.py`, `test_docslint.py`.

## 05:30 — Review: the first version read too little Markdown

- Outcome: an independent reviewer tried the checker on valid Markdown it might meet and found
  gaps, most now fixed with a test each. Fences: only triple backticks were recognised, so a
  `~~~` or four-backtick fence was checked as prose, and an inner fence closed an outer one; a
  fence is now closed only by its own character and at least its length. Files: one that is not
  UTF-8 crashed the checker, and Windows line endings were hidden by the reader; the first is
  now a finding and the second is refused as trailing whitespace. Links: footnotes, `<...>`
  targets, parentheses in a path, single-quoted and parenthesised titles, an uppercase scheme and
  a root-relative `/docs/x.md` were mis-read, a link that leaves the repository passed, and a
  heading with a closing `##` got the wrong anchor. Lists: `2.` or a year opening a wrapped line
  was taken for a list, and a numbered item after a wrapped item for text.
- Outcome: three rules the checker enforced were not in `AGENTS.md` (a language on every fence,
  no doubled blank lines, one final newline); they are written down now. The test that the
  checker reads every document now compares against the files on disk, and the reference-line
  exemption has a test that only it can satisfy.
- Not handled, on purpose: indented code blocks, HTML comments and front matter (the checker says
  so and `AGENTS.md` tells authors to use fences), a heading inside a blockquote, a table
  without leading pipes, percent-encoded anchors, and a missing `README.md`, which fails loudly
  by name. A mini-checker that guessed at these would be wrong in new ways.
- Verification: 15 mutations of the new behaviour, each caught by a test.
