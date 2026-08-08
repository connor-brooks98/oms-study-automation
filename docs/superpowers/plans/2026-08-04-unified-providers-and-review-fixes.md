# Unified Providers, Anki Source Cards, Voyage Fix, and Review Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved unified provider/model-selection spec, the Anki source-card visual redesign, the Voyage HTTP 400 fix, and the Blocker/Major plus safe-Minor findings from REVIEW.md, on branch `codex/anki-v4-implementation` (base `fa24acd`).

**Architecture:** OpenRouter joins `ProviderName` with a full `LLMProvider` adapter; a new `llm_task_assignments` table replaces the global active provider; settings UI gets four uniform provider cards with live-fetched model dropdowns plus a Task assignments section. Independent fix tasks follow existing module patterns.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, httpx + respx, Jinja2 + vanilla JS (`node --test`), pytest, mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-08-04-unified-provider-model-selection-design.md`

## Global Constraints

- mypy strict must pass (`.venv/bin/mypy` or `mypy` — packages `oms_hub`, `oms_anki_agent`); ruff must pass; `pytest` warnings are errors (`filterwarnings = ["error"]`).
- API keys must never appear in exceptions, logs, HTML, or JSON responses.
- All settings mutations are CSRF-protected browser routes (follow existing `settings_routes.py` patterns).
- JS is framework-free; tests run with `node --test tests/js`.
- Do not touch NotebookLM/Studio browser-gateway generation.
- Migrations must be idempotent (safe to run twice) — follow existing `migrations.py` upgrade patterns.
- Each task commits its own work with a conventional-commit message ending in the Claude co-author trailer.
- Deferred (do NOT do in this plan): REVIEW.md #46–47, #49, #51–57 (heavy refactors/policy decisions), and cascade retrofit beyond the FK pragma.

---

### Task 1: OpenRouter provider adapter, model catalog, and `list_models`

**Files:**
- Create: `src/oms_hub/llm/catalog.py`
- Modify: `src/oms_hub/llm/domain.py` (add `OPENROUTER` to `ProviderName`)
- Modify: `src/oms_hub/llm/openrouter.py` (add `OpenRouterProvider`; keep `MedicalAccuracyGate` for Task 3)
- Modify: `src/oms_hub/llm/provider.py` (add `list_models` to the `LLMProvider` protocol/ABC)
- Modify: `src/oms_hub/llm/openai.py`, `anthropic.py`, `gemini.py` (implement `list_models`)
- Test: `tests/llm/test_openrouter_provider.py`, extend `tests/llm/test_openai.py` etc. for `list_models`

**Interfaces:**
- Produces: `ProviderName.OPENROUTER = "openrouter"`; `LLMProvider.list_models(api_key: str) -> tuple[str, ...]`; `OpenRouterProvider(http: httpx.Client | AsyncClient per existing provider pattern)` implementing `clean`, `generate_text`, `test_connection`, structured generation identically to `OpenAIProvider`'s signatures; `catalog.FALLBACK_MODELS: Mapping[ProviderName, tuple[str, ...]]`.
- Consumes: existing `LLMRequestError` classification helpers, `SECRET_KEYS` (extended in Task 2).

**Steps:**
- [ ] Read `src/oms_hub/llm/openai.py` end to end first; `OpenRouterProvider` mirrors its request/response shaping with base URL `https://openrouter.ai/api/v1`, header `Authorization: Bearer {key}`, chat endpoint `/chat/completions`, structured output via `response_format={"type": "json_schema", "json_schema": {...}}`. Extract shared OpenAI-format helpers into module-level functions in `openai.py` and import them rather than copy-pasting.
- [ ] `catalog.py`: fallback lists —
  openai: `("gpt-5.2", "gpt-5.2-mini", "gpt-5.1", "gpt-4.1")`;
  anthropic: `("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")`;
  gemini: `("gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro", "gemini-2.5-flash")`;
  openrouter: reuse the entries currently in `settings_routes.py:_OPENROUTER_MODELS` (move them here; leave a re-import shim if referenced elsewhere).
