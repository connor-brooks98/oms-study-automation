# NotebookLM Outline and Gemini Quiz Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, button-driven lecture-outline and Gemini-quiz generation that uses exactly one lecture PDF plus its cleaned transcript, publishes a current quiz link, and synchronizes course Google Docs.

**Architecture:** Add a focused `study_generation` package behind application-owned gateway protocols. SQLite stores non-secret mappings, stage-aware jobs, and current outputs; Windows Credential Manager and an owner-only browser profile hold Google credentials. `notebooklm-py` handles notebook operations, Playwright drives the existing Gemini Quiz Gem, the Google Docs API manages course documents and exam tabs, and existing Study Hub routes serve the resulting outline PDF and quiz link.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, Jinja2, `notebooklm-py==0.7.3`, Playwright, Google Docs/Drive APIs, Google OAuth, ReportLab, pytest, Node's built-in test runner.

## Global Constraints

- Work only on `codex/notebooklm-gemini-workflow`; do not merge into `main`.
- Use one Gemini Notebook per normalized course and exam.
- Every generation request must contain exactly the selected lecture's current PDF source ID and current cleaned-transcript source ID.
- Never fall back to all notebook sources.
- Use the current derived lecture PDF, not the PPTX, and the current cleaned transcript.
- Read the outline and quiz prompts from their configured Obsidian files at job start.
- Outline and quiz generation remain separate per-lecture button actions.
- Expose one current outline PDF and one current quiz URL per lecture.
- Create one master Google Doc per course and one root-level tab per exam.
- Replace a lecture's existing Google Doc link on rerun; never append a duplicate.
- Store browser state only below the application data directory and OAuth refresh credentials only in Windows Credential Manager.
- Preserve the existing IBM Plex, course-rail, card, button, status-pill, accessibility, and responsive design language.
- Additive database migrations only.
- Pin `notebooklm-py` to the tested stable version `0.7.3`.
- Do not log cookies, OAuth tokens, prompt contents, generated quiz contents, or full remote responses.
- Creation and resume operations must be idempotent after timeouts and process restarts.

---

## File Structure

Create these focused modules:

- `src/oms_hub/study_generation/domain.py` — enums, immutable values, gateway protocols, and safe exceptions.
- `src/oms_hub/study_generation/repository.py` — prompt settings, remote mappings, jobs, and output persistence.
- `src/oms_hub/study_generation/prompts.py` — Obsidian path validation and prompt snapshots.
- `src/oms_hub/study_generation/google_connection.py` — dedicated browser profile, OAuth setup, account status, and safe connection diagnostics.
- `src/oms_hub/study_generation/notebook.py` — `notebooklm-py` adapter and hard two-source validation.
- `src/oms_hub/study_generation/outline.py` — ReportLab PDF rendering and atomic outline routing.
- `src/oms_hub/study_generation/gemini_quiz.py` — Playwright adapter for the configured Quiz Gem and share-link capture.
- `src/oms_hub/study_generation/google_docs.py` — official Docs/Drive adapter for course documents, tabs, and managed lecture links.
- `src/oms_hub/study_generation/service.py` — prerequisite checks and idempotent job queueing.
- `src/oms_hub/study_generation/worker.py` — stage-aware outline and quiz orchestration.
- `src/oms_hub/web/generation_routes.py` — Settings connection/configuration and lecture generation endpoints.
- `src/oms_hub/web/static/lecture.js` — lecture card actions and safe live status updates.

Modify these existing modules:

- `src/oms_hub/models.py` and `src/oms_hub/migrations.py` — additive schema version 4.
- `src/oms_hub/config.py` — Google profile and generation timeout settings.
- `src/oms_hub/routing.py` — validated lecture-outline destination.
- `src/oms_hub/artifacts.py` and `src/oms_hub/web/artifact_routes.py` — current outline PDF resolution.
- `src/oms_hub/app.py` and `src/oms_hub/cli.py` — dependency composition and generation worker lifecycle.
- `src/oms_hub/web/routes.py`, `src/oms_hub/web/templates/lecture.html`, `src/oms_hub/web/templates/settings.html`, and `src/oms_hub/web/static/app.css` — UI state.
- `src/oms_hub/web/static/settings.js` — Google setup and prompt-path controls.
- `pyproject.toml`, `.env.example`, `README.md`, release builder, and NUC rollout documentation.

---

### Task 1: Persistence Model and Repository Contracts

**Files:**
- Create: `src/oms_hub/study_generation/__init__.py`
- Create: `src/oms_hub/study_generation/domain.py`
- Create: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Test: `tests/study_generation/test_migration.py`
- Test: `tests/study_generation/test_repository.py`

