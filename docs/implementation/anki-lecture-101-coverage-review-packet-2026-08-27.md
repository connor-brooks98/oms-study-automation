# Anki Lecture 101 Coverage: Independent Review Packet

Date: 2026-08-27  
Repository: `/Users/connor/Developer/oms-study-automation`  
Branch: `codex/anki-v3-recovery`  
Candidate under review: `537638b95249032afa230f959670715e766d93f9`  
Status: review only; implementation of the plan below is paused pending independent approval

## 1. Purpose

Study Hub builds a lecture-specific Anki deck by:

1. extracting a fact ledger from the lecture slides, cleaned transcript, and outline;
2. retrieving cards from the user's existing AnKing collection;
3. classifying whether each existing card is grounded in the lecture and which exact lecture facts it covers;
4. generating custom AnKing-style cloze cards only for facts that no adequate existing card covers;
5. deduplicating and selecting a small, high-quality deck for review;
6. applying the approved deck only after explicit user review.

The desired result is not a fixed card count. It is a grounded, nonredundant deck that uses AnKing cards wherever they genuinely teach the lecture fact and custom cards only for true gaps.

Lecture 101 is a dense lecture on inborn errors of immunity. It emphasizes how to distinguish diseases using inheritance, gene defects, immune-cell patterns, immunoglobulin patterns, characteristic infections, diagnostic findings, and management.

This packet asks an independent reviewer to assess:

- the comparison evidence from three curation runs;
- the remaining factual omissions and false coverage decisions;
- the diagnosis of the lecture-depth meta-statement problem;
- the proposed correction plan and acceptance criteria;
- whether the plan is general enough for other lectures and not overfit to this case.

## 2. Safety and environment

- Production Study Hub is still healthy on NUC port 8765 at commit `08c94078b70efb1370adf010497166166359fded`.
- The candidate runs in an isolated NUC checkout on loopback port 8788.
- Staging uses a copied Hub database and separate job/artifact directories.
- Staging reuses the immutable existing AnKing companion index and Voyage semantic generation `7dbffd33-feaa-4bae-9d73-ad911fb01c43`.
- No Voyage re-embedding was performed.
- No staging result has been applied to Anki.
- Production was not restarted or changed for this test.

The candidate contains two additional local commits that have not been pushed:

- `7fee5df` — move evidence-ID validation into the existing repair gate;
- `537638b` — preserve content-invalid custom-card repairs as explicit unresolved gaps instead of failing the entire deck.

The full `tests/anki` suite and Ruff pass at the current local candidate.

## 3. Inputs held constant

All comparison runs used the same lecture and Anki corpus:

- lecture ID: `101`;
- slide revision: `164`;
- transcript revision: `193`;
- outline ID: `104`;
- AnKing deck allowlist: `AnKing Step Deck`;
- tag allowlist: `heme`;
- companion snapshot: `local-5fe835da-9c70-4c95-a6d2-a601ecfeeda6`;
- semantic/Voyage generation: `7dbffd33-feaa-4bae-9d73-ad911fb01c43`;
- provider for successful comparison runs: Anthropic `claude-sonnet-5`.

The current isolated result is available on the NUC at:

`http://localhost:8788/anki/jobs/bef26970-327b-4648-a12c-0a66df62e8af`

It is review-only and must not be applied.

## 4. Comparison results

| Run | Job ID | Existing AnKing | Custom | Main outcome |
|---|---|---:|---:|---|
| Original accepted run | `ff2820b5-e6fb-4a1b-8854-933a76006482` | 33 | 16 | Captured many highlighted facts, but existing cards were unmapped, so several custom cards duplicated AnKing cards. |
| Independent comparison | `c00cfadf-94ed-4ff1-ac25-2c0a3db74a10` | 45 | 5 | Reused more AnKing cards, but exposed concept-level coverage collapse, redundant XLA/Wiskott custom cards, and missing facts such as XLP. |
| Current fact-level candidate | `bef26970-327b-4648-a12c-0a66df62e8af` | 51 | 7 | Best overall card mix so far, but still over-credits partial cards, omits specific disease markers, auto-selects three weak MAYBE cards, and emits three lecture-depth meta-statements as unresolved facts. |

