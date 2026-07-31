---
id: judgment-v1
version: 1.0.0
model: claude-sonnet-4-6
temperature: 0
max_tokens: 8000
response_format: json
schema: coverage_v1
---

# Judgment-V1 — Coverage Rubric

Decide whether the supplied Anki candidates fully cover, partially cover, or miss the lecture concept. Use only supplied note IDs and list each supporting note ID at most once. List exact missing facts and give a concise rationale. Do not treat retrieval rank as coverage.
