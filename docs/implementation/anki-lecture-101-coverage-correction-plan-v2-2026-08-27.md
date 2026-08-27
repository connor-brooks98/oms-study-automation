# Anki Lecture 101 Coverage Correction Plan v2

Date: 2026-08-27  
Repository: `/Users/connor/Developer/oms-study-automation`  
Branch: `codex/anki-v3-recovery`  
Baseline candidate: `537638b95249032afa230f959670715e766d93f9`  
Baseline acceptance job: `bef26970-327b-4648-a12c-0a66df62e8af`  
Status: approved by the independent reviewer in the
`Review Anki Lecture 101 coverage` task; implementation in progress

Independent verdict: `APPROVE` (2026-08-27). The reviewer found no remaining
blockers and confirmed that all eight prior approval conditions pass.

Amendment: the Jeffrey Modell split-list requirement is superseded by
`anki-lecture-101-jeffrey-modell-depth-addendum-2026-08-27.md`; one grounded
awareness card is sufficient for this lecture.

## 1. Goal and non-goals

Correct the remaining Lecture 101 coverage errors while preserving the pipeline's
existing priorities:

- prefer a grounded AnKing card when that card actually teaches the lecture fact;
- generate a custom card only for a fact that remains uncovered;
- preserve every resolved baseline fact unless a reviewed crosswalk explains its
  replacement or removal;
- keep all writes behind the existing human apply gate.

This change will not add a second disease-specific coverage framework, a new
provider call, a new embedding generation, or a card-count quota. It will not
change production port 8765 during implementation or isolated acceptance.

## 2. Reviewer findings adopted

The revised plan accepts all eight approval conditions from the first independent
review:

1. Semantic similarity will not transfer fact coverage.
2. The concept-to-all-facts fallback will be unavailable to new v2 artifacts.
3. Facts will have a non-positional stable identity and the acceptance report will
   include an explicit old-to-new crosswalk for changed propositions.
4. A concept remains limited to five facts; additional facts spill into a
   continuation concept instead of being recombined.
5. Conflicting Extra content will use a field-level review annotation, not
   `CardFlag`.
6. Only MAYBE-derived T6 selection will be removed; fast-pass T6 remains.
7. Mechanism-level tests and per-fact no-regression acceptance replace count
   targets.
8. Diagnostic-workflow concepts are allowed only when a cited lecture passage
   teaches the workflow as a workflow. Lecture 101 will not gain a standalone
   diagnostic-framework concept from the evidence currently identified.

## 3. Minimal contract changes

### 3.1 Stable fact identity

Keep the current positional `fact_id` (`Cxx-My`) as the within-ledger routing key.
Add a deterministic `stable_fact_key` computed by application code for each fact:

```text
sha256("fact-v1\0" + normalize(primary_entity) + "\0" + normalize(fact_statement))
```

`normalize` is Unicode NFKC normalization, trim, internal-whitespace collapse,
and Unicode casefold. The model never creates or repeats the hash. The serialized
ledger exposes a positional-ID-to-stable-key map, and downstream fact evidence,
coverage, gap, dedupe, reconciliation, and acceptance records retain both values.

This preserves stable identity when concepts or facts are reordered. When a
composite baseline proposition is intentionally atomized, the acceptance report
contains a reviewed crosswalk entry with one of these dispositions:

- `unchanged`: one old stable key maps to the same new stable key;
- `superseded_by`: one old proposition maps to one or more new atomic stable keys;
- `removed_with_reason`: the old proposition was pedagogy/control metadata or was
  otherwise intentionally removed, with a source-grounded reason.

No old fact is silently matched to a new fact by position or semantic similarity.

### 3.2 Fact-level card evidence

Add one small classifier structure:

```text
CoveredFactEvidence
  fact_id
  field: text | extra
  span
```

`CardClassification.covered_fact_evidence` contains at least one evidence row for
every `covered_fact_id`, and no row for an uncovered fact. The validator requires:

- the fact IDs in the evidence rows exactly equal `covered_fact_ids`;
- each span is nonblank and occurs literally in the named normalized
  `CardRecord.text` or `CardRecord.extra` field;
- the existing concept/fact consistency and lecture-passage grounding checks
  still pass.

The lecture passage remains evidence that the card is relevant and correct. It
cannot substitute for a proposition absent from the card because it is not an
allowed location for `covered_fact_evidence`.

### 3.3 Field-level Extra review