### 4.1 Original run: useful facts later lost

The original 16 generated cards included several valid facts:

- saline-washed red blood cells for IgA-deficient patients with transfusion risk;
- anti-IgA-mediated anaphylaxis;
- X-linked SCID with normal B-cell count but absent B-cell function;
- XLA with low or absent mature B cells;
- maternal IgG waning around six months;
- CVID as a diagnosis of exclusion;
- CD40L on CD4-positive T cells;
- Hyper-IgM isotype-switching defect;
- Wiskott-Aldrich eczema, infections, and platelet-related bleeding;
- ataxia-telangiectasia neurologic, vascular, and combined immune findings;
- DiGeorge congenital heart disease as the leading cause of death;
- opportunistic-infection comparison among SCID, DiGeorge, Hyper-IgM, and transient hypogammaglobulinemia.

The problem was not primarily the generated content. It was that missing concept mappings prevented existing AnKing cards from receiving credit, so several of these cards were redundant.

### 4.2 Independent comparison findings

The independent comparison found:

- 21 grounded existing cards had no concept mapping because the classifier received opaque concept IDs without their definitions;
- after concept definitions were added, the mix improved from 33 existing + 16 custom to 45 existing + 5 custom;
- XLA and Wiskott custom cards duplicated selected AnKing cards;
- the pipeline falsely treated the following as already covered:
  - saline-washed RBCs or IgA-deficient donors;
  - the XLA versus transient hypogammaglobulinemia vaccine-response comparison;
  - CD40 expression on B cells;
  - CVID as a diagnosis of exclusion;
- X-linked lymphoproliferative syndrome had no existing or custom card.

That led to commit `95b850e`, which added stable fact IDs and `covered_fact_ids` end to end.

### 4.3 Current fact-level candidate

The current candidate selected 51 existing cards and generated seven custom cards:

1. X-linked SCID may have normal B-cell count but functionally absent B cells because T-cell help is missing.
2. Maternal IgG wanes around six months and unmasks antibody deficiency.
3. Vaccine response distinguishes XLA from transient hypogammaglobulinemia.
4. SCID, DiGeorge, and Hyper-IgM carry opportunistic-infection risk, unlike transient hypogammaglobulinemia.
5. Wiskott-Aldrich clinical triad.
6. Ataxia-telangiectasia clinical triad.
7. Congenital heart disease is the leading cause of death in DiGeorge syndrome and warrants cardiology referral.

These seven custom cards are grounded and useful. However, the current result still misses facts that the independent comparison identified, because a card can be credited for a fact that its own content does not fully state.

## 5. Current disease-by-disease coverage

### 5.1 Strong or mostly strong coverage

#### SCID

Covered by existing and custom cards:

- IL-2 receptor common gamma-chain defect;
- X-linked inheritance;
- ADA deficiency and autosomal-recessive inheritance;
- RAG/VDJ recombination defect;
- recurrent severe infection, diarrhea, thrush, and failure to thrive;
- reduced TRECs;
- stem-cell transplantation;
- normal B-cell count with absent B-cell function in X-linked SCID.

Items still worth checking against the lecture's stated depth:

- distinguishing T/B/NK patterns among common-gamma-chain and ADA SCID;
- maternal T-cell/GVHD clue;
- JAK3 and ZAP70 only if lecturer emphasis warrants cards;
- diagnostic workflow rather than isolated disease facts.

#### DiGeorge syndrome

Covered:

- 22q11 deletion;
- failure of third and fourth pharyngeal pouch development;
- thymic hypoplasia/aplasia and reduced T cells;
- reduced parathyroid hormone and hypocalcemia;
- intracellular/opportunistic infection susceptibility;
- congenital heart disease mortality and referral.

Potential gap:

- lecture-specific physical-examination clues such as long face, micrognathia/retrognathia, and cleft palate.

The selected CATCH-22 mnemonic card is classified MAYBE because the lecture teaches the component findings but not the mnemonic itself.

#### X-linked agammaglobulinemia

Covered:

- BTK mutation;
- X-linked inheritance;
- absent mature peripheral B cells;
- reduced immunoglobulins of all classes;
- presentation after maternal IgG wanes;
- encapsulated bacterial, enterovirus, and Giardia pattern;
- IVIG replacement;
- comparison with CVID and transient hypogammaglobulinemia.

