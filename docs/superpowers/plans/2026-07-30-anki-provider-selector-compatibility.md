# Anki Provider Compatibility and Pathway Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Anthropic structured generation accept Study Hub schemas and replace the lecture accordion with three dependent Course → Exam → Lecture dropdowns.

**Architecture:** Keep the original Pydantic schema for local validation while the Anthropic adapter sends a recursively normalized copy. Keep one quote-safe flat lecture payload in the page and derive each dependent select's options in JavaScript. Read provider/model defaults from the existing SQLite-backed LLM settings repository.

**Tech Stack:** FastAPI, Jinja2, Pydantic, httpx, vanilla JavaScript, CSS Grid, pytest, Node test runner.

## Global Constraints

- Preserve the curation-job API and database schema.
- Preserve strict validation against the original Pydantic output model.
- Never include provider error bodies, API keys, or credentials in diagnostics.
- Preserve the canonical editable lecture tag and checked current source revisions.
- Do not mutate Anki, its indexes, ingestion records, or the acceptance profile.
- Use native select elements and collapse to one column below 720 pixels.

---

### Task 1: Normalize Anthropic Structured Schemas

**Files:**
- Modify: `src/oms_hub/llm/anthropic.py`
- Modify: `src/oms_hub/llm/domain.py`
- Modify: `src/oms_hub/llm/provider.py`
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/static/settings.js`
- Modify: `tests/llm/test_anthropic.py`
- Modify: `tests/llm/test_error_classification.py`
- Modify: `tests/v2/test_worker_llm_retry.py`

**Interfaces:**
- Produces: `anthropic_output_schema(schema: dict[str, object]) -> dict[str, object]`
- Consumes: the unchanged original schema from `StructuredTextService`.

- [ ] Write a failing adapter test using nested `minLength`, `maxLength`,
  `minItems`, and equivalent `prefixItems`, and assert the outgoing request
  contains a normalized copy while the input object remains unchanged.
- [ ] Run the focused Anthropic test and verify it fails on unsupported
  keywords.
- [ ] Implement recursive schema-copy normalization inside the Anthropic
  adapter and use it only for `output_config.format.schema`.
- [ ] Add a failing diagnostic test proving HTTP 400 is a safe invalid-request
  error and HTTP 404 remains a model error.
- [ ] Add `DiagnosticSource.REQUEST`, its safe Settings presentation, and a
  non-retry assertion for invalid provider requests.
- [ ] Implement the diagnostic distinction without returning provider bodies
  or retrying permanent request errors.
- [ ] Run all LLM tests and verify green.

### Task 2: Use Saved Provider Defaults

**Files:**
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `tests/anki/test_web.py`

**Interfaces:**
- Consumes: `request.app.state.llm_settings.list()` and `.active()`.
- Produces: `defaults.provider`, `defaults.model`, and `provider_models`.

- [ ] Write a failing route test that changes the saved Anthropic model,
  activates Anthropic, and asserts the Anki bootstrap uses those saved values.
- [ ] Run the route test and verify the hardcoded defaults fail.
- [ ] Build the provider-model map once in `_page_context` and use the active
  preference for defaults.
- [ ] Run the route test and verify green.

### Task 3: Replace the Accordion with Dependent Selects

**Files:**
- Modify: `src/oms_hub/web/templates/anki.html`
- Modify: `src/oms_hub/web/static/anki.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `tests/js/anki.test.js`
- Modify: `tests/anki/test_web.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `courseOptions(lectures)`, `examOptions(lectures, course)`, and
  `lectureOptions(lectures, course, examNumber)`.
- Consumes: the existing quote-safe flat lecture payload.

- [ ] Write failing JavaScript tests for unique course options, course-scoped
  exam options, and course/exam-scoped lecture options.
- [ ] Write a failing template test for three labeled selects, initial disabled
  states, and absence of accordion markup.
- [ ] Run both focused test commands and verify red.
- [ ] Render the three native selects and provider-model JSON map.
- [ ] Bind Course change to reset and populate Exam; bind Exam change to reset
  and populate Lecture; bind Lecture change to source and editable-tag loading.
- [ ] Update provider change handling to fill its saved model.
- [ ] Replace accordion CSS with an equal three-column selector grid and mobile
  collapse.
- [ ] Update the README selector description.
- [ ] Run focused tests and verify green.

### Task 4: Verify, Commit, and Push

**Files:**
- Verify all modified production, test, design, plan, and documentation files.

**Interfaces:**
- Produces: a clean `codex/anki-v4-implementation` branch on GitHub.

- [ ] Run `pytest -q`.
- [ ] Run `node --test tests/js/*.test.js`.
- [ ] Run `ruff check src tests scripts`.
- [ ] Run `mypy src`.
- [ ] Run `git diff --check`.
- [ ] Commit with `fix: support Anthropic curation and cascading lectures`.
- [ ] Push `codex/anki-v4-implementation` and verify the remote commit.
