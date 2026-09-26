"""HTML → text, and HTML → tables, without a third-party dependency.

Banks send statement summaries as HTML tables far more often than the "PDF
attachment" mental model suggests, so the discovery engine needs to read both
the prose (for keyword signals) and the grid (for transactions).

``html.parser`` from the standard library is used rather than a parsing library:
the input is untrusted third-party markup, and a strict parser that simply drops
what it cannot understand is the right failure mode here.
"""
from __future__ import annotations

import html
import html.parser
import re
from typing import List, Optional

_SKIP_CONTENT_TAGS = {"script", "style", "head", "title", "meta", "link"}
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
}


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")
        elif tag == "td" or tag == "th":
            self.chunks.append("\t")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self.chunks.append(data)


def html_to_text(markup: str) -> str:
    """Flatten HTML to readable text, preserving row and cell boundaries."""
    if not markup:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        # Malformed markup: fall back to a tag strip rather than losing the
        # message entirely. A bank's marketing template is not worth an
        # exception that aborts a scan.
        return re.sub(r"<[^>]+>", " ", markup)
    text = "".join(parser.chunks)
    text = html.unescape(text)
    text = re.sub(r"[  ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


class _TableExtractor(html.parser.HTMLParser):
    """Collect every ``<table>`` as a list of rows of cell strings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._table_stack: List[List[List[str]]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean_cell("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table_stack and any(c for c in self._row):
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            finished = self._table_stack.pop()
            if finished:
                self.tables.append(finished)

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def html_tables(markup: str) -> List[List[List[str]]]:
    """Return every table in ``markup`` as rows of cells.

    Nested tables (the standard email-layout idiom) are returned individually,
    innermost first, so a transaction grid wrapped in three layout tables is
    still found.
    """
    if not markup:
        return []
    parser = _TableExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return []
    return parser.tables


def text_tables(text: str) -> List[List[List[str]]]:
    """Recover a grid from a plain-text statement body.

    Plain-text statements align columns with runs of whitespace, tabs or pipes.
    Only rows with a consistent column count are kept, which is what
    distinguishes a real table from a paragraph that happens to contain a tab.
    """
    if not text:
        return []
    rows: List[List[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "|" in stripped:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
        elif "\t" in stripped:
            cells = [c.strip() for c in stripped.split("\t")]
        else:
            cells = [c.strip() for c in re.split(r"\s{2,}", stripped)]
        cells = [c for c in cells if c != ""]
        if len(cells) >= 3:
            rows.append(cells)

    if len(rows) < 2:
        return []

    # Keep only the dominant width: a header plus its rows, not the address
    # block above it that happens to split into three pieces.
    widths: dict = {}
    for row in rows:
        widths[len(row)] = widths.get(len(row), 0) + 1
    best_width = max(widths, key=lambda w: widths[w])
    kept = [r for r in rows if len(r) == best_width]
    return [kept] if len(kept) >= 2 else []
