# O: unified intake and lecture quiz workflow — 2026-09-14

Scope: Connor requested unified slides/transcript uploads with additional file types,
searchable lecture selection, removal of the old lecture study-actions bar and
outline/duplicate quiz-generation controls, one GPT quiz button with existing
course/exam placement, individual and exam-bundle PDF exports, and optional quiz
instructions such as slide ranges or red text. NotebookLM remains an upload-only
reference library. Suggested future features are not implemented by this change.

Working branch: `codex/hub-intake-ux-2026-09-14`, based on
`20a6e5225a11aea683fdb50a59c4a5c111aaa540`. Worktree:
`/Users/connor/Developer/worktrees/oms-hub-intake-ux`.
The original checkout and its dirty files are retained. Reviewed source candidate:
`7a6aab38740c4e4a6cda3d89cc1d8778e4c56734`.

## Implementation and evidence

- `/uploads` is one mixed-file intake page. Per-file material/transcript roles use
  the existing manifest, chunk, duplicate, cancellation and reconciliation paths.
  Role groups have independent atomic manifests; an accepted group is retired from
  selection even after ambiguous response recovery. Remaining files stay visible.
- PPTX, PDF, DOCX, TXT, MD and RTF receive real content validation, matching and
  processing. Original bytes, extension, hash and immutable revisions are retained.
  PDF materials bypass Office. Word/RTF/text reading PDFs are explicitly reflowed;
  original page/block/slide locators remain authoritative. PowerPoint rendering
  still uses the configured Office converter. Legacy DOC/PPT require conversion
  before intake. Image-only PDFs can be filed; transcript cleanup requires readable
  text and enters review rather than making an empty provider request.
- Shared course/exam/search selection replaces quarantine and chat full-catalog
  lists. Results are paginated at 20; selections survive filtering and paging.
  Chat keeps exact revision IDs. Existing cascading Anki and Quiz Builder controls
  remain. Read-only source extraction supports the new document formats.
- Lecture pages omit the screenshot's study-actions container and retain the
  separate pass tracker/material cards. Existing outlines remain accessible, while
  outline generation and the duplicate bottom quiz form are removed. A single GPT
  quiz button derives coverage from both local material and transcript sources.
  Naming is `Lecture NN - Topic - Quiz`, within the existing label limit. Matching
  active automatic runs are reused; later quizzes receive retained version names
  without deleting earlier publications. Destination remains the lecture course/exam.
- Optional quiz instructions are saved with the immutable generation plan and affect
  reuse/resume identity. A contiguous slide/page range filters actual original
  locators. Red-only selection uses supported resolved PowerPoint RGB text runs;
  it excludes full-slide images and unmapped transcript text. Ordinary guidance is
  supplied to the model alongside schema/grounding rules. Unsupported, inverted,
  mixed or unreadable restrictions fail visibly; PDF color inference is not claimed.
  Scoped requests retain full original provenance while request evidence, coverage
  and accepted citations use the same eligible view. Empty instructions preserve
  compatibility with earlier saved manifests.
- NotebookLM inference entry points and queued legacy generation stop before a
  provider call. Existing standalone upload sagas remain. Successfully filed new
  PDF/cleaned-transcript revisions enqueue append-only uploads keyed by revision
  and hash; disconnected NotebookLM records a separate review status. There is no
  automatic old-catalog backfill or remote-source deletion. Missing imported answers
  remain manually correctable and require verification before publication.
- Individual public PDFs use only active published payload/media. Authenticated
  course/exam/category bundles retain library order, revalidate accepted GPT source
  evidence, and fail as a whole on incompatible or changed members. Public exports
  never include private manifests. Existing legacy provenance is labeled honestly.

## Verification boundaries

The separate Mac UI preview is `http://127.0.0.1:60431`, with its own synthetic
catalog/database/files. Background workers are disabled in this UI-only preview;
no provider session or credentials were copied. Its readiness endpoint deliberately
returns `503 worker_not_started`, with reachable schema-40 database and zero worker
starts; this is not a production-ready running Hub. Browser verification submitted a
mixed material/transcript pair through the real local API; both were retained in
quarantine for assignment. Course/exam/search returned the exact requested lecture.
Mobile checks at 390 pixels showed no horizontal overflow. The simplified lecture
page and course/exam export controls rendered correctly. Actual HTTP PDF responses
were 3 pages for one synthetic publication and 6 pages for the two-quiz exam bundle.

Local parser/pipeline, mock-provider, browser and PDF evidence does not establish
actual Windows conversion/runtime acceptance, new provider generation acceptance,
or successful remote NotebookLM uploading. Earlier runtime/provider receipts remain
bound to their recorded source and runtime. Live NUC deployment/restart, main merge,
private-source provider testing and live Anki writes remain outside this turn.

## Review

Independent reviews found and closed recovered-upload resubmission, non-PPTX chat
extraction and image-locator incompatibility, automatic quiz-label collisions, and
an imported-answer blocker that could not be cleared manually. Scope/auth/hash and
public/private PDF boundaries received a separate passing review. Final scoped
instruction review passed against the frozen files. The review also
closed inverted/mixed scope interpretation and red-run citation lookup during review.
The exact requested phrase “only generate a quiz for the info in red” is covered.

## Completion receipt

- Integrated local Python regression: **337 passed, 1 skipped**, 93.34 seconds,
  exit 0. The skip is the opt-in real browser case; this turn separately performed
  direct browser UI checks. Selected all 18 changed Python test files plus
  `tests/v2/test_gpt_lecture_acceptance.py`.
- Frontend regression: **67 passed**, exit 0: GPT, uploads, lecture picker, study
  chat and NotebookLM Studio Node checks.
- Ruff across changed Python files passed; strict mypy passed for all 29 changed
  source files; `git diff --check` passed.
- Independent final scoped review: **29 passed**, covering instructions, service
  reuse/versioning and published PDF exports. No actionable findings remained.
  Final instruction-only suite: **19 passed**. Red-only actual local parser → fake
  generation → manual verification → publication → private PDF export passed.
- The earlier integrated run loaded an older test fixture during worker editing
  and had one test expectation failure. Fresh frozen runs above replaced that
  result; no production bypass or coverage weakening was used.
- Original checkout still has the same four modified and twelve untracked entries;
  retained previews still listen on ports 62170 and 56460 with their original PIDs.
  Only the owned synthetic 60431 preview was restarted for these changes.

Next action: review this source/UI candidate. Resume the separately recorded Windows
production runtime/account acceptance before preparing a live release. No additional
AMBOSS integration is planned. Vendor sample exports and full Anki curation remain
optional/deferred. No deployment, main merge or new provider test is implied by this
local receipt.
