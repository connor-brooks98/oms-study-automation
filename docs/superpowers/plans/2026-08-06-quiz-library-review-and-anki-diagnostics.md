# Quiz Library Review and Anki Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unlock unanswered navigation in private quiz previews, improve Studio/PQ library labels, safely unpublish every released quiz type, and expose exact Anki reconciliation failures.

**Architecture:** Keep one quiz player and opt private previews into a narrow navigation capability. Add one private token-based publication-management route backed by a generic repository unpublish operation. Preserve the structured reconciliation report while deriving a detailed terminal error and logging returned terminal failures.

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2, browser JavaScript, Node test runner, pytest, Pydantic, Ruff, and mypy.

## Global Constraints

- Remove means unpublish only; never delete Studio runs, reviewed questions, media, source URLs, lecture generation artifacts, or historical publication rows.
- The unpublish mutation must remain outside the `/public` Cloudflare Access bypass and must require the existing CSRF cookie/header.
- Released quizzes must continue requiring answer submission before advancing.
- Only the explicit private-preview capability may unlock unanswered navigation.
- Preserve the Anki card-centric contract and 60–70 card cap; this change adds diagnostics, not relaxed reconciliation rules.
- Use test-driven development and preserve unrelated working-tree changes.

---

### Task 1: Private Preview Navigation and Library Labels

**Files:**
- Modify: `src/oms_hub/web/templates/studio_quiz_preview.html`
- Modify: `src/oms_hub/web/static/public_quiz.js`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/templates/public_quiz_library.html`
- Test: `tests/js/public_quiz.test.js`
- Test: `tests/v2/test_public_quiz_routes.py`

**Interfaces:**
- Consumes: the existing `[data-quiz-token]` player root and `PublishedQuizRecord`.
- Produces: `data-allow-unanswered-navigation="true"` for private preview and library row fields `primary_label` and `secondary_label`.

- [ ] **Step 1: Write failing player tests**

Add one JavaScript test that sets `app.dataset.allowUnansweredNavigation = "true"`, initializes a two-question quiz, asserts the first forward control is enabled without a selected/submitted choice, clicks it, and asserts question two renders. Keep the existing released-player assertion that the same control is disabled when the dataset flag is absent.

- [ ] **Step 2: Run the player tests and confirm the new case fails**

Run: `node --test tests/js/public_quiz.test.js`

Expected: the new private-preview case fails because `forward.disabled` is still tied only to `questionProgress.submitted`; existing cases pass.

- [ ] **Step 3: Implement the narrow preview capability**

Add this attribute to the Studio preview player root:

```html
data-allow-unanswered-navigation="true"
```

In `initialize`, derive:

```javascript
const allowUnansweredNavigation = (
  app.dataset.allowUnansweredNavigation === "true"
);
```

Use `allowUnansweredNavigation || questionProgress.submitted` for both the forward-button disabled state and the click guard. Do not change answer selection, submission, scoring, or released page templates.

- [ ] **Step 4: Write failing library-label route assertions**

Update the mixed-publication route test to assert a Studio/import row has its supplied label in the primary `<strong>` element, does not contain `Studio quiz`, and a lecture publication still contains `Lecture 1` plus its topic.

- [ ] **Step 5: Run the focused route test and confirm it fails**

Run: `pytest tests/v2/test_public_quiz_routes.py -q`

Expected: the title assertion fails against the hard-coded `Studio quiz` template branch.

- [ ] **Step 6: Implement explicit row labels**

Build each library row with:

```python
"primary_label": (
    f"Lecture {lecture.lecture_number}"
    if lecture is not None
    else (published.label or published.title)
),
"secondary_label": (
    lecture.topic
    if lecture is not None
    else None
),
```

Render `row.primary_label` in `<strong>` and render `<small>` only when `row.secondary_label` is non-empty. Retain the existing `is_studio` field only if another consumer still needs it.

- [ ] **Step 7: Run focused tests**

Run: `node --test tests/js/public_quiz.test.js && pytest tests/v2/test_public_quiz_routes.py -q`

Expected: all player and library route tests pass.

### Task 2: Unified Protected Unpublish Control

**Files:**
- Create: `src/oms_hub/web/published_quiz_routes.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/web/studio_routes.py`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/templates/public_quiz_library.html`
- Modify: `src/oms_hub/web/static/public_quiz_library.js`
- Modify: `src/oms_hub/web/static/public_quiz_library.css`
- Test: `tests/study_generation/test_repository.py`
- Test: `tests/v2/test_public_quiz_routes.py`
- Test: `tests/js/public_quiz_library.test.js`

