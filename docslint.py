#!/usr/bin/env python3
"""Checks this repository's Markdown against the style rules in AGENTS.md, and that relative links
and anchors resolve. Standard library only.

    python3 docslint.py     # check every document; exit status 1 when anything is found

The same check runs in `python3 -m unittest` (test_docslint.py).

It is a checker for this repository's documents, not a CommonMark parser. Fenced code (``` or ~~~)
is recognised; indented code blocks, HTML blocks and front matter are not, so use fences.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

LIMIT = 100  # columns of prose


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_ATX = re.compile(r"^#{1,6}( |$)")
_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_REFDEF = re.compile(r"^\s*\[(?!\^)[^\]]+\]:\s")
_UNDERLINE = re.compile(r"^(=+|-+)\s*$")
_PUNCTUATION = ",.;:!?)"


def _interrupts_a_paragraph(marker: str) -> bool:
    """Only a bullet, or a numbered list starting at 1, can start a list in the middle of a
    paragraph (CommonMark); "2." or a year at the start of a wrapped line is text."""
    return marker in ("-", "*", "+") or marker.rstrip(".)") == "1"


def _kinds(lines: list[str]) -> list[str]:
    """Classify each line: blank, code, fence-open, fence-close, heading, table, quote, item, ref,
    underline (a setext heading's), or text."""
    kinds: list[str] = []
    fence = ""  # the marker that opened the current fence, "" outside one
    in_list = False
    for line in lines:
        previous = kinds[-1] if kinds else "blank"
        closing = _FENCE_CLOSE.match(line)
        opening = _FENCE_OPEN.match(line)
        if fence:
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                kind, fence = "fence-close", ""
            else:
                kind = "code"
        elif opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            kind, fence = "fence-open", opening.group(1)
        elif not line.strip():
            kind = "blank"
        elif _ATX.match(line):
            kind = "heading"
        elif line.lstrip().startswith("|"):
            kind = "table"
        elif line.lstrip().startswith(">"):
            kind = "quote"
        elif _ITEM.match(line) and (previous != "text" or in_list or _interrupts_a_paragraph(
                _ITEM.match(line).group(1))):
            kind = "item"
        elif _REFDEF.match(line):
            kind = "ref"
        elif _UNDERLINE.match(line) and previous == "text":
            kind = "underline"
        else:
            kind = "text"
        if kind == "item":
            in_list = True
        elif kind in ("text", "quote") and not line.startswith(" "):
            in_list = False
        elif kind in ("heading", "table", "fence-open") and not line.startswith(" "):
            in_list = False
        kinds.append(kind)
    return kinds


def check_text(text: str) -> list[Finding]:
    """The style findings in one document, in line order within each rule."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    kinds = _kinds(lines)
    findings: list[Finding] = []

    def add(number: int, rule: str, message: str) -> None:
        findings.append(Finding(number, rule, message))

    if not text.endswith("\n") or (lines and lines[-1] == ""):
        add(len(lines), "final-newline", "a document ends with exactly one newline")
    h1 = 0
    in_list = False
    for index, (line, kind) in enumerate(zip(lines, kinds)):
        number = index + 1
        before = kinds[index - 1] if index else "blank"
        after = kinds[index + 1] if index + 1 < len(kinds) else "blank"
        if line != line.rstrip():
            add(number, "trailing-space", "trailing whitespace; use a blank line for a break")
        if kind == "blank" and before == "blank" and index:
            add(number, "blank-lines", "more than one blank line in a row")
        if kind == "underline":
            add(number - 1, "heading-style", "use a # heading, not an underlined one")
        if kind == "heading":
            if line.startswith("# ") or line == "#":
                h1 += 1
                if h1 == 2:
                    add(number, "one-h1", "a document has exactly one H1")
            if before != "blank" or after != "blank":
                add(number, "blank-around-heading", "a blank line goes before and after a heading")
        if kind == "fence-open":
            if not _FENCE_OPEN.match(line).group(2).strip():
                add(number, "fence-language", "a code fence names its language")
            if before != "blank":
                add(number, "blank-around-fence", "a blank line goes before a code fence")
        if kind == "fence-close" and after != "blank":
            add(number, "blank-around-fence", "a blank line goes after a code fence")
        if kind in ("text", "quote", "item") and len(line) > LIMIT and re.search(
                r"\s", line[LIMIT:]):
            add(number, "line-length", f"{len(line)} columns; prose wraps at {LIMIT}")
        if kind in ("text", "quote"):
            first = line.lstrip().lstrip(">").lstrip() if kind == "quote" else line.lstrip()
            continuing = before in ("text", "quote", "item")
            if first.startswith("#") or (continuing and first[:1] in _PUNCTUATION):
                add(number, "line-start",
                    "a wrapped line may not start with punctuation or #")
        if kind == "item":
            if before == "text" and not in_list:
                add(number, "blank-around-list", "a blank line goes before a list")
            in_list = True
        elif kind in ("text", "quote") and in_list and not line.startswith(" "):
            if before != "blank":
                add(number, "blank-around-list", "a blank line goes after a list")
            in_list = False
        elif kind in ("heading", "table", "fence-open") and not line.startswith(" "):
            in_list = False
    if h1 == 0:
        add(1, "one-h1", "a document has exactly one H1")
    return findings


# Documents that may not be edited to satisfy a rule: the journals are append-only history.
EXEMPT = {
    "docs/journals/2026-09-20-004-upstream-provider-seam-research.md": {"line-start"},
}
_LINK = re.compile(
    r"\]\(\s*(<[^>]*>|(?:[^()\s]|\([^()\s]*\))+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")
_CODE_SPAN = re.compile(r"`[^`]*`")


def _slug(heading: str) -> str:
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.replace("`", ""))
    heading = re.sub(r"\s+#+\s*$", "", heading.strip())  # a closing run of # is not text
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """GitHub-style anchors of a document's headings; a repeated heading gets -1, -2, ..."""
    lines = path.read_bytes().decode("utf-8", "replace").split("\n")
    seen: dict[str, int] = {}
    found: set[str] = set()
    for line, kind in zip(lines, _kinds(lines)):
        if kind == "heading":
            slug = _slug(line.lstrip("#"))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if not count else f"{slug}-{count}")
    return found


