# O — selective lecture images, 2026-09-14

## Scope and implementation

Connor approved inventory-first, question-planning-first generation, retrieving actual
lecture figures before finalizing visual questions. Candidate branch:
`codex/hub-intake-ux-2026-09-14`, isolated worktree `oms-hub-intake-ux`.

`03b59016f22efcc4028ba057a334f9d6f78b7c5e` adds compact complete eligible text/style
metadata, a source-qualified image inventory, a durable text-only planner, selected
original-image generation, plan/count/objective validation and exact cache binding.
New requests use `gpt-lecture-v2`; historical v1 manifests retain their previous path.
The original full manifest, hashes, evidence and source restrictions remain intact.

`46695f51897dd7e3ec9a7914310758bccb9b0610` fixes uneven batching across the entire
plan. Every batch has 3–25 questions, at most 20 actual images and 100,000 serialized
source characters. Exact order/coverage remains. Sparse/image-only pages have mandatory
real-image previews; the planner receives no pixels. Images are locally extracted and
integrity checked during intake; this is selective provider transfer, not zero local reads.
No replacement images are generated.

Independent review found and resolved explicit count/range checks, count qualifiers,
PDF-page OCR fallback, and valid uneven/cross-group batch partitions. Local acceptance:
212 passed, one opt-in browser skip; Ruff and mypy passed. The final global packing
patch passed seven affected checks, including three cap/partition regressions, and
independent review. No Windows or deployment acceptance is implied.

## Private Lecture 26 acceptance

Private evidence root (never committed):
`~/Library/Application Support/OMSStudyHub/myeloid-quiz-trial-20260914/pipeline-v2`.
Original materials remain untouched; their hashes and the actual 47-page native
PowerPoint PDF are in the [retained first trial](O-myeloid-quiz-trial-20260914.md).
The isolated app uses that hash-verified manually prepared native PDF at the converter
boundary and rasterizes it with the production renderer. This does not establish
automatic native macOS Office conversion. Parsers, subscription cleanup, planner,
generator, review/publication and exports use production code.

Actual transcript cleanup completed through the managed Codex subscription session.
The first text-only plan at `03b59016` received 87,665 characters before its manifest
hash, 64 image inventory entries and zero images. Eight sparse pages were marked for
mandatory preview. Requested count was exactly 15, with four faculty objectives.

First run `fff48e00-7ed1-4476-84ec-bcf1e4d9bc03` returned 15 plans and seven selected
images, but failed validation: two figures on slides 22/23 cited only adjacent
contextual slides. Raw output, lifecycle/provider journals and invalid result remain
retained. No final-generation call occurred for that run. The correction supplies
an explicit nonblank same-page citation anchor and strengthens planner guidance;
it does not relax validation or reinterpret the rejected output as success.

The second run `8e0ccf5e-0fc0-48c5-8c92-cfae3c7a930a` failed on extra, legitimate
selected-image placeholder citations with empty text. `23149573bf41326e1698639e7d8b32d363d0f174`
corrected that validator: all explicitly selected images are inspected, regardless
of whether the sparse-page heuristic also marks them as mandatory previews.

Run `114fd0dd-fc17-479b-882d-e35c7a6eefa7` initially returned a 15-question plan with
8 selected images but omitted two page citations. `0cc7b6bdd64b82fed41d68184ab011d437a6dc6c`
adds only existing inventory-to-source citation links, records exact additions and
before/after hashes, and allows explicit recovery of a checksum-bound completed
plan without a repeated provider call. Independently verified recovery added q6
→ block-159 and q8 → block-191, preserving all topics, objectives and image choices.
The original invalid journal and raw bytes remain. Recovery additionally requires
the matching completed lifecycle from a checksum-verified DB artifact and an
exclusive recovery record. Unknown or ambiguous references remain rejected.

