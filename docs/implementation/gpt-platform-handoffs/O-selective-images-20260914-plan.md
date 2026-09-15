# Selective lecture image generation — implementation ledger

Approved behavior: inventory lecture images; plan questions from complete text and
inventory; load selected real images; finalize questions after visual inspection.
Retain private immutable evidence, required review and existing subscription/runtime
limits. No generated replacement pictures, paid fallback or live Hub changes.

Worktree: oms-hub-intake-ux; starting candidate c636459a.

1. Compact prompt evidence and image inventory (worker selective_evidence).
2. Durable text-only question planning and source-qualified image selection
   (worker selective_plan).
3. O integration: reuse existing final-generation journals with selected images,
   enforce plan identity/coverage and source/image restrictions; preserve legacy
   manifests and cached generations.
4. Focused regression tests, independent review, then the authorized Lecture 26
   15-question subscription trial. Review actual figures and answers before local
   publication. Record failed attempts and operational limits separately.

Ruling: source assets remain locally integrity-checked and cached by existing
parsers. The inventory stage sends no image pixels to GPT; final requests load
selected original assets. Full-slide images remain valid candidates for vector
content. This does not promise zero local file reads.

Ruling: compact only prompt metadata; retain every source text segment, source
identity, source locator and teaching emphasis, with the complete audit metadata
unchanged in the source manifest. Do not raise 20-image/100,000-character ceilings.

Status: complete for the authorized selective-image implementation and bounded Mac
Lecture 26 trial. Independent code/content reviews, real subscription generation,
local publication, grading and exports accepted. Exact handoff:
[O-selective-images-20260914.md](O-selective-images-20260914.md).
Windows production acceptance and live release remain separate.
