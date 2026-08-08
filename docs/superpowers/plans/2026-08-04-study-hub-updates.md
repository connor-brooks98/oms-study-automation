# Study Hub Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add NotebookLM Studio source intake, image-aware validated quizzes, quiz review controls, OpenRouter accuracy checking, and PDF inspection while preserving the tested Anki implementation boundary.

**Architecture:** Integrate the existing remote Studio implementation into the current Anki branch’s non-Anki Hub layers. Extend native quiz contracts with safe optional metadata, route image-dependent runs through durable review, and keep public quiz interaction in browser storage. OpenRouter is a separate study-generation accuracy service, not a new Anki provider.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, Pydantic 2, NotebookLM, `pdf-inspector` from the pinned Firecrawl Git commit, PyMuPDF/pypdf/python-pptx, Pillow, httpx, Jinja2, vanilla JavaScript, pytest, Ruff, mypy, and Node’s built-in test runner.

## Global Constraints

- Treat `sources/` as read-only; it is empty in this mirror, so the private Git worktree and remote Studio branch are the source of project context.
- Do not modify `src/oms_hub/anki/**`, `src/oms_anki_agent/**`, `tests/anki/**`, or `tests/agent/**`.
- Existing lecture quizzes without image metadata remain parseable and publishable.
- Public endpoints never expose answer keys, rationales, raw NotebookLM output, credentials, local paths, or private image-review metadata.
- Store API credentials only through `SecretStore`; store only non-secret OpenRouter preferences in SQLite.
- Accept only validated, sanitized image bytes and use atomic writes for all new payload/media files.
- No automated test calls NotebookLM, OpenRouter, Office COM, or a live Google account.
- Use the pinned `pdf-inspector` Git revision `ae6246ba0c39008931b67f9cee1a898ee405d023`.

---

## File structure

### Main Hub production files

- `src/oms_hub/files/pdf.py`: PDF type/page/confidence adapter with safe fallback.
- `src/oms_hub/study_generation/studio_domain.py`: Studio source/run/image-review records.
- `src/oms_hub/study_generation/studio_repository.py`: durable Studio state and atomic review transitions.
- `src/oms_hub/study_generation/studio_service.py`: validated mixed-source intake and prompt/run queueing.
- `src/oms_hub/study_generation/studio_worker.py`: source attachment, conversion, inspection, chat, and publication orchestration.
- `src/oms_hub/study_generation/quiz_images.py`: image sanitation, extraction candidates, deterministic auto-binding.
- `src/oms_hub/study_generation/ai_settings.py`: non-secret model/gate preference repository.
- `src/oms_hub/llm/openrouter.py`: isolated OpenRouter HTTP adapter.
- `src/oms_hub/web/studio_routes.py`: private Studio/source/review/preview routes.
- `src/oms_hub/web/static/notebook_studio.js`, `studio_quiz_images.js`, `studio_quiz_preview.js`: Studio UI controllers.
- `src/oms_hub/web/static/public_quiz.js`, `public_quiz_library.js`: question navigation, flags, summaries, and reset controls.
- `src/oms_hub/web/templates/notebook_studio.html`, `studio_quiz_images.html`, `studio_quiz_preview.html`: Studio UI.
- `src/oms_hub/models.py`, `src/oms_hub/migrations.py`: additive persistence.

### Tests

- `tests/study_generation/test_native_quiz.py`, `test_quiz_images.py`, `test_accuracy.py`, `test_studio_*.py`, `test_pdf_inspector.py`.
- `tests/v2/test_studio_routes.py`, `test_llm_settings_routes.py`, `test_public_quiz_routes.py`.
- `tests/js/public_quiz.test.js`, `public_quiz_library.test.js`, `notebook_studio.test.js`, `studio_quiz_images.test.js`.

---

### Task 1: Integrate the non-Anki NotebookLM Studio slice

**Files:**
- Create: `src/oms_hub/study_generation/studio_domain.py`
- Create: `src/oms_hub/study_generation/studio_repository.py`
- Create: `src/oms_hub/study_generation/studio_service.py`
- Create: `src/oms_hub/study_generation/studio_worker.py`
- Create: `src/oms_hub/web/studio_routes.py`
- Create: `src/oms_hub/web/static/notebook_studio.js`
- Create: `src/oms_hub/web/templates/notebook_studio.html`
- Modify: `src/oms_hub/models.py`, `src/oms_hub/app.py`, `src/oms_hub/db.py`, `src/oms_hub/migrations.py`, `src/oms_hub/web/templates/base.html`
- Test: `tests/study_generation/test_studio_sources.py`, `test_studio_runs.py`, `tests/v2/test_studio_routes.py`, `tests/js/notebook_studio.test.js`

**Interfaces:**
- `StudioService.add_file/add_text/add_url(...) -> StudioSource`.
- `StudioRepository.list_sources(subject_key, exam_number) -> tuple[StudioSource, ...]`.
- `StudioRepository.queue_run(..., source_ids: list[str]) -> StudioRun`.
- `StoredNotebookLMGateway.attach_studio_source(...)`, `ask_studio(...)`, and `delete_studio_source(...)`.

