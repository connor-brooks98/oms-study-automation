# Current Card-Centric Anki Curation Process

> **Correction authority:** This remains a baseline description of coded v1 behavior.
> For `card_centric_v2` correction work, the decision-locked policy in
> [Card-Centric v2 Correction Policy](anki-card-centric-v2-correction-policy.md)
> governs deck sizing, evidence quality, model routing, failure posture, replay
> identity, and acceptance. In particular, 60/65/70 are a warning floor,
> ordinary target, and soft cap—not quotas.

This document summarizes the `card_centric_v1` Anki curation pipeline exactly as it is
coded at commit `231fe3e9899ba1f6293a601f35b29b0012de3f82`. It describes current behavior,
validation, retries, warnings, and stopping conditions. It does not describe proposed
changes.

## Pipeline overview

The run advances through an immutable, stage-based pipeline:

1. **S0 — Preflight**
2. **S1 — Source index and Anki card census**
3. **S2 — Lecture concept ledger**
4. **S3 — Existing-card tag scope**
5. **S4 — Existing-card classification**
6. **S5 — Initial coverage calculation**
7. **S6 — Residual whole-deck search**
8. **S7 — Gap-card generation**
9. **S8 — Generated-card deduplication**
10. **Selection — Existing and generated card selection**
11. **S9 — A1–A10 reconciliation**
12. **Review, envelope creation, and apply**

Each completed stage is stored as a content-addressed JSON artifact. The artifact is
bound to the job, pipeline contract, model configuration, input hash, and earlier
artifacts. A committed artifact is rejected if its file is missing, its content hash
changes, its provenance does not match the job, or the same artifact identity is reused
with different content.

## Stage-by-stage behavior before reconciliation

### S0 — Preflight

- Confirms that AnkiConnect is reachable, the collection is accessible, and sync is
  available.
- Synchronizes prompt files and records whether the prompt checkout is stale.
- Pins the full prompt snapshot used by the job, including prompt contents, versions,
  hashes, paths, and metadata.
- Extracts the selected lecture sources.
- Requires slide, transcript, and summary sources for `card_centric_v1`.

Current fallback and failure behavior:

- AnkiConnect availability, connection, and timeout failures can be retried by the
  worker.
- A source-extraction failure becomes a blocking `source_preflight_failed` artifact.
- Missing slide, transcript, or summary input becomes a blocking
  `required_sources_missing` artifact.
- A stale prompt checkout does not stop preflight; it is carried forward to A10 as a
  warning.

### S1 — Source index and Anki card census

- Builds a source index from summary, transcript, slide, and speaker-note passages.
- Rejects passages belonging to another lecture.
- Requires unique passage IDs and records source revision hashes and a source hash.
- Takes a snapshot of existing Anki notes from the configured deck scope.
- Classifies census cards as target-tagged, another-system-tagged, untagged, or excluded
  by deck.
- Pins the source, card snapshot, and census for later stages.

Current fallback and failure behavior:

- Malformed or changed pinned artifacts stop the run with `PinnedInputChanged`; this is
  not automatically retried.
- If no deck-eligible notes exist, S3 later stops the job because tag scope cannot be
  trusted.

### S2 — Lecture concept ledger

- Sends the pinned lecture sources to the configured ledger model.
- Produces a coverage checklist containing stable concept IDs, canonical statements,
  primary entities, aliases, depth, emphasis, importance, and forbidden cloze targets.
- Requires at least one concept.
- Requires unique concept IDs and unique, nonblank aliases and forbidden cloze targets.
- Validates that importance agrees with depth and emphasis.

Current fallback and failure behavior:

- Structured-output failures are worker-retryable.
- Network, quota, or provider-service errors are worker-retryable.
- Invalid ledger data that cannot satisfy the schema prevents the stage from completing.

### S3 — Existing-card tag scope

- Uses the explicitly configured tag scope when present.
- If no explicit scope was supplied when the job was created, the repository attempts to
  resolve exactly one recognized medical-system token from lecture subject or topic.
- Splits the pinned census into scoped and unscoped note IDs and verifies that the two
  sets are unique, disjoint, and cover the complete card snapshot.