#### Wiskott-Aldrich syndrome

Covered:

- WAS/WASp defect;
- X-linked inheritance;
- actin-cytoskeleton dysfunction;
- eczema, microthrombocytopenic bleeding, and recurrent bacterial sinopulmonary infections.

#### Ataxia-telangiectasia

Covered:

- ATM defect;
- cerebellar ataxia;
- telangiectasias;
- combined B/T-cell immunodeficiency.

One weak MAYBE card states that ataxia-telangiectasia is “always” caused by failure to repair double-strand breaks. The lecture supports ATM response to double-strand breaks and p53 activation, but not that overbroad wording.

### 5.2 Partial coverage with false fact credit

#### Selective IgA deficiency

Covered:

- most common primary immunodeficiency;
- low IgA with normal IgG and IgM;
- mucosal infections;
- transfusion-related anaphylaxis.

Missing or incorrectly credited:

- blood products from an IgA-deficient donor or saline-washed red cells;
- definition in a patient older than four years after excluding other causes of hypogammaglobulinemia;
- management/prognosis only to the depth emphasized by the lecture.

The classifier marked one anaphylaxis card as covering both:

- `C01-M1`: anti-IgA-mediated anaphylaxis; and
- `C01-M2`: prevention with saline-washed RBCs or IgA-deficient donors.

The card itself only states the anaphylaxis risk. Its blank Extra field does not contain the prevention fact. The classifier's reason imported that missing information from lecture evidence.

#### Hyper-IgM syndrome

Covered:

- CD40L on helper T cells;
- class-switching defect;
- high/normal IgM with low IgG, IgA, and IgE;
- X-linked inheritance;
- recurrent pyogenic and opportunistic infections;
- neutropenia.

Missing or only indirectly present:

- CD40 is on B cells;
- male + IgG at least two standard deviations below age-adjusted normal + CD40L mutation or confirmed maternal-family history;
- Ig replacement and G-CSF treatment, if retained at the lecture's deep coverage level.

The current `C09-M1` bundles “CD40L on T cells” and “CD40 on B cells.” A card stating only the first clause receives full fact credit.

#### CVID

Covered:

- adult/recurrent sinopulmonary-infection pattern;
- low immunoglobulins with B cells present and reduced plasma-cell differentiation;
- autoimmune and lymphoma risk;
- quantitative immunoglobulin testing;
- IVIG treatment.

Missing or incorrectly credited:

- diagnosis only after excluding all other defined immunodeficiencies;
- poor or absent response to immunization;
- the lecture's explicit distinction between useful clinical patterns and molecular details the lecturer said not to overlearn.

An adult recurrent-infection vignette is credited with `C10-M1`, “CVID is a diagnosis of exclusion,” although the card does not ask or state that rule.

#### Transient hypogammaglobulinemia of infancy

Covered:

- infant with low IgG and normal IgA/IgM;
- maternal IgG waning;
- normal vaccine response compared with absent response in XLA;
- intact T-cell function and lack of opportunistic infections through custom comparison cards.

Quality concern:

- the selected AnKing card's Extra says the condition usually resolves by 12 months, while the lecture slide describes normal immune function by age 2–6 years. The tested front may still be usable, but conflicting or unsupported Extra content should be surfaced during review rather than silently accepted.

### 5.3 Completely missing

#### X-linked lymphoproliferative syndrome

There is no selected existing card and no custom card.

Lecture evidence includes:

- `SLD:101:0043`: SH2D1A mutation on the long arm of the X chromosome; defective SAP/SLAM signaling; sustained T-cell proliferation with attenuated NK/T-cell elimination of EBV-infected B cells; median onset 3–5 years;
- `SLD:101:0044`: HLH, fatal fulminant infectious mononucleosis, lymphoma, hypogammaglobulinemia, fulminant hepatitis, increased B-cell lymphoma risk, stem-cell transplantation, and possible Ig replacement;
- `TRX:101:0034`: EBV predisposition, infectious mononucleosis, lymphoma, HLH, and fatal hepatic necrosis.

