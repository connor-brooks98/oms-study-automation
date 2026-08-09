---
id: source-authority-card-centric-v2
version: 1.0.0
shared: true
---

## Source authority

The three source materials are not equal authorities.

**Transcript** — highest authority. It is the only source carrying emphasis,
professor flags, and time spent on a topic. What the lecturer said is what is
testable.

**Slides** — authoritative for what was presented. May include reference
material, citation lists, and background slides the lecturer skipped or
explicitly deprioritized.

**Derived summary (NotebookLM outline)** — a generated artifact, not a primary
source. Useful as a concept index and depth map, but never an emphasis signal.
It may hallucinate, over-generalize, or collapse distinct facts.

Rules that follow:

- Prefer supplied slide or transcript evidence whenever it supports the fact.
- Summary-only support is allowed when it is the best available supplied
  evidence. Cite the honest `SUM:` passage and label the card
  `summary_grounded` downstream; never upgrade summary support to primary
  authority or inherit emphasis from it.
- Where the summary's bracketed citations resolve to supplied slide or
  transcript passages, prefer and cite the underlying primary passage rather
  than the summary text.

Passage IDs are source-prefixed: `SLD:` slides, `TRX:` transcript, `SUM:`
summary. Read authority off the prefix.
