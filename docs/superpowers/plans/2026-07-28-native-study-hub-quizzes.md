# Native Study Hub Quizzes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Gemini Quiz Gem handoff with validated, stable, publicly shareable native Study Hub quizzes.

**Architecture:** NotebookLM receives the user-managed Obsidian prompt plus a fixed JSON output contract. Study Hub parses that JSON, atomically upserts one versioned quiz per lecture, serves a tokenized public player, evaluates submitted answers, and synchronizes the stable link into the existing course Google Doc.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic, Jinja2, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Continue selecting only the current lecture PDF and cleaned transcript in NotebookLM.
- Preserve one NotebookLM notebook per course exam.
- Preserve one Google Doc per course and one tab per exam.
- The Obsidian prompt remains editable; Study Hub appends only the required JSON format.
- One stable, unguessable quiz URL is retained per lecture across regenerations.
- The public quiz stores no reader identity, answers, scores, or analytics.
- Only `/public/quizzes/` may bypass Cloudflare Access; all other Study Hub routes remain private.
- Public answer submissions remain same-origin and CSRF protected.
- Do not change the separate Gemini API provider support.
- Follow test-driven development: every production behavior starts with a failing test.

---

### Task 1: Quiz contract and validation

**Files:**
- Create: `src/oms_hub/study_generation/native_quiz.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Test: `tests/study_generation/test_native_quiz.py`

**Interfaces:**
- Produces: `QuizChoice`, `QuizQuestion`, `NativeQuiz`, and `QuizFeedback` frozen dataclasses.
- Produces: `quiz_prompt(prompt: PromptSnapshot) -> PromptSnapshot`.
- Produces: `parse_native_quiz(raw: str) -> NativeQuiz`.
- Produces: `public_quiz_content(quiz: NativeQuiz) -> dict[str, object]`.
- Produces: `grade_answer(quiz: NativeQuiz, question_id: str, choice_id: str) -> QuizFeedback`.

- [ ] **Step 1: Write parser and grader tests**

Cover literal fenced and unfenced JSON fixtures, the appended schema contract,
duplicate choices, missing rationales, out-of-range answer indexes, answer-key
omission from public content, and correct/incorrect feedback.

```python
def test_fenced_notebook_json_is_validated():
    quiz = parse_native_quiz(
        '```json\n{"title":"Seizures","questions":[{"stem":"Stem",'
        '"choices":["A","B"],"correct_index":1,"rationale":"Because."}]}\n```'
    )
    assert quiz.questions[0].choices[1].id == "c2"
    assert grade_answer(quiz, "q1", "c1").correct is False


def test_public_content_omits_answer_key_and_rationale():
    content = public_quiz_content(valid_quiz())
    assert "correct_index" not in repr(content)
    assert "rationale" not in repr(content)
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the module is absent**

Run: `pytest tests/study_generation/test_native_quiz.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the minimal validated contract**

Use `json.loads`, strip one optional Markdown fence, validate through Pydantic
models with explicit length bounds, normalize question IDs to `q1..q100` and
choice IDs to `c1..c8`, and return frozen domain objects. `grade_answer` must
raise `KeyError` for unknown question or choice IDs.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/study_generation/test_native_quiz.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/native_quiz.py \
  tests/study_generation/test_native_quiz.py
git commit -m "feat: validate native NotebookLM quizzes"
```

### Task 2: Durable publication and stable native URLs

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/native_quiz.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `tests/study_generation/test_migration.py`
- Modify: `tests/study_generation/test_repository.py`
- Modify: `tests/study_generation/test_native_quiz.py`

**Interfaces:**
- Produces: `PublishedQuizRecord(token, lecture_id, job_id, title, quiz, version)`.
- Produces: `GenerationRepository.publish_quiz(...) -> PublishedQuizRecord`.
- Produces: `GenerationRepository.published_quiz(token) -> PublishedQuizRecord | None`.
- Produces: `quiz_origin(settings: Settings) -> str`.
- Produces: `quiz_url(token: str, settings: Settings) -> str`.
- Produces: `validate_native_quiz_url(url: str, settings: Settings) -> str`.