Because the lecturer labeled XLP as surface coverage, likely output should be one or two identification cards, not a deep mechanistic deck:

1. SH2D1A/SAP defect causing impaired NK/T-cell control of EBV-infected B cells.
2. EBV-associated fulminant mononucleosis/HLH/lymphoma/fulminant hepatitis as the recognition pattern.

#### Jeffrey Modell warning signs

There is no selected existing card and no custom card.

The lecturer explicitly said to keep the ten pediatric warning signs in mind. `SLD:101:0047` and `TRX:101:0036–0037` include:

- at least eight new ear infections in one year;
- at least two serious sinus infections in one year;
- at least two months of antibiotics with little effect;
- at least two pneumonias in one year;
- failure to gain weight or grow normally;
- recurrent deep skin or organ abscesses;
- persistent thrush after age one;
- need for IV antibiotics to clear infections;
- at least two deep-seated infections;
- family history of primary immunodeficiency.

This likely warrants two split list cards or another compact reviewable representation, not ten independent low-value cards.

#### General diagnostic framework

The current deck contains individual disease cards but does not clearly preserve the lecture's overall diagnostic approach:

- identify severe, complicated, multifocal, refractory, unusual, or familial infections;
- CBC/differential with attention to neutrophils, lymphocytes, and platelets;
- quantitative IgA/IgE/IgG/IgM;
- specific antibody response to vaccines;
- lymphocyte phenotyping;
- TREC for thymic output;
- targeted BTK/CD40/CD40L/common-gamma-chain testing;
- CH50/AH50 when complement deficiency is suspected.

The reviewer should decide how much of this belongs in the lecture deck without turning the deck into a copy of every slide.

## 6. The three lecture-depth meta-statements

The current ledger contains three non-testable “facts”:

- `C11-M1`: six named immunodeficiencies received deep coverage;
- `C12-M1`: transient hypogammaglobulinemia and Jeffrey Modell signs received medium coverage;
- `C13-M1`: Wiskott-Aldrich, ataxia-telangiectasia, and XLP received surface coverage.

All three are unresolved because they describe the lecture's pedagogy rather than medicine.

They should not become cards, but simply deleting them would conceal real omissions:

- C11 is redundant control metadata because its six diseases already have individual concepts.
- C12 currently hides the missing Jeffrey Modell warning signs.
- C13 currently hides the completely missing XLP content.

The correct fix is to separate coverage/depth control metadata from atomic card facts and then require every named disease or emphasized diagnostic topic to map to concrete facts appropriate to its depth.

## 7. Root-cause analysis

### 7.1 Depth metadata is represented as card-generating content

The ledger model allows a depth summary to be emitted as a normal concept/fact. Downstream coverage and gap generation then treat it like a medical proposition.

### 7.2 Some “fact” descriptions are still composite

Stable fact IDs exist, but the statements behind some IDs bundle multiple independently testable propositions:

- anti-IgA reaction and transfusion prevention;
- CD40L on T cells and CD40 on B cells;
- a disease phenotype and its exclusion rule.

One partial card can therefore suppress generation for the entire composite fact.

### 7.3 Card entailment and lecture evidence are conflated

The current classifier has access to both card content and lecture passages. It sometimes assigns `covered_fact_ids` because the lecture evidence supports the fact, even when the card itself states only part of it.

These are distinct decisions:

1. Is the card's claim grounded in this lecture?
2. Does the card itself teach this exact lecture fact?

Lecture evidence may prove relevance and correctness. It must not fill information missing from the card when assigning fact coverage.

### 7.4 Selection uses weak MAYBE cards to approach the warning floor

The current selector's T6 tier may choose independently grounded MAYBE cards below the 60-card warning floor. Three such cards were selected:

- CATCH-22 mnemonic not explicitly taught;
- absent T cells on flow cytometry not explicitly taught;
- “always” defective double-strand-break repair overstates the lecture.

The warning floor is advisory and should not cause automatic selection of uncertain cards.

### 7.5 There is no explicit disease-marker completeness contract

The ledger can mention a disease without proving that the distinguishing dimensions taught for that disease were converted into atomic facts. Dense comparison lectures therefore need an auditable mapping from named entities and depth to concrete fact IDs.