- [ ] Copy the remote Studio domain/repository/service/worker implementation into the main Hub without copying any Anki deletions.
- [ ] Register the Studio repository/service/worker/router in `create_app`; keep the existing Anki lifespan and worker lifecycle intact.
- [ ] Add the page, course/exam scope selectors, source list, source picker, run history, and persistent Quiz Library navigation.
- [ ] Add failing tests for mixed source attachment, explicit empty `source_ids`, delete-after-refresh, and source select-all.
- [ ] Run `pytest tests/study_generation/test_studio_sources.py tests/study_generation/test_studio_runs.py tests/v2/test_studio_routes.py -q` and `node --test tests/js/notebook_studio.test.js`.

### Task 2: Harden file intake, PDF detection, and image extraction

**Files:**
- Modify: `src/oms_hub/files/pdf.py`
- Create/modify: `src/oms_hub/study_generation/quiz_images.py`, `src/oms_hub/study_generation/studio_service.py`, `src/oms_hub/study_generation/studio_worker.py`
- Modify: `src/oms_hub/files/office.py`, `pyproject.toml`, `.env.example`
- Test: `tests/study_generation/test_pdf_inspector.py`, `test_quiz_images.py`, `test_studio_sources.py`

**Interfaces:**
- `inspect_pdf(path: Path) -> PdfInspection(pdf_type, confidence, page_count, pages_needing_ocr)`.
- `extract_source_images(path: Path) -> tuple[ExtractedImageCandidate, ...]`.
- `resolve_image_reference(reference: QuizImageRef, candidates: tuple[...]) -> Path | None`.

- [ ] Add the pinned Git dependency `pdf-inspector @ git+https://github.com/firecrawl/pdf-inspector.git@ae6246ba0c39008931b67f9cee1a898ee405d023` and retain `pypdf` structural validation.
- [ ] Make `inspect_pdf` use `pdf_inspector.detect_pdf` when importable and return a deterministic pypdf fallback in local test environments.
- [ ] Accept `.pdf`, `.pptx`, `.txt`, `.md`, `.docx`, and supported image files; keep URLs/text as separate NotebookLM attachment methods.
- [ ] Extract images from PDF pages and PPTX media with page/slide/ordinal metadata. Only auto-bind an image when the reference locator maps to exactly one candidate.
- [ ] Preserve the current bounded Office conversion and delete partial conversion outputs after errors/timeouts.
- [ ] Add drag/drop and paste handling for image files and direct image URLs; use text content rather than `innerHTML` for all labels.
- [ ] Run the focused file/image/PDF tests and confirm invalid files do not create attached sources.

### Task 3: Extend the validated quiz contract and subject rules

**Files:**
- Modify: `src/oms_hub/study_generation/domain.py`, `native_quiz.py`, `prompts.py`, `worker.py`, `studio_service.py`
- Test: `tests/study_generation/test_native_quiz.py`, `test_prompts.py`, `test_worker.py`, `test_studio_runs.py`

**Interfaces:**
- `QuizImageRef(key, source_title, locator, description)`.
- `QuizQuestion(..., topic: str | None, learning_objective: str | None, image_ref: QuizImageRef | None)`.
- `quiz_prompt(prompt: PromptSnapshot, subject: str | None = None) -> PromptSnapshot`.
- `studio_quiz_prompt(prompt: str, subject: str | None = None) -> str`.
- `image_requirements(quiz: NativeQuiz) -> tuple[QuizImageRef, ...]`.

- [ ] Add optional image/topic/objective fields with strict Pydantic bounds and legacy-compatible defaults.
- [ ] Append the image-aware JSON contract to both lecture and Studio quiz prompts.
- [ ] Add explicit non-OMM exclusions and OMM-only guidance using a normalized subject classifier; include the thoracic spine-level prohibition for non-OMM subjects.
- [ ] Reject conflicting metadata for reused image keys and keep public serialization free of answer/rationale fields.
- [ ] Add tests proving lecture and Studio prompts include image rules, non-OMM prompts exclude OMM content, and legacy payloads still parse.

### Task 4: Add medical-accuracy checking and OpenRouter settings

**Files:**
- Create: `src/oms_hub/llm/openrouter.py`, `src/oms_hub/study_generation/ai_settings.py`
- Modify: `src/oms_hub/models.py`, `migrations.py`, `config.py`, `app.py`, `web/settings_routes.py`, `web/templates/settings.html`, `web/static/settings.js`, `.env.example`
- Test: `tests/study_generation/test_accuracy.py`, `tests/llm/test_openrouter.py`, `tests/v2/test_llm_settings_routes.py`, `tests/v2/test_llm_settings_ui.py`

**Interfaces:**
- `MedicalAccuracyGate.validate(quiz) -> None`.
- `MedicalAccuracyGate.assess(question, model) -> AccuracyAssessment`.
- `StudyAISettingsRepository.get/save(...) -> StudyAISettings`.

