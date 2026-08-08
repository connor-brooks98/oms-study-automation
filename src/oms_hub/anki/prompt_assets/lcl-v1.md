---
id: lcl-v1
version: 1.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 16000
response_format: json
schema: lcl_v1
---

# LCL-V1 — Lecture Concept Ledger

Generate a lecture concept ledger using only the provided passages. Cite one or more `passage_id` values for every concept. Produce a concise canonical statement, a hypothetical Anki card, exactly two distinct search paraphrases, importance, and a unique `concept_id`. Do not add unsupported medical facts.
