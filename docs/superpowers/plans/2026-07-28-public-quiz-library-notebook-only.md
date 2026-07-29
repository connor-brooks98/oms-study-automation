# Public Quiz Library and Notebook-Only Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Google Docs quiz indexes with one shared Study Hub quiz-and-outline library, show browser-local completion status, and reduce Google setup to the Gemini Notebook browser login only.

**Architecture:** Published quizzes remain server-owned, token-addressed Study Hub resources, while a new `/public/quizzes` page groups them by course, exam, and lecture. The existing per-quiz `localStorage` records remain the only user-progress store; the library reads those records to show `Not started`, `In progress`, or `Completed` without creating user accounts or server-side completion rows. The Google Docs gateway, OAuth client upload, OAuth refresh token, and Docs generation stage are retired; Gemini Notebook authentication continues through the existing `NotebookCLIAuth` browser flow and exact worker storage file.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Jinja2, vanilla JavaScript, browser `localStorage`, `notebooklm-py==0.7.3`, Playwright, pytest, Node test runner, Ruff, mypy.

## Global Constraints

- Work only on branch `codex/native-study-hub-quizzes`; do not merge to `main`.
- Preserve every existing `/public/quizzes/{token}` URL so previously shared links continue working.
- Use `/public/quizzes` as the single shareable library URL.
- List only lectures with a current `PublishedQuizModel`; do not expose private lecture files, private lecture pages, unpublished quizzes, prompts, Notebook IDs, or answer keys.
- When a listed lecture has a current outline, expose only that verified PDF through a quiz-token-scoped public route. Do not expose numeric outline IDs, filesystem paths, slides, or transcripts.
- Preserve answer-by-answer server grading so the correct answer and rationale are returned only after an answer submission.
- Store user progress only in browser `localStorage`; do not add user tables, completion APIs, analytics, identity storage, or server-side progress logs.
- A quiz regeneration increments its version; progress from an older version must display as `Not started`.
- Clearing browser site data or choosing `Reset quiz progress` clears progress for that browser only.
- Keep the current Course → Exam → Lecture accordion hierarchy and established Study Hub visual language.
- Do not publish lecture summaries beyond the existing generated outline PDF in this release. Keep the library data/view boundary extensible so another summary format can be added without changing quiz URLs.
- Gemini Notebook is the only Google surface. The OAuth desktop client JSON and Google OAuth refresh token are no longer required.
- Delete only the exact retired OAuth file and exact retired secret keys; preserve `notebooklm-storage.json`, the Notebook browser profile, all lecture data, and unrelated credentials.
- Leave legacy Google Docs database columns/tables physically present but unused so the rollout is additive and rollback-safe.
- Recover jobs already checkpointed at legacy stage `docs` by completing native publication without calling Google Docs or republishing the quiz.
- Follow red-green TDD for every behavior change and commit after each independently reviewable task.

---

## File Structure

### New files

- `src/oms_hub/study_generation/notebook_connection.py` — Notebook-only status, live test, interactive login, invalidation, and exact retirement of old Docs OAuth credentials.
- `src/oms_hub/web/templates/public_quiz_library.html` — standalone shared Course → Exam → Lecture quiz library.
- `src/oms_hub/web/static/public_quiz_library.js` — accordion behavior, local progress classification, and browser-local reset.
- `src/oms_hub/web/static/public_quiz_library.css` — public library styling aligned with the private dashboard.
- `tests/study_generation/test_notebook_connection.py` — Notebook-only connection and credential-retirement tests.
- `tests/js/public_quiz_library.test.js` — progress classification and storage-key tests.

### Modified files

- `src/oms_hub/app.py` — wire the Notebook-only connection, remove the Docs gateway, include the library root in the public access boundary, and retire old OAuth credentials.
- `src/oms_hub/study_generation/domain.py` — add the native catalog checkpoint and remove Docs-specific data from the active quiz record.
- `src/oms_hub/study_generation/repository.py` — persist Notebook-only status, list published quizzes, and record quiz output without Docs state.
- `src/oms_hub/study_generation/service.py` — require live Gemini Notebook access rather than a multi-surface Google connection.
- `src/oms_hub/study_generation/worker.py` — complete quiz publication directly after native publishing and recover legacy `docs` jobs.
- `src/oms_hub/web/generation_routes.py` — replace `/settings/google/*` with `/settings/notebook/*` and remove OAuth upload.
- `src/oms_hub/web/settings_routes.py` — provide Notebook-only status to the Settings template.
- `src/oms_hub/web/public_quiz_routes.py` — serve the library, verified token-scoped outline PDFs, its assets, and grouped published quiz rows.
- `src/oms_hub/web/artifact_routes.py` — share the current outline PDF validation/response helper with the public token-scoped route.
- `src/oms_hub/web/templates/settings.html` — replace Google Workspace/Docs/OAuth controls with a Gemini Notebook card.
- `src/oms_hub/web/templates/public_quiz.html` — add a link back to the shared quiz library.
- `src/oms_hub/web/static/settings.js` — poll and render one Notebook connection state.
- `src/oms_hub/web/static/public_quiz.js` — export the stable progress-key helper used by tests and keep completion persistence explicit.
- `src/oms_hub/web/static/app.css` or the focused public CSS file — no private route dependencies; only shared design tokens may be mirrored.
- `pyproject.toml` — remove Google Docs API/OAuth dependencies.
- `scripts/build-v2-release.py` — include the public library assets and Notebook connection module; remove Docs runtime files.
- `docs/native-quizzes-nuc-rollout.md` — document the one-link library and Notebook-only setup.
- Tests under `tests/study_generation`, `tests/v2`, and `tests/js` — replace multi-surface/OAuth/Docs assertions with the new contracts.

