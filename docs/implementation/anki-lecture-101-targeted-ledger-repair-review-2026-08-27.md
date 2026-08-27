# Lecture 101 targeted ledger-repair review

Date: 2026-08-27  
Branch: `codex/anki-v3-recovery`  
Current candidate: `dc197d77536bbcef0b9f8b00bb62e72af72461e4`
Environment: isolated NUC staging on loopback port 8788; production port 8765 was not restarted or changed

Amendment: all Jeffrey Modell ten-item completeness requirements below are
superseded by `anki-lecture-101-jeffrey-modell-depth-addendum-2026-08-27.md`,
which was independently approved after the user clarified the lecture's
awareness-only depth.

## Requested decision

Review the acceptance evidence below and return `APPROVE` or `BLOCK` for the
targeted-repair plan. Implementation is paused pending this decision.

## Previously approved invariants

The prior independent review approved these requirements, which remain unchanged:

1. Similarity cannot transfer fact coverage.
2. Concept-only fallback is limited to legacy v1 artifacts.
3. V2 facts have application-computed stable content identities.
4. Concepts contain at most five atomic facts; overflow uses continuation concepts.
5. Existing-card field issues are review annotations, not coverage flags.
6. MAYBE-derived T6 coverage is removed; the grounded fast-pass route remains.
7. Acceptance is per-fact and has no card-count quota.
8. Diagnostic workflows are standalone concepts only when the lecture teaches them as workflows.
9. Lecture-depth metadata and generic promises are not card-generating facts.
10. Named checklists preserve their source-listed items and thresholds.

## Implemented behavior that is working

- Exact fact entailment, not semantic similarity, controls coverage.
- Stable fact keys are persisted but excluded from provider schemas.
- Uncovered v2 facts cannot be suppressed by concept-level coverage.
- Dedupe holds unproven semantic matches for review instead of silently dropping facts.
- Meta statements, semicolon-bundled facts, combined expression locations, and incomplete Jeffrey Modell lists fail closed.
- Every entity named in the summary depth map must appear at the stated depth.
- Bounded matching lecture passages can substantiate named checklists without sending the full lecture to S2.
- All local Anki tests and Ruff checks passed before staging acceptance.

## Acceptance evidence

All jobs used Lecture 101, source revisions 164 and 193, outline 104, the
`AnKing Step Deck` / `heme` scope, and existing Voyage generation
`7dbffd33-feaa-4bae-9d73-ad911fb01c43`. No re-embedding or Anki apply occurred.

### Anthropic

Final candidate job: `ee29a95a-fde0-41f5-b1ea-5c458a94a970`  
Route: `anthropic / claude-sonnet-5`

The provider repeatedly returned one or more of:

- a generic Jeffrey Modell depth statement instead of the ten warning signs;
- semicolon-bundled atomic facts;
- CD40L and CD40 expression locations combined in one fact.

The primary and its one complete-ledger repair both failed deterministic validation.

### OpenAI

Cross-provider job: `1a32c60a-aaad-4491-b64e-4e61e5784eb5`  
Route: `openai / gpt-5.6-luna`

OpenAI accepted the model route and stayed well below the output-token cap, but
independently produced the same failure class. Across its bounded stage retries:

- primary outputs bundled semicolon clauses or multiple expression locations;
- repairs omitted the Jeffrey concept, retained a combined location fact, or
  replaced Jeffrey with another generic depth statement.

This makes another provider swap unlikely to address the root cause.

### Token-bound finding

Supplying matching passages for every depth-listed disease caused both Anthropic
calls to consume the full 7,000 output-token allowance. Restricting supplemental
evidence to named checklists reduced OpenAI primary calls to about 2,600-2,800
output tokens and removed the overflow. The bounded-evidence design should remain.

## Root cause

The repair contract asks the provider to regenerate the complete ledger after one
localized validation defect. That creates a large regression surface: while fixing
Jeffrey Modell, the provider can alter unrelated valid concepts and introduce a
semicolon, location bundle, omission, or new meta statement elsewhere. Two
independent providers reproduced this behavior.

The deterministic validators are correctly detecting the defects; weakening them
would hide missing lecture facts. More provider retries would be nondeterministic
and expensive.

## Proposed revised plan

### 1. Keep the primary ledger and every current validator

Do not weaken source entailment, atomicity, depth-control completeness, checklist
content, stable fact identity, dedupe, or reconciliation.

### 2. Replace complete-ledger repair with targeted concept repair

When a primary response is valid JSON but fails ledger validation:

- identify only the invalid or missing concept IDs and exact validator defects;
- send the raw affected concept objects, the exact errors, and only their bounded
  matching source passages to one repair call;
- require a small repair schema containing concept replacements and additions;
- merge by explicit concept ID into the untouched primary JSON;
- validate the complete merged ledger once;
- fail closed if an ID is unknown, a valid unrelated concept changes, the repair
  is incomplete, or the merged ledger remains invalid.

There is still only one repair provider call. Invalid or truncated primary JSON
cannot be safely targeted and should continue to fail closed.

### 3. Preserve auditability

Persist the primary attempt, exact bounded validation errors, repair request
identity, repair response, merge manifest, and final full-ledger hash. Stable fact
keys remain application-computed after the merged ledger validates.

### 4. Handle the observed defects through the same generic contract

- Jeffrey Modell: replace only its concept with a grounded fact containing all
  ten source-listed signs and thresholds.
- CD40/CD40L: replace only the affected concept with separate expression-location
  facts and a separate switching consequence.
- Semicolon/meta statements: replace only the named invalid concept.
- Missing depth entity: add only the missing concept at the required depth.
- More than five facts: return a continuation addition with a new sequential ID.

