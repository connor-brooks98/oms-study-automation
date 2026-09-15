# Clinical lecture quiz defaults — 2026-09-15

Requested behavior: 12 useful questions, predominantly USMLE/NBOME-style clinical vignettes, a few recall items, no lecture-referencing stems, and content chosen using faculty objectives and contextual professor emphasis.

- The normal blank-instructions queue freezes a 12-question request. Custom instructions, including scoped and word-form counts, remain unchanged.
- Both planning and writing prompts request ten vignettes and two recall items at the default count, five plausible choices, source-grounded reasoning, and explanations for every choice. The plan records item type, objective/concept, and supporting emphasis quote in each focus; batches preserve the overall mix.
- Automatic coverage uses one medical learning-goal requirement instead of treating every source page as a separate mandatory objective. All eligible source text/images remain available. Explicit user-supplied objective IDs still require coverage. Automatic objective alignment and item quality require content review; a `source-all` assignment alone does not prove either.
- The planner reads actual faculty objectives and the complete eligible cleaned transcript. “Star this,” “make sure you know this,” and similar emphasis prioritize the associated medical concept, with corrections/negations retained. No keyword-only extractor or extra model round trip was added.
- Live transcript-cleaning instructions already preserve emphasis verbatim. Cardio Lecture 1's cleaned source retains contextual “remember” and “important going forward” passages. No transcript revision was rewritten.
- The lecture's existing **Quiz instructions** box and **Saved teacher instructions** presets remain the customization surface. Its help text now explains the defaults.

Copyable override example:

> Generate 12 questions: 10 clinical vignettes in USMLE/NBOME style and 2 concise recall items. Emphasize mechanisms, learning objectives, and concepts the professor explicitly highlights. Use five plausible choices and explain each choice. Keep stems and choices standalone; never refer to what the lecture or professor says. Ground the answers in the supplied materials and put citations in explanations.

Final code release: `0a2d6f526bd4e896bbd10044b38460f1aad9ad34`.

The first live 12-question draft (`63438a93-38c7-49af-a71b-7dcba731bd3b`) passed structural checks but several clinical stems supplied the tested mechanism, making them recall in patient framing. The refinement requires two relevant findings, a meaningful inference, and avoidance of diagnosis/mechanism giveaways. Its usual 50–100-word guidance explicitly excludes filler. Custom counts and mixes still override defaults.

Automatic reuse now compares both writing and planning prompt hashes. Changing either creates a new draft, while unchanged prompts reuse the matching run; prior settings remain intact. Queue-level regression covers this behavior.

Validation: focused Mac suite **102 passed**. Windows suite **221 passed, 1 optional browser skip**, exit 0, under the limited interactive user. The first Windows run used long temporary paths and failed two file-lifecycle checks; the existing short scratch-directory fixture resolved both. A broader Mac run completed its assertions but exited 139 during interpreter shutdown; it is not counted as passing. Changed-file Ruff and diff checks passed. Independent read-only review found and resolved the scoped-count/default conflict and per-page coverage contradiction.

Deployed code `0cebe0be57a210f2745da5c4f6633c6ae02af41d`, tree `ee9f6d909b8ae1ec9e94b3f53cfee3560c5c8924`, with database/source/task backups, unchanged environment and runtime, 18.103 seconds measured downtime, and all three workers healthy. Receipt: `C:\ProgramData\OMSStudyHub-V2\backups\windows-worker-20260915-8e2a403a5ce0432caa9c40f28f2af774\code-deployment-result-20260915T221428147Z-5d5c6a9e753b49139ae99d14b1005bfa.json`.

The refinement passed **92 focused tests on both Mac and Windows**. Final rollout preserved configuration/runtime, measured 18.232 seconds downtime, and passed all worker readiness checks. Receipt: `C:\ProgramData\OMSStudyHub-V2\backups\windows-worker-20260915-c27b484960584f4ea7a4103ab9a02f26\code-deployment-result-20260915T222510387Z-688d4bd89d1e4f1cb2ab6f9e51c46ec0.json`.

The public lecture page displays the new defaults. Final run `888f7494-d717-4bf2-a508-bb95d6e6d37b` reached **awaiting_review**: 12 questions, five choices and source citations/answer/distractor explanations each, one source image, no lecture-referencing stems or choices, and healthy worker completion. Content inspection confirmed that patient-context application dominates and that the focus records identify faculty objectives and supported emphasis. Item 11 is straight recall; normal S2 splitting is basic mechanism recall in clinical framing. The provider plan labels 11 clinical and 1 recall, so the 10/2 mix is guidance rather than an enforced item-type quota. No exact style-ratio guarantee or medical correctness acceptance is claimed. All answers remain unverified and unpublished; both previous drafts remain intact.

[Validation evidence](evidence/quiz-style-20260915.zip), SHA256 `2394436b245aa5173ea8830b49d37079c5680caf4bda501ca6062e7d246b7469`. The archive excludes credentials, databases, lecture source text, and generated quiz content. Public review: https://studyhub.perch-bird.com/studio/runs/888f7494-d717-4bf2-a508-bb95d6e6d37b/review.
