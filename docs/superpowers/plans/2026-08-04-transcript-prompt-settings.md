# Transcript Cleaning Prompt Setting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Settings card that saves and validates the transcript-cleaning prompt path, then uses that database-backed path after Study Hub restarts.

**Architecture:** Extend the existing prompt-kind persistence with a `transcript` entry instead of adding a new table. Settings reuses the current prompt-card routes and JavaScript; transcript testing delegates to the stricter transcript `PromptLoader`, while application startup resolves the saved path before the environment fallback.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Jinja2, vanilla JavaScript, pytest, Node test runner, Ruff, mypy.

## Global Constraints

- Work only on `codex/anki-v4-unified-providers-and-review-fixes`.
- Keep `OMS_HUB_TRANSCRIPT_PROMPT_PATH` as the fallback when no saved database path exists.
- Keep `OMS_HUB_TRANSCRIPT_PROMPT_SHA256` as the approval source.
- Transcript validation must enforce readable nonempty UTF-8 content no larger than 64 KiB.
- No API or HTML response may contain prompt contents.
- The saved path takes effect after Study Hub restarts; do not hot-swap a running worker.

---

### Task 1: Persist and resolve the transcript prompt path

**Files:**
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/v2/test_generation_settings.py`

**Interfaces:**
- Produces: `PromptKind.TRANSCRIPT = "transcript"`.
- Produces: startup resolution through `GenerationRepository.prompt_path(PromptKind.TRANSCRIPT)` with `Settings.transcript_prompt_path` as fallback.
- Consumes: existing `GenerationRepository.set_prompt_path()` and `prompt_path()` methods.

- [ ] **Step 1: Write failing startup precedence tests**

Add tests that save a transcript path before `create_app`, assert `app.state.transcript_prompt.path` equals the saved path, and separately assert the environment/configured path remains the fallback with no saved row.

```python
def test_saved_transcript_prompt_path_overrides_configured_fallback(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(database_url)
    database.migrate()
    saved = tmp_path / "Moved Transcript Prompt.md"
    GenerationRepository(database).set_prompt_path(PromptKind.TRANSCRIPT, str(saved))

    app = create_app(Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=database_url,
        transcript_prompt_path=tmp_path / "Old Prompt.md",
    ))

    assert app.state.transcript_prompt.path == saved
```

- [ ] **Step 2: Run the targeted tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/v2/test_generation_settings.py -k transcript_prompt_path`

Expected: failure because `PromptKind.TRANSCRIPT` does not exist.

- [ ] **Step 3: Implement the minimal domain and startup resolution**

Add the enum value and resolve the path after `GenerationRepository` is constructed:

```python
class PromptKind(StrEnum):
    TRANSCRIPT = "transcript"
    OUTLINE = "outline"
    QUIZ = "quiz"

saved_transcript_prompt = app.state.generation_repository.prompt_path(
    PromptKind.TRANSCRIPT
)
transcript_prompt_path = (
    Path(saved_transcript_prompt)
    if saved_transcript_prompt
    else resolved.transcript_prompt_path
)
```

Pass `expanded_path(transcript_prompt_path)` to `V2PromptLoader` when non-null.

- [ ] **Step 4: Run the targeted tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/v2/test_generation_settings.py -k transcript_prompt_path`

Expected: both precedence and compatibility tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/oms_hub/study_generation/domain.py src/oms_hub/app.py tests/v2/test_generation_settings.py
git commit -m "feat: persist transcript prompt path"
```

### Task 2: Render, save, select, and validate the transcript prompt card

**Files:**
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Test: `tests/v2/test_generation_settings.py`
- Test: `tests/js/settings.test.js`

**Interfaces:**
- Consumes: `PromptKind.TRANSCRIPT` from Task 1.
- Produces: existing routes `/settings/generation/prompts/transcript`, `/select`, and `/test` with the same response shape as outline and quiz.
- Produces: a third `[data-prompt-card]` rendered first and labeled `Transcript cleaning prompt`.

