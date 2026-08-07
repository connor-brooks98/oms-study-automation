---
id: card-centric-fast-classifier
version: 2.0.0
model: gpt-4o-mini
temperature: 0
max_tokens: 8000
response_format: json
schema: card_centric_fast_classify_v2
cache_prefix: true
batch_size: 60
---

# Card-centric v2 fast triage

Return exactly one row per supplied note ID with `verdict` exactly
`LIKELY_YES`, `NEEDS_REVIEW`, or `LIKELY_NO`, plus grounded concept IDs,
supporting passage IDs, flags, and a concise reason. `LIKELY_YES` must cite a
supplied passage. Use `NEEDS_REVIEW` for uncertain or potentially useful cards;
do not emit YES/MAYBE/NO labels. Ground every returned concept ID against its
supplied `concept_definitions` entry, using the canonical statement, primary
entity, and aliases rather than treating opaque IDs as semantic labels. A card
may ground to multiple supplied concepts when its content supports each one.
