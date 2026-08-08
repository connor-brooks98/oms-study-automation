---
id: card-centric-classifier-v1
version: 1.0.0
temperature: 0
max_tokens: 8000
response_format: json
schema: card_centric_classify_v1
cache_prefix: true
batch_size: 40
---

# Card-Centric Classifier

Classify each supplied card against the cached lecture source passages only.
Return exactly one result per supplied `note_id` and never invent a note,
concept, or passage ID. Supporting passage IDs are the human-readable IDs in
the cached prefix (`SLD:...`, `TRX:...`, or `SUM:...`). `YES` requires one or
more supporting passage IDs. Use `MAYBE` for adjacent but uncertain cards; use
`NO` for out-of-lecture cards. Every result needs a nonblank one-line reason.

Record flags only from `wrong`, `outdated`, `ambiguous`, `non_atomic`,
`poor_cloze`, `context_trap`, `enumeration`, `stat_cloze`, and `over_cloze`.

The cached source prefix is authoritative: summary passages aid orientation but
do not by themselves make a card eligible to suppress later residual/gap work.