- [ ] `list_models` per provider: OpenAI `GET /v1/models` (ids), Anthropic `GET /v1/models` (`data[].id`, header `x-api-key` + `anthropic-version` as in existing client), Gemini `GET /v1beta/models?key=...` (`models[].name` stripped of `models/` prefix, filtered to entries whose `supportedGenerationMethods` contains `generateContent`), OpenRouter `GET /api/v1/models` (`data[].id`). Sort deterministically. Any exception → raise `LLMRequestError` with redacted message (callers fall back to catalog; fallback decision lives in the settings route, Task 4).
- [ ] Write respx tests first for each `list_models` (success, HTTP 401, network error → assert the raised message contains no key material) and for `OpenRouterProvider` chat + structured + `test_connection` (mirror the structure of `tests/llm/test_openai.py`).
- [ ] Run `pytest tests/llm -q`, `mypy`, `ruff check src tests`. Commit: `feat: add OpenRouter provider and model listing`.

### Task 2: Task assignments — models, repository, migration, LLMService

**Files:**
- Modify: `src/oms_hub/models.py` (new `LLMTaskAssignmentModel`: `task` PK string, `provider`, `model`, `updated_at`)
- Modify: `src/oms_hub/llm/domain.py` (new `class LLMTask(StrEnum): TRANSCRIPTS = "transcripts"; ANKI_CURATION = "anki_curation"; ACCURACY_REVIEW = "accuracy_review"`; dataclass `TaskAssignment(task, provider, model)`)
- Modify: `src/oms_hub/llm/repository.py` (add `assignment(task) -> TaskAssignment`, `set_assignment(task, provider, model)`; keep `active()` temporarily until Task 3 removes the last caller, then delete it in Task 3)
- Modify: `src/oms_hub/llm/service.py` (add `for_task(task) -> tuple[LLMProvider, str, str]` returning (adapter, model, api_key); `clean()` resolves `LLMTask.TRANSCRIPTS`)
- Modify: `src/oms_hub/migrations.py` (create table if missing; seed rows only when absent: `transcripts`/`anki_curation` from the row where `active` is true and its `model`; `accuracy_review` from `openrouter` + `study_ai_settings.openrouter_model` when that column/value exists, else first catalog fallback)
- Modify: `src/oms_hub/llm/secrets` mapping (`SECRET_KEYS[ProviderName.OPENROUTER] = "openrouter-api-key"` — find the existing constant, currently `OPENROUTER_API_KEY_SECRET` in the gate/settings code, and unify on it)
- Modify: `src/oms_hub/app.py` (register `OpenRouterProvider` in the providers mapping passed to `LLMService` — `LLMService.__init__` requires every `ProviderName` member)
- Test: `tests/llm/test_repository.py`, `tests/llm/test_service.py`, `tests/study_generation/test_migration.py` (or `tests/anki/test_migrations.py` following where LLM migrations are tested)

**Interfaces:**
- Produces: `LLMTask`, `TaskAssignment`, `LLMSettingsRepository.assignment/set_assignment`, `LLMService.for_task`.
- Consumes: Task 1's `ProviderName.OPENROUTER`, `catalog.FALLBACK_MODELS`.

**Steps:**
- [ ] Failing tests first: `assignment()` returns seeded default when table empty (defaults seeded by `_ensure_defaults`-style logic: provider from former active row or `anthropic`, model from provider settings); `set_assignment` round-trips; migration test builds a pre-migration DB with an active provider + `openrouter_model`, runs migrations twice (idempotency), asserts the three seeded rows.
- [ ] Implement; `for_task` raises the existing missing-credential error type when the key is unset (same error `clean()` raises today) so worker pause behavior is unchanged.
- [ ] `pytest tests/llm tests/study_generation/test_migration.py -q`, mypy, ruff. Commit: `feat: add per-task LLM assignments`.

### Task 3: Accuracy gate through LLMService; retire `active` and `openrouter_model`

