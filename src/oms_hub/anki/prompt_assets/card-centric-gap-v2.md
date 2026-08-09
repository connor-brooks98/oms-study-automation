---
id: card-centric-gap-v2
version: 2.0.2
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

## Quality-first instruction

Optimize for the smallest set of the best-supported, highest-yield,
nonredundant cards. Counts are soft targets, never quotas. Do not invent facts,
add weak cards, preserve marginal cards, split one fact into unnecessary cards,
or label a card eligible merely to reach a count. Prefer fewer excellent,
grounded cards over more marginal cards.

## Per-fact request and terminal contract

Return every requested fact ID M1 through MN. For each requested `fact_id`,
return exactly one terminal structure:

- one unresolved row; or
- one unsplit generated row with `split: false` and no `split_index`; or
- multiple generated rows with every row `split: true` and deterministic,
  sequential `split_index` values 1 through N for that fact.

Never return both generated and unresolved rows for the same fact. Do not split
to pad a count; split only when one grounded fact cannot be tested atomically.

Use only the `forbidden_cloze_targets_by_fact` row matching the card's
`fact_id`. Never flatten, union, or globalize targets belonging to unrelated
facts. Prefer supplied primary passages, but summary-only support is allowed
when it is the best available supplied evidence: cite the honest `SUM:` passage
and preserve `summary_grounded` downstream. Never upgrade summary support to
primary authority or inherit emphasis from it. When `is_mechanism` is true,
test the causal chain atomically. Generated cards must be valid Cloze notes with
concise, source-grounded Extra fields.
