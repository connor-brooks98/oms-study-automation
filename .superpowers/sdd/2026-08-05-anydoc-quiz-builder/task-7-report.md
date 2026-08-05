# Task 7: practice extraction provenance correction

## Status

Complete.

## Corrected interface

Extraction output now cites canonical material with immutable, strict
`SegmentCitation(source_id, segment_key)` and
`AssetCitation(source_id, asset_key)` values. The extractor validates each
citation against the document named by its `source_id` during the same
one-retry structured-output attempt. It resolves retained question citations to
`QuestionSourceRef` values using the exact canonical segment locator and exposes
them as `ExtractionResult.question_source_refs`.

`pair_supplied_answers` remains compatible with its two original arguments and
accepts the optional keyword-only `question_source_refs` for canonical
provenance. It never fabricates a source ID or locator.

## Safety behavior

- Reused local segment and asset keys remain distinct across documents.
- Validation errors (including document/key mismatches) receive one schema
  feedback retry; a repeat error retains both raw responses and provider
  metadata records in `ExtractionError`.
- Merge conflicts preserve both question records and add blockers for divergent
  composite content, same normalized identifier with different source
  references, and same references with different identifiers.
- Pairing performs exact normalized-ID pairing before residual source-order
  pairing. A residual pair is accepted only when equal totals, unique IDs, and
  non-contradictory residual labels make it safe.

## Verification

```text
./.venv/bin/python -m pytest -q tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_matching.py tests/llm/test_service.py tests/study_generation/test_practice_domain.py
32 passed

./.venv/bin/python -m mypy src/oms_hub/study_generation/practice_contracts.py src/oms_hub/study_generation/practice_extraction.py src/oms_hub/study_generation/practice_matching.py
Success: no issues found in 3 source files

./.venv/bin/python -m ruff check src/oms_hub/study_generation/practice_contracts.py src/oms_hub/study_generation/practice_extraction.py src/oms_hub/study_generation/practice_matching.py tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_extraction.py tests/study_generation/test_practice_matching.py
All checks passed!
```