def _links(text: str):
    """(line, target) for each link outside code."""
    lines = text.split("\n")
    for index, (line, kind) in enumerate(zip(lines, _kinds(lines))):
        if kind in ("code", "fence-open", "fence-close"):
            continue
        plain = _CODE_SPAN.sub("", line)
        for match in _LINK.finditer(plain):
            yield index + 1, match.group(1).strip("<>")
        if _REFDEF.match(plain):
            yield index + 1, plain.split(None, 1)[1].split()[0].strip("<>")


def check_file(path: Path, root: Path) -> list[Finding]:
    relative = path.relative_to(root).as_posix()
    try:
        text = path.read_bytes().decode("utf-8")  # bytes, so a carriage return is still seen
    except UnicodeDecodeError:
        return [Finding(1, "encoding", "not valid UTF-8", relative)]
    findings = [f for f in check_text(text) if f.rule not in EXEMPT.get(relative, ())]
    if not relative.startswith("docs/journals/"):  # entries point at paths of other projects
        for number, target in _links(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue  # a web or mail link
            name, _, fragment = target.partition("#")
            if not name:
                destination = path
            elif name.startswith("/"):
                destination = root / name.lstrip("/")
            else:
                destination = path.parent / name
            try:
                destination.resolve().relative_to(root.resolve())
            except ValueError:
                findings.append(Finding(number, "link-target", f"{target} leaves the repository"))
                continue
            if not destination.exists():
                findings.append(Finding(number, "link-target", f"{target} does not exist"))
            elif fragment and destination.suffix == ".md" and fragment not in _anchors(
                    destination):
                findings.append(Finding(number, "link-anchor", f"{target} names no heading"))
    return [Finding(f.line, f.rule, f.message, relative) for f in findings]


def markdown_files(root: Path) -> list[Path]:
    return [root / name for name in ("README.md", "AGENTS.md", "CHANGELOG.md")
            ] + sorted((root / "docs").rglob("*.md"))


def check_repo(root: Path) -> list[Finding]:
    return [finding for path in markdown_files(root) for finding in check_file(path, root)]


def main() -> int:
    findings = check_repo(Path(__file__).parent)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
