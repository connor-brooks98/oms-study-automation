# Task 9: Resumable direct-import worker

## Status

Complete.

## Implementation

`QuizImportWorker` persists canonical JSON artifacts for parse, extraction,
pairing, answer resolution, and normalization. Each artifact is guarded by a
canonical SHA-256 stage signature covering immutable source hashes, source
roles, parser versions, model/prompt fingerprints, and upstream artifact
hashes. A changed input invalidates only derived downstream artifacts and
question-review rows; source snapshots and any publication remain untouched.

The worker attaches only opted-in Supporting Reference and Combined sources
before resolving missing answers. Those notebook/source IDs are saved to the
import binding and reused on retries. Questions and answer keys are never
attached by this path. Existing deterministic pairing blockers stop at review;
the expected missing-answer markers are resolved through the Task 8
NotebookLM-first resolver before review.

All direct imports end in `awaiting_review`; this task does not publish. Provider
authentication/model failures are terminal, retryable provider failures and
SQLite-busy failures use capped backoff, and extraction contracts receive one
worker retry while retaining raw provider output in the run attempt.

`StudioWorker` now delegates direct-import runs to the new worker and retains
the existing NotebookLM workflow. App construction wires the parser router,
extractor, Task 8 resolver, and import worker without any database migration.

## Verification

```text
./.venv/bin/pytest -q tests/study_generation tests/document_processing
211 passed

./.venv/bin/mypy src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_worker.py src/oms_hub/study_generation/studio_repository.py src/oms_hub/study_generation/studio_domain.py src/oms_hub/app.py
Success: no issues found in 5 source files

./.venv/bin/ruff check src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_worker.py src/oms_hub/study_generation/studio_repository.py src/oms_hub/study_generation/studio_domain.py src/oms_hub/app.py tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_studio_worker.py
All checks passed!
```