- [ ] **Step 1: Write failing route and page tests**

Add a route test that saves and tests a real transcript prompt, asserts `state == "valid"`, asserts a SHA-256 is returned, and asserts the private contents are absent. Update the settings-page assertions to require three path cards in transcript/outline/quiz order.

```python
def test_transcript_prompt_path_can_be_saved_and_tested(tmp_path):
    client, app = prepared_client(tmp_path)
    prompt = tmp_path / "Transcript Cleaning.md"
    prompt.write_text("private transcript instructions", encoding="utf-8")

    saved = client.post(
        "/settings/generation/prompts/transcript",
        json={"path": str(prompt)},
    )
    tested = client.post("/settings/generation/prompts/transcript/test")

    assert saved.status_code == 200
    assert tested.json()["state"] == "valid"
    assert tested.json()["sha256"]
    assert "private transcript instructions" not in tested.text
    assert app.state.generation_repository.prompt_path(
        PromptKind.TRANSCRIPT
    ) == str(prompt)
```

- [ ] **Step 2: Run the route/page tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/v2/test_generation_settings.py`

Expected: transcript test fails because generic `PromptFileService` does not enforce the transcript loader contract and the settings labels/order are incomplete.

- [ ] **Step 3: Implement transcript-specific validation and settings context**

In `test_prompt_path`, branch only for the transcript kind:

```python
if selected is PromptKind.TRANSCRIPT:
    configured = _repository(request).prompt_path(selected)
    try:
        prompt = PromptLoader(
            expanded_path(Path(configured)) if configured else None,
            None,
        ).inspect()
    except PromptError as error:
        return invalid_response
    payload = {
        "kind": selected.value,
        "state": "valid",
        "path": configured,
        "sha256": prompt.sha256,
        "modified_at": None,
    }
```

Keep outline and quiz on `PromptFileService`. In the settings context, map explicit labels and order:

```python
prompt_labels = {
    PromptKind.TRANSCRIPT: "Transcript cleaning prompt",
    PromptKind.OUTLINE: "Lecture outline prompt",
    PromptKind.QUIZ: "Lecture quiz prompt",
}
```

Update the section heading/copy from Notebook-only wording to prompt-files wording. Existing JavaScript automatically binds the third data card without new runtime logic.

- [ ] **Step 4: Add/adjust the JavaScript rendering contract test**

Assert initialization binds all three `[data-prompt-card]` elements and builds the transcript save/test URLs from `data-prompt="transcript"`. No production JavaScript change is expected unless the failing test reveals a hard-coded two-card assumption.

- [ ] **Step 5: Run focused suites and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/v2/test_generation_settings.py tests/study_generation/test_prompts.py
node --test "tests/js/settings.test.js"
```

Expected: all focused tests pass and transcript contents are absent from responses.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/oms_hub/web/settings_routes.py src/oms_hub/web/generation_routes.py src/oms_hub/web/templates/settings.html tests/v2/test_generation_settings.py tests/js/settings.test.js
git commit -m "feat: add transcript prompt settings card"
```

### Task 3: Full verification and publication

**Files:**
- Verify all changed files from Tasks 1 and 2.

**Interfaces:**
- Consumes: completed feature from Tasks 1 and 2.
- Produces: pushed branch with green local and GitHub CI checks.

- [ ] **Step 1: Run all verification suites**

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring .venv/bin/python -m pytest -q -m "not windows_office"
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
node --test "tests/js/*.test.js"
git diff --check
```

Expected: 0 failures, 0 lint errors, 0 type errors, and no whitespace errors.

- [ ] **Step 2: Review branch scope**

Run: `git status -sb && git log -5 --oneline`

Expected: only the approved transcript-prompt feature commits follow `2e353d1`.

- [ ] **Step 3: Push and monitor CI**

```bash
git push origin codex/anki-v4-unified-providers-and-review-fixes
gh run list --branch codex/anki-v4-unified-providers-and-review-fixes --limit 1
```

Expected: the new CI run completes successfully.
