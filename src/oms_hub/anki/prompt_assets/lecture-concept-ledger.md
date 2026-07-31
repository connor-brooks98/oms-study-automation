---
id: lecture-concept-ledger
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 16000
response_format: json
schema: lcl_v2
includes:
  - _shared/source-authority.md
  - _shared/output-contract.md
cache_prefix: true
---

# Lecture Concept Ledger

Build the concept ledger for this lecture from the supplied passages. Every
downstream stage retrieves, judges, and generates against this ledger — a
concept you omit here is a concept that never gets a card.

## Inputs

- `lecture_title`
- `passages[]` — slide, transcript, and summary passages with source-prefixed IDs

## Coverage requirement

**Every slide and transcript passage must be cited by at least one concept.**
Before returning, verify this and add concepts for any uncited passage. Report
any passage you deliberately left uncited in `intentionally_uncited[]` with a
reason — acceptable reasons are title slides, reference/citation lists,
objectives slides, and image-only slides with no accompanying claim.

Summary passages need not be cited individually; they are an index into the
other two.

## Granularity

**Split compound facts into separate concepts.** A disease covered across
several slides is not one concept. Genetics, mechanism, morphology, diagnostic
testing, and management are separate retrievable targets and each needs its own
paraphrases. If you write a concept whose canonical statement contains more than
one testable assertion, split it.

Expect roughly one concept per content slide. A 48-slide lecture producing 19
concepts is under-extracted; the same lecture producing 35–45 is correct.

Failure mode to avoid: "Hereditary spherocytosis pathogenesis and diagnosis" as
a single concept. That collapses the causative genes, the surface-to-volume
mechanism, the splenic trapping, ↑MCHC/↑RDW, EMA binding, osmotic fragility, and
the negative DAT into one retrieval target, and the specific facts never get
searched for.

## Depth and emphasis

Read these directly from the summary's `DEPTH MAP` and
`PROFESSOR EMPHASIS FLAGS` sections.

`depth`: `deep` | `medium` | `surface` — from the DEPTH MAP. If the summary does
not classify a concept, infer from slide count and transcript time.

`emphasis_flag`: true when any of the following holds —
- the concept appears under PROFESSOR EMPHASIS FLAGS
- the transcript contains an explicit exam signal ("hot topic", "keep this in
  mind", "the lecturer expects you to know this", "remember this")
- the summary marks it "Repeated 3+ Times"

`importance`: `high` when `depth == deep` OR `emphasis_flag == true`.
`medium` when `depth == medium`. Otherwise `low`.

Every DEEP item and every EMPHASIS item in the summary must map to at least one
concept. This is checked downstream.

## Paraphrases

Emit **3 to 6** search paraphrases per concept. Emit toward the upper end for
`importance: high`.

- **Every paraphrase must contain the concept's primary entity verbatim.** No
  exceptions. A paraphrase that drops the disease name retrieves cards about
  other diseases that share the attribute.
- Vary the phrasing of the *fact*, not the subject.
- Include at least one paraphrase using the lecture's own wording and one using
  standard board terminology.
- Do not write a paraphrase that would be a correct search for a different
  disease. Test each one: if "X-linked recessive enzyme deficiency" would
  retrieve hemophilia, it is a bad paraphrase for G6PD.

## Aliases

Emit `aliases[]` per concept: synonyms, abbreviations, eponyms, and the
morphologic or lab findings uniquely associated with it. These feed a
deterministic entity gate downstream, so include terms that identify the concept
without naming it — "Heinz bodies" and "bite cells" for G6PD, "spherocytes" and
"EMA binding" for HS.

## Per concept

- `concept_id` — stable, `C01`-style
- `canonical_statement` — one testable assertion
- `hypothetical_card` — what an ideal card for this would look like
- `primary_entity` — the disease, drug, structure, or process
- `aliases[]`
- `paraphrases[]` — 3 to 6
- `depth`, `emphasis_flag`, `importance`
- `passage_ids[]` — every supporting passage

## Constraints

Use only the provided passages. Do not add medical facts the passages do not
support, and do not fill in board knowledge the lecturer did not cover.

Also emit `lecture_entity_count` — the number of distinct disease or organism
entities the lecture teaches. Downstream context-trap logic depends on it.
