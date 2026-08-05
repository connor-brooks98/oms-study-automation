---
id: card-centric-classifier
version: 1.0.0
model: claude-haiku
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
concept, or passage ID. `YES` requires supporting passage IDs. Use `MAYBE` for
adjacent but uncertain cards; use `NO` for out-of-lecture cards. Record flags
only from `wrong`, `outdated`, `ambiguous`, `non_atomic`, and `poor_cloze`.

The cached source prefix is authoritative: summary passages aid orientation but
do not by themselves make a card eligible to suppress later residual/gap work.
