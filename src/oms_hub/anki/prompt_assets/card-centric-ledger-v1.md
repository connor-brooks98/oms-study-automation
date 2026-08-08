---
id: card-centric-ledger-v1
version: 1.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 6000
response_format: json
schema: card_centric_ledger_v1
---

# Card-centric coverage checklist

Use only the NotebookLM summary passages in the request to make a coverage
checklist. This output is never a retrieval query set and MUST NOT contain
paraphrases, hypothetical cards, source references, passage IDs, scores, or
any field beyond the JSON contract below.

Return exactly:

```json
{
  "lecture_entity_count": 1,
  "forbidden_cloze_targets": ["disease name"],
  "concepts": [{
    "concept_id": "C01",
    "canonical_statement": "A testable factual statement taught in the lecture.",
    "primary_entity": "The disease, drug, organism, or process being tested",
    "aliases": ["synonym"],
    "depth": "deep",
    "emphasis_flag": true,
    "importance": "high"
  }]
}
```

`concept_id` values must be stable, unique, sequential C01/C02/... identifiers.
`depth` is exactly `deep`, `medium`, or `surface`, copied from the summary depth
map. `emphasis_flag` comes only from professor-emphasis material. `importance`
must be `high` for deep/emphasized concepts, `medium` for medium depth, and
`low` otherwise. `forbidden_cloze_targets` contains entity names that must never
be hidden in generated clozes. Do not infer facts absent from the summary.