**Files:**
- Modify: `src/oms_hub/llm/openrouter.py` (`MedicalAccuracyGate` drops its own HTTP/key/model handling; constructor takes `LLMService` + `StudyAISettingsRepository`; resolves `LLMTask.ACCURACY_REVIEW` via `service.for_task`; verdict parsing/pause semantics unchanged)
- Modify: `src/oms_hub/app.py:461` (gate wiring), `src/oms_hub/web/settings_routes.py` (`_openrouter_context`, `save_accuracy_gate`, test route — gate toggle stays; key/model settings move to the unified system)
- Modify: `src/oms_hub/llm/repository.py` (delete `active()`/`set_active()`), `src/oms_hub/study_generation/ai_settings.py` (stop reading `openrouter_model`)
- Test: existing gate tests under `tests/llm/` and `tests/v2/` — update fixtures; add a test that the gate uses the `accuracy_review` assignment's provider/model.

**Steps:**
- [ ] Grep for every `active(` / `set_active` / `openrouter_model` / `OPENROUTER_API_KEY_SECRET` caller before editing; update all of them (including templates/JS references found in Tasks 4–5).
- [ ] Update tests, run `pytest tests/llm tests/v2 tests/study_generation -q`, mypy, ruff. Commit: `refactor: route accuracy gate through task assignments`.

### Task 4: Settings API — models endpoint and assignment routes

**Files:**
- Modify: `src/oms_hub/web/settings_routes.py`, `src/oms_hub/web/llm_schemas.py`
- Test: settings route tests (follow existing settings tests, likely `tests/anki/test_settings.py` / `tests/v2/test_generation_settings.py` naming — put new ones in `tests/v2/test_llm_settings_routes.py`)

**Interfaces:**
- Produces: `GET /api/settings/providers/{provider}/models` → `{"models": [...], "source": "live"|"fallback"}`; `PUT /api/settings/task-assignments/{task}` body `{"provider": "...", "model": "..."}` → saved assignment JSON; settings page context gains `providers` (4 entries, each with `configured`, `model`) and `assignments` (3 rows with `provider`, `model`, `key_configured`).
- Consumes: Tasks 1–3.

**Steps:**
- [ ] Models endpoint: resolve key from SecretStore; if key missing or `list_models` raises → return catalog fallback with `"source": "fallback"` (HTTP 200 always). In-process TTL cache: module-level `dict[ProviderName, tuple[float, tuple[str, ...]]]` guarded by a `threading.Lock`, TTL 3600s; only cache live results.
- [ ] Assignment PUT: validate task/provider enums (404 unknown task, 422 unknown provider, 422 empty model ≤200 chars — reuse `_validated_model` rules); response includes `key_configured` bool.
- [ ] Tests: fallback when no key; live path via respx; cache hit (second call, respx asserts one request); assignment validation cases; assert no secret values in any response body. Run, mypy, ruff. Commit: `feat: add provider model and task assignment APIs`.

### Task 5: Settings UI — four provider cards, dropdowns, Task assignments section

**Files:**
- Modify: `src/oms_hub/web/templates/settings.html` (OpenRouter becomes a fourth provider card iterating the same `providers` loop; delete the "OpenRouter medical review" card; new "Task assignments" section with three rows + the accuracy enable toggle on the accuracy row)
- Modify: `src/oms_hub/web/static/settings.js`
- Test: `tests/js/settings.test.js`

**Steps:**
- [ ] Model dropdown per card: `<select data-model-select>` populated on load from the models endpoint (existing saved model preselected; if saved model not in list, insert it at top). Last option `value="__custom__"` labeled `Custom model ID…` revealing `<input data-model-custom maxlength="200">`. Save posts the resolved model string through the existing save-model route.
- [ ] Task assignments rows: provider `<select>` (4 options) + dependent model `<select>` re-fetched on provider change; Save button per row → `PUT /api/settings/task-assignments/{task}`; show `key not configured` inline flag from response. Disable each row's controls while a request is in flight (REVIEW #31 convention).
- [ ] While editing this file, also fix REVIEW #30 (guard `response.json()` parses in `postJson`/`getJson` — copy the guarded pattern from `anki.js:27–32`) and #31 (disable prompt-path Save/Select/Test buttons during flight).
- [ ] `node --test tests/js` covering: dropdown population from mocked fetch, custom reveal, dependent repopulation, in-flight disabling, non-JSON error handling. Commit: `feat: unify provider cards and task assignment settings UI`.