**Interfaces:**
- Produces: `GenerationKind`, `GenerationStage`, `GenerationState`, `SourceKind`, `PromptKind`, `GenerationJob`, `GenerationRepository`.
- `GenerationRepository.queue(lecture_id: int, kind: GenerationKind) -> GenerationJob`.
- `GenerationRepository.claim_next(now: datetime) -> GenerationJob | None`.
- `GenerationRepository.advance(job_id: str, stage: GenerationStage, **fields: object) -> GenerationJob`.
- `GenerationRepository.current_outline(lecture_id: int) -> OutlineRecord | None`.
- `GenerationRepository.current_quiz(lecture_id: int) -> QuizRecord | None`.

- [ ] **Step 1: Write the migration and repository tests**

Create tests that migrate a version-3 database and assert all new tables exist,
then exercise unique job and mapping contracts:

```python
def test_schema_v4_adds_generation_tables_without_changing_lectures(tmp_path):
    database = legacy_v3_database(tmp_path)
    database.migrate()
    names = set(inspect(database.engine).get_table_names())
    assert {
        "google_connection",
        "study_prompt_settings",
        "notebook_mappings",
        "notebook_source_mappings",
        "course_quiz_documents",
        "exam_quiz_tabs",
        "generation_jobs",
        "outline_outputs",
        "quiz_outputs",
    } <= names
    assert database.session_factory_is_usable()
    assert schema_version(database) == 4


def test_queue_is_idempotent_while_same_kind_job_is_active(database):
    repository = GenerationRepository(database)
    first = repository.queue(lecture_id=7, kind=GenerationKind.OUTLINE)
    second = repository.queue(lecture_id=7, kind=GenerationKind.OUTLINE)
    quiz = repository.queue(lecture_id=7, kind=GenerationKind.QUIZ)
    assert second.id == first.id
    assert quiz.id != first.id
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py -v
```

Expected: collection fails because `oms_hub.study_generation` does not exist.

- [ ] **Step 3: Add the domain types and SQLAlchemy models**

Define exact string values:

```python
class GenerationKind(StrEnum):
    OUTLINE = "outline"
    QUIZ = "quiz"


class GenerationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"


class GenerationStage(StrEnum):
    VALIDATE = "validate"
    NOTEBOOK = "notebook"
    SOURCES = "sources"
    NOTEBOOK_PROMPT = "notebook_prompt"
    PDF = "pdf"
    GEMINI = "gemini"
    SHARE = "share"
    DOCS = "docs"
    COMPLETE = "complete"


class SourceKind(StrEnum):
    LECTURE_PDF = "lecture_pdf"
    CLEANED_TRANSCRIPT = "cleaned_transcript"


class PromptKind(StrEnum):
    OUTLINE = "outline"
    QUIZ = "quiz"
```

Add the nine tables from the design. Store job IDs as UUID strings. Put unique
constraints on course/exam notebook mappings, notebook/revision/source-kind
mappings, course document mappings, document/exam tab mappings, and one output
per job. Add partial active-job enforcement in repository logic because SQLite
partial constraints are harder to keep portable.

- [ ] **Step 4: Implement repository transitions**

Implement compare-and-set claims and explicit stage transitions. `queue` must
return an existing queued/running/paused job of the same kind for the lecture.
`claim_next` changes only a queued row to running. `recover_interrupted` changes
running jobs back to queued without clearing the recorded stage or remote IDs.

- [ ] **Step 5: Run repository tests**

Run:

```bash
python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py -v
python -m ruff check src/oms_hub/study_generation src/oms_hub/models.py src/oms_hub/migrations.py tests/study_generation
python -m mypy src/oms_hub/study_generation
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation src/oms_hub/models.py src/oms_hub/migrations.py tests/study_generation
git commit -m "feat: add study generation persistence"
```

---

### Task 2: Obsidian Prompt Configuration

**Files:**
- Create: `src/oms_hub/study_generation/prompts.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Test: `tests/study_generation/test_prompts.py`
- Test: `tests/v2/test_generation_settings.py`
- Test: `tests/js/settings-generation.test.js`

**Interfaces:**
- Consumes: `PromptKind`, `GenerationRepository`.
- Produces: `PromptSnapshot(path: Path, content: str, sha256: str, modified_at: str)`.
- `PromptFileService.inspect(kind: PromptKind) -> PromptSnapshot`.
- `GenerationRepository.set_prompt_path(kind: PromptKind, path: str) -> None`.

- [ ] **Step 1: Write failing prompt tests**

```python
def test_prompt_snapshot_reads_latest_obsidian_content_and_fingerprints(tmp_path):
    path = tmp_path / "NotebookLM Quiz Prompt.md"
    path.write_text("Create 15 questions", encoding="utf-8")
    repository = configured_repository(tmp_path, PromptKind.QUIZ, path)
    first = PromptFileService(repository).inspect(PromptKind.QUIZ)
    path.write_text("Create 20 questions", encoding="utf-8")
    second = PromptFileService(repository).inspect(PromptKind.QUIZ)
    assert first.content == "Create 15 questions"
    assert second.content == "Create 20 questions"
    assert first.sha256 != second.sha256


