# 2026-10-27-004 — the markdown lint moves from npm to pip

## 03:00 — pymarkdownlnt replaces markdownlint-cli2

- Question: why does a Python project use npm? Answer from the record: it was a convenience. The
  only thing npm did was install `markdownlint-cli2` (2026-09-20-005), and `npm test` was an
  alias for the Python suite. The router never imported anything from Node.
- Decision (owner): replace it with a Python tool. `pymarkdownlnt` 0.9.41 is installed with pip;
  `requirements-dev.txt` pins it and its dependencies, `.pymarkdown` holds the rules, and
  `scripts/lint-md` runs it with no arguments. `package.json`, `package-lock.json`,
  `.markdownlint.json` and `.markdownlint-cli2.jsonc` are gone, and so is the 13 MB `node_modules`.
- Parity, measured before anything was removed: a seeded file with about 30 kinds of mistake was
  scanned by both tools. They flag the same rules for blank lines around fences and lists,
  trailing spaces and tabs, list style and indent, heading level and spacing, bare URLs, inline
  HTML, fenced code without a language, links and images, and a missing final newline. The new
  tool also flags a second H1, emphasis used as a heading and non-descriptive links, and lacks
  the deprecated `MD002`. On the real docs it first reported 94 line-length hits, all table rows:
  its tables extension had to be enabled so rows are exempt, as in the old tool. A newer
  table-style rule (`MD060`, pipe alignment) hit seven tables and is switched off; it is not a
  convention here.
- Found: the new tool refuses a wrapped line that starts with `#`, such as an issue number, which
  `AGENTS.md` already asked authors to avoid and the old tool did not check. Four lines in four
  files had it. Three, in a plan and a research document, were re-wrapped without changing a
  word. The fourth is in `2026-09-20-004-upstream-provider-seam-research.md`, an append-only
  journal entry, so `scripts/lint-md` leaves that one file out by name.
- Verification: a file with a long prose line, trailing spaces, a fence without blank lines and a
  line starting with `#` fails the script, and its long table row is accepted. A fresh virtual
  environment built from `requirements-dev.txt` alone freezes back to the same pins, and the
  script finds the linter on `PATH` when there is no `.venv`. The Python suite passes.
- Review: an independent reviewer found the rule mapping correct and the table, strict and
  heading exemptions preserved, and the set of scanned files identical to the tracked ones. It
  found one rule lost: `MD052`, undefined reference links, which `pymarkdownlnt` 0.9.41 does not
  have. No current document has one (checked by script when this entry was written), so nothing
  needed fixing; the loss is recorded in the CHANGELOG. The script now says how to install the
  linter when it is missing, instead of `exec: pymarkdown: not found`.
- Not changed: the old config files are not replaced for editors; a contributor using the
  VS Code markdownlint extension needs its own settings, or the pymarkdown extension.

## 04:00 — A test failed on a loaded machine

- Outcome: while verifying, the suite failed six times in `EnvironmentTests`, then passed on the
  next run with no code change. The machine's load average was above 60, and starting Python
  took over ten seconds. The test starts the router as a subprocess and gave it five seconds; on
  a timeout it reported "the router started despite ...", which was false. The limit is now 60
  seconds, and the message says the router did not exit in time. A router that wrongly starts
  still fails the test, only later.
