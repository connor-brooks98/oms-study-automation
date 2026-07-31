---
id: coverage-rubric
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 8000
response_format: json
schema: coverage_v2
includes:
  - _shared/output-contract.md
---

# Coverage Rubric

You receive one lecture concept and a set of Anki note candidates. Determine
what the candidates cover and, precisely, what they do not.

Semantic relevance is judged elsewhere by a separate audit stage. Your job is
coverage. Do not attempt to reject candidates for being off-topic — assess
whether the concept's facts are present in the supplied set.

## Inputs

- `concept` — canonical statement, primary entity, passage evidence
- `candidates[]` — note ID, text, extra

## Task

**Step 1 — supporting notes.** List the note IDs that state any part of the
concept. Each ID at most once. Retrieval rank is not evidence of support; a
note supports the concept only if its content states one of the concept's facts.

**Step 2 — missing facts.** List every fact in the concept that no supporting
note states.

This is the output that matters. Each entry must be:

- **atomic** — one testable assertion, directly convertible to a single card
- **specific** — name the fact, not the category. "does not cover the diagnostic
  workup" is unusable. "does not state that HS shows increased MCHC" is usable.
- **grounded** — traceable to a supplied passage

Return `[]` only when nothing is missing. A concept whose candidates cover the
general shape but omit the specific numbers, names, or thresholds the lecture
gave has missing facts, and you should list them.

**Step 3 — rationale.** One or two sentences: what the supporting notes
establish, and what remains uncovered.

## Note on downstream routing

Every entry in `missing_facts[]` becomes a generated card or an explicit
unresolved record. There is no partial-credit state where a listed missing fact
is quietly dropped. List what is genuinely missing and nothing more — but do not
under-report to make the concept look covered.

## Output

```json
{
  "concept_id": "C07",
  "supporting_note_ids": [123, 456],
  "missing_facts": [
    { "fact_id": "C07-M1",
      "statement": "Hereditary spherocytosis shows an increased MCHC",
      "passage_ids": ["SLD:07:0031"] }
  ],
  "rationale": "..."
}
```