### Task 6: Anki start form — model dropdown + pinned model

**Files:**
- Modify: `src/oms_hub/web/templates/anki.html` (model `<select>` next to the provider select), `src/oms_hub/web/static/anki.js`, `src/oms_hub/web/anki_routes.py` (accept + validate `model` on job creation; default from `anki_curation` assignment; include in `provider_models` context), anki job creation path (`anki/repository.py` / domain — extend existing provider pinning with `model`; jobs without a stored model fall back to the provider-card model as today)
- Test: `tests/anki/test_web.py`, `tests/js/anki.test.js`

**Steps:**
- [ ] Failing tests: POST job with explicit model pins it; POST without model uses the `anki_curation` assignment; stage execution uses the pinned model (locate where `_provider(context)` resolves the model — `stages.py` — and thread the pinned model through the same context).
- [ ] Implement; run `pytest tests/anki -q`, `node --test tests/js`, mypy, ruff. Commit: `feat: pin provider and model per anki curation run`.

### Task 7: Anki source cards — remove checkboxes, whole-card green/red

**Files:**
- Modify: `src/oms_hub/web/static/anki.js` (`renderSourceOptions` region, ~lines 260–304, and form submit ~line 655), `src/oms_hub/web/static/app.css` (`.anki-source-option*` rules, ~779–830), `src/oms_hub/web/templates/anki.html:73–77` (fieldset copy if needed)
- Test: `tests/js/anki.test.js`

**Steps:**
- [ ] EDGE CASE (verified): today's checkboxes are functional — checked `input[name=source_revision_ids]` are collected at `anki.js:655` and submitted. Replace each ready source's visible checkbox with `<input type="hidden" name="source_revision_ids" value="...">` so submission is unchanged; the submit-time collector must switch from `:checked` selector to reading the hidden inputs.
- [ ] Remove the `✓`/`×`/`–` `anki-source-lock` glyph entirely. Card states: ready → solid green treatment (`background: color-mix(in srgb, var(--complete) 14%, white); border-color: var(--complete)`), missing/bad → solid red treatment (same formula with `var(--failed)`), neutral/unknown → current muted look. Keep text contrast ≥4.5:1 (dark text on the tinted backgrounds). Keep the existing `<small>` detail line ("Ready"/"Not found or not ready") since color alone must not carry meaning.
- [ ] This also resolves REVIEW #7's `/anki` glyph symptom; still define `--accent` in Task 9.
- [ ] Update/extend `tests/js/anki.test.js` assertions that reference source checkboxes. Run `node --test tests/js`. Commit: `feat: color-coded anki source cards without checkboxes`.

### Task 8: Voyage embeddings — surface 400 detail and adaptive batch splitting

**Files:**
- Modify: `src/oms_hub/anki/semantic/voyage.py`
- Test: Voyage client tests (grep for `VoyageEmbeddingClient` under `tests/anki/` — likely `test_embeddings.py` or a `semantic` test module)

**Steps:**
- [ ] Context: user hits `Voyage embedding batch 0 failed with HTTP 400` on `voyage-4-large` (a valid model; params `output_dimension: 1024`, `output_dtype: "float"` are valid per Voyage docs). The response body is currently discarded (`voyage.py:149–154`). Two changes:
  1. On non-retryable HTTP errors, extract `detail` from the JSON body (fall back to first 200 chars of text; never include the API key) and append it: `Voyage embedding batch {i} failed with HTTP {status}{suffix}: {detail}`.
  2. Adaptive splitting: if a 400 body's detail matches token/size limits (case-insensitive regex `token|too long|max allowed|payload|limit`) and the batch has >1 text, split the batch in half and embed the halves recursively (preserving order) instead of failing; only raise when a single text still 400s. Also lower `_MAX_BATCH_CHARACTERS` from 400_000 to 280_000 (~80K tokens at 3.5 chars/token, under the 120K-token request limit with margin).
