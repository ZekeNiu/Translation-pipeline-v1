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

## Local MinerU GPU deployment research (2026-09-09)

- Official MinerU releases currently show `3.4.5` as the newest stable release (released 2026-08-14); `4.0.0a6` is a pre-release, so “latest” will mean the latest stable package unless local package metadata proves otherwise.
- Official quick-start supports `mineru[all]` on Windows and recommends Python 3.10-3.12 on Windows because `ray` does not support Python 3.13 there. Hardware guidance: GPU acceleration requires NVIDIA Volta or newer, at least 4 GB VRAM for pipeline/hybrid and 8 GB for VLM engine, 16 GB+ RAM, and 20 GB+ SSD space.
- Official Windows FAQ distinguishes GPU families: RTX 20/30/40 (through Ada) use CUDA-enabled PyTorch/torchvision selected from PyTorch official guidance; RTX 50 (Blackwell) instead needs the official `lmdeploy 0.11.1 + cu128` Windows wheel path.
- The supplied blog tutorial is useful for workflow/checks but targets MinerU 3.1.x and hard-codes PyTorch cu124. Its overall sequence (isolated Python 3.12 env, install `mineru[all]`, CUDA verification, ModelScope model download) is sound, but exact CUDA/lmdeploy choices must be selected from the actual GPU and current official compatibility guidance.
- Official CLI default automatically selects an accelerated backend when supported; explicit `-b pipeline` is the CPU-compatible fallback. Current official docs also expose `hybrid-auto-engine` and `vlm-auto-engine` paths for high-accuracy GPU use.
- Local hardware is an NVIDIA GeForce RTX 5070 Ti Laptop GPU (Blackwell, compute capability 12.0) with 12,227 MiB VRAM; driver 581.57 advertises CUDA 13.0 support. The separately installed CUDA Toolkit is 12.4, but wheel-based PyTorch/lmdeploy carries its own runtime and the official Blackwell route is cu128.
- Host is Windows 11 x64 with 31.5 GB RAM and 132 GB free on D:. This comfortably meets current MinerU requirements. Existing Conda is healthy at `D:\Application\Anaconda` (Conda 26.1.1). The base Python 3.13 must not be used because Windows MinerU supports only 3.10-3.12; a new Python 3.12 prefix environment is required.
- Preferred root `D:\Application\MinerU-Local` already exists but is empty. No existing MinerU command or Conda environment was detected, so the directory can safely host a new prefix environment and models without collision.
- PyPI JSON and GitHub Releases API independently confirm MinerU 3.4.5 as current stable. PyPI declares Python >=3.10,<3.14 and, on Windows, the `all` extra selects `lmdeploy` rather than Linux-only `vllm`.
- Current official extension guidance warns not to install vLLM and lmdeploy together; Windows should use `mineru[core,lmdeploy]` (or the platform-resolved `all` extra). For this Blackwell GPU, official FAQ gives the exact `lmdeploy 0.11.1 + cu128`, CPython 3.12 Windows wheel and a PyTorch cu128 index.
- Official model documentation says the downloader does not accept a custom model destination. Supported relocation is: download to its cache, move the completed model directories, update `mineru.json`, then use `MINERU_MODEL_SOURCE=local`. An environment-specific `MINERU_TOOLS_CONFIG_JSON` can isolate the config, avoiding changes to a user's global MinerU configuration.
- Direct use of the supported `HF_HOME` and `MODELSCOPE_CACHE` variables keeps both official caches inside `D:\Application\MinerU-Local`; no post-download relocation is needed. MinerU's generated config points to the exact immutable snapshot. `MINERU_MODEL_SOURCE=local` will be saved only after both pipeline and VLM snapshots pass completion checks.
- On this network, the most reliable transfer shape differs by file set: four ordinary HTTP workers sustain roughly 1.8-2.0 MB/s for the mixed pipeline assets; Xet submits large chunks less frequently but resumes the 2.31 GB VLM tensor safely. Keeping both strategies in the recovery script avoids optimizing one model class at the expense of the other.
- The installed stack is operational end-to-end on this exact Blackwell laptop GPU: lmdeploy chooses CUDA/TurboMind, MinerU hybrid mode processes the representative PDF, and the local Gradio UI serves successfully. This is stronger evidence than package imports alone.

## 2026-09-15 CLI integration
- Existing deployment records locate MinerU at D:\Application\MinerU-Local\env. Project CLI resolver only checks explicit paths and PATH; isolated Conda environment is not discovered.
- Existing working-tree changes were limited to the three planning records and will be preserved.
- Confirmed Conda registers the actual MinerU prefix in ~/.conda/environments.txt; prefix conda-meta/state contains isolated local model configuration. Direct process launches currently omit these variables.
- Diagnostic command used a word instead of integer for Select-Object -First and failed without mutations; corrected in next inspection.
- Smoke harness initially used the shell's GBK stdout and failed while printing an existing emoji progress message before MinerU launched. Reran the diagnostic with Python UTF-8 enabled; product GUI uses its queue and is unaffected.
- Review evidence: consistency guide currently supplies generic rules/acronyms, sidecar stores summaries rather than block page/bbox provenance, local CLI captures logs without live progress, and local parse cache identity omits engine/model version. These remain recommendations only.
- Reference projects checked: BabelDOC glossary and bilingual outputs; Docling unified document representation; PDFMathTranslate-next Windows distribution.
