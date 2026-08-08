# Card-Centric v2 Lifecycle Fixture

The S0 smoke test invokes the real `CurationServicesRunner` dispatch and proves
that stage products are retained as exposed fixture artifacts. The reusable
script and doubles provide deterministic inputs for:

- S2 ledger output;
- S4b fast batches;
- S4c and S6 thorough batches;
- S7 multi-fact and split-card generation;
- S8 embeddings and semantic dedupe;
- selection output; and
- S9 reconciliation output.

P4 extends this harness into an unskipped full lifecycle. The completed fixture
must cover S2b evidence diagnostics/quality, S4a inclusion and exclusion, S4b
degradation, S4c/S6 routing, the explicit 0.40–0.50 audit disposition,
multi-fact and sequential split generation, unique and duplicate S8 outcomes,
quality-first selection, S9 fact/status/cloze conservation,
`READY_FOR_REVIEW`, and envelope eligibility.

Tests must assert externally meaningful stage artifacts. No live provider,
blanket skip, permanent `xfail`, or implementation-detail-only substitute is
accepted.