@pytest.mark.parametrize("payload", ["", "   "])
def test_empty_prompt_is_rejected(tmp_path, payload):
    path = tmp_path / "empty.md"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(PromptConfigurationError, match="empty"):
        configured_prompt_service(tmp_path, path).inspect(PromptKind.OUTLINE)
```

Add route tests confirming Settings returns path and status but never returns
prompt contents.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
python -m pytest tests/study_generation/test_prompts.py tests/v2/test_generation_settings.py -v
```

Expected: imports or routes are missing.

- [ ] **Step 3: Implement prompt validation and routes**

Resolve environment variables and `~`, require an existing regular file, read
strict UTF-8, strip only for the empty check, fingerprint the exact bytes, and
record last validation metadata. Add:

```text
POST /settings/generation/prompts/{outline|quiz}
POST /settings/generation/prompts/{outline|quiz}/test
```

Both JSON responses use `Cache-Control: no-store`. Accept paths up to 2,048
characters. Do not accept prompt body text.

- [ ] **Step 4: Extend the existing Settings design**

Add one card titled **Notebook prompts** with two path inputs, **Save path**, and
**Test file** controls. Reuse `.provider-card`, `.provider-field`,
`.field-message`, and existing status classes. Extend `settings.js` through
text-only DOM updates.

- [ ] **Step 5: Run Python and JavaScript tests**

```bash
python -m pytest tests/study_generation/test_prompts.py tests/v2/test_generation_settings.py -v
node --test tests/js/settings.test.js tests/js/settings-generation.test.js
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation/prompts.py src/oms_hub/study_generation/repository.py src/oms_hub/web tests/study_generation/test_prompts.py tests/v2/test_generation_settings.py tests/js/settings-generation.test.js
git commit -m "feat: link Notebook prompts from Obsidian"
```

---

### Task 3: Google Connection and Secure Session Setup

**Files:**
- Create: `src/oms_hub/study_generation/google_connection.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/security/secret_store.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Modify: `pyproject.toml`
- Test: `tests/study_generation/test_google_connection.py`
- Test: `tests/v2/test_google_settings_routes.py`
- Test: `tests/v2/test_generation_secret_safety.py`

**Interfaces:**
- Produces: `GoogleConnectionService`, `GoogleConnectionStatus`, `GoogleSurface`.
- `GoogleConnectionService.start_interactive() -> GoogleConnectionStatus`.
- `GoogleConnectionService.status(test_live: bool = False) -> GoogleConnectionStatus`.
- `GoogleConnectionService.oauth_credentials() -> google.oauth2.credentials.Credentials`.
- Secret keys: `google-oauth-refresh-token`, `google-connected-email`.

- [ ] **Step 1: Add dependency and connection contract tests**

Pin/add:

```toml
"notebooklm-py==0.7.3",
"playwright>=1.55,<2",
"google-api-python-client>=2.180,<3",
"google-auth-oauthlib>=1.2,<2",
"reportlab>=4.4,<5",
```

Write tests with fake browser/OAuth ports:

```python
def test_connected_status_requires_same_account_on_all_surfaces(tmp_path):
    service = connection_service(
        tmp_path,
        notebook_email="student@example.com",
        gemini_email="student@example.com",
        oauth_email="other@example.com",
    )
    status = service.status(test_live=True)
    assert status.state == "failed"
    assert status.message == "Google surfaces are connected to different accounts"


def test_status_and_http_payload_never_expose_browser_or_oauth_secrets(client):
    response = client.get("/settings/google/status")
    forbidden = ("SID", "refresh_token", "client_secret", "ya29.")
    assert all(value not in response.text for value in forbidden)
