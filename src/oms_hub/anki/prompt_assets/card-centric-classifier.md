---
id: card-centric-classifier
version: 2.0.0
temperature: 0
max_tokens: 8000
response_format: json
schema: card_centric_classify_v1
cache_prefix: true
batch_size: 30
---

# Card-Centric Classifier

Optimize for the smallest set of the best-supported, highest-yield,
nonredundant cards. Card counts are soft targets, not quotas. Do not invent
facts, split one fact into unnecessary cards, preserve a weak card, or label a
card eligible merely to reach a count. Prefer fewer excellent, grounded,
nonredundant cards over more marginal cards.

Classify each supplied card against the cached lecture source passages only.
Return exactly one result per supplied `note_id` and never invent a note,
concept, or passage ID. Supporting passage IDs are the human-readable IDs in
the cached prefix (`SLD:...`, `TRX:...`, or `SUM:...`). `YES` requires one or
more supporting passage IDs. Use `MAYBE` for adjacent but uncertain cards; use
`NO` for out-of-lecture cards. Every result needs a nonblank one-line reason.

Record flags only from `wrong`, `outdated`, `ambiguous`, `non_atomic`,
`poor_cloze`, `context_trap`, `enumeration`, `stat_cloze`, and `over_cloze`.

The cached source prefix is authoritative. A supplied summary passage may
support `YES` when it genuinely supports the card; keep its supporting ID so
downstream evidence policy can label it `summary_grounded`. Do not call a
summary passage primary-source evidence.
