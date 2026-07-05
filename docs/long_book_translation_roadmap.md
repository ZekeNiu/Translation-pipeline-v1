# Long Book Translation Roadmap

This document tracks the path from the current MinerU Markdown translator to a robust long-book translation system.

## Goals

- Translate academic papers and long books with high translation quality.
- Preserve document structure, tables, formulas, figures, captions, footnotes, and references as much as practical.
- Keep the workflow simple for non-technical users.
- Make long jobs resumable, auditable, and predictable in cost and time.

## Non-goals

- Do not reproduce PDF headers, footers, copyright lines, or running titles by default.
- Do not force automatically extracted English terms to remain English.
- Do not use cross-document translation memory by default, because stale terms from older projects can pollute new books.
- Do not depend on one fixed MinerU JSON schema as the only source of truth.

## Phase 1: Structure Stability

- Keep MinerU Markdown as the primary source, with JSON sidecars used only as structural assistance.
- Build a shared inline semantic layer for body text, tables, captions, headings, and notes.
- Classify inline content as ordinary text, pure math, formula terms, script markers, citations, or simple HTML formatting before translation.
- Ensure ordinary source language is translated unless it is clearly a formula, citation, unit, acronym, name, or reference-list entry.
- Add quality checks for untranslated English, broken formulas, missing images, failed tables, and leaked HTML tags.

## Phase 2: Long-document Task System

- Split books by chapter and section instead of only by character count.
- Store each chapter as an independent task with durable status, cache keys, retry count, timings, and provider metadata.
- Support pause, resume, retry failed chapter, and regenerate selected chapter.
- Estimate token usage, cost, and runtime before starting large jobs.
- Keep chunk prompts light; do not repeat long glossary or entity lists in every request.

## Phase 3: Consistency System

- Use document-local translation memory for exact repeated titles, captions, table cells, and short repeated sentences.
- Extract terminology candidates for audit first, not as forced prompt constraints.
- Allow optional user-provided glossary import when the user has authoritative terms.
- Generate a terminology consistency report showing source term, observed translations, locations, and suspected conflicts.
- Protect acronyms by general pattern rules, not by document-specific hardcoded lists.

## Phase 4: Quality Audit

- Report untranslated ordinary English in body text, headings, captions, and tables.
- Report formula artifacts such as raw `\mathrm`, damaged delimiters, or suspicious placeholder leakage.
- Report table cells that failed translation, lost rowspan/colspan, or contain residual HTML.
- Report missing figures, untranslated captions, and reference-list translation mistakes.
- Produce a per-chapter audit summary so long books can be reviewed incrementally.

## Phase 5: Output Formats and MinerU Integration

- Improve DOCX fidelity for tables, footnotes, formulas, images, captions, and bilingual layouts.
- Add optional bilingual DOCX, Markdown, HTML, and EPUB outputs.
- Integrate MinerU local deployment or MinerU API from the GUI.
- Let users run PDF-to-translation from one workflow while still exposing intermediate files for debugging.
- Keep provider/model selection editable and avoid hardcoding live model lists.

## Acceptance Direction

A future long-book-ready version should be able to translate by chapter, resume after interruption, reuse document-local memory, produce a quality report, and regenerate only the problematic parts without rerunning the whole book.
