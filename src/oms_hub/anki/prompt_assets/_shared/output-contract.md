---
id: output-contract
version: 2.0.0
shared: true
---

## Output contract

Return strict JSON matching the schema named in this prompt's frontmatter.

- No prose, no preamble, no trailing commentary, no markdown code fences.
- Every input item must appear in the output exactly once. Do not silently drop
  items you are unsure about — use the designated uncertain or unresolved
  status.
- Never emit an ID that was not supplied to you.
- Never emit the same ID twice within one list or across mutually exclusive
  lists.
- If you cannot complete an item, emit it with an explicit status and reason
  rather than omitting it. Silent omission is the failure this contract exists
  to prevent.
