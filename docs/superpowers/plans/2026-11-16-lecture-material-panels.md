# Lecture material panels implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Add independent lecture-bound Slides and Transcript panels while preserving the verified deployed shell.

**Architecture:** Render panels with the existing Jinja lecture page and artifact service. Send single-file uploads through the existing manifest pipeline with explicit lecture and kind. Keep recoverable remove/restore and content-hash safeguards.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja, vanilla JavaScript, pytest, Node test runner.

**Spec:** Approved task delegation in this conversation, 2026-11-16.

## Global constraints

- Preserve current header, navigation, menu, previous/next, styling and unrelated buttons.
- Slides: PPTX/PDF; transcript: TXT. Existing pipeline owns canonical names, conversion and cleanup.
- Download directly; view in a new tab; only verified available files get actions.
- Tracker immediately under panels, processing next; retain existing outline below. Per subsequent approved refinement, remove lecture Clinical Notebook, NotebookLM delivery and quiz workflow boxes; global integrations remain unchanged.
- Ready transcript: filename, Download TXT, View TXT, accessible options disclosure with Delete transcript. No uploader, ready badge or duplicate explanatory text.
- Empty slides: passive drop zone, bottom Upload Slides button is the sole picker; chosen/dropped file uploads to the exact lecture.
- No live writes, installs, push, deployment, security changes or external communication.
- Preserve original checkout and its three uncommitted restoration files; carry copies into isolated worktree.

## Review focus

1. Removed identical bytes produce restore guidance without enqueuing or losing originals.
2. Two concurrent panel uploads cannot overwrite targeting or reload the other panel mid-upload.
3. Missing/changed original and derived files independently hide their corresponding actions.
4. PDF originals never advertise a nonexistent PPTX.
5. Failed processing, replacement confirmation and uncertain network outcomes remain recoverable.

## Tasks

### Task 1: Characterize recovery and artifact actions

Files: tests/v2/test_lecture_materials.py, src/oms_hub/web/artifact_routes.py, src/oms_hub/lecture_materials.py, src/oms_hub/web/routes.py.
Interfaces: existing artifact resolution and explicit lecture upload API; new PDF attachment endpoint and per-kind available role sets.
- [x] Write fixture tests for ready/missing actions, direct PDF attachment, identical remove/readd/restore and lecture/type isolation.
- [x] Observe new action tests fail, preserve existing restore regression passing.
- [x] Add PDF download endpoint and verified per-file availability context; keep repository invariants.
- [x] Run material and artifact tests.

### Task 2: Independent material panels

Files: lecture.html, new components/lecture_material_panel.html, new static/lecture_materials.css and .js, tests/js/lecture_materials.test.js, relevant existing UI assertions.
Interfaces: per-kind available role sets; existing /uploads/{kind}, manifest/chunk/batch APIs with explicit lecture_id; existing remove/restore forms.
- [x] Write failing HTML and JS tests for empty/ready states, file type validation, targeting, processing/failure, duplicate/restore guidance and concurrent panels.
- [x] Render roomy drop zones and file-first ready actions, preserving surrounding shell. Keep global tools.
- [x] Implement per-panel single-file upload, chunk transport for large files, bounded status polling and actionable recovery without automatic page reload.
- [x] Run relevant Python and Node suites.

### Task 3: Verify fake-data UI and final diff

- [x] Serve only generated fixture pages locally and inspect desktop/mobile through supported browser tools.
- [x] Compare unchanged shell files and observed live header/navigation to fixture shell.
- [x] Review final diff and preserve evidence, worktree location, tests and any gaps.

## Baseline and rulings

- Live signed-in Mac Chrome /lectures/200 verified 2026-11-16; Cloudflare barrier absent. Header screenshot limited to navigation; no lecture content captured.
- Local HEAD a901cc9990ff0cb1d3bc1efd2eff450308c3c0e1, codex/notebook-upload-cleanup. Native worktree tool unavailable; created detached /tmp/studyhub-lecture-material-panels with Git.
- Ruling superseded by later user instruction: remove the lecture Clinical Notebook box and retain the optional outline after processing; preserve the global integration.
- Ruling: current task already authorizes implementation; continue without a second plan approval.
- Ruling: original selected mockup images were not available in the delegated context or message search; implement the specified roomy empty/file-first ready treatments with current tokens; exact image fidelity remains unverified.
- Baseline: 15 material tests pass with copied, pre-existing 409 guidance patch.

## Execution evidence and review

- Native implementation continued under the existing implementation authorization.
- New material-action/attachment tests failed before implementation; new picker/menu tests failed before the user-requested refinement.
- Independent reviewer found stale dependent UI after panel refresh. Added failing reconciliation test, then updated the processing checklist and replacement-review region without replacing the pass tracker or other material panel. Clinical Notebook availability reconciliation became unnecessary when its lecture box was removed by the later user instruction.
- DOM-event tests cover concurrent picker/drop uploads, independent failures, removed-file guidance, replacement decisions, compact transcript initialization, deletion cancellation, and the sole slides picker.
- Fixture UI inspected at 1440x1050 and 390x844. Latest mobile check: no horizontal overflow, zero lecture Clinical Notebook boxes, exactly one visible empty-slides picker button, zero controls inside the passive drop zone, transcript actions in the approved order.
- Screenshots saved to /tmp/studyhub-panels-refined-desktop.jpg and /tmp/studyhub-panels-refined-mobile.jpg.
- Full Python repository suite is outside this bounded verification; targeted material, intake, manifest, lifecycle, duplicate, replacement, artifact-download and lecture UI suites are recorded in the verification report.

## Final approved slide re-upload refinement

- Reproduce identical-byte removal/re-upload with synthetic files and real upload routes.
- Hide only the removed Slides disclosure; retain Transcript recovery UI and backend history.
- Accept one explicitly targeted slide upload by reusing its exact lecture/type/hash removed revision after existing retained-file and idle/current checks. Commit restoration and upload admission together under the lecture artifact claim; keep active deduplication unchanged.
- Verify failed admission rollback, post-commit cleanup, busy/current conflicts, changed retained files, other-lecture isolation, normal picker/drop behavior, and desktop/mobile layout.
- Preserve the verified result in this isolated local branch, with no push/deploy/live-data change.