- [ ] Failing tests first (respx): 400 with `{"detail": "max allowed tokens..."}` on the full batch then 200s on each half → success, correct order, correct vector count; 400 with non-limit detail → raises with the detail text in the message; single-text 400 → raises; message never contains the api key.
- [ ] Run `pytest tests/anki -q -k "voyage or embed"`, mypy, ruff. Commit: `fix: surface Voyage error detail and split oversized batches adaptively`.

### Task 9: REVIEW.md Blocker + UI Majors (#1, #6, #7, #8, #9, #10, #11, #12, #17, #18)

**Files:**
- Modify: `src/oms_hub/web/static/anki.js`, `lecture.js`, `notebook_studio.js`, `public_quiz.js`, `public_quiz_library.js`, `app.css`, `tokens.css`, `templates/public_quiz.html`, `templates/studio_quiz_preview.html`
- Test: `tests/js/*.test.js`

**Steps (each is an independent fix; REVIEW.md numbers refer to the report — line numbers may have drifted ±20 since `fa24acd`; locate by the quoted code, not the number):**
- [ ] #1: in `anki.js` apply-plan flow, add a `close` listener on the confirm `<dialog>` that re-enables "Save review" and "Review apply plan" when the dialog closes without confirmation (guard a `confirmed` flag set by the proceed handler).
- [ ] #6: wrap the rescheduled `refreshJob` calls (`setTimeout(() => { refreshJob().catch(handlePollError); }, delay)`); `handlePollError` shows the error in the existing job message region and retries with doubled delay capped at 30s. Apply the same pattern to `lecture.js:60–70`.
- [ ] #7: define `--accent` in `tokens.css` as an alias of the existing focus/brand color (inspect tokens.css and pick the focus-visible color); confirm `.password-control:focus-within` shows an outline.
- [ ] #8: add global `button:disabled, .button:disabled { opacity: .55; cursor: not-allowed; }` in `app.css` near the base button rules (~178); remove the now-redundant scoped rule at ~1162 if identical.
- [ ] #9: disable submit buttons during POST in `notebook_studio.js` attach-source and run forms (`button.disabled = true` in a `try/finally`).
- [ ] #10: in `notebook_studio.js` poll catch (~246): keep lists rendered, write the error to a status element, and re-call `scheduleRefresh()` with backoff.
- [ ] #11: wrap `public_quiz.js` `initialize` body in try/catch rendering the existing could-not-load state on any failure (including `response.json()` throw).
- [ ] #12: add `confirm()` guards to library-page global reset, per-quiz reset, and the results-screen "Start Over".
- [ ] #17: darken `--text-3` in `tokens.css` to reach ≥4.5:1 on white (target oklch lightness ≈0.51; verify against `--surface` values) and introduce `--review-text` (darkened orange ≥4.5:1) used where `--review` colors text (app.css ~381).
- [ ] #18: add `tabindex="-1"` to the player `<main>` in `public_quiz.html:16–23` and `studio_quiz_preview.html:26–33`; after re-render, focus the previously focused control's equivalent (track `data-focus-key` on interactive elements; fall back to the player container).
- [ ] Update/extend JS tests for #1, #6, #11, #12 behaviors. Run `node --test tests/js`. Commit: `fix: apply review UI blocker and major fixes`.

### Task 10: REVIEW.md data/correctness Majors (#3, #4, #5, #13, #15-pragma, #16) + #48-retryable

**Files:**
- Modify: `src/oms_hub/anki/repository.py`, `src/oms_hub/anki/models.py`, `src/oms_hub/migrations.py`, `src/oms_hub/db.py`, `src/oms_hub/anki/worker.py`, `src/oms_hub/ingestion/worker.py`, `src/oms_hub/study_generation/studio_worker.py`, `src/oms_hub/study_generation/studio_repository.py`
- Test: `tests/anki/test_anki_repository.py`, `tests/anki/test_migrations.py`, `tests/anki/test_worker.py`, `tests/study_generation/*` (db-level tests: grep where `Database` is tested first)