### Deleted files

- `src/oms_hub/study_generation/google_connection.py`
- `src/oms_hub/study_generation/google_docs.py`
- `tests/study_generation/test_google_connection.py`
- `tests/study_generation/test_google_docs.py`
- `tests/v2/test_google_settings_routes.py`

Legacy SQLAlchemy models `GoogleConnectionModel`, `CourseQuizDocumentModel`, `ExamQuizTabModel`, and column `QuizOutputModel.docs_synced` remain mapped for database compatibility but are not consumed by active runtime behavior.

---

### Task 1: Build the Notebook-Only Connection Boundary

**Files:**

- Create: `src/oms_hub/study_generation/notebook_connection.py`
- Create: `tests/study_generation/test_notebook_connection.py`
- Modify: `src/oms_hub/study_generation/repository.py`

**Interfaces:**

- Consumes: `NotebookCLIAuth.login() -> None`, `NotebookCLIAuth.check() -> NotebookAuthCheck`, `SecretStore.delete(key: str) -> None`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class NotebookConnectionStatus:
    state: str
    message: str | None = None


class NotebookConnectionService:
    def status(self) -> NotebookConnectionStatus: ...
    def test(self) -> NotebookConnectionStatus: ...
    def require_live(self) -> NotebookConnectionStatus: ...
    def start_interactive(self) -> NotebookConnectionStatus: ...
    def invalidate(self, message: str) -> NotebookConnectionStatus: ...


def retire_docs_oauth(data_dir: Path, secrets: SecretStore) -> None: ...
```

- Persists active state through:

```python
GenerationRepository.save_notebook_status(
    *,
    state: str,
    diagnostic: str | None,
    tested_at: str,
) -> None

GenerationRepository.notebook_status() -> GoogleConnectionModel | None
```

The repository continues using the existing `google_connection` table but writes `notebook_state=state`, `docs_state="retired"`, `gemini_state="unused"`, and clears `account_email`.

- [ ] **Step 1: Write failing Notebook-only connection tests**

```python
class NotebookAuth:
    def __init__(self, connected: bool):
        self.connected = connected
        self.login_calls = 0

    def login(self) -> None:
        self.login_calls += 1

    def check(self) -> NotebookAuthCheck:
        return NotebookAuthCheck(
            self.connected,
            None if self.connected else "Gemini Notebook login is required.",
        )


def test_connection_has_one_notebook_surface(tmp_path):
    service = connection_service(tmp_path, NotebookAuth(True))

    assert service.test() == NotebookConnectionStatus("connected")


def test_interactive_connection_runs_login_then_live_check(tmp_path):
    auth = NotebookAuth(True)
    service = connection_service(tmp_path, auth)

    assert service.start_interactive().state == "connected"
    assert auth.login_calls == 1


def test_live_requirement_fails_without_notebook_session(tmp_path):
    service = connection_service(tmp_path, NotebookAuth(False))

    with pytest.raises(RuntimeError, match="Gemini Notebook"):
        service.require_live()
```

- [ ] **Step 2: Run the connection tests and verify the old multi-surface implementation fails**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_notebook_connection.py -v
```

Expected: collection/import failure because `notebook_connection.py` and `NotebookConnectionStatus` do not exist.

- [ ] **Step 3: Implement the single-surface service**

Implement a process lock identical to the existing connection lock. `start_interactive()` must persist `connecting`, call `auth.login()`, then call `test()`. `test()` must call `auth.check()` and persist only `connected` or `failed`. `require_live()` must call `test()` and raise:

```python
raise RuntimeError(
    status.message or "Connect Gemini Notebook in Settings before generating"
)
```

`invalidate()` must persist `failed` without exposing subprocess output or cookie values.

- [ ] **Step 4: Add exact OAuth retirement tests**

```python
def test_retirement_deletes_only_docs_oauth_material(tmp_path):
    google = tmp_path / "google"
    google.mkdir()
    (google / "oauth-client.json").write_text("secret", encoding="utf-8")
    notebook = google / "notebooklm-storage.json"
    notebook.write_text('{"cookies":[]}', encoding="utf-8")
    secrets = MemorySecrets(
        {
            "google-oauth-refresh-token": "refresh",
            "google-connected-email": "student@example.com",
            "openai-api-key": "keep",
        }
    )

    retire_docs_oauth(tmp_path, secrets)

    assert not (google / "oauth-client.json").exists()
    assert notebook.exists()
    assert secrets.get("google-oauth-refresh-token") is None
    assert secrets.get("google-connected-email") is None
    assert secrets.get("openai-api-key") == "keep"
```

- [ ] **Step 5: Implement exact credential retirement**

Use only these constants:

```python
RETIRED_SECRET_KEYS = (
    "google-oauth-refresh-token",
    "google-connected-email",
)
RETIRED_OAUTH_FILE = Path("google") / "oauth-client.json"
```