Final generation used that recovered plan, 16 actual images (8 selected + 8 mandatory
previews), and completed with 15 questions, five choices/explanations each, and
8 attached original figures. All four faculty objectives are covered (question
membership counts 2 / 3 / 8 / 5). Provider-returned model provenance is still
unverified; requested model was the accepted subscription GPT-5.5. Across the
private trial there were one transcript-cleaning call, three planning calls and
one final-generation call; recovery made no planning call.

Independent source/content review found all keyed answers supported. Completed
review edits: balance the raw all-A choices; correct q4's ≥20% explanation using
slide 15 and explicitly identify conflicting source wording; replace q1's lecture
organization trivia; remove q14's answer-reciting stem. The labeled FISH figure is
appropriate for q7's treatment-mechanism question. Final independent content review passed all 15 keyed answers, all 75 choice
explanations, eight original figures and four objectives after the last q4
attribution correction. Exactly three correct answers occupy each A–E position.
These are review edits, not a
claim that unreviewed provider output was study-ready or automatically balanced.

The real review UI also exposed repeated whole-deck image normalization for each
question. The performance correction is committed at
`db472f54366782580a58f23c288683e4a44b7831`: reuse validated asset indices and cache
normalization only within one review validation. Each lookup still verifies current
path, hash and size; the request cache resets in `finally`. Public standalone asset
validation is unchanged. Actual direct validation fell from 22.142 to 2.630 seconds,
and normalization calls from 576 to 64. Fresh and same-request mutation checks pass.
The final affected suite passed **114 tests, 1 opt-in skip**, with Ruff/mypy passing;
independent performance review passed 98 checks without actionable findings. The original lecture/outline/transcript hashes were rechecked unchanged.

Final implementation/recovery regression: **241 passed, 1 skipped**, Ruff and mypy
passed; independent normalization/recovery review passed 182 relevant tests.

## Final local publication and exports

Reviewed code: `db472f54366782580a58f23c288683e4a44b7831`. Final content PASS by `/root/selective_review`;
all 15 answer verifications saved through production review routes and zero blockers
before publication. [Open the 15-question lecture quiz](http://127.0.0.1:55496/public/quizzes/458eac28979a70f5fd03d8a8f1a9f08a56061a864a57d337aff51c33d0389f8b).
The isolated loopback preview runs this exact source with background workers disabled;
new jobs need an explicit test worker tick. This is not deployment of the live Hub.

All 15 correct choices graded correctly. The public pre-answer payload omits answer
keys/rationales, and all eight original image URLs load. JSON and ZIP payloads agree;
the ZIP contains exactly eight figures and passes integrity validation. Private and
exam PDFs each have 26 pages; the published quiz PDF has 22. All three retain all 15
stems, 75 choices, complete rationales and eight figures. Published PDF pages 2, 10,
11 and 22 were visually inspected with no observed clipping. Actual in-app browser
checks graded questions 1/2 correctly and displayed the original flow-cytometry
figure on question 3. The pass tracker remains immediately below the lecture panels.

Private receipts: `acceptance-final.json`, `content-review-approved.json`,
`review-verified.json`, `grading-check.json`, `export-check.json`,
`pdf-structure-check.json` and `publish.log` under the evidence root above.
Acceptance receipt SHA-256: `21d5eafaf6e8796fd93739e2dee8d6709ebddeaa12f04007f3bb25092f7d691a`.
The raw provider outputs, rejected plans and manual edit receipts are retained.
No further provider call was made for review, recovery, grading or export checks.

## Limits and next acceptance

The 20-image and 100,000-character ceilings remain real limits. Mandatory previews
are conservatively repeated in each final batch; more than 20 required previews or
full compact evidence that cannot fit still pauses generation. Entirely image-only
PDFs without source text remain unsupported; mixed PDFs with actual page images can
recover the exact OCR-unavailable warning. Automatic source-section coverage and
arbitrary larger lectures are not established by a trial with four faculty objectives.

Windows production launcher/account/provider acceptance, packaging and approved live
cutover remain separate. AMBOSS is deferred; vendor exports remain optional pending.
No live Hub, original lecture, Anki schedule, Google/Gate 2B resource, paid fallback,
reset credit or purchase was changed.