**Steps:**
- [ ] #3: in `save_review` gap-edit lookup (`repository.py:953–967` area): when `edit.card_id` is blank, count gap cards for `(job_id, concept_id)`; if >1 raise the same validation error type used for other bad review payloads with message `gap card edit requires card_id when a concept has multiple cards`. Failing test: two cards sharing a concept + blank card_id edit → error; single card + blank card_id still works.
- [ ] #4: add `Index("ix_anki_gap_cards_job_concept", "job_id", "concept_id")` to `AnkiGapCardModel` and to the rebuilt DDL inside `_upgrade_gap_card_identity`; new idempotent migration step `CREATE INDEX IF NOT EXISTS` for existing DBs. Test: run migrations on a pre-existing DB and assert the index exists (`PRAGMA index_list('anki_gap_cards')`).
- [ ] #5: in each of the three workers' failure classification, treat `is_sqlite_busy(error)` (import from `oms_hub.db`) as retryable/transient exactly as `study_generation/worker.py:367–374` does; for `studio_worker.py`'s bare `except Exception`, split: busy → leave run leased/retry (mirror the ingestion worker's transient path), else `fail_run`. Unit tests with a fake raising a SQLAlchemy `OperationalError` wrapping `sqlite3.OperationalError("database is locked")`.
- [ ] #13: in `await_image_review` (`studio_repository.py:445–487`): before deleting requirement rows, collect `asset_path`s of rows being replaced and `Path.unlink(missing_ok=True)` files not referenced by any published-quiz media row. Test: re-entry deletes the orphaned file; published/still-referenced files survive.
- [ ] #15 (pragma only): in `db.py:_configure_sqlite_connection`, execute `PRAGMA foreign_keys=ON` alongside the existing pragmas. Run the FULL pytest suite after this step specifically — if any existing test fails on FK enforcement, fix the ordering bug it exposes (do not revert the pragma; report it).
- [ ] #16: add to `queue_run` an `IntegrityError` catch translating to the existing `ValueError("...already in use...")`, backed by a new partial unique index migration: `CREATE UNIQUE INDEX IF NOT EXISTS ix_studio_runs_active_label ON studio_runs (destination_subject_key, destination_exam_number, label_key) WHERE state IN (...)` — read `studio_domain.py` first for the real active-state values and column names; adjust to actuals. Test: two inserts, second raises the ValueError.
- [ ] #48 (S variant): add `SemanticSnapshotError` to `AnkiCurationWorker._is_retryable`'s retryable set.
- [ ] Run `pytest -q` (full), mypy, ruff. Commit: `fix: apply review data and correctness fixes`.

### Task 11: REVIEW.md safe Minors + CI + docs

**Files:**
- Modify per item below; Create: `.github/workflows/ci.yml`
- Test: run full suites at the end.

