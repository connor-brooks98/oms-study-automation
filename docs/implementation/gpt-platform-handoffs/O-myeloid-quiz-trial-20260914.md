# O — Pass tracker and real-lecture trial, 2026-09-14

UI source commit: `8d0e8a5bbf5e2e25927c32da96da634eaa1dacf4` on `codex/hub-intake-ux-2026-09-14`.

The pass tracker now follows the lecture-material/transcript/quiz workspace,
before material-processing status and the processing checklist. Its behavior and
saved passes are unchanged. The CSS asset version was advanced. Focused UI suites:
35 passed in 3.04 seconds. Browser inspection at http://127.0.0.1:60431/lectures/1
confirmed the actual layout. This preview remains isolated, workers disabled.

## Requested real-lecture test: blocked before GPT

Connor authorized a 12–15 question mock generation using Lecture 26, Pathology of
Myeloid Neoplasms I, Heme/Lymph Exam 3. Target selected: 15 questions.
No question was generated and no provider call or transcript cleanup was dispatched.

The original PowerPoint, transcript and retained outline hashes are unchanged.
Only PowerPoint/transcript copies were prepared for the test; the outline was not
used to generate anything. Actual production embedded extraction yielded:

- 47 slides, 17 embedded images and 323 parsed segments.
- 563 formatting runs; their serialized sidecar alone is 276,664 characters.
- Visual inspection of all 17 extracted images confirmed intact microscopy,
  flow-cytometry, FISH and tabular/diagram assets with source slide locators.
- Production OCR is unavailable on this Mac and reports blockers on slides 4/11.
  A separate native Apple Vision preparation recovered text for those images.
  This is diagnostic preparation, not an implemented production OCR fix.
- An actual local PowerPoint PDF export produced 47 pages, then the real
  PresentationRenderer rasterizer produced 47 numbered slide images. The export
  used the local printing option; this is not automated Mac converter acceptance.

The existing production `_batch_sources` limit check returned `context_limit`:

| Prospective evidence | Images | Serialized characters |
| --- | ---: | ---: |
| Full source plus all slide renders | 64 | 431,820 |
| Embedded images, without full-slide renders | 17 | 419,393 |

Limits remain 20 images and 100,000 characters per batch. These prospective sizing
checks explicitly used unapproved source bindings and the raw transcript; they did
not manufacture accepted revisions, clean-transcript provenance or queued jobs.
An independent read-only check removed the entire transcript and used one objective:
17 images / 378,721 characters still failed. Thus transcript cleanup cannot resolve
this blocker. A 15-question instruction does not alter source size; objective
batching currently repeats all evidence. Restricting slides would change coverage.

## Next implementation work

Compact redundant prompt metadata while retaining complete immutable provenance;
implement deterministic source/image batches with explicit coverage reconciliation
for whole lectures. Preserve the current ceilings and stop on unsupported coverage.
Resolve the Mac OCR/conversion gap through a supported production path. Then rerun
this exact authorized lecture trial, targeting 12–15 questions, and independently
review coverage, answer correctness, image identity and answer-revealing labels.

This is a failed real-lecture preflight, not provider, Windows or deployed acceptance.
Earlier synthetic acceptance remains historical evidence. Live Hub/Anki/retained
resources were untouched; no credentials, paid fallback or reset credit were used.

## Local private evidence

Root: `/Users/connor/Library/Application Support/OMSStudyHub/myeloid-quiz-trial-20260914`.
`preflight-result.json` SHA256: `bf18dae7e82d364d1fb2576d05c4286324415d2f5a749ab312c20b572b96451b`.
Includes original hashes, native PDF hash, precise limits and zero dispatch count.
`parsed-slides.json`, `parsed-slides-with-ocr.json`, `run-styles.json`, `ocr.json`,
`slide-renders/`, and `extracted-images-1.jpg` through `extracted-images-3.jpg`
remain local; private lecture evidence is not included in Git.
