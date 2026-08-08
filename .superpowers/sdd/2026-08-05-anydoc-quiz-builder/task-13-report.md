# Task 13: Safe Anydoc lecture-document evaluation

## Status

Complete.

## Implementation

`OMS_HUB_DOCUMENT_PARSER_MODE` is validated as `legacy`, `shadow`, or
`anydoc`, with safe `shadow` as the default. Slide filing preserves the
immutable PowerPoint first, then runs a non-Anki document evaluator. Parser
diagnostics or report-write failures cannot interrupt the PowerPoint/PDF filing
workflow.

The evaluator compares a local legacy PowerPoint parser against Anydoc and
produces atomic JSON reports under `document-processing/shadow`. Reports retain
only checksums, parser metadata, timings, counts, coverage, stable warning
fingerprints, and normalized-text hashes—never source text, exception strings,
URLs, or credentials. Candidate failures, warnings, empty output, text-hash
differences, reduced coverage, and lower note/table/asset counts are promotion
blockers but do not block shadow-mode slide filing. Anydoc-primary mode returns
the candidate semantic document only when the comparison has no blockers; it
otherwise uses the legacy result with `degraded` and `fallback_used` reporting.

The legacy baseline derives its asset count directly from embedded PPTX picture
blobs, keeping only SHA-256-addressed metadata. Duplicate image bytes are
counted once; their slide locator is retained only when all occurrences are on
the same slide. No baseline image bytes are persisted or copied.

PPTX enrichment retains Anydoc segment keys, text, and ordering while restoring
only unambiguous slide and asset locations. Missing speaker notes are appended as
source-proven note records rather than replacing candidate semantic text.

`scripts/evaluate_anydoc_corpus.py` recursively reads only PPTX sources in a
casefolded, case-sensitive tie-broken order, uses temporary asset storage,
atomically replaces only its aggregate report, and exits nonzero when a
promotion blocker is found. An empty or unsupported-only root is itself a
stable `no comparable PPTX files` promotion blocker, so corpus approval cannot
be vacuous. It does not consult or change the configured parser mode.

## Design judgments

- The legacy PowerPoint baseline lives in the new document-processing module,
  not in Anki. This keeps parser evaluation independent from Anki extraction
  code and artifacts.
- Shadow report writes occur before PDF conversion but after immutable PPTX
  preservation, so comparison always sees the verified source while parser
  issues remain non-blocking for filing.
- Parser errors are reduced to role-specific stable codes and warning values to
  SHA-256 fingerprints. This deliberately trades raw diagnostics for durable
  reports that cannot leak source excerpts or secrets.
- The legacy parser mode directly invokes only the legacy processor. Candidate
  calls remain isolated to shadow and Anydoc-primary evaluation.
- The corpus command uses a temporary asset directory rather than writing
  parser assets into the corpus or report directory; the aggregate JSON is its
  only durable output.

## Verification

```text
./.venv/bin/pytest -q tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py tests/document_processing/test_pptx_locator.py tests/document_processing/test_anydoc_adapter.py
30 passed

./.venv/bin/pytest -q tests/document_processing tests/v2
passed

./.venv/bin/mypy src/oms_hub/document_processing/shadow.py scripts/evaluate_anydoc_corpus.py src/oms_hub/slides/pipeline.py src/oms_hub/config.py src/oms_hub/app.py
Success: no issues found in 5 source files

./.venv/bin/ruff check src tests scripts
All checks passed!

./.venv/bin/python scripts/evaluate_anydoc_corpus.py --help
passed

git diff --check
passed

`git diff --name-only | rg '(^|/)anki/'` produced no output.
```