Resolve the target below `data_dir`, unlink only that exact regular file with `missing_ok=True`, and call `secrets.delete()` only for the two exact keys. Do not enumerate directories, use globs, or touch `notebooklm-storage.json`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/pytest \
  tests/study_generation/test_notebook_auth.py \
  tests/study_generation/test_notebook_login_compat.py \
  tests/study_generation/test_notebook_connection.py -v
```

Expected: all focused Notebook connection tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/oms_hub/study_generation/notebook_connection.py \
  src/oms_hub/study_generation/repository.py \
  tests/study_generation/test_notebook_connection.py
git commit -m "refactor: reduce Google connection to Gemini Notebook"
```

---

### Task 2: Remove Docs/OAuth from Settings and Application Wiring

**Files:**

- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Modify: `tests/js/settings.test.js`
- Create: `tests/v2/test_notebook_settings_routes.py`
- Delete after replacement: `tests/v2/test_google_settings_routes.py`

**Interfaces:**

- Consumes: `NotebookConnectionService` from Task 1.
- Produces these owner-only endpoints:

```text
GET  /settings/notebook/status
POST /settings/notebook/test
POST /settings/notebook/connect
```

- All status responses use:

```json
{
  "state": "connected",
  "message": null
}
```

- The asynchronous connect response uses HTTP 202 and:

```json
{
  "state": "connecting",
  "message": "Complete Gemini Notebook sign-in in the browser window."
}
```

- [ ] **Step 1: Write failing Settings route tests**

```python
class FakeNotebookConnection:
    def status(self):
        return NotebookConnectionStatus("connected")

    def test(self):
        return self.status()

    def start_interactive(self):
        return self.status()


def test_settings_shows_notebook_only_connection_card(tmp_path):
    page = TestClient(configured_app(tmp_path)).get("/settings")

    assert "Gemini Notebook" in page.text
    assert "Connect Gemini Notebook" in page.text
    assert "Google Docs" not in page.text
    assert "OAuth client JSON" not in page.text


def test_notebook_status_is_small_and_secret_safe(tmp_path):
    app = configured_app(tmp_path)
    app.state.notebook_connection = FakeNotebookConnection()

    response = TestClient(app).get("/settings/notebook/status")

    assert response.json() == {"state": "connected", "message": None}
    assert response.headers["cache-control"] == "no-store"


def test_retired_google_oauth_route_is_absent(tmp_path):
    response = TestClient(configured_app(tmp_path)).post(
        "/settings/google/oauth-client",
        files={"client_file": ("client.json", "{}")},
    )

    assert response.status_code == 404
```

- [ ] **Step 2: Run route/UI tests and verify failure**

Run:

```bash
.venv/bin/pytest tests/v2/test_notebook_settings_routes.py -v
node --test tests/js/settings.test.js
```

Expected: Python tests fail because the Notebook endpoints/card are absent; the existing JavaScript test still expects `notebook` and `docs` surfaces.

- [ ] **Step 3: Replace route payloads and application state**

In `create_app()`, add the Notebook-only connection alongside the old internal Google/Docs objects so this task remains regression-safe until Task 3 removes the generation dependency:

```python
notebook_connection = NotebookConnectionService(
    app.state.generation_repository,
    app.state.notebook_auth,
)
app.state.notebook_connection = notebook_connection
```

Remove the old public Settings routes and rename the settings router variable to `notebook_router`. Do not delete `app.state.google_connection`, `OAuthGoogleDocsGateway`, or retired credential material in this task; Task 3 removes them atomically with the last generation consumer.

The connect endpoint must retain the daemon-thread behavior:

```python
threading.Thread(
    target=_notebook(request).start_interactive,
    name="oms-notebook-connect",
    daemon=True,
).start()
```

- [ ] **Step 4: Replace the Settings card**

Render one status badge and these controls:

```html
<section class="settings-card" data-notebook-card>
  <p class="provider-kicker">Gemini Notebook</p>
  <h2>Lecture generation source</h2>
  <p>
    Connect the Google account Study Hub uses to create exam notebooks,
    upload lecture sources, and generate outlines and quizzes.
  </p>
  <span class="status-pill" data-notebook-badge>Not connected</span>
  <p class="field-message" data-notebook-status></p>
  <button type="button" data-notebook-connect>Connect Gemini Notebook</button>
  <button type="button" data-notebook-test>Test connection</button>
</section>
```

Do not render a file input, account email, Docs status, OAuth client state, or OAuth instructions.

- [ ] **Step 5: Replace the JavaScript renderer**

Export and test:

```javascript
const notebookPresentation = (state) => ({
  disconnected: { label: "Not connected", className: "is-idle" },
  connecting: { label: "Connecting…", className: "is-testing" },
  connected: { label: "Connected", className: "is-connected" },
  failed: { label: "Connection failed", className: "is-failed" },
}[state] || { label: "Not connected", className: "is-idle" });
```

Poll only while `state === "connecting"`. Remove all `FormData`, OAuth upload, `data-google-surface`, `oauth_client_configured`, and Docs rendering logic.

- [ ] **Step 6: Run focused Settings tests**

Run:

