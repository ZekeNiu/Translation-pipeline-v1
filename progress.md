# Progress

- Planning completed and explicitly approved for implementation.
- Baseline tests: 25 passed. Working tree initially clean; main tracks origin/main at 0001160.
- Implementation begun; skills read; no live credentials printed or API translation requested.
- Git push for rollback tag failed (github.com:443 connection timeout). Using authenticated GitHub refs API for the same baseline tag; Git transport will be retried for commits.
- Rollback tag successfully created remotely at baseline via GitHub API.
- Stage 1: per-user DPAPI settings, provider isolation, compact conditional panels, frozen worker input snapshots and queue-delivered errors. Seven targeted tests passed, including real Windows DPAPI round trip.
- One patch application rejected a delete/add of the same file; split into sequential operations, no partial patch applied.
- Stage 2: official v4 upload/poll/download, saved part states, lossless page splitting, measured byte limits, chapter hints, seam windows, namespaced assets and conservative merge are implemented. 47 tests pass. Synthetic 199/200/201-page files preserve page dimensions, rotation and text; byte-limit and resume scenarios pass.
- Stages 1 and 2 committed as 2d27364 and 39b8f57 and pushed to codex/mineru-quality-reliability; Git HTTP/1.1 transport succeeded.
- Stage 3 implemented: document blocks, reference boundaries, per-block integrity checks, targeted paragraph/cell revisions, validated persistent caches, source/config fingerprints, bounded workers and task resume. DOCX now preserves rowspan/colspan and inline-image surrounding text; failed exports produce review items.
- Validation: 80 tests pass on Windows/Python 3.14. Added Windows Python 3.11/3.14 CI. Credentials absent from source/docs scan; outputs and credentials ignored by Git.
- Synthetic academic live sample completed through existing DeepSeek configuration. Initial three requests took about 81.4 seconds; one unnecessary revision exposed CJK-adjacent numeric false positives. Fixed and regression-tested. Revalidated real cached responses with network disabled: completed, no warnings, zero new requests.
- Default DOCX renderer could not run (system Python lacked pdf2image; bundled runtime had it but no LibreOffice). Used an isolated invisible Word COM instance to export DOCX to PDF, then Poppler to render; manually inspected the whole page, tables, superscripts/subscripts, figure/caption and post-reference chapters.
- GUI API view inspected using a temporary settings store and empty keys. No real MinerU token is configured, so official protocol behavior is mock-tested and live OCR is explicitly unverified.
- GitHub Python 3.11 CI exposed cp1252 redirected-stdout failure when printing Chinese CLI report paths; fixed CLI stream encoding to UTF-8 and reproduced with an explicit cp1252 test. Local 80 tests pass; rerunning both remote versions.
