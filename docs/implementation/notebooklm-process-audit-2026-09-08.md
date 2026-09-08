# NotebookLM process audit — 2026-09-08

Running record of confirmed findings, fixes, and verification. This audit covers the current local application at base commit `8cbd942c61bf804edff3e93febe95c6b30cf6396`, including the uncommitted reconnect correction. Local code/tests do not establish NUC deployment or live Google acceptance.

## Coverage

- [x] Connection checks, sign-in, error classification, and settings UI
- [x] Lecture uploads, source selection, generation, and outline/quiz delivery
- [x] Studio source upload/reconciliation/deletion and generation
- [x] Imported questions, answer resolution, review, and publishing
- [x] Job recovery/concurrency and redundant paths

## Findings and fixes

| ID | Priority | Finding | Status | Verification |
| --- | --- | --- | --- | --- |
| NLM-001 | P2 | Inconclusive authentication checks were presented as expired login and forced reconnect guidance. | Fixed in preceding change; included in this audit. | Missing storage, malformed CLI output, timeouts, inconclusive checks, settings rendering and generation prerequisite regressions. Final combined run below. |
| NLM-002 | Tradeoff | Fresh lecture generation maintains exam sources, then uploads both files again in a disposable notebook. | Retained deliberately: commit `89892c5e` introduced isolation from exam chat history. Removing it would undo that protection. | Disposable-notebook regression verifies exam chat is untouched, selected source IDs are isolated, and the temporary notebook is deleted. |
| NLM-003 | P1 | Source cleanup could delete unrelated remote uploads that shared a lecture filename. | Fixed: delete only replaced bound IDs and lecture-specific legacy IDs. | Same-title preservation regression failed before fix; gateway/prompt suites: 28 passed. |
| NLM-004 | P2 | Outline prompts permitted a Studio-created acknowledgment instead of the actual outline, which could become the PDF body. | Fixed prompt contract: complete Markdown inline, no separate artifact/status-only output. | Inline contract regression failed before fix; gateway/prompt suites: 28 passed. Provider compliance remains unverified. |
| NLM-005 | P2 | Studio retries requested another model response even when the answer was already saved. | Fixed: persist notebook identity and reuse durable responses; discard malformed responses only for the bounded contract retry. | Publication/attempt-save busy errors and interrupted runs reuse the answer; invalid JSON gets one fresh response. |
| NLM-006 | P1 | Lecture quizzes could publish image-dependent questions without any media attachment/review path. | Fixed: lecture prompts require self-contained text questions; publication rejects required images and uses the bounded contract retry. Studio retains image review. | Repository rejects image-dependent lecture publication without publishing; worker clears the invalid answer for regeneration. |
| NLM-007 | P2 | Studio workers could claim files/text before the payload finished saving; write failures could leave pending rows. | Fixed: claim only prepared file/text payloads and record failed local writes without reviving terminal sources. | Deterministic worker poll during write, write failure, terminal-state preservation, and URL eligibility regressions. |
| NLM-008 | P2 | Network errors mentioning a Google login URL and generic configuration errors were classified as expired sessions. | Fixed: typed failures take precedence; fallback text matching requires explicit authentication failure. | Network/timeouts remain retryable even with login text; configuration is not expiry; observed legacy expiry messages still request login. |
| NLM-009 | P2 | JSON answer indices accepted booleans, numeric strings, and integral floats as integers, silently choosing an answer. | Fixed: strict integer validation at quiz and edit request boundaries. | Boolean, string and float indices fail parser validation or return HTTP 422; real integers remain accepted. |
| NLM-010 | P1 | Editing an imported question's stem preserved its previous AI-answer verification. | Fixed: changing the stem invalidates previous answer verification, as answer/rationale edits already do. | Verified-question edit blocks publication again; metadata-only edits retain verification. |
| NLM-011 | P2 | Interrupted-upload reconciliation treated a single new source ID as attached even while that source was processing or failed. | Fixed: both Studio and import reconciliation require the new source to be ready; preserve its ID to avoid duplicate uploads. | Processing/failed single-source deltas stay unbound; later readiness adopts without a duplicate upload; both callers pass the baseline. |
| NLM-012 | P2 | An import answering several missing questions repeated already successful answers when a later question failed. | Fixed: checkpoint each resolved question using existing artifacts keyed by input hashes. | A later-question failure retries only that question; changed model/input signatures invalidate partial answers; supplied answers and order remain unchanged. |
| NLM-013 | P3 | Obsolete Gemini browser/share adapters and an older NotebookLM adapter duplicated inactive workflows. The Studio upload extension allowlist was also duplicated. | Removed the unused adapters and their adapter-only tests after caller inspection; reused the existing extension validator. Historical database fields remain compatible. | Caller search found only adapter tests. Current gateway, native publication, application routes and import acceptance are included in the final regression. |
| NLM-014 | P2 | Imported-answer resolution validated evidence and uncertainty notes, then discarded them before human review. | Fixed: preserve evidence/uncertainty through artifacts and review data; label it model-provided rather than verified citations; clear it after question edits. | Resolver and artifact roundtrips, old-record defaults, review edits, route payloads and text-safe rendering regressions. |
| NLM-015 | P2 | A local database error while advancing Studio validation/image review escaped the worker, leaving the run running until a Hub restart. | Fixed: extend the existing durable retry boundary; preserve manual image review after optional image auto-binding fails. | Validation, image-review and completion write failures retry from saved chat; auto-binding failure retains unresolved manual review. |