```bash
.venv/bin/pytest \
  tests/v2/test_notebook_settings_routes.py \
  tests/v2/test_generation_settings.py \
  tests/v2/test_llm_settings_ui.py -v
node --test tests/js/settings.test.js
```

Expected: all Settings tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/oms_hub/app.py \
  src/oms_hub/web/generation_routes.py \
  src/oms_hub/web/settings_routes.py \
  src/oms_hub/web/templates/settings.html \
  src/oms_hub/web/static/settings.js \
  tests/js/settings.test.js \
  tests/v2/test_notebook_settings_routes.py
git rm tests/v2/test_google_settings_routes.py
git commit -m "feat: simplify settings to Gemini Notebook login"
```

---

### Task 3: Remove Google Docs from the Generation Pipeline

**Files:**

- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/study_generation/service.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Modify: `src/oms_hub/app.py`
- Modify: `tests/study_generation/test_service.py`
- Modify: `tests/study_generation/test_worker.py`
- Modify: `tests/study_generation/test_repository.py`
- Modify: `tests/v2/test_generation_routes.py`
- Delete: `src/oms_hub/study_generation/google_docs.py`
- Delete: `src/oms_hub/study_generation/google_connection.py`
- Delete: `tests/study_generation/test_google_docs.py`
- Delete: `tests/study_generation/test_google_connection.py`

**Interfaces:**

- `GenerationWorker.__init__` becomes:

```python
def __init__(
    self,
    repository: Any,
    catalog: Any,
    ingestion: Any,
    prompts: Any,
    notebook: Any,
    outline: Any,
    publisher: Any,
    notebook_connection: Any | None = None,
) -> None: ...
```

- `GenerationRepository.record_quiz` becomes:

```python
def record_quiz(
    self,
    lecture_id: int,
    job_id: str,
    url: str,
) -> QuizRecord: ...
```

- `QuizRecord` drops `docs_synced`.
- Add `GenerationStage.CATALOG = "catalog"`.
- Keep `GenerationStage.DOCS = "docs"` as a legacy readable value for already persisted jobs; no new job advances to it.

- [ ] **Step 1: Rewrite worker tests around direct publication**

```python
def test_worker_publishes_and_completes_without_docs(tmp_path):
    worker, repository, publisher = quiz_worker(
        tmp_path,
        stage=GenerationStage.QUIZ_VALIDATE,
        notebook_answer=QUIZ_JSON,
    )

    assert worker.run_once()
    assert len(publisher.calls) == 1
    assert repository.quiz == (1, "job-1", QUIZ_URL)
    assert repository.current.state is GenerationState.COMPLETE
    assert [stage for stage, _ in repository.advances] == [
        GenerationStage.PUBLISH,
        GenerationStage.CATALOG,
    ]


def test_legacy_docs_checkpoint_completes_without_republishing(tmp_path):
    worker, repository, publisher = quiz_worker(
        tmp_path,
        stage=GenerationStage.DOCS,
        notebook_answer=QUIZ_JSON,
        quiz_url=QUIZ_URL,
        publisher=Publisher(fail_if_called=True),
    )

    assert worker.run_once()
    assert publisher.calls == []
    assert repository.quiz == (1, "job-1", QUIZ_URL)
    assert repository.current.state is GenerationState.COMPLETE
```

- [ ] **Step 2: Run worker tests and verify the Docs calls fail the new assertions**

Run:

```bash
.venv/bin/pytest tests/study_generation/test_worker.py -v
```

Expected: failure because the worker constructor still requires `docs`, advances to `DOCS`, and calls `sync_quiz_link`.

- [ ] **Step 3: Implement the native-only checkpoint flow**

Use this quiz tail:

```python
if job.quiz_url:
    quiz_url = job.quiz_url
else:
    quiz = parse_native_quiz(answer.text)
    job = self.repository.advance(job.id, GenerationStage.PUBLISH)
    quiz_url = self.publisher.publish(job.lecture_id, job.id, quiz)
    job = self.repository.advance(
        job.id,
        GenerationStage.CATALOG,
        quiz_url=quiz_url,
    )

self.repository.record_quiz(job.lecture_id, job.id, quiz_url)
self.repository.complete(job.id)
```

On `NotebookAuthenticationError`, call:

```python
self.notebook_connection.invalidate(str(error))
```

Remove `GoogleDocsAuthenticationError`, `GoogleSurface`, and all Docs retry/error branches. Preserve the current safe error sanitization and Notebook auth pause behavior.

- [ ] **Step 4: Update service prerequisites**

Rename `self.google` to `self.notebook_connection`. Require `require_live()` and use these messages:

```text
Reconnect Gemini Notebook in Settings before generating
Connect Gemini Notebook in Settings before generating
```

Do not require an OAuth client file, Docs state, account email, or matching multi-surface account.

- [ ] **Step 5: Remove Docs-specific repository behavior**

Stop importing and exposing active methods for `CourseQuizDocumentModel` and `ExamQuizTabModel`. Leave their model definitions intact. When recording a quiz, leave the legacy `QuizOutputModel.docs_synced` column at its default `False`; exclude it from `QuizRecord`.

- [ ] **Step 6: Finish application wiring and retire old credentials**

Remove `app.state.google_connection` and `OAuthGoogleDocsGateway` from `create_app()`. Pass `app.state.notebook_connection` to both `GenerationService` and `GenerationWorker`, then call:

```python
retire_docs_oauth(resolved.data_dir, app.state.secrets)
```

Delete the old multi-surface connection and Docs modules only after imports and application wiring no longer reference them.

- [ ] **Step 7: Run focused generation tests**

Run:

```bash
.venv/bin/pytest \
  tests/study_generation/test_service.py \
  tests/study_generation/test_worker.py \
  tests/study_generation/test_repository.py \
  tests/v2/test_generation_routes.py \
  tests/v2/test_generation_restart_recovery.py -v
