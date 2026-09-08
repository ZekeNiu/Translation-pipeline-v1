# Findings

- GUI reads .env but never saves edits; worker accesses Tk controls; exception callback captures an expired exception variable.
- API runner supports /file_parse and /tasks, not official v4 local-file upload protocol.
- Existing 25 tests pass. Reproduced long HTML table splitting, references swallowing later chapters, and accepted missing math/image placeholders.
- Current default output timestamps prevent useful GUI resume. Caches lack validation and temperature; fallback table text enters memory as success.
- Official API: POST /api/v4/file-urls/batch (max 50); PUT signed URLs; GET /api/v4/extract-results/batch/{batch_id}; result full_zip_url. Upload URLs expire after 24h. Token must not reach object storage.
- Reuse ideas, independently implemented: deusyu/translate-book (manifest and neighbor context), KazKozDev/book-translator (localized verified corrections). Keep MinerU Markdown primary.
- GitHub main confirmed at baseline via API. Git transport had one connection reset during planning; authentication available.
