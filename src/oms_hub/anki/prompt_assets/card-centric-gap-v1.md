---
id: card-centric-gap-v1
version: 1.0.0
model: claude-sonnet-4-6
temperature: 0.2
max_tokens: 8000
response_format: json
schema: card_centric_gap_v1
includes:
  - _shared/source-authority.md
  - _shared/card-house-rules.md
---

# Card-centric grounded gap cards

For the single supplied concept and its missing facts, return exactly a
`resolutions` array. Each requested fact must resolve as one or more generated
cards (split cards set `split: true`) or exactly one explicit unresolved record.
Use only the supplied slide/transcript evidence passages. Never use a summary
as the only evidence and never hide a `forbidden_cloze_targets` value.

```json
{"resolutions":[{
  "fact_id":"C01-M1",
  "status":"generated",
  "text":"A focused {{c1::answer}} cloze",
  "extra":"source-grounded explanation",
  "note_type":"AnKingOverhaul (AnKing Step Deck / AnKingMed)",
  "source_passage_ids":["SLD:...:P:..."],
  "split":false,
  "reason":""
}]}
```

For unresolved output use only `fact_id`, `status: "unresolved"`, and a
nonblank `reason`. No tags, aliases, paraphrases, or uncited claims.
