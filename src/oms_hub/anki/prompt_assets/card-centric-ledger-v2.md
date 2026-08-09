---
id: card-centric-ledger-v2
version: 2.0.1
model: claude-sonnet-4-6
temperature: 0
max_tokens: 7000
response_format: json
schema: lcl_v2
---

# Card-centric v2 coverage checklist

## Quality-first deck policy

Optimize for the smallest set of the best-supported, highest-yield,
nonredundant cards. Treat 60 as a warning floor, 65 as the ordinary target,
and 70 as a soft cap; these are never quotas and the ledger must never pad
coverage merely to reach a count. Do not omit a unique, grounded, high-value
fact solely because the ordinary target has been reached.

Use only supplied lecture passages. Return the v2 ledger contract with stable
sequential concept IDs. Each concept MUST include `suggested_fact_count` (1-5),
exactly that many nonblank `fact_descriptions`, one optional forbidden-target
array per fact in `forbidden_cloze_targets_by_fact`, and `is_mechanism`.
`canonical_statement` remains the single canonical concept statement. Preserve
the legacy top-level `forbidden_cloze_targets` for compatibility. Never invent
source IDs or material absent from the lecture.
