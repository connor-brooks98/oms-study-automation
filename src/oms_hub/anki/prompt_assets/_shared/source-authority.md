---
id: source-authority
version: 2.0.0
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
source. Useful as a concept index, depth map, and emphasis signal. It may
hallucinate, over-generalize, or collapse distinct facts.

Rules that follow:

- A card may not be kept on summary support alone. The summary corroborates; it
  does not carry.
- A generated card may not cite a summary passage as its sole evidence.
- Where the summary's bracketed citations resolve to slide or transcript
  passages, treat the underlying passage as the authority, not the summary text.

Passage IDs are source-prefixed: `SLD:` slides, `TRX:` transcript, `SUM:`
summary. Read authority off the prefix.