- Chooses residual mode from the untagged-card rate:
  - Below 15%: `gaps_only`.
  - At or above 15%: `all_concepts`.

Current fallback and failure behavior:

- A zero-card census is a blocking tag-scope failure.
- A legacy failed job with a blank saved tag scope has a special manual-retry repair. The
  repository resolves the scope from lecture metadata and rewinds that job from the
  source-index portion of the pipeline.
- An untagged rate above 3% is not blocked here. It is reported by A9, and a rate of at
  least 15% activates the broader `all_concepts` residual sweep.

### S4 — Existing-card classification

- Classifies scoped cards as `YES`, `MAYBE`, or `NO` in batches of 40, with up to eight
  concurrent batches.
- Requires exactly one classification for every card in each batch.
- Rejects duplicate, omitted, or invented note IDs.
- Rejects invented concept IDs and supporting passage IDs.
- Requires every `YES` classification to cite a source passage.
- Stores batch request IDs, token usage, cost, and cache telemetry.

Current fallback and failure behavior:

- Provider network, quota, and service failures can be retried by the worker.
- Structured-output failures can be retried by the worker.
- A classification that violates the exact-partition or grounding rules is rejected
  before S5.

### S5 — Initial coverage calculation

- Creates a coverage entry for every concept in the S2 ledger.
- A card counts as coverage only when it is:
  - Classified `YES`.
  - Free of classifier flags.
  - Supported by at least one non-summary lecture passage.
- Concepts with qualifying evidence become `covered`; all others become `uncovered`.

Current fallback and failure behavior:

- `MAYBE`, `NO`, flagged `YES`, summary-only `YES`, and ungrounded output cannot suppress
  later residual or gap work.
- Every concept left uncovered is sent to S6 when residual mode is `gaps_only`.

### S6 — Residual whole-deck search

- Uses `gaps_only` mode for concepts uncovered by S5.
- Uses `all_concepts` mode when the census untagged rate is at least 15%.
- Builds semantic queries from each targeted concept's primary entity and aliases.
- Searches the pinned Anki snapshot with a limit of 12 hits per query.
- Excludes already-scoped note IDs from the residual results.
- Runs the same grounded classifier over residual hits.
- Merges only clean, primary-source-supported `YES` results into coverage.
- Rejects a note if the primary and residual classifiers judged the same note twice.

Current fallback and failure behavior:

- If there are no residual targets, S6 writes an empty successful residual artifact.
- If semantic search finds no qualifying card, the concept remains uncovered and moves to
  S7.
- Voyage, semantic-snapshot, connection, and timeout failures can be retried by the
  worker.
- A8 later verifies that every concept uncovered after S5 was included in the residual
  target list.

### S7 — Gap-card generation

- Processes each concept still uncovered after the merged S5/S6 coverage calculation.
- Creates one required fact ID per uncovered concept: `<concept_id>-M1`.
- Sends primary lecture evidence to the configured gap-generation model; summaries are
  excluded from the evidence payload.
- Requires each output row to be either:
  - A generated card with nonblank text, note type, and cited source passages; or
  - An unresolved record with a nonblank reason.
- Requires the set of returned fact IDs to equal the expected fact ID for that concept.
- Rejects invented passage IDs and generated cards supported only by summary passages.
- Materializes source-evidence records from the cited immutable lecture passages.
- Creates a deterministic card ID from concept ID, fact ID, text, and extra text.
- The prompt and schema permit multiple generated rows for the same fact when they are
  split cards.

Current fallback and failure behavior:

- Unsupported facts can be represented explicitly as unresolved instead of being filled
  from general medical knowledge.
- Network, quota, provider-service, and structured-output errors can be retried by the
  worker.
- The stage validates the **set** of returned fact IDs, but it does not currently enforce
  one output row per fact or validate repeated fact IDs against the `split` flag.

### S8 — Generated-card deduplication

- Performs deterministic lexical comparison within each concept cluster.
- Compares generated cards with eligible existing cards and with generated cards already
  accepted in the same run.