## 8. Proposed correction plan

Implementation must remain paused until an independent reviewer approves or corrects this plan.

### Phase 1: Separate coverage controls from card facts

Change the ledger contract so that:

- depth is metadata on a named lecture entity or concept;
- depth summaries cannot be emitted as normal card facts;
- every card fact is an atomic, independently testable medical proposition;
- non-cardable coverage controls do not enter gap generation or unresolved-fact counts;
- every named disease or emphasized diagnostic topic must either:
  - map to one or more atomic fact IDs; or
  - carry a source-grounded reason that no card is appropriate.

Expected treatment of the current meta rows:

- remove C11 from card-generating facts after verifying its diseases have concrete concepts;
- split C12 into THI facts and Jeffrey Modell facts;
- split C13 into WAS, ataxia-telangiectasia, and XLP facts at surface depth.

### Phase 2: Enforce atomic fact granularity

Split composite facts before classification:

- IgA anaphylaxis risk and safe-blood-product prevention become separate facts;
- CD40L location, CD40 location, and isotype-switching consequence become separate facts;
- CVID phenotype, vaccine-response finding, and exclusion diagnosis become separate facts;
- comparison facts remain explicit only when the comparison itself is what the lecturer emphasized.

Add validation and tests for:

- one card covering only one clause of a former composite fact;
- two facts under one concept receiving independent coverage decisions;
- partial coverage generating only the missing fact;
- no depth/meta statement reaching gap generation.

### Phase 3: Separate source grounding from card entailment

Refine classification so `covered_fact_ids` means:

- the card's own testable content—front plus permitted explanatory fields—entails the entire fact;
- the fact is also supported by admissible lecture evidence;
- the supporting lecture passage cannot supply a proposition absent from the card;
- unsupported or contradictory Extra content is flagged for review rather than silently accepted.

Required regressions:

- an IgA anaphylaxis card cannot cover saline-washed RBC prevention;
- a CD40L-on-T-cell card cannot cover CD40-on-B-cell location;
- an adult CVID vignette cannot cover “diagnosis of exclusion” unless that rule appears in the card;
- a card with a grounded front but conflicting Extra content is review-required or has an auditable field-level disposition.

### Phase 4: Add an auditable disease-marker checklist

For each named disease/topic, preserve a source-grounded checklist appropriate to lecturer depth. Candidate dimensions are:

- recognition/clinical pattern;
- inheritance;
- gene/protein/pathway;
- affected T/B/NK cells;
- immunoglobulin pattern;
- characteristic organisms/infections;
- diagnostic discriminator;
- management/prognosis;
- explicitly emphasized comparisons.

This checklist should not impose every dimension on every disease. It should record:

- taught and represented by fact IDs;
- taught but intentionally omitted, with reason;
- not taught/not applicable.

Depth should control expected granularity:

- deep: retain emphasized pathogenesis, diagnostic discriminators, major clinical pattern, and management;
- medium: retain recognition pattern and explicitly emphasized thresholds/comparisons;
- surface: retain one or two high-yield identification facts and avoid unnecessary mechanistic detail.

The reviewer should assess whether this can be added without overengineering or overfitting to disease lectures.

### Phase 5: Stop automatic MAYBE selection

Change quality-first selection so:

- MAYBE cards remain visible in review candidates;
- MAYBE cards are not selected automatically merely to approach the warning floor;
- the 60-card warning remains a warning, not a quota;
- only a deliberate reviewer action may promote a MAYBE card.

The three current weak MAYBE selections should therefore be excluded by default.

### Phase 6: Rerun isolated acceptance

Use the existing port-8788 staging arrangement and the same immutable inputs/indexes. Do not deploy or apply.

Compare the new run against all three prior runs at exact fact identity, not only card count.

## 9. Expected corrected deck shape

This is an estimate, not a quota:

- approximately 48 grounded existing AnKing cards after removing the three weak MAYBE selections;
- retain the seven useful current custom cards unless dedupe finds a genuinely equivalent AnKing card;
- add or recover cards for:
  - IgA-safe transfusion products;
  - CD40 on B cells;
  - CVID exclusion diagnosis and possibly vaccine response;
  - XLP recognition/mechanism at surface depth;
  - Jeffrey Modell warning signs at medium depth;
  - any independently confirmed diagnostic-framework gaps;
