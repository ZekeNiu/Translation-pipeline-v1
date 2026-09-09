# Findings

- GUI reads .env but never saves edits; worker accesses Tk controls; exception callback captures an expired exception variable.
- API runner supports /file_parse and /tasks, not official v4 local-file upload protocol.
- Existing 25 tests pass. Reproduced long HTML table splitting, references swallowing later chapters, and accepted missing math/image placeholders.
- Current default output timestamps prevent useful GUI resume. Caches lack validation and temperature; fallback table text enters memory as success.
- Official API: POST /api/v4/file-urls/batch (max 50); PUT signed URLs; GET /api/v4/extract-results/batch/{batch_id}; result full_zip_url. Upload URLs expire after 24h. Token must not reach object storage.
- Reuse ideas, independently implemented: deusyu/translate-book (manifest and neighbor context), KazKozDev/book-translator (localized verified corrections). Keep MinerU Markdown primary.
- GitHub main confirmed at baseline via API. Git transport had one connection reset during planning; authentication available.
- Live sample found a numeric-check bug: Python Unicode word boundaries skipped digits adjacent to Chinese text. ASCII letter/digit boundaries now correctly match “为45秒” and “表1”.
- Revisions operate only on paragraphs with identified concerns and preserve the other already-validated paragraphs. Retry timing is recorded separately without reducing checks or enabling extra concurrency.
- DOCX visual inspection confirmed merged headers, formulas, images and references followed by translated discussion/appendix. MinerU OCR quality still requires a configured token and representative real books.
- Follow-up real report: MinerU succeeded (33 pages); apparent stall was hidden table/model activity. English month dates becoming Chinese numeric months triggered whole-batch retries. Also observed equivalent percentage spacing, x/×, written counts, and unsupported DOCX mathsf/boldsymbol. See docs/bugfix-20260909.md.
