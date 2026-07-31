---
id: paraphrase-expansion
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0.3
max_tokens: 2000
response_format: json
schema: paraphrase_v2
includes:
  - _shared/output-contract.md
---

# Paraphrase Expansion

A concept has exhausted its search paraphrases but has not converged — each
retrieval pass is still surfacing substantial numbers of previously unseen
cards. Generate additional paraphrases targeting what the existing ones are
missing.

## Inputs

- `concept` — canonical statement, primary entity, aliases
- `used_paraphrases[]` — every query already run for this concept
- `found_card_fronts[]` — a sample of what those queries retrieved
- `missing_facts[]` — if a coverage judgment has already run

## Task

Emit 3 new paraphrases.

Read `found_card_fronts[]` to see what the existing queries are already
reaching, and aim the new ones elsewhere. If `missing_facts[]` is supplied,
target those facts specifically — they are the known holes.

## Rules

- **Every paraphrase must contain the concept's primary entity verbatim**, or an
  alias that uniquely identifies it. This is the rule that prevents attribute
  drift, where "PNH clinical presentation" becomes "episodic dark urine worst in
  the morning" and retrieves cards about cortisol rhythm.
- Vary the phrasing of the fact, not the subject.
- Do not restate an existing paraphrase in different words. New angles only —
  a different aspect of the concept, a different vocabulary register, a
  diagnostic finding instead of a mechanism.
- Do not write a paraphrase that would be a correct search for a different
  disease.

## Output

```json
{
  "concept_id": "C07",
  "paraphrases": ["...", "...", "..."],
  "targeting": "one sentence on what these aim at that prior queries missed"
}
```
