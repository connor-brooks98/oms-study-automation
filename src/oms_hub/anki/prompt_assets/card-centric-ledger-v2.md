---
id: card-centric-ledger-v2
version: 2.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 7000
response_format: json
schema: lcl_v2
---

# Card-centric v2 coverage checklist

Use only supplied lecture passages. Return the v2 ledger contract with stable
sequential concept IDs. Each concept MUST include `suggested_fact_count` (1-5),
exactly that many nonblank `fact_descriptions`, one optional forbidden-target
array per fact in `forbidden_cloze_targets_by_fact`, and `is_mechanism`.
`canonical_statement` remains the single canonical concept statement. Preserve
the legacy top-level `forbidden_cloze_targets` for compatibility. Never invent
source IDs or material absent from the lecture.