- likely final range: roughly 60–66 cards, with about three-quarters coming from AnKing.

Quality and complete grounded coverage take precedence over this range.

## 10. Acceptance criteria

### Contract and tests

- No lecture-depth or pedagogy statement is emitted as a card-generating fact.
- Every fact presented to classification and gap generation is atomic and testable.
- Partial card entailment cannot produce full fact coverage.
- Lecture evidence cannot fill a proposition absent from the card.
- Partial concept coverage generates only the missing facts.
- Legacy persisted artifacts retain their documented compatibility behavior.
- Full `tests/anki` and Ruff pass.

### Lecture 101 content acceptance

- XLP has at least one adequate recognition card and no unsupported deep expansion.
- Jeffrey Modell warning signs are represented compactly and completely enough for the lecture's medium emphasis.
- IgA-safe transfusion products are directly tested by a card.
- CD40 on B cells is directly represented or an existing card is proven to contain it.
- CVID diagnosis of exclusion is directly represented.
- XLA versus THI vaccine-response distinction remains represented.
- SCID, DiGeorge, XLA, CVID, IgA deficiency, Hyper-IgM, THI, WAS, ataxia-telangiectasia, and XLP each have an auditable marker summary.
- The three weak MAYBE cards are not selected automatically.
- No custom card duplicates an adequate selected AnKing card at the same fact identity.
- Meta/control records are visible in diagnostics if useful but are not counted as unresolved medical facts.
- Any conflicting or unsupported AnKing Extra content is visible to the reviewer.

### Operational acceptance

- The run reuses the pinned companion/Voyage generations without re-embedding.
- Staging remains isolated from production job data and Anki apply state.
- Production 8765 remains online unless separate downtime approval is granted.
- Nothing is applied to Anki during comparison acceptance.

## 11. Likely implementation surface

The independent reviewer should inspect these areas rather than relying only on this document:

- `src/oms_hub/anki/card_centric_contracts.py`
  - `CardConcept`, `CardConceptLedger`, `CardClassification`, fact IDs;
- `src/oms_hub/anki/card_centric.py`
  - ledger/classification helpers, coverage, and `select_high_yield_v2`;
- `src/oms_hub/anki/stages.py`
  - ledger, classification, coverage merge, gap fill, dedupe, and selection stages;
- `src/oms_hub/anki/prompt_assets/card-centric-ledger-v2.md`;
- `src/oms_hub/anki/prompt_assets/card-centric-classifier.md`;
- `src/oms_hub/anki/prompt_assets/card-centric-gap-v2.md`;
- `tests/anki/test_card_centric.py`;
- `tests/anki/test_stages.py`;
- `tests/anki/test_v2_lifecycle_full.py`;
- `tests/anki/test_p3_selection.py` and related reconciliation tests.

The plan should reuse the existing fact-ID, coverage, correction, and unresolved structures wherever possible rather than adding a parallel pipeline.

## 12. Questions for the independent reviewer

1. Is the root-cause analysis supported by the evidence, or is a different shared failure more likely?
2. Does separating lecture grounding from card-content entailment close the false-coverage cases without rejecting useful AnKing cards unnecessarily?
3. Is the proposed depth/control separation the smallest durable fix for C11–C13?
4. Is a disease-marker checklist sufficiently general for other dense comparison lectures, or is it overfit/too complex?
5. Which proposed gaps are genuinely required by this lecture's emphasis, and which would over-expand the deck?
6. Should unsupported/conflicting Extra content block selection, merely flag review, or be handled field-by-field?
7. Is removing automatic MAYBE selection correct, or should any narrow exception remain?
8. Are the tests and acceptance criteria strong enough to prevent recurrence?
9. Is there a simpler design that achieves the same auditability and safety?
10. Verdict: `APPROVE` or `REQUEST_CHANGES`. Any requested change should identify the exact risk, affected phase, and required correction.

## 13. Implementation hold

Do not implement, push, deploy, restart production, or apply any Anki changes as part of this review. Implementation may begin only after the user returns an independent approval or explicitly accepts requested plan corrections.