**Steps:**
- [ ] #19: serve `tokens.css` from the `public_quiz_routes.py` asset allowlist (~43–73) since `studio_quiz_preview.html:11` links it.
- [ ] #20: delete `FastEmbedder` from `anki/embeddings.py`; drop `fastembed` from `pyproject.toml`.
- [ ] #21/#24: delete the seven dead config fields (`generation_timeout_seconds`, `anki_agent_heartbeat_max_age_seconds`, `anki_snapshot_max_age_hours`, `anki_embedding_model`, `anki_image_low/medium/high_estimate_usd`) from `config.py`, their `.env.example` lines, and the validation test at `tests/anki/test_settings.py:62`; add the ~11 missing real settings to `.env.example` with current defaults (enumerate from `config.py`).
- [ ] #22: change `config.py` `dashboard_port` default 8765 → 8787.
- [ ] #23: change `transcript_prompt_path` default to `None` (type `Path | None`; grep and guard all call sites first).
- [ ] #25: delete `Verdict` and `EnvelopeOperationType` from `anki/domain.py`.
- [ ] #26–#29 (anki.js): catch on jobs Refresh writing to `#anki-jobs-message`; bind view-switcher/search listeners once in `initializeReview` (guard flag); split dialog status vs error nodes (status text goes to a non-alert element); call `refreshJob()` after successful apply.
- [ ] #32: disable studio image upload button during upload; replace the "Loading image requirements…" placeholder with an error + retry button when the initial fetch fails.
- [ ] #33: co-locate delete-source errors with the sources list; add a loading state on exam switch; `role="button"` on the dropzone; move `aria-live` off the rebuilt `<ul>` onto a compact status line.
- [ ] #34: `aria-label="Quiz progress"` on the progressbar; persistent `role="status"` feedback container; width/height attributes on quiz images when the media payload includes dimensions (check it; skip if absent); add the chevron rotation rule to `public_quiz_library.css` (copy from `app.css:237`).
- [ ] #35: `aria-pressed` on the review view-switcher buttons, toggled in JS.
- [ ] #36: add CSS for `checkbox-inline`, `studio-image-card`, `is-overridden`, `studio-image-upload`, `studio-preview-actions`, `studio-preview-link` (minimal card/label styling consistent with `app.css` card patterns; `is-overridden` gets a visible badge/border).
- [ ] #37: set `AutomationSecurity = msoAutomationSecurityForceDisable` (value 3) on the Office COM application object in `files/office.py:_convert_sync` before opening documents (guard with `getattr`/try; Windows-only path).
- [ ] #38: in `app.py` mutation branch (~396–425), require the CSRF token check for loopback requests too (same gate as public); update tests that relied on tokenless loopback POSTs (grep tests for the middleware behavior first — if dozens depend on it, add the token to the shared test client helper rather than each test).
- [ ] #39: shrink `scripts/build-v2-release.py` `HOTFIX_FILES` to only the non-`src/` entries.
- [ ] #40: define `class SyncWorker(Protocol)` (confirm the real method signatures from `ingestion/worker.py`) in `src/oms_hub/workers.py`; use it in `app.py:120–131` and `cli.py:56`, deleting the `type: ignore` comments.
- [ ] #44: add `[tool.ruff.lint] select = ["E", "F", "I", "UP", "B"]` to `pyproject.toml`; `ruff check --fix` for mechanical fixes; if the remaining violation count is huge, keep `I`/`UP`/`B` scoped via per-file-ignores rather than reformatting the whole repo.
- [ ] #45: README dev section — add one-liners for `mypy`, `ruff check`, `pytest`, `node --test tests/js`.
- [ ] #50: batch-load `StudioRunSourceModel` rows for all runs in `list_runs` (single `IN` query grouped in Python) and add `limit: int = 50`.
- [ ] #14: create `.github/workflows/ci.yml`: on push/PR — Python 3.12, `pip install -e ".[dev]"`, `ruff check src tests scripts`, `mypy`, `pytest -q`, Node 22 `node --test tests/js`. Verify `windows_office`-marked tests skip off-Windows; add `-m "not windows_office"` if needed.
- [ ] Full verification: `pytest -q`, `node --test tests/js`, `mypy`, `ruff check`. Commit: `chore: apply review minor fixes and add CI`.

### Task 12: Final integration verification and push

**Steps:**
- [ ] `pytest -q` full suite green; `node --test tests/js` green; `mypy` clean; `ruff check` clean.
- [ ] Smoke via existing route tests: settings page renders with 4 provider cards; anki page renders; public quiz page renders.
- [ ] Do NOT re-add REVIEW.md to the repo (it lives in the user's Downloads).
- [ ] `git push origin codex/anki-v4-implementation`.
- [ ] Report: what shipped, what was deferred (#46–47, #49, #51–57, FK cascades), and NUC follow-ups (stale `.env` vars removed in #21/#24; transcripts/anki assignments seeded from the old active provider).