Add an orthogonal review annotation rather than extending `CardFlag`:

```text
CardFieldReview
  field: text | extra
  disposition: exclude_from_fact_evidence
  reason
```

An annotated card remains eligible when its clean field proves the covered fact.
The excluded field cannot supply `CoveredFactEvidence`, and the annotation is
persisted into candidate provenance and shown in the existing review diagnostics.
This lets a usable Text field survive when Extra is unsupported or conflicts with
the lecture without silently endorsing Extra or generating a replacement for the
tested fact.

No new automatic conflict detector is introduced. The classifier reports the
field disposition from the same source/card comparison it already performs, and
the validator enforces the disposition mechanically.

## 4. Ledger correction

### 4.1 Depth is control metadata, not a card proposition

Keep `CardConcept.depth` and the existing derived `importance`. Update the ledger
prompt and validator so a concept/fact must be an independently testable medical
proposition. Reject a ledger whose fact is solely a statement about lecture depth,
coverage, emphasis, or pedagogy.

This is a prompt-and-validator correction, not a second contract for coverage
controls. The Lecture 101 meta rows are handled as follows in the acceptance
crosswalk:

- old C11 depth summary: `removed_with_reason`; its named diseases remain covered
  by their medical facts;
- old C12 depth summary: `superseded_by` concrete THI and Jeffrey Modell facts;
- old C13 depth summary: `superseded_by` concrete WAS, ataxia-telangiectasia, and
  XLP facts.

The validator is deliberately narrow: it rejects control-only statements, not
medical facts merely because their wording mentions severity or clinical
importance.

### 4.2 Atomicity and the five-fact ceiling

Update the ledger prompt to forbid independently testable clauses from sharing a
fact description. In Lecture 101 this separates at least:

- IgA-mediated transfusion reaction from safe-product prevention;
- CD40L location, CD40 location, and the isotype-switch consequence;
- CVID phenotype, vaccine-response finding, and diagnosis by exclusion.

Keep `suggested_fact_count <= 5`. If an entity needs a sixth fact, the model must
emit a continuation concept with the same `primary_entity`, the next sequential
concept ID, and at most five facts. Stable fact keys make this split auditable.
The validator rejects a continuation that merely repeats or recombines a fact.

Jeffrey Modell is not represented as ten facts. It is one medium-depth recognition
fact whose existing split-card generator may produce two ordered list cards. No
new split mechanism is needed. XLP is limited to one or two surface facts:

- SH2D1A/SAP dysfunction with impaired control of EBV-infected B cells;
- the EBV-associated fulminant mononucleosis/HLH/lymphoma/hepatitis recognition
  pattern.

### 4.3 Diagnostic-framework scope

Do not create a standalone diagnostic-framework concept for Lecture 101. Existing
disease concepts retain their explicitly taught diagnostic facts. A future lecture
may have a workflow concept only when at least one cited slide/transcript passage
teaches that workflow as a workflow; scattered tests across disease sections are
not sufficient.

## 5. Close every fact-credit path

### 5.1 Classification and coverage

For new card-centric v2 classifier output, missing `covered_fact_ids` or missing
`covered_fact_evidence` fails validation. `_classification_fact_ids` may perform
concept-to-all-facts expansion only for `card_centric_v1`; v2 coverage must use
the validated fact IDs from the artifact.

Persisted v1 behavior remains loadable. Persisted v2 artifacts do not gain legacy
concept-wide credit during replay.

### 5.2 Semantic dedupe

Cosine similarity remains a retrieval signal only. In `_card_dedupe_v2`, a
generated fact may resolve as `duplicate_of_existing` only when the nearest
existing note's independently validated classification already contains:

- the same stable fact key; and
- valid `CoveredFactEvidence` for that fact.

If similarity is at or above the current threshold but that proof is absent, keep
the generated card as a `semantic_dedupe_review` non-terminal. Do not transfer its
fact ID to the existing note. The existing semantic-review path already excludes
that generated card from automatic selection and leaves the fact unresolved for
human review, so no new terminal state is needed.

Generated-to-generated duplicate handling remains unchanged because both cards
originate from the same requested fact. Record the existing nearest-match score in
the semantic review/terminal diagnostics rather than treating the free-text reason
as authority.

### 5.3 Selection and reconciliation

Delete automatic unions from `duplicate_fact_ids_by_note` in both
`select_high_yield_v2` and `_card_reconciliation`. An existing note's fact coverage
comes only from its validated classification evidence. A proven duplicate may
still conserve the exact target identity, but it cannot add new facts to that
target.