```

- [ ] **Step 2: Verify focused tests fail**

```bash
python -m pytest tests/study_generation/test_google_connection.py tests/v2/test_google_settings_routes.py tests/v2/test_generation_secret_safety.py -v
```

Expected: missing service and routes.

- [ ] **Step 3: Implement owner-only profile and OAuth storage**

Use `Settings.data_dir / "google" / "browser-profile"` and
`Settings.data_dir / "google" / "notebooklm-storage.json"`. Refuse a profile
path outside `data_dir`. On Windows, set the directory ACL through the same
account running Study Hub; on POSIX tests, require mode `0700` and storage-state
mode `0600`.

`start_interactive` is single-flight. It launches a headed persistent Chromium
context, opens Gemini Notebook and Gemini, and permits the user to sign in. It
then exports NotebookLM-compatible storage state and runs installed-app OAuth
consent for scopes:

```python
GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
)
```

Store only the refresh token and connected email in `SecretStore`. Store
client ID and client secret in an owner-only JSON file after a validated
Settings upload; never store them in SQLite.

- [ ] **Step 4: Add non-blocking connection routes and Settings card**

Add:

```text
POST /settings/google/oauth-client
POST /settings/google/connect
POST /settings/google/test
GET  /settings/google/status
```

The connect route starts a background connection operation and returns `202`.
The browser polls the status route. Show separate Notebook, Gemini, and Docs
surface states under one **Google workspace** card.

- [ ] **Step 5: Run connection, security, and UI tests**

```bash
python -m pytest tests/study_generation/test_google_connection.py tests/v2/test_google_settings_routes.py tests/v2/test_generation_secret_safety.py -v
node --test tests/js/settings.test.js tests/js/settings-generation.test.js
python -m ruff check src tests
python -m mypy src/oms_hub
```

Expected: all pass without a live Google account.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/oms_hub/config.py src/oms_hub/security src/oms_hub/study_generation/google_connection.py src/oms_hub/web tests
git commit -m "feat: add guided Google connection"
```

---

### Task 4: NotebookLM Adapter and Exact Two-Source Isolation

**Files:**
- Create: `src/oms_hub/study_generation/notebook.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Test: `tests/study_generation/test_notebook_gateway.py`
- Test: `tests/study_generation/test_source_isolation.py`

**Interfaces:**
- Consumes: current Study Hub `StudyRevision` values and NotebookLM storage path.
- Produces: `NotebookGateway` protocol and `NotebookLMGateway`.
- `ensure_notebook(subject: str, exam_number: int) -> NotebookRef`.
- `ensure_sources(notebook: NotebookRef, lecture_id: int, pdf: RevisionSource, transcript: RevisionSource) -> LectureSourceSet`.
- `ask(notebook: NotebookRef, sources: LectureSourceSet, prompt: PromptSnapshot) -> NotebookAnswer`.

- [ ] **Step 1: Write gateway contract tests**

Use a fake NotebookLM client:

```python
def test_ask_passes_exactly_pdf_and_transcript_source_ids(gateway):
    answer = gateway.ask(
        NotebookRef("nb-1", "Neuro · Exam 1"),
        LectureSourceSet(
            lecture_id=12,
            pdf=RemoteSource("pdf-1", 101, "a" * 64),
            transcript=RemoteSource("txt-1", 202, "b" * 64),
        ),
        prompt_snapshot("Build the quiz"),
    )
    assert gateway.client.last_ask == {
        "notebook_id": "nb-1",
        "question": "Build the quiz",
        "source_ids": ["pdf-1", "txt-1"],
    }
    assert answer.text


@pytest.mark.parametrize(
    "source_set",
    [missing_pdf_set(), missing_transcript_set(), cross_lecture_set(), stale_set()],
)
def test_invalid_source_set_stops_before_remote_prompt(gateway, source_set):
    with pytest.raises(SourceIsolationError):
        gateway.ask(notebook_ref(), source_set, prompt_snapshot("Prompt"))
    assert gateway.client.ask_calls == []
```

Also test notebook/source probe-after-timeout behavior and source readiness.

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/study_generation/test_notebook_gateway.py tests/study_generation/test_source_isolation.py -v
```

Expected: missing adapter types.

- [ ] **Step 3: Implement the gateway**

Wrap imports so application unit tests can substitute a client factory. Use
`NotebookLMClient.from_storage(storage_path)`, search stored notebook mappings
first, probe the remote list before creating, and call:

```python
result = await client.chat.ask(
    notebook.id,
    prompt.content,
    source_ids=[sources.pdf.remote_id, sources.transcript.remote_id],
)
```

Do not expose a gateway method that omits `source_ids`. `LectureSourceSet`
validates two distinct source kinds, one lecture ID, current revision IDs, and
ready state in `__post_init__`.

- [ ] **Step 4: Implement revision-keyed source reuse**

Use the current slide revision's `canonical_derived_path` and `derived_sha256`;
use the current transcript revision's `canonical_derived_path` and
`derived_sha256`. Validate both files before upload. Store the returned remote
IDs only after NotebookLM reports each source ready.

