# Task 12: Separate quiz and practice-question libraries

## Status

Complete.

## Implementation

Published quiz records now retain their content kind on every publication path:
lecture generation writes `lecture_quiz`, while Quiz Builder and direct-import
publication copy the selected Studio run kind. Replacements update the existing
publication's kind within the same transaction as the successor run, preserving
the shared quiz token and its version progression.

`GenerationRepository.published_quizzes()` accepts content-kind filters. The
existing `/public/quizzes` library includes only lecture and exam-review quizzes;
the new `/public/practice-questions` library includes only practice questions.
Both libraries render through the same grouping helper and link into the
unchanged token-based player, content, answer, media, and outline endpoints.

Both library pages provide **Quizzes** and **Practice Questions** navigation,
and the application navigation exposes the same destinations. Existing browser
progress storage remains keyed by token and version, so moving between library
views does not merge progress.

## Judgment calls

- `published_quizzes()` keeps an optional filter for backward compatibility
  with existing internal callers that deliberately need all active
  publications. The new library routes always supply explicit immutable kind
  sets, so the new public behavior remains strictly separated.
- The configured-public-host boundary now exempts exactly the two library
  routes plus the established `/public/quizzes/*` player/endpoint family.
  `/public/practice-questions` gets no new token or asset subtree, which keeps
  the shared-player surface unchanged and private application paths protected.

## Verification

```text
./.venv/bin/pytest -q tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py
21 passed

./.venv/bin/pytest -q tests/study_generation tests/v2
passed

node --test tests/js/public_quiz_library.test.js tests/js/public_quiz.test.js
23 passed

./.venv/bin/mypy src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/repository.py src/oms_hub/web/public_quiz_routes.py src/oms_hub/app.py
Success: no issues found in 4 source files

./.venv/bin/ruff check src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/repository.py src/oms_hub/web/public_quiz_routes.py src/oms_hub/app.py tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py
All checks passed!

git diff --check
passed
```
