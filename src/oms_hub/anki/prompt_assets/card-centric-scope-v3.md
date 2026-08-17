---
id: card-centric-scope-v3
version: "3.0"
temperature: 0
max_tokens: 4096
response_format: json
schema: scope_v3
---

Use `scope_instruction` from the frozen course policy and apply its authority/mode rules.
`colored_text` is explicit formatting evidence; `transcript` is primary lecturer evidence;
`outline` is derived/index evidence and never professor emphasis. Use only allowed evidence
IDs, and ground every concept and fact in them. Reconcile conflicts into one supported
statement or omit the claim. Return JSON only: semantic concepts and facts, never IDs,
hashes, evidence rows, policy fields, or provider/model information.
