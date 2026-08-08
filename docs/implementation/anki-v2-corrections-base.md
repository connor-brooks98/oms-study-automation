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
