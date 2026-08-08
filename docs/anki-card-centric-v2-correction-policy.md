# Card-Centric v2 Correction Policy

Status: decision-locked governing policy for the `card_centric_v2` correction
branches. This document supersedes older 72/80, 70–75, fixed-model, and
primary-source-only language wherever those rules conflict with this policy.

## Quality-first deck sizing

- 60 selected cards is a warning floor.
- 65 is the ordinary target.
- 70 is a soft cap.
- These counts are never quotas. No stage may generate, promote, retain, or
  select a weak, irrelevant, ungrounded, unclassified, malformed, or duplicate
  card merely to reach 60, 65, or 70.
- Fewer than 60 cards is allowed when fewer than 60 cards meet the quality and
  grounding requirements. It must produce a visible warning.
- Ordinary selection stops at 65. Cards 66–70 require explicit nonredundant
  marginal value: the only valid coverage of a required high/medium fact, a
  unique emphasized/testable distinction, or a validated necessary split.
  Count proximity, extra examples, general comprehensiveness, and possible
  usefulness are not reasons.
- Before selecting a marginal card, apply a dominance check. A card whose
  coverage is a subset of a selected card and whose grounding/quality is no
  better is excluded.
- More than 70 is allowed only for validated mandatory, high-value,
  nonredundant coverage. It requires an explicit overflow reason and the
  existing signed manual acknowledgement before envelope issuance or apply.
- Unrecovered S4a semantic exclusions never become padding. Grounded fast
  `LIKELY_YES` and thorough `MAYBE` cards are T6 candidates only below 60 and
  remain subject to all independent eligibility requirements.

The governing tier order is T1 generated critical/high, T2 generated medium,
T3 existing `YES` critical/high, T4 generated low, T5 existing `YES`
medium/low, and T6 grounded fast `LIKELY_YES` plus thorough `MAYBE` below 60.
Within each tier, deterministic ranking prefers mandatory coverage,
primary-source grounding, unique uncovered-fact contribution, higher
importance/emphasis, stronger classification with fewer flags, lower
redundancy, and finally stable identity.

## Required model instruction

Every resolved v2 prompt used by S2, S4b, S4c, S6, and S7 must carry an
instruction adapted from this policy:

> Optimize for the smallest set of the best-supported, highest-yield,
> nonredundant cards. Card counts are soft targets, not quotas. Do not invent
> facts, split one fact into unnecessary cards, preserve a weak card, or label
> a card eligible merely to reach a count. Prefer fewer excellent, grounded,
> nonredundant cards over more marginal cards. A card is valuable only when it
> contributes unique, grounded coverage worth reviewing independently.

Prompt snapshot tests must prove the instruction is present. Deterministic
selection must independently enforce it; wording is not an enforcement layer.

## Evidence and resolution contracts

- Prefer primary-source evidence.
- Grounded summary-only evidence is admissible when labeled
  `summary_grounded`; it is never silently treated as primary-source evidence.
- Preserve `primary_source`, `summary_grounded`, or `fast_pass` through
  coverage, selection, reconciliation, and review.
- S2b uses concept-specific deterministic normalized phrase/token matching
  against `primary_entity` and aliases. It records passage IDs, matched
  character counts, and `total_concepts`. Evidence-poor status is diagnostic
  and review-visible; it does not automatically exclude a card or force a
  generated card.
- Per-fact forbidden cloze targets stay keyed by `fact_id`.
- Split generated cards carry sequential `split_index` values beginning at 1.
- `generated`, `unresolved`, and `duplicate_of_existing` remain distinct
  terminal resolutions.
- Canonical all-generated outputs remain separate from selected deck outputs,
  so selection cannot erase valid fact resolution or skip validation of an
  unselected generated card.

The shared additive interface is
`oms_hub.anki.correction_contracts`; existing v1 and persisted v2 artifacts are
not silently rewritten by S0.

## Configurable routing and replay identity

- S2, S4b, S4c/S6, and S7 use persisted configurable provider/model routes.
  No provider or Claude model name is an architectural invariant.
- Replay identity includes the exact resolved provider, model, relevant
  generation parameters, exact prompt contents/hashes, and persisted batch or
  concurrency settings.
- S4c defaults to batch size 30 unless persisted configuration overrides it.
- S4b uses bounded configurable concurrency with deterministic output ordering;
  no concurrency of 12 is hard-coded as policy.
- Lecture title/required metadata and semantic generation/model/dimensions are
  pinned inputs.
- A11 history is a frozen, hashed window of distinct prior jobs, not a
  revision-weighted live query.

## Failure posture and orphan durability

- Invalid optional S4b output degrades the entire affected batch to
  `NEEDS_REVIEW`; retryable provider failure propagates to worker retry.
- Invalid S4c/S6 output uses the established structured/provider retry policy
  and then blocks. It never becomes eligible or silently degrades.
- Per-card S4a embedding unavailability may pass that card to classification
  only while the pinned snapshot remains valid. Snapshot corruption,
  generation mismatch, and coverage corruption block.
- Semantic-dedupe infrastructure failures retry. After exhausted semantic
  availability, lexical similarity is advisory only and cannot automatically
  declare uniqueness. Invalid/nonfinite/zero/wrong-shape vectors are integrity
  failures.
- Stage success and failure are fenced by exact expected state, running stage,
  and lease owner. A stale worker cannot commit or fail reclaimed work.
- S0 defines but does not scan for orphan artifacts. P1 may adopt only an
  orphan matching the exact job ID, stage, input hash, kind/schema version,
  content checksum, complete-write evidence, and absence of a conflicting
  committed artifact. Any missing or conflicting evidence means ignore and
  recompute. Provider idempotency keys are used only when supported.

## Acceptance gates

- The deterministic fixture lifecycle must use real `CurationServicesRunner`
  handlers and expose stage artifacts without live providers.
- Provider/fault, replay, lease, v1 compatibility, prompt snapshot, full Anki,
  migration, Ruff, mypy, and diff checks must pass with no skipped, xfailed, or
  weakened tests.
- Before merge to `main`, the approved NUC-local heme-synthesis real-data run
  is mandatory. Approximate findings (about seven new cards and three to four
  near-duplicates) never override grounding, fact conservation, or quality.
- A fresh read-only Sol reviewer must approve S0, every lane, the integrated
  candidate, and the acceptance evidence. No implementing agent can approve
  its own diff.
