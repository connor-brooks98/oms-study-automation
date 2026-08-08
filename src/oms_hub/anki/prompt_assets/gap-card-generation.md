---
id: gap-card-generation
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0.2
max_tokens: 8000
response_format: json
schema: gap_cards_v2
includes:
  - _shared/source-authority.md
  - _shared/card-house-rules.md
  - _shared/output-contract.md
---

# Gap Card Generation

Write source-grounded Anki cloze cards for the facts a lecture's existing cards
do not cover.

## Inputs

- `concept` — canonical statement, primary entity, importance, depth
- `missing_facts[]` — each with `fact_id`, `statement`, `passage_ids`
- `evidence_passages[]` — full passage text for the concept's slide and
  transcript range, not only the cited IDs
- `lecture_title`, `lecture_entity_count`, `forbidden_cloze_targets_by_fact[]` —
  each row names exactly one `fact_id`; never apply another fact's targets
- `existing_supports[]` — the kept cards already covering this concept, so you
  do not duplicate them

## Task

Generate cards for every entry in `missing_facts[]`. Optimize for the smallest
set of the best-supported, highest-yield, nonredundant cards. Counts are soft
targets, never quotas: do not invent facts, add a weak card, or split a fact to
pad a count. Prefer fewer excellent grounded cards over marginal cards.

**Splitting is correct behavior, not a rule violation.** If a single missing
fact cannot be tested atomically in one card, produce two or more and set
`"split": true` with deterministic `"split_index"` values 1, 2, ..., N. Do
not split merely to increase card count. A fact naming three management steps
is three cards, not one enumeration card.

**Never omit a fact silently.** If the evidence does not support a card for a
given fact, emit `{"status": "unresolved", "fact_id": ..., "reason": ...}` for
it. Do not generate from general medical knowledge to fill a hole the sources
do not fill — an unresolved record is the correct output and is routed for
manual review.

Check each generated card against `existing_supports[]` before returning. If it
duplicates a card already covering this concept, emit it as `unresolved` with
reason `duplicate_of_existing` and the note ID.

## Output

Return one strict JSON object with a `resolutions` array. Every `fact_id` in
`missing_facts[]` must have exactly one terminal outcome: one unsplit generated
card with no `split_index`, two or more generated split cards indexed 1..N, or
one unresolved record. A `fact_id` may never appear in both a generated and
unresolved record. When choosing cloze content, use only that fact's matching
`forbidden_cloze_targets_by_fact` row; never treat targets for other facts as
prohibited.

```json
{
  "resolutions": [{
    "fact_id": "C07-M1",
    "status": "generated",
    "text": "<b>Hereditary spherocytosis</b> shows a(n) {{c1::<b>increased</b>}} MCHC on <u>CBC</u>",
    "extra": "Membrane loss reduces surface area while hemoglobin content is unchanged.",
    "note_type": "AnKingOverhaul (AnKing Step Deck / AnKingMed)",
    "source_passage_ids": ["SLD:07:0031", "TRX:07:0198"],
    "split": false,
    "split_index": null,
    "image_needed": null
  }]
}
```

Tags are attached by the pipeline, not by you. Do not emit a tags field.
