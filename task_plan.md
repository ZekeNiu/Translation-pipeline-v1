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
- [done] Fix branch published; GitHub Windows Python 3.11/3.14 tests pass (run 34319874773); merge and remote verification recorded in the delivery response.

## Follow-up: local MinerU GPU deployment (2026-09-09)

Goal: deploy the current official MinerU GPU release on Windows using the Conda installation at `D:\Application\Anaconda`, preferring an isolated install rooted at `D:\Application\MinerU-Local` when officially supportable.

1. [done] Verify current official installation guidance, Windows/GPU constraints, and the referenced tutorial; inspect local GPU, driver, Conda, Python, disk, and existing MinerU state.
2. [done] Choose and record the least-fragile Windows deployment architecture and exact pinned environment/model locations.
3. [done] Create the isolated environment and install the official current MinerU GPU package without modifying unrelated environments.
4. [done] Configure model/cache paths under the preferred install root and obtain required model files through an official/recommended route.
5. [done] Run package, GPU-backend, CLI, Web UI, and representative PDF smoke tests; record versions and recovery commands.
6. [done] Deliver a local run/upgrade/resume guide and leave an idempotent checkpoint script and durable state record.

Safety/recovery rules: never delete or overwrite an existing environment without explicit need; inspect before each mutation; log commands, versions, paths, errors, and verification results in `progress.md`; resume from observed state rather than blindly rerunning installation.

## Follow-up: local CLI integration and project review (2026-09-15)
- [in progress] Inspect existing deployment and reproduce CLI discovery failure.
- [pending] Apply a scoped CLI integration fix and run regression plus local PDF smoke checks.
- [pending] Deliver prioritized project recommendations grounded in code and official reference projects; do not implement review suggestions.

### Acceptance (2026-09-15)
- [done] Reproduced missing CLI and missing prefix configuration; found both the old and intended MinerU installations.
- [done] Scoped fix, explicit 3.4.5 GUI configuration with backup, 90 regression tests, two-page live parse, GUI reload and no-process cache resume verified.
- [done] Code review and official reference research completed; recommendations delivered only as advice.

## High-priority implementation (approved 2026-09-15)
1. [done] Publish verified local CLI baseline and rollback/pre-high-priority-20260915 (517df3e).
2. [done] Local parse progress, process-tree cancellation, validated version/model cache and locking.
3. [done] Two-level opt-in glossary, selective cache invalidation, management/extraction UI and cost metrics.
4. [done] Persisted source/translation alignment, manual revisions, targeted retry and review window.
5. [done] Regression/live/UI verification, Windows CI, staged publication, main merge and release tag.
Defaults: global/book independently off, book overrides global, manual/imported terms with explicit extraction; no full-book review or native Word equation work.

## High-priority implementation acceptance
- [done] Observable local parsing, model/version cache validation and process-tree cancellation.
- [done] Two-level opt-in glossary, bounded explicit extraction, selective cache behavior and management UI.
- [done] Persisted source/translation alignment, reversible edits, targeted retry, offline recovery and review UI.
- [done] Final functional code passed both Windows CI versions (117 tests); release 8c88eff merged to main, tagged release/high-priority-20260915 and verified remotely. Rollback tag points to 517df3e. Subsequent commits only record this publication audit.
# Output/review/layout follow-up (2026-09-16)

- [done] Back up baseline and user's task; migrate auxiliary outputs into `_internal` with recoverable path handling.
- [done] Unified located HTML/Markdown quality issues and confirmed-retention review workflow.
- [done] Conservative page furniture filtering, omission detection and explicit selected recovery.
- [done] A4 semantic pagination, wide tables, original furniture with new page fields, preview/settings.
- [done] Windows 3.11/3.14 regression, actual-document no-model reexport and Word/PDF visual inspection; staged GitHub release.

Defaults: preserve original furniture style where safe; comfortable Chinese reflow; AI suggestions/recovery only on explicit selection. Do not retranslate existing task automatically.

## Long-book usability (approved 2026-09-16)
Baseline: 6f32e3a. Preserve user data, original interfaces, opt-in model calls and staged GitHub rollback.
1. [done] Baseline tag and branch; revision/export transaction and durable issue state (47078c1).
2. [done] Issue-first review, grouped tables, chapter reading, task library and independent save/export; actual report and 10k-unit GUI validated.
3. [done] Resumable local/custom API long-PDF parsing, safe boundaries and shared local service; GPU/local async API live checks passed.
4. [done] Bounded semantic context and context-aware table caches; stable content identities; four synthetic paid requests verified usage.
5. [done] 152 tests passed locally and on GitHub Windows 3.11/3.14 (run 35057801854); actual report no-model reexport and 28-page native Word/PDF verification done. Release 305ab34 merged to main and tagged release/long-book-usability-20260916; remote release and rollback commit identities verified.
Acceptance: 10k-unit UI search/switch <=500 ms and single-edit save target <=1s; 1k-page synthetic planning; no model requests for navigation/confirmation/export; retain existing manual revisions and previous valid artifacts.