The current shared validator contains an explicit Lecture-101 Jeffrey Modell
item checklist. This is a deliberate acceptance-specific exception, not a
general checklist framework. The targeted repair remains driven by validator
errors and bounded source evidence. Generalizing checklist extraction is outside
this correction unless a second lecture demonstrates the same need.

### 5. Deterministic tests before another provider call

Add tests proving:

- a localized repair cannot modify valid unrelated concepts;
- replacement/addition IDs are explicit, unique, and complete;
- missing depth entities can be added without reordering existing concepts;
- invalid JSON, unknown IDs, duplicate IDs, partial repairs, and invalid merged
  ledgers fail closed;
- stable fact keys are computed only from the final validated ledger;
- the persisted merge manifest reproduces the final ledger exactly.

Run all `tests/anki`, Ruff, and diff checks.

### 6. Isolated acceptance

Deploy only to NUC staging port 8788 and rerun the identical Lecture 101 inputs
with existing Voyage encodings. Start with direct OpenAI Luna because it stayed
within the token bound. Audit the ledger before later stages, then allow the job
to finish. Do not apply cards. Do not restart or deploy production port 8765.

## Reviewer questions

1. Does targeted concept repair preserve the approved fail-closed and auditable contract?
2. Should invalid JSON remain an immediate failure, or may it use the old complete-ledger repair?
3. Is one targeted repair call sufficient, or should any failed merge stop the job immediately?
4. Are any additional invariants required before implementation?

## Revision after reviewer block

The reviewer returned `VERDICT: BLOCK` on the first targeted-repair draft. This
revision adopts all eight approval conditions.

### Prerequisites completed

1. The actual corrected Lecture 101 payload is recorded in
   `anki-lecture-101-depth-control-evidence-2026-08-27.md`. It contains slide 47
   and transcript 36. Slide 47 alone includes all ten signs and thresholds.
2. The fixed two-passage cap remains because the selected slide is complete; the
   matcher now uses required token inclusion instead of a brittle contiguous
   phrase. A future named checklist must add its own source-completeness proof
   before it may use this repair path.
3. Multiple molecule/location pairs now reject both `X is expressed on A, Y is
   expressed on B` and the compact `X is expressed on A and Y on B` shape. The
   first clause is anchored to literal expression wording, and tests prove that
   ordinary BTK and ATM `acts on ... and ...` mechanism facts remain valid.
4. Jeffrey completeness is keyed from the concept entity/aliases/canonical/fact
   corpus, so renaming the fact to `Ten warning signs...` cannot evade validation.

### Complete layered defect collection

Before a targeted repair request, parse the sanitized primary JSON exactly once.
Unparseable or truncated JSON fails immediately and receives no repair call.

For parseable JSON:

- Preserve the raw parsed concept array and map each array index to its explicit
  `concept_id`.
- A missing, malformed, or duplicate `concept_id`, a malformed concept object, or
  malformed top-level ledger routing fields is not safely targetable and fails
  immediately.
- Refactor the existing concept, ledger, and source-depth checks into pure defect
  collectors used by both normal validation and repair targeting. Collect every
  defect in one pass rather than stopping at the first Pydantic layer.
- Concept-level defects name the raw array index and resolved `concept_id`.
  Ledger-wide duplicate-fact defects name every involved concept. Missing depth
  entities are additions and carry their required entity and depth.
- The repair request includes the full collected defect set, affected raw concept
  objects only, and bounded matching lecture passages only.

This refactor must not duplicate validation rules: the same collectors remain the
single authority for ordinary ledger validation and repair eligibility.

### Targeted repair merge invariants

- The repair schema permits replacements and additions only. It cannot delete a
  concept.
- Every replacement must name one existing defective `concept_id`; every named
  defect must receive exactly one replacement or authorized addition.
- Existing concepts retain their order and IDs. Additions use the next sequential
  IDs and append after existing concepts; no renumbering is allowed.
- Unmodified concepts remain the same parsed objects. Their canonical JSON byte
  hashes before and after merge must be identical; the original provider bytes
  remain preserved separately in the attempt record.
- Repair scope is capped at `min(3, max(2, ceil(20% * existing concept count)))`
  affected replacements/additions. A primary exceeding that ceiling is not a
  localized defect and fails without a repair call.
- There is exactly one targeted repair call. Unknown IDs, duplicate repair IDs,
  omissions, excess scope, changed unrelated concepts, or an invalid merged ledger
  stop the job immediately. There is no complete-ledger fallback and no second
  repair.
- Stable fact keys are computed only after the merged full ledger passes every
  concept, ledger, and source-depth validator.

### Audit additions

Persist a merge manifest containing the primary response hash, full collected
defect list, index-to-ID mapping, replacement IDs, addition IDs, unchanged-concept
hashes, bounded evidence IDs, repair response hash, and final ledger hash. Review
diagnostics show the count and identities of repaired concepts.

### Required deterministic tests

In addition to the earlier tests, prove:

- the actual Lecture 101 evidence matcher returns slide 47 with all ten signs;
- compact `X on A and Y on B` facts fail atomicity;
- a Jeffrey concept whose fact omits the literal name still cannot evade
  completeness;
- all validation layers contribute defects to one request;
- malformed or duplicate concept IDs and invalid JSON receive no repair call;
- the repair schema cannot delete, reorder, or renumber concepts;
- untouched concepts retain identical canonical byte hashes;
- additions use only the next sequential IDs;
- the repair-scope ceiling fails closed;
- any invalid or incomplete merge stops after one repair call; and
- the merge manifest reproduces the final ledger and stable-key map.