- [ ] **Step 5: Run adapter tests and static checks**

```bash
python -m pytest tests/study_generation/test_notebook_gateway.py tests/study_generation/test_source_isolation.py -v
python -m ruff check src/oms_hub/study_generation tests/study_generation
python -m mypy src/oms_hub/study_generation
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation tests/study_generation
git commit -m "feat: add isolated NotebookLM generation"
```

---

### Task 5: Durable Generation Service and Worker

**Files:**
- Create: `src/oms_hub/study_generation/service.py`
- Create: `src/oms_hub/study_generation/worker.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/cli.py`
- Test: `tests/study_generation/test_service.py`
- Test: `tests/study_generation/test_worker.py`
- Test: `tests/v2/test_generation_restart_recovery.py`

**Interfaces:**
- Consumes: repositories, prompt service, notebook gateway.
- Produces: `GenerationService.queue_outline`, `GenerationService.queue_quiz`, `GenerationWorker.run_once`, `GenerationWorker.recover_interrupted_jobs`.

- [ ] **Step 1: Write failing prerequisite and recovery tests**

```python
def test_outline_queue_requires_current_pdf_and_cleaned_transcript(service):
    with pytest.raises(GenerationPrerequisiteError) as error:
        service.queue_outline(lecture_id=4)
    assert str(error.value) == "Current lecture PDF and cleaned transcript are required"


def test_worker_resume_starts_at_recorded_stage_after_restart(worker, repository):
    job = running_quiz_job(stage=GenerationStage.SHARE, gemini_url="https://gemini.google.com/share/x")
    repository.save_for_test(job)
    assert worker.recover_interrupted_jobs() == 1
    worker.run_once()
    assert worker.gemini.generate_calls == 0
    assert worker.gemini.share_calls == 1
```

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/study_generation/test_service.py tests/study_generation/test_worker.py tests/v2/test_generation_restart_recovery.py -v
```

Expected: missing service and worker.

- [ ] **Step 3: Implement queue authorization**

At queue time, resolve the lecture, current slide/transcript revisions, prompt,
and Google connection. Persist their IDs and prompt fingerprint on the job.
Repeat all mutable checks immediately before NotebookLM submission.

- [ ] **Step 4: Implement stage-aware worker**

Each stage returns a value that is persisted before the next side effect.
Transient failures schedule bounded exponential retry. Authentication failures
pause. Contract/page-shape failures fail with `needs_review`. The worker must
never clear remote IDs while retrying.

- [ ] **Step 5: Compose and run both workers**

Add `app.state.generation_worker`. Update `serve` to start one generation
thread beside the ingestion thread and stop/join both during shutdown. Update
`recover-jobs` and `worker-once` to include both worker types without changing
their existing output keys.

- [ ] **Step 6: Run worker and existing ingestion tests**

```bash
python -m pytest tests/study_generation/test_service.py tests/study_generation/test_worker.py tests/v2/test_generation_restart_recovery.py tests/v2/test_worker_llm_retry.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation src/oms_hub/app.py src/oms_hub/cli.py tests
git commit -m "feat: add durable generation worker"
```

---

### Task 6: Outline PDF Rendering, Routing, and Artifact Access

**Files:**
- Create: `src/oms_hub/study_generation/outline.py`
- Modify: `src/oms_hub/routing.py`
- Modify: `src/oms_hub/artifacts.py`
- Modify: `src/oms_hub/web/artifact_routes.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Test: `tests/study_generation/test_outline_pdf.py`
- Test: `tests/v2/test_outline_artifact.py`

**Interfaces:**
- Produces: `OutlinePdfRenderer.render(title: str, content: str) -> bytes`.
- Produces: `OutlineService.file(job: GenerationJob, answer: NotebookAnswer) -> OutlineRecord`.
- Adds `ArtifactRole.OUTLINE`.

- [ ] **Step 1: Write failing PDF and artifact tests**

