# Implementation plan

Baseline: 000116058b053f3016dfb57388b4aa34c1d360a2.

1. [done] Push rollback tag; persist per-provider GUI settings with DPAPI; simplify UI and use a worker queue.
2. [done] Official MinerU upload/poll/resume; structure-aware PDF parts, conservative seam repair and asset-safe merge.
3. [in progress] Structure-first translation, in-place references, validated caches, resumable tasks and selective retries.
4. [pending] Regression tests, generated academic fixture and visual checks; documentation; push branch and main and verify remote hashes.

Constraints: scientific English to Simplified Chinese; PDF splitting only; 200 pages / 190,000,000 bytes per upload; default two translation workers; no full-book second review; retain existing CLI and output path interfaces.

Errors and limitations will be recorded in progress.md. No plaintext credentials, documents or generated outputs are committed.
