# Lecture 101 targeted ledger-repair review

Date: 2026-08-27  
Branch: `codex/anki-v3-recovery`  
Current candidate: `5cdb06904f1a6584de4b51c7b78f5ee0e3859164`  
Environment: isolated NUC staging on loopback port 8788; production port 8765 was not restarted or changed

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

No Lecture-101 medical content is hard-coded into production code; the repair is
driven by validator errors and bounded source evidence.

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
