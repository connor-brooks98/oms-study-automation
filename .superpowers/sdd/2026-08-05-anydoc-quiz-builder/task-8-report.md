# Task 8: NotebookLM-first missing-answer resolution

## Status

Complete.

## Implementation

`StoredNotebookLMGateway.answer_studio_question` reuses the existing stored
client lifecycle and course/exam notebook resolution. It verifies that every
selected supporting source is distinct, present in that notebook, and ready;
the question request is sent only with those selected IDs. Strict JSON parsing
produces either a decisive `answered` result or explicit `no_support`.
Malformed, empty, hedged, internally inconsistent, and out-of-range results
raise a contract error and do not become no-support results.

`PracticeAnswerResolver` returns supplied answers unchanged. Missing answers
go to NotebookLM first. Only a valid `no_support` result calls the configured
`LLMTask.QUIZ_ANSWER_GENERATION` path. Generated answers validate one in-range
index, rationale, evidence, and uncertainty note, then remain
`generated_by_ai`, unverified, and without a verification timestamp.

## Verification

```text
./.venv/bin/pytest -q tests/study_generation/test_stored_notebook_gateway.py tests/study_generation/test_practice_answers.py
21 passed

./.venv/bin/pytest -q tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_matching.py tests/llm/test_service.py tests/study_generation/test_practice_domain.py
34 passed

./.venv/bin/mypy src/oms_hub/study_generation/notebook.py src/oms_hub/study_generation/practice_answers.py
Success: no issues found in 2 source files

./.venv/bin/ruff check src/oms_hub/study_generation/notebook.py src/oms_hub/study_generation/practice_answers.py tests/study_generation/test_stored_notebook_gateway.py tests/study_generation/test_practice_answers.py
All checks passed!
```

## Related correction

Task 7 identifier safety correction is committed separately as
`562cd93 fix: reject contradictory practice identifiers`.
