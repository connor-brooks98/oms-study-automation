# Task 11: Quiz Builder workflows

## Status

Complete.

## Implementation

`/studio` remains the stable route and API prefix, while the navigation, title,
headings, generic client errors, and workflow controls now call the feature
**Quiz Builder**. NotebookLM remains named only where a generated quiz uses it.

The page contains two native workflow buttons with `aria-pressed`. Generate
Quiz remains the deterministic default, and both workflow panels stay in the
DOM. Its original NotebookLM source intake, filtering, selection, deletion,
destination, resizable prompt, and run history remain available.

Import Practice Questions adds file, pasted-text, and URL snapshot intake. Each
source has an explicit role. The optional NotebookLM attachment is enabled only
for supporting-reference and combined-question/answer roles; switching to a
question or answer-key role clears it. Import-run payloads use the existing
direct-import endpoint with `practice_questions` content and role-specific
source entries.

Run history now identifies workflow/content kinds without returning raw provider
responses. Direct-import stages have user-facing parse, extraction, pairing,
answer-resolution, and review labels, and awaiting-review imports receive a
safe review URL. Existing polling retains the rendered content on refresh
failures.

## Judgment calls

- The UI sends `workflow_kind: direct_import` as explicit client intent even
  though the established import endpoint already selects that workflow. This
  remains forward-compatible with its current request model while making the
  browser contract unambiguous.
- Imported snapshot rows are removable from the pending run locally. This does
  not delete the saved server snapshot, avoiding accidental loss before a user
  queues the run.

## Verification

```text
./.venv/bin/pytest -q tests/v2/test_quiz_builder_routes.py
9 passed

./.venv/bin/pytest -q tests/study_generation tests/v2
passed

node --test tests/js/notebook_studio.test.js tests/js/studio_quiz_review.test.js tests/js/studio_quiz_images.test.js tests/js/public_quiz.test.js
25 passed

./.venv/bin/ruff check src/oms_hub/web/studio_routes.py tests/v2/test_quiz_builder_routes.py
All checks passed!

./.venv/bin/mypy src/oms_hub/web/studio_routes.py
Success: no issues found in 1 source file
```
