# Unified Provider Setup, Model Dropdowns, and Per-Task Assignments — Design

Date: 2026-08-04
Status: Approved (design review with owner)
Branch: codex/anki-v4-implementation

## Goal

OpenRouter becomes a fourth first-class LLM provider alongside OpenAI, Gemini, and
Anthropic. Every provider card in Settings gets a model dropdown (live-fetched, with a
custom escape hatch) instead of a free-text model input. Settings is the only place API
keys are entered. Each LLM-driven task — transcript cleaning, Anki curation, medical
accuracy review — binds its own provider + model, replacing the single global "active
provider".

Out of scope: NotebookLM/Studio quiz generation (browser-gateway based, uses no provider
API), any change to prompt content, any change to the accuracy-gate verdict semantics.

## Data model and migration

- `ProviderName` gains `OPENROUTER = "openrouter"`. `LLMSettingsRepository._ensure_defaults`
  seeds its `llm_provider_settings` row like the other three.
- New table `llm_task_assignments`:
  - `task` (primary key, string enum: `transcripts`, `anki_curation`, `accuracy_review`)
  - `provider` (ProviderName value)
  - `model` (non-empty string, same validation as provider model today)
  - `updated_at`
- Migration (in `migrations.py`, following the existing idempotent upgrade pattern):
  1. Create `llm_task_assignments` if missing.
  2. Seed `transcripts` and `anki_curation` from the current active provider row and its
     model.
  3. Seed `accuracy_review` with provider `openrouter` and the current
     `StudyAISettings.openrouter_model` value.
  4. Retire the `active` column usage (column may remain physically; code stops reading
     it) and stop reading `StudyAISettings.openrouter_model`.
- `accuracy_gate_enabled` on StudyAISettings is unchanged.
- The OpenRouter API key keeps its existing secret slot (`openrouter-api-key`) in the
  SecretStore; no re-entry needed. `SECRET_KEYS` gains the openrouter mapping.
- Model lists are not persisted. They are fetched on demand server-side with a short
  in-memory TTL cache (target: 1 hour) and curated fallback constants per provider in a
  new `llm/catalog.py`.

## Provider layer

- New `OpenRouterProvider` in `llm/openrouter.py` implementing the full `LLMProvider`
  interface (`clean`, `generate_text`, `test_connection`, structured generation) against
  `https://openrouter.ai/api/v1/chat/completions`. The request/response shaping is
  OpenAI-compatible; share helpers with `llm/openai.py` rather than duplicating them.
  Structured output uses OpenRouter's `response_format` JSON-schema passthrough.
- `LLMProvider` gains `list_models(api_key) -> tuple[str, ...]`:
  - OpenAI: `GET /v1/models`
  - Anthropic: `GET /v1/models`
  - Gemini: `GET /v1beta/models`
  - OpenRouter: `GET /api/v1/models`
  Filter to chat-capable models where the API exposes capability metadata; sort
  deterministically; on any failure return the curated fallback from `llm/catalog.py`.
  Errors are classified with the existing redacted error-classification helpers — API
  keys must never appear in exceptions, logs, or responses.
- `MedicalAccuracyGate` is refactored to resolve its provider, model, and credential
  through `LLMService` using the `accuracy_review` task assignment. It no longer owns an
  HTTP client, key lookup, or model setting. Verdict semantics (pass / review / fail,
  pause on missing key or malformed output) are unchanged.

## Service layer

- `LLMSettingsRepository` gains `assignment(task)` / `set_assignment(task, provider,
  model)` and drops `active()` / `set_active()`.
- `LLMService.for_task(task)` resolves assignment → provider adapter + model +
  credential. `clean()` resolves `transcripts`; the accuracy gate resolves
  `accuracy_review`; Anki curation resolves `anki_curation` as the default for the start
  form.
- Anki per-run override: the start form submits provider and model; the job pins both
  (extending the existing provider pinning). Historical jobs without a pinned model fall
  back to the provider's card model, preserving replayability.

## Web and UI

- Settings page: four identical provider cards (OpenAI, Gemini, Anthropic, OpenRouter),
  each with key input, Test connection, configured-state badge, and a model dropdown
  populated from `GET /api/settings/providers/{provider}/models`, always ending with a
  "Custom model ID…" option that reveals a text input. The existing
  "OpenRouter medical review" card is deleted; its enable toggle moves to the accuracy
  row of Task assignments. Key inputs on these cards are the only key-entry points in
  the app.
- New Task assignments section in Settings: three rows (Transcript cleaning, Anki
  curation default, Accuracy review). Each row: provider dropdown + dependent model
  dropdown (repopulated when the provider changes). The accuracy row includes the
  existing gate enable toggle. Saving posts to a new
  `PUT /api/settings/task-assignments/{task}` route (CSRF-protected like all settings
  mutations).
- Anki start form (`anki.html`): existing provider dropdown gains a dependent model
  dropdown, defaulting to the `anki_curation` assignment; both are submitted and pinned
  on the job.
- New settings JSON route `GET /api/settings/providers/{provider}/models` returns
  `{"models": [...], "source": "live" | "fallback"}` and never blocks the page render;
  the dropdowns load asynchronously with the current saved value preselected.

## Error handling

- Model fetch failure or missing key → curated fallback list plus a quiet inline notice
  on the card; never an error page.
- Assignment saved for a provider with no configured key → allowed, with a visible
  "key not configured" flag on the row. At run time: transcripts pause with the existing
  missing-credential diagnostic; the accuracy gate pauses publication (existing
  behavior); the Anki start form validates the selected provider's key before queueing.
- OpenRouter structured-output failures classify through the existing
  `StructuredOutputError` retry path. Known limitation: some OpenRouter models cannot
  reliably produce schema-valid JSON; Test connection is the early signal, and curation
  retries then fail with the existing diagnostics if a model is unsuitable.

## Testing

- `tests/llm/test_openrouter_provider.py`: respx coverage for chat, structured output,
  `list_models`, error classification, and key-redaction assertions mirroring the
  existing provider tests.
- Migration test: seeding of all three assignments from a pre-migration database with an
  active provider and an OpenRouter review model configured.
- Settings route tests: models endpoint (live fetch, TTL cache, fallback), assignment
  save/validation, no key material in any response.
- Accuracy gate tests updated to resolve through `LLMService`; verdict and pause
  behavior unchanged.
- JS tests (`tests/js/settings.test.js`): dropdown population, custom-option reveal,
  dependent model dropdown on assignment rows; `tests/js/anki.test.js`: start-form model
  dropdown defaults and submission.
- Update existing fixtures that enumerate providers (LLMService requires an adapter for
  every ProviderName member) to include OpenRouter.

## Notes

- Resolves part of REVIEW.md #21 (retires the dead `openrouter_model` settings path) and
  the provider-card model-input inconsistency.
- The `pywin32`/NUC deployment is unaffected; all new HTTP calls go through the existing
  httpx client patterns.
