#!/usr/bin/env python3
"""Tests for docslint, the standard-library Markdown checker, and the check of this repository's
own documentation against the rules in AGENTS.md."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("docslint.py")
SPEC = importlib.util.spec_from_file_location("docslint", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
docslint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = docslint
SPEC.loader.exec_module(docslint)


def rules(text: str) -> list[tuple[int, str]]:
    """(line, rule) for every finding in a document that has no links to resolve."""
    return [(finding.line, finding.rule) for finding in docslint.check_text(text)]


GOOD = """# Title

A paragraph that is short.

- one
- two

```text
code
```

## Section

| a | b |
| --- | --- |
| 1 | 2 |
"""


class StyleRuleTests(unittest.TestCase):
    """Each rule of the Markdown style in AGENTS.md, on a document that breaks only it."""

    def test_a_clean_document_has_no_findings(self):
        self.assertEqual(rules(GOOD), [])

    def test_prose_is_wrapped_at_100_columns(self):
        long = "word " * 24 + "end"  # 123 characters, with whitespace past the limit
        self.assertEqual(rules(f"# T\n\n{long}\n"), [(3, "line-length")])
        self.assertEqual(rules(f"# T\n\n{'word ' * 19}end\n"), [])  # 98 columns

    def test_tables_code_headings_and_references_are_exempt_from_the_limit(self):
        long = " ".join(["word"] * 40)
        text = (f"# {long}\n\n| {long} |\n| --- |\n\n```text\n{long}\n```\n\n"
                f"[ref]: https://example.com/{'x' * 120} \"a title with several words\"\n")
        self.assertEqual(rules(text), [])

    def test_a_long_unbreakable_token_is_not_a_violation(self):
        # Nothing could be wrapped: no whitespace after the limit.
        self.assertEqual(rules(f"# T\n\nsee https://example.com/{'x' * 120}\n"), [])

    def test_trailing_whitespace_is_refused_including_a_hard_break(self):
        self.assertEqual(rules("# T\n\nends with two  \nnext\n"), [(3, "trailing-space")])
        self.assertEqual(rules("# T\n\nends with a tab\t\n"), [(3, "trailing-space")])

    def test_two_blank_lines_in_a_row_are_refused(self):
        self.assertEqual(rules("# T\n\n\ntext\n"), [(3, "blank-lines")])

    def test_a_document_ends_with_exactly_one_newline(self):
        self.assertEqual(rules("# T\n\ntext"), [(3, "final-newline")])
        self.assertEqual(rules("# T\n\ntext\n\n"), [(4, "final-newline")])

    def test_headings_are_atx_not_underlined(self):
        self.assertIn((1, "heading-style"), rules("Title\n=====\n\ntext\n"))
        self.assertIn((3, "heading-style"), rules("# T\n\nSection\n-------\n\ntext\n"))

    def test_a_horizontal_rule_after_a_blank_line_is_not_a_heading(self):
        self.assertEqual(rules("# T\n\ntext\n\n---\n\nmore\n"), [])

    def test_exactly_one_h1(self):
        self.assertEqual(rules("## Only a section\n\ntext\n"), [(1, "one-h1")])
        self.assertEqual(rules("# One\n\ntext\n\n# Two\n\ntext\n"), [(5, "one-h1")])

    def test_h1_inside_a_code_fence_does_not_count(self):
        self.assertEqual(rules("# One\n\n```text\n# not a heading\n```\n"), [])

    def test_a_blank_line_surrounds_every_heading(self):
        self.assertEqual(rules("# T\ntext\n"), [(1, "blank-around-heading")])
        self.assertEqual(rules("# T\n\ntext\n## S\n\nmore\n"), [(4, "blank-around-heading")])

    def test_a_blank_line_surrounds_every_fence(self):
        self.assertEqual(rules("# T\n\ntext\n```text\ncode\n```\n"), [(4, "blank-around-fence")])
        self.assertEqual(rules("# T\n\n```text\ncode\n```\ntext\n"), [(5, "blank-around-fence")])

    def test_tilde_and_longer_fences_hold_code_that_is_not_checked_as_prose(self):
        long = " ".join(["word"] * 40)
        for fence in ("~~~", "````"):
            with self.subTest(fence):
                self.assertEqual(rules(f"# T\n\n{fence}text\n{long}\n#h\n{fence}\n"), [])

    def test_a_fence_closes_only_with_its_own_character_and_at_least_its_length(self):
        # The inner triple backticks are content of the four-backtick fence.
        self.assertEqual(rules("# T\n\n````md\n```text\ninner\n```\n````\n\ntext\n"), [])
        self.assertEqual(rules("# T\n\n~~~text\n```\n~~~\n\ntext\n"), [])

    def test_a_fence_names_its_language(self):
        self.assertEqual(rules("# T\n\n```\ncode\n```\n"), [(3, "fence-language")])

    def test_a_blank_line_surrounds_every_list(self):
        self.assertEqual(rules("# T\n\ntext\n- one\n- two\n"), [(4, "blank-around-list")])
        self.assertEqual(rules("# T\n\n- one\n- two\ntext\n"), [(5, "blank-around-list")])

    def test_list_items_may_wrap_with_an_indent_and_nest(self):
        text = "# T\n\n1. one\n   wrapped\n   - nested\n2. two\n\ntext\n"
        self.assertEqual(rules(text), [])

    def test_only_a_list_that_can_interrupt_a_paragraph_needs_a_blank_line(self):
        # In CommonMark only a bullet or a list starting at 1 interrupts a paragraph; "2." or a
        # year at the start of a wrapped line is text.
        self.assertEqual(rules("# T\n\ntext\n2. x\n"), [])
        self.assertEqual(rules("# T\n\nin\n1986. it began\n"), [])
        self.assertEqual(rules("# T\n\ntext\n1. x\n"), [(4, "blank-around-list")])

    def test_a_numbered_item_after_a_wrapped_item_is_a_new_item_not_text(self):
        # The list is in progress, so "2." continues it, and no blank line is needed.
        self.assertEqual(rules("# T\n\n1. one\n   wrapped\n2. two\n   wrapped\n3. three\n"), [])

    def test_a_wrapped_line_may_not_start_with_punctuation_or_a_hash(self):
        self.assertEqual(rules("# T\n\nsee the issue\n#123 for details\n"), [(4, "line-start")])
        self.assertEqual(rules("# T\n\nthe end\n, and more\n"), [(4, "line-start")])
        self.assertEqual(rules("# T\n\n> quoted\n> #123 here\n"), [(4, "line-start")])
        self.assertEqual(rules("# T\n\n- item\n  #123 wrapped\n"), [(4, "line-start")])

    def test_a_line_that_starts_a_paragraph_or_a_list_may_start_with_a_symbol(self):
        text = "# T\n\n(parenthesis) first\n\n`code` first\n\n.env is ignored\n\n- item\n"
        self.assertEqual(rules(text), [])

    def test_a_hash_without_a_space_is_not_taken_for_a_heading(self):
        self.assertIn((3, "line-start"), rules("# T\n\n#nospace\n"))


class LinkTests(unittest.TestCase):
    """Relative links and anchors must resolve, so a moved page is found at commit time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "page.md").write_text("# Page\n\n## Setup steps\n\n## Setup steps\n")

    def _check(self, name: str, text: str) -> list[tuple[int, str]]:
        path = self.root / name
        path.write_text(text)
        return [(f.line, f.rule) for f in docslint.check_file(path, self.root)]

    def test_a_link_to_a_missing_file_is_found(self):
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/nope.md)\n"), [(3, "link-target")])

    def test_a_link_to_an_existing_file_and_heading_passes(self):
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/page.md#setup-steps)\n"), [])

    def test_a_missing_anchor_is_found_in_another_file_and_in_the_same_one(self):
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/page.md#gone)\n"),
                         [(3, "link-anchor")])
        self.assertEqual(self._check("a.md", "# A\n\n[x](#gone)\n"), [(3, "link-anchor")])
        self.assertEqual(self._check("a.md", "# A\n\n## Local part\n\n[x](#local-part)\n"), [])

    def test_a_repeated_heading_gets_a_numbered_anchor(self):
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/page.md#setup-steps-1)\n"), [])

    def test_link_syntax_beyond_the_plain_case_is_read_correctly(self):
        (self.root / "docs" / "a_(b).md").write_text("# B\n")
        cases = {
            "parentheses in the path": "[x](docs/a_(b).md)",
            "single-quoted title": "[x](docs/page.md 'a title')",
            "parenthesised title": "[x](docs/page.md (a title))",
            "angle brackets": "[x](<docs/page.md>)",
            "uppercase scheme": "[x](HTTPS://example.com/x)",
            "footnote definition": "text[^1]\n\n[^1]: See the note about things.",
            "reference with angle brackets": "[x][r]\n\n[r]: <docs/page.md>",
            "a closing # on the heading": "[x](docs/closed.md#t)",
        }
        (self.root / "docs" / "closed.md").write_text("# C\n\n## T ##\n")
        for label, body in cases.items():
            with self.subTest(label):
                self.assertEqual(self._check("a.md", f"# A\n\n{body}\n"), [])

    def test_a_titled_link_to_a_missing_file_is_still_found(self):
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/nope.md 'title')\n"),
                         [(3, "link-target")])
        self.assertEqual(self._check("a.md", "# A\n\n[x](docs/a_(nope).md)\n"),
                         [(3, "link-target")])

    def test_a_root_relative_link_resolves_from_the_repository_root(self):
        self.assertEqual(self._check("docs/b.md", "# B\n\n[x](/docs/page.md)\n"), [])
        self.assertEqual(self._check("docs/b.md", "# B\n\n[x](/docs/nope.md)\n"),
                         [(3, "link-target")])

    def test_a_link_out_of_the_repository_is_refused_even_if_the_file_exists(self):
        outside = self.root.parent / "outside-target.md"
        outside.write_text("# O\n")
        self.addCleanup(outside.unlink)

        self.assertEqual(self._check("a.md", "# A\n\n[x](../outside-target.md)\n"),
                         [(3, "link-target")])

    def test_a_file_that_is_not_utf8_is_a_finding_not_a_crash(self):
        path = self.root / "latin.md"
        path.write_bytes("# T\n\ncaf\xe9\n".encode("latin-1"))

        self.assertEqual([(f.line, f.rule) for f in docslint.check_file(path, self.root)],
                         [(1, "encoding")])

    def test_windows_line_endings_are_refused_as_trailing_whitespace(self):
        path = self.root / "crlf.md"
        path.write_bytes(b"# T\r\n\r\ntext\r\n")

        rules_found = {f.rule for f in docslint.check_file(path, self.root)}

        self.assertEqual(rules_found, {"trailing-space"})

    def test_web_links_and_links_in_code_are_not_followed(self):
        text = "# A\n\n[x](https://example.com/none)\n\n`[y](gone.md)`\n\n```text\n[z](gone.md)\n```\n"
        self.assertEqual(self._check("a.md", text), [])

    def test_links_in_a_journal_are_not_followed(self):
        (self.root / "docs" / "journals").mkdir()
        path = self.root / "docs" / "journals" / "2026-01-01-001-x.md"
        path.write_text("# J\n\n[old](../../elsewhere/gone.md)\n")

        self.assertEqual(docslint.check_file(path, self.root), [])


