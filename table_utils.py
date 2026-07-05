from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import re

INLINE_TAGS = {"sup", "sub", "i", "em", "b", "strong"}


@dataclass
class TableCell:
    tag: str = "td"
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""


@dataclass
class TableRow:
    attrs: dict[str, str] = field(default_factory=dict)
    cells: list[TableCell] = field(default_factory=list)


@dataclass
class ParsedTable:
    attrs: dict[str, str] = field(default_factory=dict)
    rows: list[TableRow] = field(default_factory=list)


class _HTMLTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[ParsedTable] = []
        self._table: ParsedTable | None = None
        self._row: TableRow | None = None
        self._cell: TableCell | None = None
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {k.lower(): v for k, v in attrs if k}
        if tag == "table":
            self._table = ParsedTable(attrs=attrs_dict)
        elif tag == "tr" and self._table is not None:
            self._row = TableRow(attrs=attrs_dict)
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = TableCell(tag=tag, attrs=attrs_dict)
            self._cell_parts = []
        elif tag == "br" and self._cell is not None:
            self._cell_parts.append("\n")
        elif tag in INLINE_TAGS and self._cell is not None:
            self._cell_parts.append(f"<{tag}>")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell.text = normalize_cell_text("".join(self._cell_parts))
            self._row.cells.append(self._cell)
            self._cell = None
            self._cell_parts = []
        elif tag in INLINE_TAGS and self._cell is not None:
            self._cell_parts.append(f"</{tag}>")
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def normalize_cell_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return text.strip()


def parse_html_tables(html_text: str) -> list[ParsedTable]:
    parser = _HTMLTableParser()
    parser.feed(html_text)
    parser.close()
    return parser.tables


def _attrs_to_html(attrs: dict[str, str]) -> str:
    if not attrs:
        return ""
    parts = []
    for key, value in attrs.items():
        if value is None:
            parts.append(escape(key, quote=True))
        else:
            parts.append(f'{escape(key, quote=True)}="{escape(str(value), quote=True)}"')
    return " " + " ".join(parts)


def render_html_table(table: ParsedTable) -> str:
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = escape(cell.text, quote=False).replace("\n", "<br/>")
            for tag in INLINE_TAGS:
                text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
                text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
            cells.append(f"<{cell.tag}{_attrs_to_html(cell.attrs)}>{text}</{cell.tag}>")
        rows.append(f"<tr{_attrs_to_html(row.attrs)}>{''.join(cells)}</tr>")
    return f"<table{_attrs_to_html(table.attrs)}>{''.join(rows)}</table>"


def iter_cells(tables: list[ParsedTable]):
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                yield cell


def html_table_blocks(text: str):
    pattern = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
    return pattern.finditer(text)
