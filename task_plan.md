# Implementation plan

Baseline: 000116058b053f3016dfb57388b4aa34c1d360a2.

1. [done] Push rollback tag; persist per-provider GUI settings with DPAPI; simplify UI and use a worker queue.
2. [done] Official MinerU upload/poll/resume; structure-aware PDF parts, conservative seam repair and asset-safe merge.
3. [done] Structure-first translation, in-place references, validated caches, resumable tasks and selective retries.
4. [done] Local and GitHub Windows Python 3.11/3.14 regression checks passed; visual checks and documentation complete; code merged to main, pushed and verified against the remote. Rollback tag verified.

Constraints: scientific English to Simplified Chinese; PDF splitting only; 200 pages / 190,000,000 bytes per upload; default two translation workers; no full-book second review; retain existing CLI and output path interfaces.

Validation: 80 Windows regression tests pass; generated academic sample translated by the existing DeepSeek service and visually checked through Word/PDF; cached real responses revalidated with no new model calls. MinerU cloud live OCR remains unverified because no MinerU token is configured. See docs/validation-and-rollback.md.

## Follow-up: reported API task appears stalled
- [done] Reproduce against the authorized 33-page document; verify original MinerU task and source hash.
- [done] Fix date/notation false positives, selective cell retries, substage/elapsed progress, original filename fallback, and observed Word formula rendering defects.
- [done] 88 local tests pass; live task completed 8/8, 11 tables and 29 images; no-new-request resume succeeds; Word/PDF visual check performed.
- [in progress] Publish fix branch, verify CI, merge main and verify remote.
