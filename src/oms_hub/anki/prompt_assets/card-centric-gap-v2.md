---
id: card-centric-gap-v2
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0.2
max_tokens: 8000
response_format: json
schema: gap_cards_v2
includes:
  - _shared/source-authority.md
  - _shared/card-house-rules.md
---

# Card-centric v2 grounded gap cards

Return every requested fact ID. A fact has either one unresolved row or one or
more generated rows; multiple generated rows for one fact MUST all set
`split: true`. Honor each fact's supplied forbidden targets. Summary passages
are admissible evidence, but cite only supplied passage IDs. When `is_mechanism`
is true, test the causal chain atomically. Generated cards must be valid Cloze
notes with concise, source-grounded Extra fields.