- [ ] Store `openrouter-api-key` only through `SecretStore`; never echo it to HTML/JSON/logs.
- [ ] Implement the OpenRouter `/api/v1/chat/completions` adapter with redacted error classification and structured JSON validation.
- [ ] Add a curated model dropdown plus custom model option and a gate toggle in Settings.
- [ ] Wire the gate into lecture and Studio publication; enabled runs pause on missing key, malformed output, `review`, or `fail` verdicts.
- [ ] Keep the default gate disabled for existing installations until the user configures OpenRouter, while making the publication state explicit.
- [ ] Mock HTTP failures, malformed JSON, and mixed verdicts; assert secrets are absent from responses and exceptions.

### Task 5: Persist automatic/manual image review and explicit publication

**Files:**
- Modify: `models.py`, `migrations.py`, `study_generation/studio_repository.py`, `study_generation/studio_worker.py`, `study_generation/repository.py`
- Create/modify: `study_generation/quiz_images.py`, `web/studio_routes.py`, `web/static/studio_quiz_images.js`, `web/static/studio_quiz_preview.js`, `web/templates/studio_quiz_images.html`, `studio_quiz_preview.html`
- Modify: `web/public_quiz_routes.py`, `study_generation/native_quiz.py`
- Test: `tests/study_generation/test_studio_image_review.py`, `test_studio_publication.py`, `tests/v2/test_public_quiz_routes.py`, `tests/js/studio_quiz_images.test.js`, `tests/js/public_quiz.test.js`

**Interfaces:**
- `StudioRepository.await_image_review(...)`, `quiz_review(...)`, `bind_image(...)`, `set_image_override(...)`, `resolved_quiz(...)`.
- `sanitize_quiz_image(payload) -> SanitizedQuizImage`.
- `GenerationRepository.publish_reviewed_studio_quiz(run_id) -> PublishedQuizRecord`.

- [ ] Carry automatically matched images into the requirement rows; leave ambiguous references in `awaiting_images`.
- [ ] Keep replacement quizzes private until preview publication and copy immutable media bindings into `published_quiz_media`.
- [ ] Add upload/replace, per-question “No image needed” override/reversal, preview, and publish routes with CSRF and private access checks.
- [ ] Serve only active token/key media and verify stored SHA-256 before `FileResponse`.
- [ ] Verify public content exposes only `image_url`/`image_alt` and still omits answer keys/rationales.

### Task 6: Upgrade quiz player navigation, flags, reset, and summaries

**Files:**
- Modify: `src/oms_hub/web/static/public_quiz.js`, `public_quiz_library.js`, `public_quiz.css`, `public_quiz_library.css`, `templates/public_quiz.html`, `public_quiz_library.html`
- Test: `tests/js/public_quiz.test.js`, `tests/js/public_quiz_library.test.js`, `tests/v2/test_public_quiz_routes.py`

**Interfaces:**
- `navigateQuestion(state, index, totalQuestions)`, `setFlagReason(state, questionId, reason)`.
- `performanceSummary(content, state) -> grouped area/objective/topic counts`.
- `resetProgress(storage, token, version)` for per-quiz library reset.

- [ ] Add Previous/Next controls after answered questions, a review button from the result screen, and persistence of the current index.
- [ ] Add a per-question flag dropdown with reasons: inaccurate question, want to review, unclear wording, and other.
- [ ] Add a per-quiz reset control in the player/library; retain the existing reset-all control with confirmation.
- [ ] Group the final summary by area, `learning_objective`, and topic, with right/needs-review counts and flagged-question counts.
- [ ] Render optional question images with relative-URL validation, accessible alt text, and full-size links.
- [ ] Add unit tests for backwards navigation, restored flags, summaries, reset isolation, and image rendering.

### Task 7: Complete shared UI/navigation and verification

**Files:**
- Modify: `src/oms_hub/web/templates/base.html`, `app.css`, `routes.py`, `lecture.html`, `lecture.js`, `uploads.html`, `review.html`, `settings.html`
- Add/update: `README.md`, `docs/notebooklm-studio-rollout.md`, `docs/notebooklm-studio-acceptance.md`
- Test: `tests/js/lecture.test.js`, `tests/test_interface_improvements.py`, full existing suites

- [ ] Add previous/next lecture navigation and keyboard shortcuts without changing Anki route behavior.
- [ ] Add the Quiz Library link to private pages through the shared base template.
- [ ] Keep prompt textareas vertically resizable only (`resize: vertical`) and ensure responsive controls remain keyboard accessible.
- [ ] Run `pytest -q`, `node --test tests/js/*.test.js`, `ruff check src tests`, and `mypy src`.
- [ ] Run the unchanged Anki test slices explicitly and confirm no Anki implementation/test files changed:
  `pytest tests/anki tests/agent -q`.
- [ ] Record any Windows-only Office/NotebookLM/OpenRouter acceptance steps without claiming them as local passes.

## Completion gate

The work is complete only when the focused tests, full test suites, Ruff, and
mypy pass; the migration starts cleanly and upgrades an existing database; the
Anki boundary is unchanged; and the final response links the two plan files,
lists changed behavior, and calls out any Windows/live-service checks still
pending.