- [ ] **Step 1: Write migration, publication, retry, regeneration, and URL-policy tests**

```python
def test_publish_keeps_token_and_increments_version_for_new_job(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    first = repository.publish_quiz(lecture_id, "job-1", valid_quiz())
    retried = repository.publish_quiz(lecture_id, "job-1", valid_quiz())
    regenerated = repository.publish_quiz(lecture_id, "job-2", changed_quiz())
    assert retried.token == first.token
    assert retried.version == 1
    assert regenerated.token == first.token
    assert regenerated.version == 2
```

Update the schema assertion to require `published_quizzes` and schema version 5.
URL tests must reject foreign hosts, credentials, query strings, fragments, and
non-token paths.

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run: `pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py tests/study_generation/test_native_quiz.py -q`

Expected: failures name the missing table, methods, and URL helpers.

- [ ] **Step 3: Implement schema and repository upsert**

Add `PublishedQuizModel` with a 64-character random hex token, unique
`lecture_id`, current `job_id`, title, `payload_json`, version, and timestamps.
Serialize only validated domain data. Upsert under one database transaction;
reuse the row without change for the same job and preserve the token for a new
job.

- [ ] **Step 4: Implement exact-origin native URL policy**

Build an HTTPS origin from `public_hostname`; use the local dashboard origin
only when no public hostname is configured. Validate the parsed URL against the
exact origin and `/public/quizzes/{64 lowercase hex}`.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py tests/study_generation/test_native_quiz.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/config.py \
  src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/native_quiz.py \
  src/oms_hub/study_generation/repository.py \
  tests/study_generation/test_migration.py \
  tests/study_generation/test_repository.py \
  tests/study_generation/test_native_quiz.py
git commit -m "feat: publish stable native lecture quizzes"
```

### Task 3: Public quiz API and security boundary

**Files:**
- Create: `src/oms_hub/web/public_quiz_routes.py`
- Create: `src/oms_hub/web/templates/public_quiz.html`
- Modify: `src/oms_hub/app.py`
- Create: `tests/v2/test_public_quiz_routes.py`
- Modify: `tests/v2/test_baseline_smoke.py`

**Interfaces:**
- Produces: router mounted at `/public/quizzes`.
- Consumes: `GenerationRepository.published_quiz`.
- Consumes: `public_quiz_content` and `grade_answer`.
- Request: `POST /public/quizzes/{token}/answer` with
  `{"question_id":"q1","choice_id":"c2"}`.
- Response: `{"correct":true,"correct_choice_id":"c2","rationale":"..."}`.

- [ ] **Step 1: Write public page, content, feedback, 404, Access-bypass, and CSRF tests**

Create a real database quiz fixture. Assert that content has stems and choices
but no answer key, feedback covers only the requested question, an invalid token
returns 404, and the page response has hardened headers.

With `public_hostname="study.example.com"` and no Access verifier:

```python
assert client.get(
    f"/public/quizzes/{token}", headers={"host": "study.example.com"}
).status_code == 200
assert client.get("/", headers={"host": "study.example.com"}).status_code == 503
```

Post once without a CSRF header and assert 403. Then GET the page, copy the
issued CSRF cookie into `X-CSRF-Token`, POST with a same-origin header, and
assert the requested feedback is returned.

- [ ] **Step 2: Run route tests and confirm expected failures**

Run: `pytest tests/v2/test_public_quiz_routes.py tests/v2/test_baseline_smoke.py -q`

Expected: public routes are 404 before implementation.

- [ ] **Step 3: Implement the public router and HTML shell**

Render only quiz metadata and endpoint URLs in the shell. Serve the dedicated
player JS and CSS from `/public/quizzes/assets/` so a Cloudflare path bypass
does not need to expose the private `/static` tree.

- [ ] **Step 4: Narrow the Access exemption**

In the security middleware, skip Cloudflare identity verification only when
the request path starts with `/public/quizzes/`. Keep host validation,
hardening headers, origin checks, and CSRF enforcement unchanged.

- [ ] **Step 5: Run route tests**

Run: `pytest tests/v2/test_public_quiz_routes.py tests/v2/test_baseline_smoke.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/app.py src/oms_hub/web/public_quiz_routes.py \
  src/oms_hub/web/templates/public_quiz.html \
  tests/v2/test_public_quiz_routes.py tests/v2/test_baseline_smoke.py