```

Expected: all focused generation and recovery tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  src/oms_hub/app.py \
  src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/repository.py \
  src/oms_hub/study_generation/service.py \
  src/oms_hub/study_generation/worker.py \
  tests/study_generation/test_service.py \
  tests/study_generation/test_worker.py \
  tests/study_generation/test_repository.py \
  tests/v2/test_generation_routes.py
git rm \
  src/oms_hub/study_generation/google_connection.py \
  src/oms_hub/study_generation/google_docs.py \
  tests/study_generation/test_google_connection.py \
  tests/study_generation/test_google_docs.py
git commit -m "refactor: publish quizzes without Google Docs"
```

---

### Task 4: Add the Shared Course → Exam → Lecture Quiz Library

**Files:**

- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/artifact_routes.py`
- Modify: `src/oms_hub/app.py`
- Create: `src/oms_hub/web/templates/public_quiz_library.html`
- Create: `src/oms_hub/web/static/public_quiz_library.js`
- Create: `src/oms_hub/web/static/public_quiz_library.css`
- Modify: `src/oms_hub/web/templates/public_quiz.html`
- Modify: `tests/study_generation/test_repository.py`
- Modify: `tests/v2/test_public_quiz_routes.py`

**Interfaces:**

- Add:

```python
def GenerationRepository.published_quizzes(
    self,
) -> tuple[PublishedQuizRecord, ...]: ...
```

- The route builds this Jinja context shape:

```python
{
    "courses": (
        {
            "name": "Neuro",
            "hue": 290,
            "quiz_count": 2,
            "exams": (
                {
                    "number": 1,
                    "quizzes": (
                        {
                            "token": "a" * 64,
                            "version": 1,
                            "title": "Lecture 1 Practice Quiz",
                            "lecture_number": 1,
                            "topic": "General CNS Pathology",
                            "url": "/public/quizzes/" + "a" * 64,
                            "outline_url": "/public/quizzes/" + "a" * 64 + "/outline",
                        },
                    ),
                },
            ),
        },
    ),
}
```

- [ ] **Step 1: Add failing repository list tests**

```python
def test_published_quizzes_returns_one_current_record_per_lecture(tmp_path):
    repository, first_lecture, second_lecture = published_repository(tmp_path)
    first = repository.publish_quiz(first_lecture, "job-1", quiz("First"))
    repository.publish_quiz(first_lecture, "job-2", quiz("Regenerated"))
    second = repository.publish_quiz(second_lecture, "job-3", quiz("Second"))

    listed = repository.published_quizzes()

    assert [(item.token, item.title, item.version) for item in listed] == [
        (first.token, "Regenerated", 2),
        (second.token, "Second", 1),
    ]
```

- [ ] **Step 2: Add failing public library route tests**

```python
def test_public_library_groups_only_published_quizzes(tmp_path):
    app, published = app_with_published_and_unpublished_lectures(tmp_path)

    response = TestClient(app).get("/public/quizzes")

    assert response.status_code == 200
    assert "Course quiz library" in response.text
    assert "Neuro" in response.text
    assert "Exam 1" in response.text
    assert "Lecture 1" in response.text
    assert published.token in response.text
    assert "Unpublished lecture" not in response.text


def test_public_library_root_uses_same_access_boundary_as_quiz_pages(tmp_path):
    app = public_hostname_app(tmp_path)

    response = TestClient(
        app,
        base_url="https://study.example.com",
    ).get("/public/quizzes", headers={"host": "study.example.com"})

    assert response.status_code == 200


