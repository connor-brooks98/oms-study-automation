# Task 13: Safe Anydoc lecture-document evaluation

## Status

Complete.

## Implementation

`OMS_HUB_DOCUMENT_PARSER_MODE` is validated as `legacy`, `shadow`, or
`anydoc`, with safe `shadow` as the default. Slide filing preserves the
immutable PowerPoint first, then runs a non-Anki document evaluator. Parser
diagnostics cannot interrupt the PowerPoint/PDF filing workflow.

The evaluator compares a local legacy PowerPoint parser against Anydoc and
produces atomic JSON reports under `document-processing/shadow`. Reports retain
only checksums, parser metadata, timings, counts, coverage, warning diagnostics,
and normalized-text hashes—never source text or credentials. Candidate failures
are recorded as promotion blockers but do not block shadow-mode slide filing.
Anydoc-primary mode returns the candidate semantic document when available and
otherwise uses the legacy result with `degraded` and `fallback_used` reporting.

`scripts/evaluate_anydoc_corpus.py` recursively reads supported documents,
uses temporary asset storage, atomically replaces only its aggregate report,
and exits nonzero when a promotion blocker is found. It does not consult or
change the configured parser mode.

## Design judgments

- The legacy PowerPoint baseline lives in the new document-processing module,
  not in Anki. This keeps parser evaluation independent from Anki extraction
  code and artifacts.
- Shadow report writes occur before PDF conversion but after immutable PPTX
  preservation, so comparison always sees the verified source while parser
  issues remain non-blocking for filing.
- The corpus command uses a temporary asset directory rather than writing
  parser assets into the corpus or report directory; the aggregate JSON is its
  only durable output.

## Verification

```text
./.venv/bin/pytest -q tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py
3 passed

./.venv/bin/pytest -q tests/document_processing tests/v2
passed

./.venv/bin/mypy src/oms_hub/document_processing/shadow.py scripts/evaluate_anydoc_corpus.py src/oms_hub/slides/pipeline.py src/oms_hub/config.py src/oms_hub/app.py
Success: no issues found in 5 source files

./.venv/bin/ruff check src/oms_hub/document_processing/shadow.py scripts/evaluate_anydoc_corpus.py src/oms_hub/slides/pipeline.py src/oms_hub/config.py src/oms_hub/app.py tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py
All checks passed!

./.venv/bin/python scripts/evaluate_anydoc_corpus.py --help
passed

git diff --check
passed
```