git commit -m "feat: expose tokenized public quiz API"
```

### Task 4: Study Focus quiz player

**Files:**
- Create: `src/oms_hub/web/static/public_quiz.js`
- Create: `src/oms_hub/web/static/public_quiz.css`
- Create: `tests/js/public_quiz.test.js`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/templates/public_quiz.html`

**Interfaces:**
- Produces: `createQuizState`, `selectChoice`, `toggleEliminated`,
  `recordFeedback`, `serializeProgress`, and `restoreProgress` exported for Node
  tests.
- Consumes: public content and answer endpoints from Task 3.

- [ ] **Step 1: Write state-machine and request tests**

Prove:

- selecting A then B retains only B before submission;
- eliminating a selected answer clears the selection;
- eliminated answers can be restored;
- a submitted question is locked;
- correct and incorrect feedback update score exactly once;
- serialization and restoration are namespaced by quiz token and version; and
- answer requests send the CSRF header and contain no answer data in the URL.

```javascript
test("answer selection stays editable until submission", () => {
  let state = quiz.createQuizState(content);
  state = quiz.selectChoice(state, "q1", "c1");
  state = quiz.selectChoice(state, "q1", "c2");
  assert.equal(state.questions.q1.selectedChoiceId, "c2");
});
```

- [ ] **Step 2: Run Node tests and confirm the module is absent**

Run: `node --test tests/js/public_quiz.test.js`

Expected: failure loading `public_quiz.js`.

- [ ] **Step 3: Implement the state machine and API client**

Keep state updates immutable and separate from DOM rendering. Use browser
`localStorage` under `oms-study-hub-quiz:{token}:v{version}`. Store no Google
or Study Hub credentials.

- [ ] **Step 4: Implement accessible Study Focus rendering**

Render one question, semantic answer buttons, independent strike buttons,
bottom **Submit Answer**, feedback region with `aria-live`, and **Continue**.
Use yellow `<mark>` wrappers for selected question text and a clear-highlights
action. Disable all answer and strike controls after submission. Finish with a
score and **Start Over**.

- [ ] **Step 5: Run Node and route tests**

Run: `node --test tests/js/public_quiz.test.js && pytest tests/v2/test_public_quiz_routes.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/web/public_quiz_routes.py \
  src/oms_hub/web/templates/public_quiz.html \
  src/oms_hub/web/static/public_quiz.js \
  src/oms_hub/web/static/public_quiz.css \
  tests/js/public_quiz.test.js
git commit -m "feat: add Study Focus quiz player"
```

### Task 5: Native worker, Google Docs, and lecture integration