- Uses a token-overlap threshold of 0.80.
- Keeps a generated card when neither comparison reaches the threshold.
- Marks a match as `duplicate_of_existing` and records the existing note ID or generated
  card ID and a reason.

Current fallback and failure behavior:

- A duplicate is retained as a resolution record but is not treated as a newly generated
  card.
- During reconciliation, duplicate resolutions become intentional gaps rather than new
  output.
- Distinct split cards below the 0.80 lexical-overlap threshold remain separate generated
  cards.

### Selection

- Eligible existing cards are clean, grounded `YES` classifications.
- Cards covering emphasized or high-importance concepts are mandatory and selected first.
- Remaining cards are ranked by emphasis, importance, depth, coverage breadth, and stable
  note ID.
- The selector next favors coverage diversity, then fills toward a target of 65.
- The ordinary cap is 70.
- Generated cards fill only remaining capacity; they are not used solely to manufacture a
  60-card minimum.
- `minimum_target: 60` is stored in the selection artifact, but the card-centric
  reconciler's coded hard minimum is A6's total of 10 selected cards.
- If mandatory existing cards alone exceed 70, they are not truncated. The selection must
  later receive an exact server-issued overflow acknowledgement.

## S9 — A1–A10 reconciliation

A failed hard assertion sets `can_render_envelope` to `false`, creates a blocking
reconciliation error, and moves the job to `failed`. A warning is recorded but does not
block review.

### A1 — Every required fact is represented

Check:

- Required fact IDs must equal the union of generated fact IDs and unresolved fact IDs.
- A fact cannot be both generated and unresolved.
- Required fact IDs must be unique.

Current safeguards and fallback:

- S6 tries to find an existing card before a fact is declared missing.
- S7 must return a generated or explicit unresolved resolution for the expected fact ID.
- Unresolved output is the coded fallback when primary lecture evidence does not support a
  generated card.
- A1 is a hard stop; it has no reconciliation-time repair.

### A2 — Exact generated/unresolved row count

Check:

- The number of required fact IDs must equal generated-card rows plus unresolved rows.
- Generated fact IDs must be unique across generated-card rows.
- Unresolved fact IDs must be unique across unresolved rows.

Current safeguards and fallback:

- S7 requires a nonempty structured resolution list and validates the expected fact-ID
  set.
- Generated and unresolved row shapes are schema-validated.
- There is no A2 repair or normalization step.
- Although S7 permits split cards with repeated fact IDs, A2 currently rejects repeated
  generated fact IDs. Distinct split cards that survive S8 therefore fail A2.
- Manually retrying an A2 reconciliation failure resumes at reconciliation and reuses the
  same S7/S8 artifacts; it does not regenerate or collapse the card rows.

### A3 — Every scoped card has exactly one classification

Check:

- Observed scoped classification note IDs must exactly equal the expected scoped note IDs.
- No scoped note ID may appear more than once.

Current safeguards and fallback:

- S3 creates an exact card partition.
- S4 validates every classifier batch for missing, invented, or duplicate note IDs.
- Primary and residual classifier outputs are also checked for duplicate note IDs when
  merged.
- A3 is a hard stop; there is no reconciliation-time classification fallback.

### A4 — Every ledger concept has a final state

Check:

- The final coverage map must contain exactly the S2 concept IDs.
- Every concept must be `covered` or `intentional_gap`; `uncovered` is not accepted.

Current safeguards and fallback:

- S5 initializes coverage for every ledger concept.
- S6 can add coverage from unscoped existing cards.
- S7 generated cards convert their concepts to `covered`.
- Unresolved or deduplicated S7 resolutions convert their concepts to
  `intentional_gap`.
- A4 is a hard stop if any concept remains uncovered or the concept map changes.

### A5 — Forbidden cloze targets are protected

Check:

- Generated card text is scanned for cloze deletions.
- HTML is removed and whitespace is normalized before comparing the clozed answer with
  the ledger's forbidden cloze targets.

Current safeguards and fallback:

- S2 records forbidden targets.
- S7 receives those targets in its generation prompt.
- The final deterministic A5 scan does not rely on the model's self-report.
- A5 is a hard stop; no automatic rewrite or regeneration occurs at reconciliation.