def test_public_outline_uses_quiz_token_and_returns_current_verified_pdf(tmp_path):
    app, published = app_with_published_quiz_and_outline(tmp_path)

    response = TestClient(app).get(
        f"/public/quizzes/{published.token}/outline",
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_public_outline_does_not_expose_other_lecture_artifacts(tmp_path):
    app, published = app_with_published_quiz_without_outline(tmp_path)

    response = TestClient(app).get(
        f"/public/quizzes/{published.token}/outline",
    )

    assert response.status_code == 404
```

- [ ] **Step 3: Run the tests and verify 404/access failures**

Run:

```bash
.venv/bin/pytest \
  tests/study_generation/test_repository.py \
  tests/v2/test_public_quiz_routes.py -v
```

Expected: failure because `published_quizzes()` and the `/public/quizzes` root do not exist; on a public hostname the exact root is not included in the current bypass predicate.

- [ ] **Step 4: Implement the published quiz query and grouping**

Sort records deterministically by:

```python
(
    lecture.subject.casefold(),
    lecture.exam_number,
    lecture.lecture_number,
)
```

Skip a published record if its lecture has been removed. Do not include the quiz payload or answer key in the library context. Resolve the lecture's current outline and include `outline_url` only when one exists.

- [ ] **Step 5: Implement the public library page**

The standalone template must include:

```html
<main class="public-library" data-quiz-library>
  <header class="library-heading">
    <p class="eyebrow">Study Hub</p>
    <h1>Course quiz library</h1>
    <p>Choose a course, exam, and lecture quiz.</p>
    <button type="button" data-reset-progress>Reset quiz progress</button>
  </header>
  <!-- Course and exam disclosure buttons -->
  <!-- Lecture quiz links with data-token and data-version -->
</main>
```

Each lecture link must contain:

```html
<a
  href="/public/quizzes/{{ row.token }}"
  data-quiz-row
  data-quiz-token="{{ row.token }}"
  data-quiz-version="{{ row.version }}"
>
  <span>Lecture {{ row.lecture_number }}: {{ row.topic }}</span>
  <span data-quiz-progress>Not started</span>
</a>
```

When `row.outline_url` exists, render a separate `Lecture Outline` button beside the quiz link. The button must open the same current PDF used by the private lecture page through `/public/quizzes/{token}/outline`; it must not link to the private numeric artifact route.

Use buttons with `aria-expanded`/`aria-controls` for course and exam accordions. Open the first course and first exam by default. Render a friendly empty state when no quiz is published.

- [ ] **Step 6: Add disclosure behavior and serve both library assets**

Implement the same `setExpanded(button, expanded)` behavior as the private dashboard with the public-specific session key prefix `study-hub:public-library:disclosure:`. Storage failures must leave the server-rendered disclosure defaults working.

Serve:

```text
GET /public/quizzes/assets/library.js
GET /public/quizzes/assets/library.css
```

Return `Cache-Control: public, max-age=3600` and CSP-compatible JavaScript/CSS MIME types. Extend the route test to assert both assets return HTTP 200.

Refactor the existing private outline response validation into a shared helper and reuse it for the public token-scoped outline route. Preserve the study-root containment, file existence, SHA-256, and valid-PDF checks. Return `404` for an unknown token or missing outline and `409` when the recorded artifact has changed or is invalid.

- [ ] **Step 7: Add the library to the public access predicate and quiz page**

Change the middleware predicate to:

```python
is_public_quiz = (
    request.url.path == "/public/quizzes"
    or request.url.path.startswith("/public/quizzes/")
)
```

Add a visible `Back to quiz library` link to `public_quiz.html`. Keep existing token routes and asset paths unchanged.

- [ ] **Step 8: Run focused route tests**

Run:

```bash
.venv/bin/pytest tests/v2/test_public_quiz_routes.py -v
```

Expected: all public library, token route, answer-key isolation, CSRF, and access-boundary tests pass.

- [ ] **Step 9: Commit**

```bash
git add \
  src/oms_hub/app.py \
  src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/repository.py \
  src/oms_hub/web/artifact_routes.py \
  src/oms_hub/web/public_quiz_routes.py \
  src/oms_hub/web/templates/public_quiz_library.html \
  src/oms_hub/web/templates/public_quiz.html \
  src/oms_hub/web/static/public_quiz_library.js \
  src/oms_hub/web/static/public_quiz_library.css \
  tests/study_generation/test_repository.py \
  tests/v2/test_public_quiz_routes.py
git commit -m "feat: add shared course quiz library"
```

---

### Task 5: Show Browser-Local Quiz Progress in the Library

**Files:**

- Modify: `src/oms_hub/web/static/public_quiz_library.js`
- Modify: `src/oms_hub/web/static/public_quiz.js`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/templates/public_quiz_library.html`
- Create: `tests/js/public_quiz_library.test.js`
- Modify: `tests/js/public_quiz.test.js`
- Modify: `tests/v2/test_public_quiz_routes.py`

**Interfaces:**

- Preserve the existing storage key:

```javascript
`oms-study-hub-quiz:${token}:v${version}`
```

- Export:

```javascript
function quizStorageKey(token, version) {}
function progressStatus(serialized, token, version) {}
function resetQuizProgress(storage) {}
```

- `progressStatus` returns exactly one of:

```javascript
{ state: "not-started", label: "Not started" }
{ state: "in-progress", label: "In progress" }
{ state: "completed", label: "Completed" }
```

- [ ] **Step 1: Write failing JavaScript tests**

```javascript
test("library recognizes completed current-version progress", () => {
  const saved = JSON.stringify({
    token,
    version: 2,
    currentIndex: 20,
    questions: { q1: { submitted: true } },
  });

  assert.deepEqual(library.progressStatus(saved, token, 2), {
    state: "completed",
    label: "Completed",
  });
});

test("library treats old quiz versions as not started", () => {
  const saved = JSON.stringify({
    token,
    version: 1,
    currentIndex: 20,
    questions: { q1: { submitted: true } },
  });

  assert.equal(
    library.progressStatus(saved, token, 2).state,
    "not-started",
  );
});

test("reset removes only Study Hub quiz progress", () => {
  const storage = memoryStorage({
    [`oms-study-hub-quiz:${token}:v2`]: "{}",
    "unrelated-setting": "keep",
  });

  library.resetQuizProgress(storage);

  assert.equal(storage.getItem(`oms-study-hub-quiz:${token}:v2`), null);
  assert.equal(storage.getItem("unrelated-setting"), "keep");
});
```

- [ ] **Step 2: Run JavaScript tests and verify failure**

Run:

```bash
node --test \
  tests/js/public_quiz.test.js \
  tests/js/public_quiz_library.test.js
```

Expected: missing-export failures because the disclosure-only library script does not yet implement progress classification or reset.

- [ ] **Step 3: Implement strict progress classification**

Classification rules:

```javascript
if (!serialized) return NOT_STARTED;
const saved = JSON.parse(serialized);
if (saved.token !== token || saved.version !== version) return NOT_STARTED;
if (!Number.isInteger(saved.currentIndex) || saved.currentIndex < 0) {
  return NOT_STARTED;
}
const questions = Object.values(saved.questions || {});
if (questions.length === 0) return NOT_STARTED;
if (saved.currentIndex >= questions.length) return COMPLETED;
const interacted = (
  saved.currentIndex > 0
  || questions.some((question) => (
    question.submitted === true
    || question.selectedChoiceId
    || (question.eliminatedChoiceIds || []).length > 0
    || (question.highlights || []).length > 0
  ))
);
return interacted ? IN_PROGRESS : NOT_STARTED;
```

Catch malformed JSON and return `Not started`. Never inject saved values into HTML.

- [ ] **Step 4: Wire progress into the library**

On initialization:

1. Restore course/exam disclosure state from `sessionStorage` using a public-library-specific prefix.
2. For every `[data-quiz-row]`, compute the stable key from token and version.
3. Read only that key from `localStorage`.
4. Set the status text and `data-progress-state`.
5. Change the link action copy to `Start quiz`, `Resume quiz`, or `Review quiz`.

The reset button must call `window.confirm("Reset quiz progress stored in this browser?")`. On confirmation, delete only keys beginning with `oms-study-hub-quiz:` and rerender all rows without reloading the page.

- [ ] **Step 5: Keep the player and library key contract synchronized**

Export `storageKey` from `public_quiz.js` as `quizStorageKey` and add:

```javascript
test("player storage key matches public library contract", () => {
  assert.equal(
    quiz.quizStorageKey(content.token, content.version),
    `oms-study-hub-quiz:${content.token}:v${content.version}`,
  );
});
```

The player continues storing full local progress after every selection, elimination, highlight, submission, and question advance. `Start Over` continues clearing only the active quiz/version key.

- [ ] **Step 6: Verify the existing public asset boundary**

Extend the route test from Task 4 to confirm `library.js` and `library.css` remain available at their `/public/quizzes/assets/*` URLs after progress behavior is added. No new authenticated `/static/*` dependency may be introduced.

- [ ] **Step 7: Run JavaScript and route tests**

Run:

```bash
node --test tests/js/public_quiz.test.js tests/js/public_quiz_library.test.js
.venv/bin/pytest tests/v2/test_public_quiz_routes.py -v
```

Expected: all progress, reset, asset, and route tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  src/oms_hub/web/public_quiz_routes.py \
  src/oms_hub/web/static/public_quiz.js \
  src/oms_hub/web/static/public_quiz_library.js \
  src/oms_hub/web/static/public_quiz_library.css \
  src/oms_hub/web/templates/public_quiz_library.html \
  tests/js/public_quiz.test.js \
  tests/js/public_quiz_library.test.js \
  tests/v2/test_public_quiz_routes.py
git commit -m "feat: show browser-local quiz completion"
```

---

### Task 6: Remove Docs Dependencies and Update Release Packaging

**Files:**

- Modify: `pyproject.toml`
- Modify: `scripts/build-v2-release.py`
- Modify: `tests/v2/test_notebooklm_release_package.py`
- Modify: `tests/v2/test_notebooklm_acceptance_contract.py`
- Modify: `docs/native-quizzes-nuc-rollout.md`
- Modify: `README.md`

**Interfaces:**

- Remove:

```toml
"google-api-python-client>=2.180,<3",
"google-auth-oauthlib>=1.2,<2",
```

- Keep:

```toml
"notebooklm-py==0.7.3",
"playwright>=1.55,<2",
```

- [ ] **Step 1: Write failing release-package expectations**

Update tests to assert:

```python
assert "src/oms_hub/study_generation/notebook_connection.py" in names
assert "src/oms_hub/web/templates/public_quiz_library.html" in names
assert "src/oms_hub/web/static/public_quiz_library.js" in names
assert "src/oms_hub/web/static/public_quiz_library.css" in names
assert "src/oms_hub/study_generation/google_docs.py" not in names
assert "src/oms_hub/study_generation/google_connection.py" not in names
```

Also assert that source and hotfix archives exclude filenames containing:

```python
("storage_state", "notebooklm-storage", "browser-profile", "oauth-client", "token.json")
```

- [ ] **Step 2: Run release tests and verify failure**

Run:

```bash
.venv/bin/pytest \
  tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py -v
```

Expected: failure because the builder still names Docs files and does not explicitly assert the public library assets.

- [ ] **Step 3: Update dependency and archive manifests**

Remove the two Google API/OAuth packages. Replace the old runtime entries in `HOTFIX_FILES` with the new Notebook connection and library files. Retain the secret-file exclusion tests.

- [ ] **Step 4: Update the NUC rollout**

Document:

1. Stop `OMS Study Hub V2` before editable reinstall so Windows releases `oms-hub.exe`.
2. Pull branch `codex/native-study-hub-quizzes`.
3. Install with `.\.venv\Scripts\python.exe -m pip install -e .`.
4. Start the scheduled task and verify `/health`.
5. Open Settings and use `Connect Gemini Notebook`; no OAuth JSON is selected.
6. Confirm `/public/quizzes` shows the shared accordion library.
7. Complete one quiz, return to the library, and confirm `Completed`.
8. Confirm another browser shows `Not started`.
9. Confirm `Reset quiz progress` changes local statuses to `Not started`.
10. Confirm an old direct quiz link still opens.

State explicitly that startup deletes only the retired OAuth client file and retired Docs refresh/email keys. Note that this is intentional and that rollback would require re-uploading the OAuth client only if rolling back to the obsolete Docs version.

- [ ] **Step 5: Run packaging tests**

Run:

```bash
.venv/bin/pytest \
  tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py -v
```

Expected: all release and acceptance-contract tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  pyproject.toml \
  scripts/build-v2-release.py \
  tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py \
  docs/native-quizzes-nuc-rollout.md \
  README.md
git commit -m "docs: release Notebook-only shared quiz library"
```

---

### Task 7: Full Verification and Branch Delivery

**Files:**

- Verify all changed files.
- Update only failing tests or documentation directly related to this release.

**Interfaces:**

- Produces a clean branch pushed to `origin/codex/native-study-hub-quizzes`.
- Produces the exact commit SHA and NUC update command for user testing.

- [ ] **Step 1: Scan for retired runtime references**

Run:

```bash
rg -n \
  "OAuthGoogleDocs|GoogleDocsGateway|google-auth-oauthlib|google-api-python-client|OAuth client JSON|Google Docs" \
  src tests pyproject.toml scripts README.md docs/native-quizzes-nuc-rollout.md
```

Expected: no active runtime, test, dependency, or rollout references. Historical design/plan documents outside the rollout file remain unchanged.

- [ ] **Step 2: Run the complete Python suite**

Run:

```bash
.venv/bin/pytest
```

Expected: all Python tests pass with zero warnings promoted to errors.

- [ ] **Step 3: Run the complete JavaScript suite**

Run:

```bash
node --test tests/js/*.test.js
```

Expected: all JavaScript tests pass.

- [ ] **Step 4: Run static checks**

Run:

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Build and inspect release archives**

Run:

```bash
.venv/bin/python scripts/build-v2-release.py
```

Expected: source and hotfix archives build successfully, include the Notebook-only connection and shared-library assets, and exclude credential/browser state.

- [ ] **Step 6: Review the final diff and branch state**

Run:

```bash
git status --short --branch
git diff origin/codex/native-study-hub-quizzes...HEAD --stat
git log --oneline --decorate -8
```

Expected: only intended commits are ahead of the remote branch and no uncommitted files remain.

- [ ] **Step 7: Push the testing branch**

```bash
git push origin codex/native-study-hub-quizzes
```

- [ ] **Step 8: Hand off NUC acceptance**

Report:

- branch `codex/native-study-hub-quizzes`;
- exact pushed commit SHA;
- exact stop → pull → install → start PowerShell block;
- `/public/quizzes` as the single shared link;
- Notebook-only Settings connection instructions;
- local-only progress behavior and reset behavior;
- verification totals from the fresh final runs.

Do not merge to `main` until the user confirms live NUC generation, library listing, local completion status, and old direct quiz links all pass.

---

## Acceptance Checklist

- [ ] Settings contains one Gemini Notebook connection card.
- [ ] No Google Docs status, OAuth JSON selector, OAuth client upload endpoint, or Docs refresh-token path remains active.
- [ ] Existing Notebook storage survives credential retirement.
- [ ] Outline and quiz generation require only live Gemini Notebook authentication.
- [ ] Native quiz publication completes without Google Docs.
- [ ] A job persisted at legacy stage `docs` completes without republishing or freezing.
- [ ] `/public/quizzes` lists only published quizzes grouped Course → Exam → Lecture.
- [ ] A listed lecture with a current outline shows a `Lecture Outline` button.
- [ ] The public outline route serves the same verified PDF as the private lecture page without exposing numeric artifact IDs, paths, slides, or transcripts.
- [ ] Existing `/public/quizzes/{token}` links remain stable.
- [ ] Answer keys remain absent from page HTML and quiz-content JSON.
- [ ] The library labels valid local progress as `Not started`, `In progress`, or `Completed`.
- [ ] Regenerated quiz versions ignore older progress.
- [ ] Reset removes only Study Hub quiz-progress keys.
- [ ] No server-side user or completion records are created.
- [ ] The public library follows the existing Study Hub design language and remains usable on mobile.
- [ ] Public quiz routes retain CSP, no-store protections where required, and CSRF-protected answer submission.
- [ ] Release archives contain all new runtime assets and no Google credential/browser-state files.
- [ ] Python tests, JavaScript tests, Ruff, mypy, diff checks, and release builds all pass before push.