## Process traced

1. **Lecture outline/quiz:** validate current canonical PDF and cleaned-transcript revisions and their hashes; run the connection prerequisite check; queue a durable job; maintain bound exam sources; upload the same two inputs to a disposable notebook; ask using exactly those IDs. Save the answer before local PDF generation or native quiz publication. Quizzes are hosted and graded by Study Hub, not forwarded to a Gemini Gem.
2. **Quiz Builder generation:** save a file/text payload or URL; claim a durable source operation; record the remote baseline before upload; wait for normal upload readiness or reconcile an interrupted result. Generation snapshots selected remote IDs, saves the response, validates JSON, then publishes text-only quizzes or pauses for required-image review. Per-notebook leases serialize Hub mutations.
3. **Practice-question import:** preserve local snapshots; parse and extract with source-qualified references; pair supplied answers first. Only selected supporting/combined sources attach to NotebookLM. Missing answers use NotebookLM first; the configured fallback runs only after explicit no-support, with human verification required. Input hashes keep stage and per-question caches current across retries. Questions, evidence and images are reviewed before transactional publication.
4. **Student delivery:** the public content route serves published questions and attached media without answer keys; grading returns feedback for the submitted question. CSRF checks, versioning, publication ownership, image review and source isolation remain covered by existing regressions.

## Final verification

- Combined Python regression: **573 passed in 155.17 seconds**. Includes all `tests/study_generation` tests plus Quiz Builder routes/acceptance, generation routes, public quiz routes, Notebook settings routes and the Notebook rollout contract.
- JavaScript: **123 passed**, covering settings, Notebook Studio, quiz review/images/preview, public quiz and public library.
- Ruff: passed for generation code, affected route and Python tests.
- Mypy: passed for all 30 current generation/route source files.
- `git diff --check`: passed.
- Reviewed the resulting source/test diff locally, including the pre-existing reconnect correction.
- Final code/test fingerprint: `1b8aafe833ede5e6609b41fec1473d1af4e8f3fefd874f1e350728dfefa93a32`. This hashes sorted changed-path/content-SHA256 pairs (or `deleted`) for 42 code/test paths, including the new error-classification test. The report and unrelated untracked files are excluded; the base commit is recorded above.

## Practical limits

- Google AI cloud API migration remains future work through Sol 1–10; ExtendLM setup is deferred.
- Existing generated PDFs/quizzes and real source data were not rewritten or repaired by this local patch.

- The disposable lecture notebook deliberately costs two extra uploads and processing waits. The live connection prerequisite also adds latency. These safeguards were retained; this patch reduces avoidable retries rather than promising uniformly fast provider responses.
- Upload reconciliation still relies on the recorded remote baseline and Hub mutation lease. External manual changes in NotebookLM are outside that lease; ambiguous deltas require review.
- A crash before a provider response reaches durable storage can still require another request. Saved responses and completed per-question answers are now reused.
- Historical evidence that was already discarded cannot be recovered. Older saved drafts remain readable with empty evidence fields.
- The outline prompt now requires inline content; offline tests verify the contract, not future model compliance or medical accuracy.
- This is a local, uncommitted patch. No NUC/Hub deployment, production restart, Google login, real-source upload/deletion, provider generation, or live speed benchmark was performed.
