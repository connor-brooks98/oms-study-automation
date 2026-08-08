# Card-Centric v2 Corrections — S0 Base Record

Recorded: 2026-08-08

## Verified source

- Repository: `connor-brooks98/oms-study-automation`
- Isolated S0 branch: `codex/anki-v2-foundation`
- Freshly fetched `origin/main`:
  `62c6d5f6f78600938c4190cdabc392e1cd409280`
- Reviewed baseline: `0291a3b8c61e5346996e45e7383b39e44e0fc263`
- `src/oms_hub/anki` tree at both revisions:
  `62bfe3707ac930fad926da051fc3955c0ab14d05`
- `tests/anki` tree at both revisions:
  `4d73276d7fae33103188d542fc9209b82b271449`
- Focused `git diff --name-status` across both Anki trees: empty.

The planned `<REPO_PATH>` resolves to
`/Users/connor/Developer/oms-study-automation` as the verified local clone.
S0 work itself is performed only in the isolated clean worktree created from
the exact remote SHA above.

## Environment and baseline

- Python: 3.13.14, satisfying `>=3.12,<3.14`.
- Failure-first baseline modules:
  `test_envelope.py`, `test_pipeline.py`, `test_reconciliation.py`,
  `test_stages.py`, `test_v2_contracts.py`, `test_web.py`, and
  `test_worker.py`.
- Baseline result before S0 changes: 124 passed in 10.54 seconds.

## S0 decisions frozen for downstream lanes

1. P1 owns replay/durability integration using exact prompt/model identity,
   pinned lecture metadata, pinned semantic generation, distinct-job A11
   snapshots, and the strict orphan-adoption evidence contract.
2. P2 owns evidence/classification wiring using the evidence-quality enum,
   configurable routes, persisted parameters, and the quality-first prompt
   requirement.
3. P3 owns fact/generation/selection/reconciliation wiring using fact-keyed
   cloze targets, sequential split indices, conserved terminal resolutions,
   canonical all-generated outputs, selection metadata, and 60/65/70 policy.
4. P4 owns the complete real-handler lifecycle, fault matrix, and review
   surfacing by extending the deterministic fixture foundation.
5. No lane may infer a broad orphan filesystem scanner from the S0 contract.
   Adoption is exact-evidence-only; otherwise recompute.
6. No S0 change mutates curation/apply behavior, unrelated Study Hub behavior,
   Quiz Builder, or public quizzes.

P1–P4 must branch from the final pushed S0 tip reported in the S0 handoff, not
from `origin/main` or an intermediate S0 commit. They may not start until a
separate fresh Sol reviewer reports no blocking S0 finding.

## S0 correction rerun

The failure-first follow-up closed the S0 review blockers before a new handoff:

- stage claim, start, commit, failure, expiry, and reclaim now enforce exact
  state, owner, and unexpired-lease fencing;
- required fact resolutions exactly conserve canonical generated outputs and
  reject cross-fact card links;
- replay inputs retain exact prompt contents and canonical, deeply immutable
  JSON for model parameters and lecture metadata;
- the deterministic lifecycle smoke test routes provider and embedding doubles
  through the production S2, S2b, and S8 handlers.

Verification rerun from the corrected isolated S0 worktree:

- baseline seven-module evidence remains `124 passed` from the immutable base;
- named S0 handoff suite: `177 passed in 18.74s`;
- complete Anki suite: `468 passed in 21.64s`;
- `ruff check .`: passed;
- `mypy src/oms_hub/anki`: passed for 46 source files;
- `git diff --check`: passed.

## Fresh-review correction rerun

The fresh S0 review found two bounded blockers, both corrected on the same
isolated foundation branch before any P1--P4 lane began:

- worker-level `defer_job` and `fail_job` mutations now use atomic conditions
  for exact owner, expected state, and unexpired lease; a worker that has lost
  ownership or lease validity yields without failing or deferring the job;
- the lifecycle fixture is importable through the repository's documented
  `pytest` console entry point, not only through `python -m pytest`.

Failure-first evidence was observed before the fixes: the new worker regression
ended in `failed`, the expired `defer_job`/`fail_job` calls accepted no fencing
timestamp, and the console-script lifecycle test failed collection with
`ModuleNotFoundError: No module named 'tests'`.

Exact zero-skip/zero-xfail S0 rerun commands and results:

```text
.venv/bin/pytest tests/anki/test_envelope.py tests/anki/test_pipeline.py tests/anki/test_reconciliation.py tests/anki/test_stages.py tests/anki/test_v2_contracts.py tests/anki/test_web.py tests/anki/test_worker.py tests/anki/test_anki_repository.py tests/anki/test_v2_correction_contracts.py tests/anki/test_v2_lifecycle_fixture.py -ra
178 passed in 19.55s

.venv/bin/pytest tests/anki -ra
469 passed in 22.57s

.venv/bin/ruff check .
All checks passed!

.venv/bin/mypy src/oms_hub/anki
Success: no issues found in 46 source files

git diff --check
passed
```

The final pushed foundation tip is reported in the external S0 handoff after
the verification commit is created and the remote ref is independently read
back. P1--P4 remain blocked until a fresh reviewer approves that exact tip.