**Files:**
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Modify: `src/oms_hub/study_generation/google_docs.py`
- Modify: `src/oms_hub/study_generation/google_connection.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/templates/lecture.html`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/app.py`
- Modify: `tests/study_generation/test_worker.py`
- Modify: `tests/study_generation/test_google_connection.py`
- Create: `tests/study_generation/test_google_docs.py`
- Modify: `tests/v2/test_google_settings_routes.py`
- Modify: `tests/v2/test_lecture_generation_ui.py`

**Interfaces:**
- Adds durable stages `quiz_validate` and `publish`.
- `GenerationWorker` consumes the native publisher instead of a Gemini gateway.
- `GoogleDocsGateway` validates links with the configured Study Hub origin.
- Google consumer surfaces become NotebookLM and Google Docs.

- [ ] **Step 1: Replace the worker regression test with native publication tests**

Use a real valid JSON NotebookLM answer. Assert the worker:

- adds the fixed JSON contract before asking NotebookLM;
- publishes without any Gemini dependency;
- advances through native stages;
- records the native URL before Docs synchronization;
- resumes at Docs without republishing; and
- marks `quiz_published` complete after link synchronization.

- [ ] **Step 2: Write Google integration and UI expectation tests**

Update connection expectations to `("notebook", "docs")`, verify interactive
connection does not open Gemini, test exact-origin Google Doc URL validation,
and assert lecture/settings copy says Study Hub rather than Gemini Quiz Gem.

- [ ] **Step 3: Run focused tests and confirm failures**

Run: `pytest tests/study_generation/test_worker.py tests/study_generation/test_google_connection.py tests/study_generation/test_google_docs.py tests/v2/test_google_settings_routes.py tests/v2/test_lecture_generation_ui.py -q`

Expected: tests fail against the old Gemini workflow and wording.

- [ ] **Step 4: Implement native worker stages and publication**

Enhance only quiz prompts with `quiz_prompt`, parse before publication, persist
the native link, sync it to Docs, and complete the job. Keep legacy enum values
and database columns readable for old records but remove runtime Gemini calls.

- [ ] **Step 5: Update Google consumer connection and Docs link policy**

Probe NotebookLM and Docs only, set the retained database `gemini_state` column
to `"unused"`, remove Gemini from interactive browser startup, and inject
`Settings` into Google Docs URL validation.

- [ ] **Step 6: Update lecture and settings copy**

Preserve the current controls and polling behavior while replacing Gemini
consumer wording with native Study Hub wording.

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/study_generation/test_worker.py tests/study_generation/test_google_connection.py tests/study_generation/test_google_docs.py tests/v2/test_google_settings_routes.py tests/v2/test_lecture_generation_ui.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/worker.py \
  src/oms_hub/study_generation/google_docs.py \
  src/oms_hub/study_generation/google_connection.py \
  src/oms_hub/web/generation_routes.py \
  src/oms_hub/web/templates/lecture.html \
  src/oms_hub/web/templates/settings.html src/oms_hub/app.py \
  tests/study_generation/test_worker.py \
  tests/study_generation/test_google_connection.py \
  tests/study_generation/test_google_docs.py \
  tests/v2/test_google_settings_routes.py \
  tests/v2/test_lecture_generation_ui.py
git commit -m "feat: publish NotebookLM quizzes in Study Hub"
```

### Task 6: Release package, rollout documentation, and final verification

**Files:**
- Modify: `scripts/build-v2-release.py`
- Modify: `tests/v2/test_notebooklm_release_package.py`
- Modify: `README.md`
- Create: `docs/native-quizzes-nuc-rollout.md`

**Interfaces:**
- Release hotfix contains every native quiz runtime file.
- Rollout documents the implementation branch and Cloudflare path bypass.

- [ ] **Step 1: Write release archive expectations**

Require the native quiz module, route, template, JS, and CSS in the hotfix.
Retain all credential-exclusion assertions.

- [ ] **Step 2: Run release tests and confirm missing-file failures**

Run: `pytest tests/v2/test_notebooklm_release_package.py tests/v2/test_release_package.py -q`

Expected: hotfix assertions fail for the new files.

- [ ] **Step 3: Update the release list and documentation**

Document stopping the Windows service before reinstalling, fetching and
switching to `codex/native-study-hub-quizzes`, reinstalling the editable
package, restarting the service, checking `/health`, and configuring a
Cloudflare Access Bypass application for `/public/quizzes/*`.

- [ ] **Step 4: Run focused release tests**

Run: `pytest tests/v2/test_notebooklm_release_package.py tests/v2/test_release_package.py -q`

Expected: all tests pass.

- [ ] **Step 5: Run full verification**

```bash
pytest
node --test tests/js/*.test.js
ruff check .
mypy src/oms_hub
git diff --check
```

Expected: every command exits 0 with no failures.

- [ ] **Step 6: Commit**

```bash
git add scripts/build-v2-release.py \
  tests/v2/test_notebooklm_release_package.py README.md \
  docs/native-quizzes-nuc-rollout.md
git commit -m "docs: add native quiz NUC rollout"
```

- [ ] **Step 7: Push the branch**

```bash
git push -u origin codex/native-study-hub-quizzes
```

Expected: the remote branch is updated without force-pushing.