**Interfaces:**
- Produces: `GenerationRepository.unpublish_quiz(token: str) -> str`.
- Produces: authenticated/CSRF-protected `DELETE /api/published-quizzes/{token}` returning `{"token": token, "state": "unpublished"}`.
- Preserves: `DELETE /studio/runs/{run_id}/publication`, now delegating to the generic token operation after resolving the active Studio publication.

- [ ] **Step 1: Write failing repository lifecycle tests**

Add tests for a lecture and Studio publication. For each, call `unpublish_quiz(token)`, assert `published_quiz(token) is None`, and query the database to prove the inactive `PublishedQuizModel` still exists. For Studio, also assert the `StudioRunModel` still exists and its `published_token` is cleared. Republish each through its normal publication path and assert it becomes active again without resurrecting deleted data.

- [ ] **Step 2: Run repository tests and confirm the API is missing**

Run: `pytest tests/study_generation/test_repository.py -q`

Expected: failure because `GenerationRepository.unpublish_quiz` does not exist.

- [ ] **Step 3: Implement generic repository unpublish**

Implement:

```python
def unpublish_quiz(self, token: str) -> str:
    with self.database.session() as session:
        model = session.get(PublishedQuizModel, token)
        if model is None or not model.active:
            raise KeyError(token)
        model.active = False
        if model.studio_run_id is not None:
            run = session.get(StudioRunModel, model.studio_run_id)
            if run is not None and run.published_token == token:
                run.published_token = None
        return model.token
```

Keep `unpublish_studio_quiz(run_id)` as a compatibility method that resolves the active Studio token and delegates to the same lifecycle semantics without nested sessions.

- [ ] **Step 4: Write failing protected-route tests**

Add route tests that:

- obtain a CSRF cookie from the local library and successfully delete both a lecture token and a Studio token;
- receive 403 without the matching CSRF header;
- receive 404 for an unknown or already inactive token; and
- on `public_hostname`, let an anonymous visitor obtain and echo a valid CSRF cookie/header but still receive the existing private-route access failure without Cloudflare Access, then assert the publication remains active.

- [ ] **Step 5: Run route tests and confirm the endpoint is absent**

Run: `pytest tests/v2/test_public_quiz_routes.py -q`

Expected: DELETE requests return 404 because the private management router is not registered.

- [ ] **Step 6: Implement and register the private management router**

Create an `APIRouter(prefix="/api/published-quizzes")`, validate the token as exactly 64 lowercase hexadecimal characters, call `require_form_csrf(request, None)`, invoke `unpublish_quiz`, map `KeyError` to 404, and return the settled JSON response. Register it in `create_app`. Do not add any DELETE route under `/public`.

- [ ] **Step 7: Write failing browser-control tests**

Extend the fake library DOM so it exposes `[data-remove-quiz]`, `closest(".lecture-row")`, document cookies, and a removable row. Test cancellation, successful DELETE with `X-CSRF-Token`, clearing `progressKey(token, version)`, row removal, and a non-OK response that preserves the row and reports `payload.detail`.

- [ ] **Step 8: Run the browser tests and confirm controls are unimplemented**

Run: `node --test tests/js/public_quiz_library.test.js`

Expected: new removal tests fail because no remove handlers exist.

- [ ] **Step 9: Implement the Remove control**