```python
def test_outline_renderer_creates_valid_single_pdf(tmp_path):
    payload = OutlinePdfRenderer().render(
        "Neuro - Lecture 01 - Seizures - Lecture Outline",
        "# Objectives\n- Localize seizure onset\n\n## Pearl\nTreat status quickly.",
    )
    path = tmp_path / "outline.pdf"
    path.write_bytes(payload)
    assert validate_pdf(path).page_count >= 1
    assert PdfReader(path).pages[0].extract_text().startswith("Neuro")


def test_outline_route_rejects_changed_current_file(client, outline_record):
    outline_record.path.write_bytes(b"changed")
    response = client.get(f"/artifacts/outlines/{outline_record.id}")
    assert response.status_code == 409
```

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/study_generation/test_outline_pdf.py tests/v2/test_outline_artifact.py -v
```

Expected: renderer and route are missing.

- [ ] **Step 3: Implement PDF rendering**

Use ReportLab Platypus with embedded standard fonts, escaped paragraphs,
heading/bullet recognition, page numbers, stable margins, and wrapped text.
Reject empty output and validate the rendered bytes with `pypdf`.

- [ ] **Step 4: Add the canonical outline destination**

Add:

```python
def build_outline_destination(settings: Settings, lecture: LectureKey) -> Path:
    return (
        expanded_path(settings.study_root)
        / sanitize_filename(lecture.subject)
        / f"Exam {lecture.exam_number}"
        / "Lecture Outlines"
        / (
            sanitize_filename(
                f"{lecture.subject} - Lecture {lecture.lecture_number:02d} - "
                f"{lecture.topic} - Lecture Outline"
            )
            + ".pdf"
        )
    )
```

Enforce containment under `study_root`, write an immutable job copy first, then
atomically promote the canonical file and mark the prior output non-current.

- [ ] **Step 5: Add validated inline artifact access**

Add `GET /artifacts/outlines/{outline_id}`. Resolve only recorded paths, verify
containment and SHA-256, run `validate_pdf`, return `application/pdf` inline,
and set `Cache-Control: private, no-store`.

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/study_generation/test_outline_pdf.py tests/v2/test_outline_artifact.py -v
python -m pytest tests/v2/test_transcript_download.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation/outline.py src/oms_hub/study_generation/worker.py src/oms_hub/routing.py src/oms_hub/artifacts.py src/oms_hub/web/artifact_routes.py tests
git commit -m "feat: create lecture outline PDFs"
```

---

### Task 7: Gemini Quiz Gem Browser Adapter

**Files:**
- Create: `src/oms_hub/study_generation/gemini_quiz.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Test: `tests/study_generation/test_gemini_quiz_gateway.py`
- Test: `tests/study_generation/fixtures/gemini_gem.html`
- Test: `tests/study_generation/fixtures/gemini_share.html`

**Interfaces:**
- Produces: `GeminiQuizGateway`.
- `generate(job_id: str, quiz_content: str) -> GeminiQuizRef`.
- `share(quiz: GeminiQuizRef) -> SharedQuiz`.
- `SharedQuiz.url` must be an allowlisted HTTPS Gemini share URL.

- [ ] **Step 1: Write fixture-driven browser contract tests**

```python
def test_gateway_uses_configured_gem_and_captures_share_url(fake_gemini_page):
    gateway = GeminiQuizGateway(
        page=fake_gemini_page,
        configured_gem_url="https://gemini.google.com/gem/quiz-gem-id",
    )
    quiz = gateway.generate("job-1", "1. Which tract crosses?")
    shared = gateway.share(quiz)
    assert fake_gemini_page.opened_url.endswith("/gem/quiz-gem-id")
    assert fake_gemini_page.submitted_text == "1. Which tract crosses?"
    assert shared.url == "https://gemini.google.com/share/quiz-123"


@pytest.mark.parametrize(
    "url",
    ["http://gemini.google.com/share/x", "https://evil.example/x", "javascript:alert(1)"],
)
def test_gateway_rejects_untrusted_share_urls(url):
    with pytest.raises(GeminiContractError):
        validate_shared_quiz_url(url)
```

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/study_generation/test_gemini_quiz_gateway.py -v
```

Expected: adapter is missing.

- [ ] **Step 3: Implement strict locator-based automation**

Open the configured stable Gem URL in the dedicated persistent profile. Use
role/name/test-id locators with bounded waits; do not use screen coordinates.
Submit quiz content once. Detect an existing job marker before resubmitting
after an ambiguous timeout. Wait for the interactive quiz/Canvas result.

- [ ] **Step 4: Implement sharing as a separate resumable stage**

Open Share, choose anyone-with-link/use access, copy or read the generated URL,
and validate scheme and host. Return distinct errors for authentication,
configured Gem missing, page contract changed, generation timeout, and sharing
failure. Do not include quiz content in exception strings.

- [ ] **Step 5: Run fixture tests**

```bash
python -m pytest tests/study_generation/test_gemini_quiz_gateway.py -v
```

Expected: all pass without network.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation/gemini_quiz.py src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/worker.py tests/study_generation
git commit -m "feat: automate Gemini Quiz Gem"
```

---

### Task 8: Course Google Documents and Exam Tabs

**Files:**
- Create: `src/oms_hub/study_generation/google_docs.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Test: `tests/study_generation/test_google_docs_gateway.py`
- Test: `tests/study_generation/test_quiz_document_sync.py`