Remove the classifier-MAYBE branch that creates T6 candidates. Preserve the
separate fast-classifier T6 loop and its below-floor recall behavior unchanged.
MAYBE cards remain visible in the frozen candidate partition and review data but
are not selected automatically.

After atomization, explicitly exercise `_without_dominated_candidates` against the
seven useful baseline custom cards. A baseline fact may disappear only through an
`unchanged`, `superseded_by`, or reviewed `removed_with_reason` crosswalk outcome;
subset pruning is not a valid disappearance reason.

## 6. Deterministic implementation checks

Add focused tests beside the existing card-centric v2 lifecycle tests. At minimum:

1. A classification cannot claim a fact without a literal Text/Extra evidence
   span, and a lecture passage cannot fill the missing card text.
2. A 0.88-similar existing note does not inherit a generated fact without its own
   validated fact evidence; the result becomes semantic review, not a duplicate
   terminal.
3. A v2 classification with concepts but no fact IDs fails closed; the v1
   compatibility fixture still expands concept coverage.
4. A card with excluded Extra and valid Text evidence remains selectable, while
   Extra cannot prove coverage.
5. A MAYBE thorough classification is not selected as T6, while an eligible fast
   classification still is.
6. Stable fact keys remain unchanged after concept/fact reordering, and an
   acceptance crosswalk can map a composite old fact to multiple new atomic facts.
7. Five facts validate; a sixth in one concept fails; a continuation concept
   preserves all six distinct stable keys.
8. The seven useful baseline custom-fact outcomes survive candidate dominance or
   have an explicit reviewed disposition.
9. IgA prevention, CD40-on-B-cell location, and CVID-exclusion fixtures cannot be
   credited to partial cards.
10. Depth-only rows cannot reach gap generation or unresolved medical-fact counts.

Run:

- the focused new tests first;
- the complete `tests/anki` suite;
- Ruff on all changed Python files;
- `git diff --check`.

Implementation stops if any unrelated failure is shown to be caused by this
candidate. Existing unrelated failures, if any, must be identified separately and
must not be waived as coverage success.

## 7. Isolated NUC acceptance on port 8788

Only after independent approval and local verification:

1. Transfer the exact tested commit to the existing isolated NUC staging checkout.
2. Restart only staging port 8788 and verify production port 8765 still reports
   its unchanged revision and remains healthy.
3. Reuse the copied database, immutable companion snapshot, and Voyage semantic
   generation from the baseline run. Do not re-embed the AnKing collection.
4. Start a new Lecture 101 curation job with the same slide, transcript, outline,
   deck/tag scope, and supported provider route.
5. Do not apply the result to Anki.
6. Save the new job ID, exact revision, ledger stable-key map, old-to-new
   crosswalk, per-fact resolution comparison, selection audit, and review URL.

### 7.1 Per-fact no-regression gate

For every medical fact resolved by baseline job
`bef26970-327b-4648-a12c-0a66df62e8af`, require exactly one outcome:

- the same stable fact key remains resolved;
- every `superseded_by` successor is resolved; or
- a human-reviewed `removed_with_reason` entry explains why the old fact should no
  longer produce a card.

The seven useful baseline custom facts are named assertions, not inferred from a
target count. Newly exposed gaps must resolve as a grounded existing note, a valid
custom card, or an explicit unresolved/review-required outcome. No count range is
an acceptance condition.

### 7.2 Lecture 101 content assertions

The review must show:

- IgA-safe transfusion products directly tested;
- CD40 on B cells directly represented;
- CVID diagnosis by exclusion directly represented;
- XLA-versus-THI vaccine response preserved;
- XLP represented by no more than two source-supported surface facts;
- Jeffrey Modell warning signs represented compactly through the existing split
  path;
- no depth/pedagogy statement in medical gap or unresolved counts;
- no automatically selected thorough MAYBE cards;
- conflicting THI Extra visible as a field-level review annotation if the card is
  otherwise retained;
- production port 8765 untouched and no staging result applied.

## 8. Stop conditions

Do not proceed to implementation until the independent reviewer returns
`VERDICT: APPROVE` on this revision. If it requests changes, revise this same plan
and resubmit it until approved.

After isolated acceptance, report the result to the user. Do not push, merge,
restart production, deploy to port 8765, or apply cards without a separate user
instruction covering that action.
