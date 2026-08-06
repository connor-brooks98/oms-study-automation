# Task 10: Unified imported-question review

## Status

Complete across the three checkpoint commits:

- `700cc16 feat: gate imported quiz publication by review`
- `61ad3ee feat: bind imported quiz image candidates`
- `HEAD feat: add unified imported question review UI`

## Implementation

Direct imports now have one review page that exposes only review-safe question
data. It supports editing, answer verification, candidate-image selection, and
candidate previews. Candidate metadata never contains an asset path or bytes;
the preview handler re-resolves the question-scoped candidate server-side,
checks the canonical hash, and returns an inline private `FileResponse`.

The route layer accepts direct-import runs only while they are awaiting review.
It uses 404 for a non-import workflow and 409 for an invalid review state. The
legacy `/images` route redirects to `/review`; Notebook-generation runs at the
new destination continue to render the established image upload/override page.
Publication remains server-gated by the review service inside the existing
transaction, so a disabled browser button is never relied upon for correctness.

The review client renders untrusted question, source, and candidate text with
DOM APIs and `textContent`. It always refreshes authoritative review data after
an edit, verification, or image selection. Failed refreshes and mutations leave
the already rendered question cards intact and report the error through an
`aria-live` message.

## Judgment calls

- Image selection is a dedicated CSRF-protected POST instead of mixing a
  candidate identifier into a question PATCH. This avoids partial persistence
  if candidate verification/copying fails.
- A null candidate selection is accepted only for a question that has no image
  requirement; a required image cannot be cleared from the client.
- Direct preview uses the review-normalized quiz and stays separately gated by
  the same blockers. Candidate cards provide verified per-candidate previews;
  publication media remains the sanitized copied asset from checkpoint B.

## Verification

```text
./.venv/bin/pytest -q tests/study_generation/test_practice_review.py tests/study_generation/test_studio_repository.py tests/study_generation/test_studio_worker.py tests/v2/test_quiz_builder_routes.py
34 passed

./.venv/bin/pytest -q tests/study_generation tests/v2
passed

node --test tests/js/studio_quiz_review.test.js tests/js/studio_quiz_images.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js
21 passed

./.venv/bin/mypy src/oms_hub/study_generation/practice_review.py src/oms_hub/web/studio_routes.py
Success: no issues found in 2 source files

./.venv/bin/ruff check src/oms_hub/study_generation/practice_review.py src/oms_hub/web/studio_routes.py tests/v2/test_quiz_builder_routes.py
All checks passed!
```

## Second follow-up contract correction

`fix: preserve quiz review edits and public ids` assigns `q1`, `q2`, and so on
only when the private review draft becomes a native/public quiz. Imported draft
identifiers remain private review-route keys and cannot become public schema
identifiers. Preview and published public content/answer flows therefore use
the same valid public IDs while retaining the existing choice IDs and media
references.

Partial review edits now normalize and validate only supplied fields. Omitted
answer fields are preserved exactly, which permits metadata review of incomplete
drafts and avoids reopening verification for a stem/topic/area/objective-only
edit. Explicit choices, correct-index, and rationale changes retain strict
validation and the established manual-correction/reverification transition;
invalid edits are rejected before anything is persisted.

Second-correction verification:

```text
./.venv/bin/pytest -q tests/study_generation/test_practice_review.py tests/study_generation/test_studio_repository.py tests/study_generation/test_studio_worker.py tests/v2/test_quiz_builder_routes.py tests/v2/test_public_quiz_routes.py
passed

./.venv/bin/pytest -q tests/study_generation tests/v2
passed

node --test tests/js/studio_quiz_review.test.js tests/js/studio_quiz_images.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js
21 passed

./.venv/bin/mypy src/oms_hub/study_generation/practice_review.py
Success: no issues found in 1 source file

./.venv/bin/ruff check src/oms_hub/study_generation/practice_review.py tests/study_generation/test_practice_review.py tests/v2/test_quiz_builder_routes.py
All checks passed!
```

## Follow-up verification correction

`fix: reverify edited quiz rationales` closes a review-state gap: a normalized
rationale change now counts as an answer edit. It marks the answer manually
corrected, clears any prior verification timestamp, and retains the historical
verification requirement. A supplied or NotebookLM answer receives the manual
correction provenance on a rationale edit but is not newly made verification
required when it had never required verification. Stem and review-metadata-only
edits leave answer provenance and verification timing unchanged.

The regression suite covers a generated-answer verify → rationale edit →
reverify cycle and a stale publication attempt after that rationale edit; the
latter creates no published record and does not change the run token/state.

Correction verification:

```text
./.venv/bin/pytest -q tests/study_generation/test_practice_review.py tests/study_generation/test_studio_repository.py tests/study_generation/test_studio_worker.py tests/v2/test_quiz_builder_routes.py
38 passed

./.venv/bin/pytest -q tests/study_generation tests/v2
passed

node --test tests/js/studio_quiz_review.test.js tests/js/studio_quiz_images.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js
21 passed

./.venv/bin/mypy src/oms_hub/study_generation/practice_review.py
Success: no issues found in 1 source file

./.venv/bin/ruff check src/oms_hub/study_generation/practice_review.py tests/study_generation/test_practice_review.py
All checks passed!
```