**Interfaces:**
- Produces: `GoogleDocsGateway`.
- `ensure_course_document(subject: str) -> CourseDocumentRef`.
- `ensure_exam_tab(document: CourseDocumentRef, exam_number: int) -> ExamTabRef`.
- `sync_quiz_link(tab: ExamTabRef, lecture_number: int, url: str) -> None`.

- [ ] **Step 1: Write API-payload and idempotency tests**

```python
def test_missing_exam_tab_uses_add_document_tab_request(gateway):
    tab = gateway.ensure_exam_tab(CourseDocumentRef("doc-1", "Neuro Quizzes"), 2)
    assert gateway.docs.batch_requests == [
        {"addDocumentTab": {"tabProperties": {"title": "Exam 2"}}}
    ]
    assert tab.title == "Exam 2"


def test_sync_replaces_existing_lecture_and_preserves_order(gateway):
    gateway.seed_tab(
        "tab-1",
        "Lecture 1: old\nLecture 3: https://gemini.google.com/share/3\n",
    )
    gateway.sync_quiz_link(
        ExamTabRef("doc-1", "tab-1", 1),
        lecture_number=1,
        url="https://gemini.google.com/share/new",
    )
    assert gateway.read_managed_lines("tab-1") == [
        ("Lecture 1", "https://gemini.google.com/share/new"),
        ("Lecture 3", "https://gemini.google.com/share/3"),
    ]
```

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/study_generation/test_google_docs_gateway.py tests/study_generation/test_quiz_document_sync.py -v
```

Expected: gateway is missing.

- [ ] **Step 3: Implement official API adapter**

Create a document only when no stored ID exists or `documents.get` confirms it
was deleted. Fetch with `includeTabsContent=True`. Add a root-level tab using
`addDocumentTab`. Store returned document and tab IDs.

- [ ] **Step 4: Implement managed lecture entries**

Represent each managed line through a named range:

`oms-study-hub-quiz-lecture-<number>`

Replace an existing named range atomically. When absent, insert the line in
lecture-number order and create its named range. Apply link text style only to
the URL label. Do not use global `replaceAllText`; do not edit other tabs or
unmanaged ranges.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/study_generation/test_google_docs_gateway.py tests/study_generation/test_quiz_document_sync.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation/google_docs.py src/oms_hub/study_generation/repository.py src/oms_hub/study_generation/worker.py tests/study_generation
git commit -m "feat: sync course quiz documents"
```

---

### Task 9: Lecture Page Controls and Live Job Status

**Files:**
- Create: `src/oms_hub/web/generation_routes.py`
- Create: `src/oms_hub/web/static/lecture.js`
- Modify: `src/oms_hub/web/routes.py`
- Modify: `src/oms_hub/web/templates/lecture.html`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/app.py`
- Test: `tests/v2/test_generation_routes.py`
- Test: `tests/v2/test_lecture_generation_ui.py`
- Test: `tests/js/lecture.test.js`

**Interfaces:**
- Adds:
  - `POST /lectures/{lecture_id}/outline`
  - `POST /lectures/{lecture_id}/quiz`
  - `GET /lectures/{lecture_id}/generation-status`
- Consumes: `GenerationService` and repository output records.

- [ ] **Step 1: Write route authorization and UI tests**

```python
def test_generate_outline_queues_one_job_and_returns_no_store(client, ready_lecture):
    response = client.post(f"/lectures/{ready_lecture.id}/outline")
    assert response.status_code == 202
    assert response.json()["kind"] == "outline"
    assert response.headers["cache-control"] == "no-store"


def test_lecture_page_reuses_existing_file_card_design(client, complete_outputs):
    page = client.get(f"/lectures/{complete_outputs.lecture_id}")
    assert page.text.count("file-card") >= 4
    assert "Open Lecture Outline" in page.text
    assert "Take Lecture Quiz" in page.text
    assert "https://gemini.google.com/share/" in page.text
