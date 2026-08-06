# Task 14: Quiz Builder acceptance and release operations

## Status

Complete, with native Windows/Office execution explicitly deferred to a
controlled Windows host.

## Delivered

- Added end-to-end Studio acceptance coverage proving that one supplied-answer
  question retains its exact stem and provenance without invoking NotebookLM or
  AI fallback, and that a tracked explicit-no-support generated answer cannot
  publish until its individual answer is verified.
- Added release-package contracts for Python `>=3.12,<3.14`, the exact
  `firecrawl-anydoc==0.1.3` pin, the retained pinned PDF-Inspector extra, the
  complete Task 1-13 OMS Hub runtime/template/static inventory in both release
  archives, retained Hatch wheel packages, and the `oms-anki-agent` entry
  point.
- Added a Windows Python 3.12 CI job that creates a virtual environment,
  installs the `dev`, `document-processing`, and `pdf-inspection` extras,
  imports `anydoc` and `pdf_inspector`, and exercises document-processing,
  study-generation, and V2 tests. The Ubuntu job installs the same optional
  extras. Both default jobs exclude the existing `windows_office` marker because
  GitHub-hosted runners do not provide desktop Office.
- Added a real deterministic PPTX-to-PDF smoke test using
  `SerialOfficeConverter`, marked `windows_office` and platform-guarded. It is
  collected but skipped outside Windows; it was not executed in this local run.
- Added the Quiz Builder operator guide and linked it from the README. It
  covers installation, model task setup, parser mode rollback, corpus promotion,
  NotebookLM/fallback behavior, human verification, test/live ports, recovery,
  and release evidence without copying secrets.

## Verification

```text
./.venv/bin/python -m pytest tests/document_processing tests/llm tests/study_generation tests/v2 tests/test_progress.py -q
462 passed, 1 skipped (`windows_office` native smoke is deferred)

node --test tests/js/lecture.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js tests/js/public_quiz_library.test.js tests/js/settings.test.js tests/js/studio_quiz_images.test.js tests/js/studio_quiz_review.test.js tests/js/uploads.test.js
63 passed

./.venv/bin/python -m pytest tests/anki tests/agent -q
459 passed

node --test tests/js/anki.test.js
27 passed

./.venv/bin/python -m ruff check src tests
All checks passed

./.venv/bin/python -m mypy src
Success: no issues found in 163 source files

./.venv/bin/pytest -q tests/document_processing
66 passed

./.venv/bin/pytest -q tests/v2/test_quiz_builder_acceptance.py tests/v2/test_anydoc_release.py tests/document_processing/test_windows_office.py
5 passed, 1 skipped (native Windows Office smoke deferred)

./.venv/bin/python -m pytest tests/v2/test_release_package.py tests/v2/test_notebooklm_release_package.py tests/v2/test_anydoc_release.py -q
6 passed

git diff --check
passed
```

## Deferred native evidence

The Windows CI job validates Python 3.12 installation and imports, but it
intentionally leaves `windows_office` marked. No manual or native Office test
has been run for this change. Before release, run the marked Office automation
checks on a controlled Windows host with desktop Microsoft Office installed,
then record that result alongside the tested Anydoc, PDF-Inspector, Python,
provider, and model versions. Also perform the required manual copied-data
acceptance on port 8787 before operating the live port 8765.

## Scope audit

Task 14 changes documentation, CI, acceptance/release/native-smoke tests, this
report, and one bounded shared static-gate line in `src/oms_hub/files/pdf.py`:
the stale `pdf_inspector` import ignore was removed after the optional package
became installed and typed. It does not change Anki implementation, Anki tests,
or package manifests.
