"""DOCX generator for translated MinerU markdown."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from inline_semantics import iter_styled_runs, normalize_inline_output
from table_utils import parse_html_tables

try:
    from docx import Document
    from docx.shared import Pt, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml.ns import qn
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-docx"])
    from docx import Document
    from docx.shared import Pt, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml.ns import qn


BODY_SIZE = 11
HEADING_SIZES = {1: 17, 2: 14, 3: 13, 4: 12}
LINE_SPACING = 1.5
FIRST_INDENT = Cm(0.74)
CITATION_SIZE = 8
TABLE_SIZE = 8.5
CITE_RE = re.compile(r"(\[\d+(?:[,\s\u2013–-]+\d+)*\])")
XML_BAD_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
REFERENCE_HEADING_TEXT_RE = re.compile(
    r"^(references|bibliography|works\s+cited|literature\s+cited|参考文献|參考文獻)$",
    re.IGNORECASE,
)


def set_font(run, bold=False, italic=False, size=None):
    run.bold = bold
    run.italic = italic
    run.font.name = "Times New Roman"
    if size:
        run.font.size = Pt(size)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = run._element.makeelement(qn("w:rFonts"), {})
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), "宋体")
    rFonts.set(qn("w:ascii"), "Times New Roman")
    rFonts.set(qn("w:hAnsi"), "Times New Roman")


def _add_text_runs(paragraph, text: str, size=BODY_SIZE, bold=False, italic=False):
    text = XML_BAD_CHAR_RE.sub("", text)
    text = normalize_inline_output(text)
    for styled in iter_styled_runs(text):
        parts = CITE_RE.split(styled.text)
        for part in parts:
            if not part:
                continue
            run = paragraph.add_run(part)
            is_citation = bool(CITE_RE.fullmatch(part))
            run.font.superscript = styled.superscript or is_citation
            run.font.subscript = styled.subscript and not is_citation
            set_font(
                run,
                bold=bold or styled.bold,
                italic=italic or styled.italic,
                size=CITATION_SIZE if is_citation else size,
            )


def _paragraph(doc, text: str, size=BODY_SIZE, first_indent=True, italic=False, bold=False):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    pf = p.paragraph_format
    pf.first_line_indent = FIRST_INDENT if first_indent else Cm(0)
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = LINE_SPACING
    pf.space_after = Pt(2)
    _add_text_runs(p, text, size=size, bold=bold, italic=italic)
    return p


def _is_reference_heading(text: str) -> bool:
    return bool(REFERENCE_HEADING_TEXT_RE.fullmatch(text.strip()))


def _reference_paragraph(doc, text: str):
    p = _paragraph(doc, text, size=8, first_indent=False)
    pf = p.paragraph_format
    pf.left_indent = Cm(0.45)
    pf.first_line_indent = Cm(-0.45)
    pf.space_after = Pt(1)
    pf.line_spacing = 1.0
    return p


def _safe_int(value, default=1):
    try:
        return max(default, int(value))
    except (TypeError, ValueError):
        return default


def _add_html_table(doc, html: str) -> bool:
    parsed_tables = parse_html_tables(html)
    if not parsed_tables:
        return False

    for parsed in parsed_tables:
        rows = [row for row in parsed.rows if row.cells]
        if not rows:
            continue
        max_cols = max(sum(_safe_int(cell.attrs.get("colspan"), 1) for cell in row.cells) for row in rows)
        table = doc.add_table(rows=len(rows), cols=max_cols)
        try:
            table.style = "Table Grid"
        except Exception:
            pass

        for row_idx, parsed_row in enumerate(rows):
            col_idx = 0
            for parsed_cell in parsed_row.cells:
                if col_idx >= max_cols:
                    break
                colspan = _safe_int(parsed_cell.attrs.get("colspan"), 1)
                cell = table.cell(row_idx, col_idx)
                if colspan > 1 and col_idx + colspan - 1 < max_cols:
                    cell = cell.merge(table.cell(row_idx, col_idx + colspan - 1))
                cell.text = ""
                paragraph = cell.paragraphs[0]
                paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
                paragraph.paragraph_format.line_spacing = 1.0
                paragraph.paragraph_format.space_after = Pt(0)
                _add_text_runs(
                    paragraph,
                    parsed_cell.text,
                    size=TABLE_SIZE,
                    bold=(parsed_cell.tag == "th"),
                )
                col_idx += colspan
        doc.add_paragraph()
    return True


def _image_candidates(rel_path: str, images_search_root: Path):
    rel = Path(rel_path)
    if rel.is_absolute():
        yield rel
        return
    yield images_search_root / rel
    yield images_search_root.parent / rel
    yield images_search_root / "images" / rel.name


def _add_image(doc, image_line: str, images_search_root: Path | None):
    m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", image_line)
    if not (m and images_search_root):
        return
    rel_path = m.group(1)
    for ip in _image_candidates(rel_path, images_search_root):
        ip = ip.resolve()
        if ip.exists():
            try:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = p.add_run()
                r.add_picture(str(ip), width=Inches(4.8))
            except Exception:
                pass
            return


def _configure_document(doc):
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    style.paragraph_format.line_spacing = LINE_SPACING

    for lv in range(1, 5):
        try:
            hs = doc.styles[f"Heading {lv}"]
        except KeyError:
            continue
        hs.font.name = "Times New Roman"
        hs.font.size = Pt(HEADING_SIZES.get(lv, 12))
        hs.font.bold = True
        hs.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        hs.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        hs.paragraph_format.line_spacing = 1.3

    for sec in doc.sections:
        sec.top_margin = Inches(0.8)
        sec.bottom_margin = Inches(0.8)
        sec.left_margin = Inches(1.0)
        sec.right_margin = Inches(1.0)


def make_docx(md_text: str, docx_path, images_search_root=None):
    doc = Document()
    _configure_document(doc)
    root = Path(images_search_root) if images_search_root else None

    lines = md_text.splitlines()
    i = 0
    reference_mode = False
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue

        if "<table" in s.lower():
            block = [line]
            while "</table>" not in lines[i].lower() and i + 1 < len(lines):
                i += 1
                block.append(lines[i])
            if _add_html_table(doc, "\n".join(block)):
                i += 1
                continue

        if s.startswith("!["):
            _add_image(doc, s, root)
            i += 1
            continue

        hm = re.match(r"^(#{1,4})\s+(.+)$", line)
        if hm:
            lv = len(hm.group(1))
            text = hm.group(2).strip()
            p = doc.add_heading(text, level=lv)
            for r in p.runs:
                set_font(r, bold=True)
            reference_mode = _is_reference_heading(text)
            i += 1
            continue

        if reference_mode:
            _reference_paragraph(doc, s)
            i += 1
            continue

        if re.match(r"^(图|表)\s*\d", s):
            _paragraph(doc, s, size=9, first_indent=False, italic=True)
            i += 1
            continue

        if re.match(r"^\d+\.\s+", s):
            p = _paragraph(doc, s, size=8, first_indent=False)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.0
            i += 1
            continue

        _paragraph(doc, s)
        i += 1

    doc.save(str(docx_path))
    print(f"Wrote {docx_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python make_docx.py <input.md> [output.docx] [images_root]")
        sys.exit(1)
    md_path = Path(args[0])
    docx_path = Path(args[1]) if len(args) > 1 else md_path.with_suffix(".docx")
    img_root = Path(args[2]) if len(args) > 2 else md_path.parent
    make_docx(md_path.read_text(encoding="utf-8"), docx_path, img_root)