```

Add JavaScript tests for pending, complete, paused/reconnect, and failed/retry
states using text-only DOM rendering.

- [ ] **Step 2: Verify tests fail**

```bash
python -m pytest tests/v2/test_generation_routes.py tests/v2/test_lecture_generation_ui.py -v
node --test tests/js/lecture.test.js
```

Expected: routes, cards, and script are missing.

- [ ] **Step 3: Add server-authorized endpoints**

Routes call `GenerationService`, map missing lecture to 404, prerequisites to
409, active-job reuse to 202, and completed current output to 200. Return only
safe job/status fields. Set `Cache-Control: no-store`.

- [ ] **Step 4: Extend the lecture template in the established design**

Add **Lecture Outline (PDF)** and **Lecture Quiz** cards to
`.file-card-grid`. Reuse `.file-actions`, `.button.primary`,
`.button.secondary`, `.status-pill`, and `.field-message`. Show disabled reason,
current stage, safe failure, **Open Lecture Outline**, **Take Lecture Quiz**,
regenerate, and retry actions. External quiz links use
`target="_blank" rel="noopener noreferrer"`.

- [ ] **Step 5: Add accessible live status**

`lecture.js` posts with the existing CSRF header, disables only the selected
card while queuing, polls while active, updates via `textContent`, and announces
changes through `aria-live`. Stop polling on complete, paused, or failed.

- [ ] **Step 6: Run interface tests and regression tests**

```bash
python -m pytest tests/v2/test_generation_routes.py tests/v2/test_lecture_generation_ui.py tests/v2/test_baseline_smoke.py -v
node --test tests/js/settings.test.js tests/js/settings-generation.test.js tests/js/lecture.test.js
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/app.py src/oms_hub/web tests/v2 tests/js
git commit -m "feat: add lecture outline and quiz controls"
```

---

### Task 10: Release Packaging, Documentation, and Full Verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `scripts/build-v2-release.py`
- Create: `docs/notebooklm-gemini-nuc-rollout.md`
- Create: `tests/v2/test_notebooklm_release_package.py`
- Create: `tests/v2/test_notebooklm_acceptance_contract.py`

**Interfaces:**
- Produces: source archive containing every required integration file and no credentials/browser state.
- Produces: exact NUC update, Google setup, test, and rollback instructions.

- [ ] **Step 1: Write release safety tests**

```python
def test_source_release_includes_generation_runtime_and_excludes_google_state(tmp_path):
    archive = build_source_release(tmp_path)
    names = archive_names(archive)
    assert "src/oms_hub/study_generation/notebook.py" in names
    assert "src/oms_hub/study_generation/gemini_quiz.py" in names
    assert "src/oms_hub/study_generation/google_docs.py" in names
    forbidden = ("storage_state", "browser-profile", "client_secret", "token.json")
    assert not any(any(part in name for part in forbidden) for name in names)
```

Add acceptance-contract tests that assert the stable NotebookLM pin, Google
dependencies, Settings controls, lecture buttons, and rollout guide commands.

- [ ] **Step 2: Verify release tests fail**

```bash
python -m pytest tests/v2/test_notebooklm_release_package.py tests/v2/test_notebooklm_acceptance_contract.py -v
```

Expected: required files or guide are missing.

- [ ] **Step 3: Update configuration and documentation**

Document:

- Obsidian outline and quiz prompt path setup;
- Google OAuth desktop-client JSON creation/upload;
- **Connect Google** on the NUC or through Remote Desktop;
- Playwright Chromium installation;
- NotebookLM/Gemini/Docs connection tests;
- Quiz Gem stable URL configuration;
- one-lecture live acceptance;
- second-lecture same-exam and different-exam boundary checks;
- signed-out quiz sharing check;
- service restart recovery check; and
- rollback to `origin/main` without deleting Study Hub data.

- [ ] **Step 4: Update release builder**

Include the complete generation package and UI files in the hotfix set. Extend
the archive filter to reject `.superpowers`, browser profiles, NotebookLM
storage state, OAuth client files, refresh-token files, screenshots, trace
archives, and generated output PDFs.

- [ ] **Step 5: Run the full verification gate**

```bash
python -m pytest
node --test tests/js/*.test.js
python -m ruff check .
python -m mypy src/oms_hub
git diff --check
python scripts/build-v2-release.py
python -m pytest tests/v2/test_notebooklm_release_package.py -v
git status --short
```

Expected:

- every Python and JavaScript test passes;
- lint, typing, and whitespace checks pass;
- release archives build deterministically;
- release archive inspection contains no secrets or browser state; and
- the worktree contains only intentional tracked changes.

- [ ] **Step 6: Commit release materials**

```bash
git add .env.example README.md scripts/build-v2-release.py docs/notebooklm-gemini-nuc-rollout.md tests/v2
git commit -m "docs: add NotebookLM Gemini NUC rollout"
```

- [ ] **Step 7: Review the completed branch**

Compare against `origin/main`, verify every spec requirement maps to a passing
test or live acceptance step, and ensure no `.superpowers` session files are
tracked.

- [ ] **Step 8: Push the feature branch**

```bash
git push -u origin codex/notebooklm-gemini-workflow
```

Do not create or merge a pull request. Hand off the branch name, verification
results, and the NUC rollout guide for user testing.