### A6 — Minimum selected output

Check:

- Selected existing note IDs plus selected generated card IDs must total at least 10.

Current safeguards and fallback:

- Selection aims for 65 cards and protects mandatory high-importance evidence.
- Generated cards can use remaining capacity when grounded gaps exist.
- The selector does not add low-quality padding to reach 60 or 65.
- A6 is a hard stop below 10; reconciliation does not generate additional cards.

### A7 — Excessive `NO` classification rate

Check:

- Calculates the proportion of scoped classifications mapped to `drop` (`NO`).
- Warns when the rate is greater than 60%.

Current safeguards and fallback:

- A7 is warning-only and does not stop the run.
- S6 residual search can still find qualifying cards outside the scoped set.
- Remaining gaps can proceed to grounded generation or explicit unresolved output in S7.

### A8 — Residual sweep coverage

Check:

- Every concept uncovered after S5 must appear in S6's recorded residual target list.

Current safeguards and fallback:

- `gaps_only` explicitly targets all S5-uncovered concepts.
- `all_concepts` targets the complete ledger when the untagged rate is at least 15%.
- A8 checks that the fallback search actually ran for every initial gap.
- A8 is a hard stop if the recorded residual target set is incomplete.

### A9 — Untagged-card census rate

Check:

- Warns when the census untagged rate exceeds 3%.

Current safeguards and fallback:

- A9 is warning-only and does not stop the run.
- At an untagged rate of at least 15%, S3 changes S6 from `gaps_only` to
  `all_concepts`, causing a broader whole-deck residual search.
- Between greater than 3% and less than 15%, the warning is recorded and residual mode
  remains `gaps_only`.
- A census with no eligible denominator is blocked earlier at S3.

### A10 — Prompt synchronization state

Check:

- Warns when preflight reported that the prompt checkout was stale.

Current safeguards and fallback:

- Preflight attempts prompt synchronization before loading the job prompt snapshot.
- The exact prompt contents and hashes used by the job are pinned for reproducibility.
- A10 is warning-only; stale prompt state does not stop review.

## Additional reconciliation assertions

The card-centric reconciler also runs three named selection assertions after A10:

- **`selection_cap`** — Output above 70 requires all mandatory notes to remain selected
  and an acknowledgement containing a token, selection digest, and signature.
- **`selection_conservation`** — Selected existing cards must come from eligible `YES`
  cards, and selected generated IDs must come from generated output.
- **`selection_mandatory`** — Mandatory evidence-backed cards cannot be removed.

These are hard assertions. Any failure prevents the envelope from being rendered.

## Review and apply behavior

- A passing initial reconciliation moves the job to `ready_for_review`.
- Each saved review revision recalculates selected existing cards, selected generated
  cards, and concept coverage from the actual reviewed selection.
- A reviewed concept becomes `uncovered` if its supporting selected cards are removed,
  unless it was already an intentional gap.
- Reconciliation is rerun for every saved review revision.
- Selection overflow acknowledgement is generated and stored server-side, HMAC-signed,
  and bound to the exact job, review revision, selection digest, pipeline contract, model
  configuration, and cap.
- Before Anki is mutated, apply verifies the current review revision and any required
  overflow acknowledgement. A missing, stale, or forged acknowledgement fails before
  apply and performs no mutation.

## Global retry behavior

The worker allows up to three attempts for retryable stage exceptions. Delay starts at
five seconds, doubles with each failed attempt, and is capped at 300 seconds.

Automatically retryable categories are:

- SQLite busy errors.
- LLM network, quota, and provider-service errors.
- Structured-output errors.
- AnkiConnect unavailability.
- Voyage embedding errors.
- Semantic snapshot errors.
- Connection and timeout errors.

`PinnedInputChanged` is explicitly non-retryable. Blocking stage products—including
failed A assertions—also move the job directly to `failed`; they do not enter the
worker's automatic exception-retry path.

Manual retry resumes the recorded failed stage. For a card-centric reconciliation
failure, it resumes at S9 using the already committed upstream artifacts.
