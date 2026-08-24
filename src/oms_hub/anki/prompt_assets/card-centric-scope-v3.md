---
id: card-centric-scope-v3
version: "3.3"
temperature: 0
max_tokens: 4096
response_format: json
schema: scope_v3
---

Use `scope_instruction` from the frozen course policy and apply its authority/mode rules.
`colored_text` is explicit formatting evidence; `transcript` is primary lecturer evidence;
`outline` is derived/index evidence and never professor emphasis. Use only allowed evidence
IDs, and ground every concept and fact in them. Reconcile conflicts into one supported
statement or omit the claim. Each fact must be one independently testable assertion. Never
join separately testable claims merely to fit the fact limit; omit the lower-priority claim
instead. Write every fact as a complete sentence of at most 160 characters ending in `.`, `?`,
or `!`; compress phrasing or omit lower-priority detail instead of truncating. A causal
mechanism may remain one fact only when every step is necessary to test that single mechanism.
Return JSON only: semantic concepts and facts, never IDs, hashes,
evidence rows, policy fields, or provider/model information. Set `generation_allowed` true
unless the frozen policy's `generation_style_profile` is `disabled`.
