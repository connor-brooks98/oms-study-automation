# Task 14: Quiz Builder acceptance and release operations

## Status

Complete, with native Windows/Office validation explicitly deferred to CI and a
controlled Windows host.

## Delivered

- Added end-to-end Studio acceptance coverage proving that supplied-answer
  imports never invoke NotebookLM and that an explicit-no-support generated
  answer cannot publish until its individual answer is verified.
- Added release-package contracts for Python `>=3.12,<3.14`, the exact
  `firecrawl-anydoc==0.1.3` pin, the retained pinned PDF-Inspector extra, the
  Quiz Builder document-processing/runtime/template/static assets, and the
  retained `oms-anki-agent` entry point.
- Added a Windows Python 3.12 CI job that creates a virtual environment,
  installs the `dev`, `document-processing`, and `pdf-inspection` extras,
  imports `anydoc` and `pdf_inspector`, and exercises document-processing,
  study-generation, and V2 tests. It excludes the existing `windows_office`
  marker because GitHub-hosted Windows runners do not provide desktop Office.
- Added the Quiz Builder operator guide and linked it from the README. It
  covers installation, model task setup, parser mode rollback, corpus promotion,
  NotebookLM/fallback behavior, human verification, test/live ports, recovery,
  and release evidence without copying secrets.

## Verification

```text
./.venv/bin/python -m pytest tests/document_processing tests/llm tests/study_generation tests/v2 tests/test_progress.py -q
461 passed

node --test tests/js/lecture.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js tests/js/public_quiz_library.test.js tests/js/settings.test.js tests/js/studio_quiz_images.test.js tests/js/studio_quiz_review.test.js tests/js/uploads.test.js
63 passed

./.venv/bin/python -m pytest tests/anki tests/agent -q
459 passed

node --test tests/js/anki.test.js
27 passed

./.venv/bin/python -m ruff check src tests
All checks passed

./.venv/bin/python -m pytest tests/v2/test_release_package.py tests/v2/test_notebooklm_release_package.py tests/v2/test_anydoc_release.py -q
5 passed

git diff --check
passed
```

`./.venv/bin/python -m mypy src` was also run. It reports one pre-existing,
out-of-scope error: `src/oms_hub/files/pdf.py:36` has an unused type-ignore
comment. No Task 14 production file was changed to suppress or work around it.

## Deferred native evidence

The Windows CI job validates Python 3.12 installation and imports, but it
intentionally leaves `windows_office` marked. Before release, run the marked
Office automation checks on a controlled Windows host with desktop Microsoft
Office installed, then record that result alongside the tested Anydoc,
PDF-Inspector, Python, provider, and model versions. Also perform the required
manual copied-data acceptance on port 8787 before operating the live port 8765.

## Scope audit

Task 14 changes only documentation, CI, acceptance/release tests, and this
report. It does not change Anki implementation, Anki tests, or package
manifests.