class RepositoryDocsTests(unittest.TestCase):
    ROOT = Path(__file__).parent

    def test_the_one_exempt_journal_exists_and_still_needs_its_exemption(self):
        # Rule: a journal entry is append-only, so the rule it breaks is exempted by name; if the
        # entry no longer breaks it, or is gone, the exemption must go too.
        for relative, exempted in docslint.EXEMPT.items():
            path = self.ROOT / relative
            self.assertTrue(path.exists(), relative)
            broken = {finding.rule for finding in docslint.check_text(path.read_text())}
            self.assertEqual(broken & exempted, exempted, relative)
            self.assertEqual(docslint.check_file(path, self.ROOT), [])

    def test_the_repositorys_documentation_follows_its_own_rules(self):
        findings = docslint.check_repo(Path(__file__).parent)

        self.assertEqual(findings, [], "\n" + "\n".join(str(f) for f in findings))

    def test_the_check_reads_every_document_in_the_repository(self):
        found = {path.relative_to(self.ROOT).as_posix()
                 for path in docslint.markdown_files(self.ROOT)}
        on_disk = {"README.md", "AGENTS.md", "CHANGELOG.md"} | {
            path.relative_to(self.ROOT).as_posix() for path in (self.ROOT / "docs").rglob("*.md")}

        self.assertEqual(found, on_disk)
        self.assertTrue(any(name.startswith("docs/guides/") for name in found))
        self.assertTrue(any(name.startswith("docs/reference/") for name in found))
        self.assertTrue(any(name.startswith("docs/journals/") for name in found))


if __name__ == "__main__":
    unittest.main()
