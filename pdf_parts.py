"""Lossless PDF page partitions with structural boundary hints and seam windows."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import zipfile
from pypdf import PdfReader, PdfWriter
from task_state import check_cancel, file_hash

MAX_PAGES = 200
MAX_BYTES = 190_000_000
PDF_PLAN_VERSION = 1


@dataclass
class ParsePart:
    id: str
    path: str
    start: int
    end: int
    kind: str = "main"
    boundary: int | None = None
    reason: str = "limit"
    sha256: str = ""

    def record(self):
        return asdict(self)


def _write_pages(reader, start, end, target):
    writer = PdfWriter()
    for index in range(start, end):
        writer.add_page(reader.pages[index])
    with target.open("wb") as fh:
        writer.write(fh)
    writer.close()


def _outline_boundaries(reader):
    boundaries = {}
    def visit(items, depth=0):
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1)
            else:
                try:
                    page = reader.get_destination_page_number(item)
                    if page is not None and page > 0:
                        rank = 4 if depth == 0 else 3
                        if rank > boundaries.get(page, (0, ""))[0]:
                            boundaries[page] = (rank, "chapter" if depth == 0 else "section")
                except (ValueError, KeyError, TypeError):
                    pass
    try:
        visit(reader.outline)
    except (ValueError, KeyError, TypeError):
        pass
    return boundaries


def _text_hint(reader, index):
    try:
        text = reader.pages[index].extract_text() or ""
    except Exception:
        return ""
    return text.strip()


def _choose_boundary(reader, start, ceiling, outlines, text_cache):
    if ceiling == len(reader.pages):
        return ceiling, "end"
    candidates = {p: hint for p, hint in outlines.items() if start < p <= ceiling}
    # Only inspect the last twenty possible edges; scanned pages are unknown, not blank.
    for page in range(max(start + 1, ceiling - 19), ceiling + 1):
        if candidates.get(page, (0, ""))[0] >= 3:
            continue
        if page not in text_cache:
            text_cache[page] = _text_hint(reader, page)
        lines = text_cache[page].splitlines()
        if any(re.match(r"(?i)^\s*(chapter\s+[\divxlc]+|appendix\s+[A-Z\d]|第.{1,12}[章节]|\d+(?:\.\d+)*\s+[A-Z][a-z])\b", line) for line in lines[:4]):
            candidates[page] = (3, "section")
        else:
            if page - 1 not in text_cache:
                text_cache[page - 1] = _text_hint(reader, page - 1)
            previous = text_cache[page - 1]
            if previous and re.search(r"[.!?。！？][\"')\]]?\s*$", previous):
                candidates[page] = (1, "paragraph_hint")
    if not candidates:
        return ceiling, "unknown"
    page = max(candidates, key=lambda p: (candidates[p][0], p))
    return page, candidates[page][1]


def prepare_parts(source: Path, directory: Path, *, max_pages=MAX_PAGES, max_bytes=MAX_BYTES, cancel_event=None):
    source, directory = Path(source).resolve(), Path(directory)
    if not source.is_file():
        raise ValueError("请选择存在的文档文件。")
    if max_pages < 1 or max_bytes < 1:
        raise ValueError("Invalid upload limits")
    directory.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() != ".pdf":
        if source.stat().st_size > max_bytes:
            raise ValueError("文件体积超限，请先导出为 PDF 后自动拆分。")
        if source.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
            with zipfile.ZipFile(source) as archive:
                if "docProps/app.xml" in archive.namelist():
                    app = archive.read("docProps/app.xml").decode("utf-8")
                    pages = re.search(r"<(?:\w+:)?(?:Pages|Slides)>(\d+)</", app)
                    if pages and int(pages.group(1)) > max_pages:
                        raise ValueError("Office 文件页数超限，请先导出为 PDF 后自动拆分。")
        part = ParsePart("part_0001", str(source), 0, 0, reason="non_pdf", sha256=file_hash(source))
        return [part], [], None
    try:
        reader = PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 已加密，请先提供可读取的 PDF。")
        count = len(reader.pages)
        if not count:
            raise ValueError("PDF 没有可读取页面。")
    except Exception as exc:
        raise ValueError("PDF 无法读取（损坏或加密），请检查原文件。") from exc
    if count <= max_pages and source.stat().st_size <= max_bytes:
        return [ParsePart("part_0001", str(source), 0, count, reason="whole", sha256=file_hash(source))], [], count
    outlines, text_cache = _outline_boundaries(reader), {}
    parts, warnings = [], []
    start = 0
    while start < count:
        check_cancel(cancel_event)
        ceiling = min(start + max_pages, count)
        end, reason = _choose_boundary(reader, start, ceiling, outlines, text_cache)
        target = directory / f"part_{len(parts) + 1:04d}.pdf"
        while True:
            check_cancel(cancel_event)
            _write_pages(reader, start, end, target)
            if target.stat().st_size <= max_bytes:
                break
            if end - start == 1:
                target.unlink(missing_ok=True)
                raise ValueError(f"原 PDF 第 {start + 1} 页单页体积仍超限；未降低图像质量，请处理该页后重试。")
            # File size is measured, not inferred from average input bytes/page.
            end, reason = _choose_boundary(reader, start, start + max(1, (end - start) // 2), outlines, text_cache)
        parts.append(ParsePart(target.stem, str(target.resolve()), start, end, reason=reason, sha256=file_hash(target)))
        start = end
    main_parts = list(parts)
    for left, right in zip(main_parts, main_parts[1:]):
        if left.reason in {"chapter", "section"}:
            continue
        boundary = left.end
        start, end = max(left.start, boundary - 2), min(right.end, boundary + 2)
        target = directory / f"seam_{boundary:06d}.pdf"
        if end - start > max_pages:
            start, end = boundary - 1, boundary + 1
        if end - start > max_pages:
            warnings.append({"pages": [boundary, boundary + 1], "reason": "接缝窗口超过页数限制，保留主分片内容，需检查。"})
            continue
        _write_pages(reader, start, end, target)
        if target.stat().st_size > max_bytes:
            start, end = boundary - 1, boundary + 1
            _write_pages(reader, start, end, target)
        if target.stat().st_size > max_bytes:
            target.unlink(missing_ok=True)
            warnings.append({"pages": [boundary, boundary + 1], "reason": "接缝窗口体积超限，保留主分片内容，需检查。"})
            continue
        parts.append(ParsePart(target.stem, str(target.resolve()), start, end, "seam", boundary, sha256=file_hash(target)))
    return parts, warnings, count