Add a red button per row with token, version, and `/api/published-quizzes/{token}` in data attributes. Read the existing CSRF cookie, confirm `Remove this released quiz? Its source and run history will be preserved.`, send DELETE with `X-CSRF-Token`, and only after success clear local progress and remove the closest row. On failure, leave the row untouched and put the server detail in `[data-reset-message]`. Add disabled/loading behavior to prevent duplicate requests and restore the button on error.

- [ ] **Step 10: Run all unpublish tests**

Run: `pytest tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py -q && node --test tests/js/public_quiz_library.test.js`

Expected: all repository, route, and browser library tests pass.

### Task 3: Actionable Card-Centric Reconciliation Failures

**Files:**
- Modify: `src/oms_hub/anki/stages.py`
- Modify: `src/oms_hub/anki/worker.py`
- Test: `tests/anki/test_stages.py`
- Test: `tests/anki/test_worker.py`

**Interfaces:**
- Consumes: `ReconciliationReport.failed`, whose items contain `assertion_id` and `message`.
- Preserves: the structured reconciliation artifact and every A1–A10/selection invariant.
- Produces: a bounded job error beginning `Card-centric reconciliation failed:` followed by every failed finding.

- [ ] **Step 1: Write failing failure-format tests**

Add a pure-stage test using a `ReconciliationReport` with two failed findings. Assert the formatter returns:

```text
Card-centric reconciliation failed: A6: YES plus generated cards must total at least 10 | selection_conservation: Selected cards must be drawn from eligible existing or generated output
```

Also assert a report with no failures returns `None`.

- [ ] **Step 2: Run the focused stage tests and confirm generic output**

Run: `pytest tests/anki/test_stages.py -q`

Expected: the new formatter test fails because card-centric reconciliation uses a generic literal.

- [ ] **Step 3: Implement detailed stage failure formatting**

Add a private helper that returns `None` when `can_render_envelope` is true, otherwise joins every failed item as `assertion_id: message` using ` | `. Use that helper as the card-centric `StageProduct.blocking_error`. Keep the full findings in the artifact payload.

- [ ] **Step 4: Write a failing worker-log test**

Build a worker with a pipeline stub whose `run_stage` returns a terminal failed result and a repository stub whose refreshed job has the detailed error. With `caplog`, assert one ERROR record contains the job ID and exact persisted error. Ensure raised exceptions still use the existing `_handle_failure` path and are not double-logged.

- [ ] **Step 5: Run the worker test and confirm returned failure is silent**

Run: `pytest tests/anki/test_worker.py -q`

Expected: no terminal log record is captured for a returned failed stage.

- [ ] **Step 6: Log returned terminal failures once**

Capture the `StageRunResult` from `pipeline.run_stage`. When its state is `CurationState.FAILED`, refresh the job and log:

```python
logger.error("Anki curation job %s stopped: %s", job.id, current.error)
```

Do not send returned blocking failures through retry logic. Do not emit a second terminal line for exception-driven failures.

- [ ] **Step 7: Run focused Anki tests**

Run: `pytest tests/anki/test_stages.py tests/anki/test_worker.py -q`

Expected: all focused Anki tests pass and the detailed assertion text appears in the log test.

### Task 4: Complete Verification

**Files:**
- Inspect: all files changed by Tasks 1–3.

**Interfaces:**
- Consumes: the completed behavior from Tasks 1–3.
- Produces: sandbox evidence suitable for final Sol review.

- [ ] **Step 1: Run formatting and type gates**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`

Expected: Ruff exits 0 and mypy reports success for all configured source files.

- [ ] **Step 2: Run the complete JavaScript suite**

Run: `node --test tests/js/*.test.js`

Expected: every JavaScript test passes with zero failures.

- [ ] **Step 3: Run the complete Python suite**

Run: `.venv/bin/pytest -q`

Expected: all Python tests pass; only established platform-specific skips are allowed.

- [ ] **Step 4: Inspect scope and patch hygiene**

Run: `git status --short && git diff --check && git diff --stat && git diff`

Expected: no whitespace errors, no secret material, no destructive deletion path, no mutation under `/public`, and only the design/plan plus files and tests listed above are modified.
