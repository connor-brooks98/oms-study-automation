# Study Hub V2 Multi-Provider Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconstruct the deployed Study Hub V2 baseline in the private repository and add secure, fully functional OpenAI, Gemini, and Anthropic credential management, provider selection, connection testing, diagnostics, and transcript routing.

**Architecture:** Provider-specific HTTP adapters implement one `LLMProvider` protocol. `LLMService` resolves the active provider and model from SQLite at the start of each job while retrieving credentials only from Windows Credential Manager. FastAPI Settings endpoints and a small JavaScript controller expose safe configuration and test operations without ever returning stored secrets.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, httpx, Jinja2, vanilla JavaScript, pytest, respx, Ruff, mypy, Windows Credential Manager via keyring.

## Global Constraints

- Preserve V1 in Git history and make all V2 changes on `codex/v2-multi-provider-settings`.
- Reconstruct the baseline from the full V2 package plus the deployed CSRF, tracker Windows, and tracker-preview hotfixes.
- Support OpenAI, Google Gemini, and Anthropic Claude as functional transcript providers.
- Store credentials only in Windows Credential Manager; never SQLite, `.env`, URLs, logs, rendered HTML, JSON responses, redirects, or browser storage.
- Preserve the existing `openai-api-key` keyring entry.
- Changing the active provider affects new jobs without restarting Study Hub.
- Protect all mutations with the existing Cloudflare Access and same-origin/CSRF boundaries.
- Return `Cache-Control: no-store` from credential, model, provider-selection, and test endpoints.
- Never make live provider requests from automated tests.
- Keep the deployed Windows root at `C:\Services\oms-study-automation-v2`.

---

## File Structure

New provider code is isolated under `src/oms_hub/llm/`:

- `domain.py`: provider names, normalized result types, diagnostic categories, and safe exceptions.
- `provider.py`: the `LLMProvider` protocol and shared HTTP error-classification helpers.
- `openai.py`, `gemini.py`, `anthropic.py`: provider-specific request and response logic.
- `repository.py`: non-secret SQLite preferences and safe last-test metadata.
- `service.py`: provider registry, active-provider resolution, credential lookup, connection testing, and transcript-cleaner facade.

Settings remains split by responsibility:

- `web/settings_routes.py`: HTTP request validation and sanitized responses.
- `web/templates/settings.html`: provider cards and active-provider form.
- `web/static/settings.js`: password visibility, asynchronous save/test, and status rendering.
- `web/static/app.css`: provider-card status styles.

Existing transcript code consumes the normalized service:

- `transcripts/cleaner.py`: retains the `TranscriptCleaner` protocol and fixed transcript safety constraints, but delegates provider calls.
- `transcripts/pipeline.py`: records provider and model from normalized results.
- `ingestion/repository.py` and `models.py`: persist non-secret provider attribution.

---

### Task 1: Reconstruct and Lock the Deployed V2 Baseline

**Files:**
- Replace: V1 application files with the full V2 package contents
- Overlay: deployed CSRF, tracker Windows, and tracker-preview hotfix files
- Preserve: `docs/superpowers/specs/2026-07-26-v2-multi-provider-credentials-design.md`
- Create: `tests/v2/test_baseline_smoke.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: the four locally preserved V2 package/hotfix archives
- Produces: the deployed V2 FastAPI application with `create_app(settings) -> FastAPI`, `/health`, `/settings`, upload routes, and the hotfixed tracker preview

- [ ] **Step 1: Add a failing V2 baseline smoke test**

```python
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def test_v2_health_and_settings_are_available(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
    )
    client = TestClient(create_app(settings))
    assert client.get("/health").json()["status"] == "ok"
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Lecture exam tracker" in page.text
```

- [ ] **Step 2: Run the smoke test against V1**

Run: `python -m pytest tests/v2/test_baseline_smoke.py -q`

Expected: FAIL because the V1 Settings page and V2 configuration contract are absent.

- [ ] **Step 3: Replace the branch contents with the deployed V2 baseline**

Extract the full V2 source package, then overlay the three hotfix packages in deployment order. Preserve `.git`, the approved spec, and this plan. Remove V1-only tracked files so the branch reflects one coherent V2 application rather than a mixed tree.

- [ ] **Step 4: Restore development dependencies and ignore rules**

Ensure `pyproject.toml` includes:

```toml
[project.optional-dependencies]
dev = [
  "mypy>=1.14,<2",
  "pytest>=8.3,<9",
  "respx>=0.22,<1",
  "ruff>=0.9,<1",
]
```

Ignore local caches, virtual environments, generated release archives, and temporary test data.

- [ ] **Step 5: Run baseline verification**

Run:

```bash
python -m pytest tests/v2/test_baseline_smoke.py -q
python -m ruff check src tests/v2
python -m mypy src/oms_hub
```

Expected: all commands pass.

- [ ] **Step 6: Commit the reconstructed baseline**

```bash
git add -A
git commit -m "feat: establish Study Hub V2 baseline"
```

---

### Task 2: Add Non-Secret Provider Preferences and Migration

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/ingestion/domain.py`
- Modify: `src/oms_hub/ingestion/repository.py`
- Create: `src/oms_hub/llm/__init__.py`
- Create: `src/oms_hub/llm/domain.py`
- Create: `src/oms_hub/llm/repository.py`
- Create: `tests/llm/test_repository.py`
- Create: `tests/v2/test_llm_migration.py`

**Interfaces:**
- Produces: `ProviderName`, `ProviderPreference`, `ProviderTestRecord`, `LLMSettingsRepository.get(provider)`, `LLMSettingsRepository.list()`, `LLMSettingsRepository.set_model(provider, model)`, `LLMSettingsRepository.set_active(provider)`, and `LLMSettingsRepository.record_test(...)`
- Produces: `IngestionRepository.record_study_usage(..., provider: str, model: str, ...)`

- [ ] **Step 1: Write failing repository and migration tests**

```python
def test_repository_seeds_openai_and_keeps_one_active_provider(database):
    repository = LLMSettingsRepository(database, default_openai_model="gpt-5.1")
    preferences = repository.list()
    assert [item.provider for item in preferences] == [
        ProviderName.OPENAI,
        ProviderName.GEMINI,
        ProviderName.ANTHROPIC,
    ]
    assert repository.active().provider is ProviderName.OPENAI


def test_existing_usage_rows_are_read_as_openai_after_migration(database_v2):
    migrate_database(database_v2)
    row = database_v2.session().execute(text(
        "select provider from study_usage limit 1"
    )).scalar_one()
    assert row == "openai"
```

- [ ] **Step 2: Run the new tests**

Run: `python -m pytest tests/llm/test_repository.py tests/v2/test_llm_migration.py -q`

Expected: FAIL because provider models, repository, and migration do not exist.

- [ ] **Step 3: Add provider domain types**

Define:

```python
class ProviderName(StrEnum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True, slots=True)
class ProviderPreference:
    provider: ProviderName
    model: str
    active: bool
    last_test_state: str | None
    last_tested_at: str | None
    diagnostic_source: str | None
    diagnostic_message: str | None
    http_status: int | None
    provider_request_id: str | None
```

- [ ] **Step 4: Add additive schema migration**

Add `LLMProviderSettingModel` keyed by provider. Increment the schema version.
Seed all three providers, seed OpenAI from the current configured model, and
make OpenAI active only when no active preference exists.

Add a non-null `provider` column to `study_usage` with server default
`"openai"`. Use an explicit SQLite `ALTER TABLE` migration so existing V2
databases receive the column; do not rely on `create_all()` to change tables.

- [ ] **Step 5: Implement `LLMSettingsRepository`**

Validate provider names through `ProviderName`, trim model values, reject empty
models, update one active row transactionally, and store only sanitized
connection metadata.

- [ ] **Step 6: Update usage recording**

Add `provider` to the usage domain model and repository write/read paths. Treat
legacy rows as OpenAI for display.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/llm/test_repository.py tests/v2/test_llm_migration.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py \
  src/oms_hub/ingestion src/oms_hub/llm tests/llm tests/v2/test_llm_migration.py
git commit -m "feat: persist non-secret LLM provider settings"
```

---

### Task 3: Implement Normalized Provider Adapters

**Files:**
- Create: `src/oms_hub/llm/provider.py`
- Create: `src/oms_hub/llm/openai.py`
- Create: `src/oms_hub/llm/gemini.py`
- Create: `src/oms_hub/llm/anthropic.py`
- Create: `tests/llm/test_openai.py`
- Create: `tests/llm/test_gemini.py`
- Create: `tests/llm/test_anthropic.py`
- Create: `tests/llm/test_error_classification.py`

**Interfaces:**
- Consumes: `ProviderName`
- Produces: `LLMProvider.clean(api_key, model, raw_text, prompt) -> CleanResult`
- Produces: `LLMProvider.test_connection(api_key, model) -> ProviderConnection`
- Produces: normalized `LLMRequestError` with `DiagnosticSource`, safe status, and safe provider request ID

- [ ] **Step 1: Write failing provider request/response tests**

Use `respx` to assert:

```python
assert request.headers["authorization"] == "Bearer secret"
assert request.url == "https://api.openai.com/v1/responses"

assert request.headers["x-goog-api-key"] == "secret"
assert request.url.path.endswith("/models/gemini-model:generateContent")

assert request.headers["x-api-key"] == "secret"
assert request.headers["anthropic-version"] == "2023-06-01"
assert request.url == "https://api.anthropic.com/v1/messages"
```

For every provider, verify cleaned text, model, request ID, input tokens, output
tokens, and safe cost fallback.

- [ ] **Step 2: Write failing classification tests**

Cover network timeout, authentication, invalid model, quota/rate limit,
provider 5xx, malformed JSON, incomplete result, and empty output. Assert that
the raised exception does not contain the API key or raw provider response.

- [ ] **Step 3: Run adapter tests**

Run: `python -m pytest tests/llm/test_openai.py tests/llm/test_gemini.py tests/llm/test_anthropic.py tests/llm/test_error_classification.py -q`

Expected: FAIL because adapters are absent.

- [ ] **Step 4: Implement the shared provider contract**

Define:

```python
class DiagnosticSource(StrEnum):
    STUDY_HUB = "study_hub"
    NETWORK = "network"
    AUTHENTICATION = "provider_authentication"
    MODEL = "provider_model"
    QUOTA = "provider_quota"
    SERVICE = "provider_service"


@dataclass(frozen=True, slots=True)
class CleanResult:
    text: str
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    provider: ProviderName
    model: str
    request_id: str | None
```

`LLMRequestError` exposes only a safe message, category, HTTP status, and
request ID.

- [ ] **Step 5: Implement OpenAI adapter**

Use the Responses API, the existing fixed transcript constraints, bearer
authentication, and existing response parsing behavior. Connection testing uses
a fixed minimal prompt and minimal output.

- [ ] **Step 6: Implement Gemini adapter**

Use `generateContent` with `x-goog-api-key`, `systemInstruction`, `contents`,
and bounded output. Parse `candidates[].content.parts[].text` and
`usageMetadata`.

- [ ] **Step 7: Implement Anthropic adapter**

Use Messages API with `x-api-key`, `anthropic-version`, a `system` prompt,
bounded `max_tokens`, and user content. Parse text content blocks and `usage`.

- [ ] **Step 8: Run adapter tests**

Run:

```bash
python -m pytest tests/llm -q
python -m ruff check src/oms_hub/llm tests/llm
python -m mypy src/oms_hub/llm
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/oms_hub/llm tests/llm
git commit -m "feat: add OpenAI Gemini and Anthropic adapters"
```

---

### Task 4: Route Transcript Jobs Through the Active Provider

**Files:**
- Create: `src/oms_hub/llm/service.py`
- Modify: `src/oms_hub/transcripts/cleaner.py`
- Modify: `src/oms_hub/transcripts/pipeline.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/ingestion/repository.py`
- Create: `tests/llm/test_service.py`
- Create: `tests/v2/test_multi_provider_pipeline.py`

**Interfaces:**
- Consumes: provider adapters, `LLMSettingsRepository`, `SecretStore`
- Produces: `LLMService.clean(raw_text, prompt) -> CleanResult`
- Produces: `LLMService.test_connection(provider) -> ConnectionDiagnostic`
- Produces: `LLMService.credential_configured(provider) -> bool`

- [ ] **Step 1: Write failing service tests**

Verify:

```python
def test_clean_resolves_active_provider_for_each_new_call(...):
    repository.set_active(ProviderName.GEMINI)
    first = service.clean("raw", prompt)
    repository.set_active(ProviderName.ANTHROPIC)
    second = service.clean("raw", prompt)
    assert first.provider is ProviderName.GEMINI
    assert second.provider is ProviderName.ANTHROPIC
```

Also verify missing credentials produce an authentication diagnostic without
calling a provider, and a running call uses the provider/model captured before
the preference changes.

- [ ] **Step 2: Write failing transcript pipeline test**

Process one mocked transcript through each active provider and assert the usage
row records the corresponding provider and model.

- [ ] **Step 3: Run focused tests**

Run: `python -m pytest tests/llm/test_service.py tests/v2/test_multi_provider_pipeline.py -q`

Expected: FAIL because `LLMService` and provider attribution are absent.

- [ ] **Step 4: Implement the provider registry and service**

Use fixed provider allowlists and keyring names:

```python
SECRET_KEYS = {
    ProviderName.OPENAI: "openai-api-key",
    ProviderName.GEMINI: "gemini-api-key",
    ProviderName.ANTHROPIC: "anthropic-api-key",
}
```

Resolve the active preference once per `clean()` invocation, then retrieve that
provider's credential and invoke only that adapter.

- [ ] **Step 5: Replace the fixed OpenAI app wiring**

Construct `LLMSettingsRepository`, the three adapters, and `LLMService` in
`create_app()`. Inject the service into `TranscriptPipeline` through the
existing `TranscriptCleaner` protocol.

- [ ] **Step 6: Persist provider attribution**

Pass `result.provider.value` when recording study usage. Keep existing output
validation, retries, immutable files, and lecture status behavior unchanged.

- [ ] **Step 7: Run focused and regression tests**

Run:

```bash
python -m pytest tests/llm/test_service.py tests/v2/test_multi_provider_pipeline.py -q
python -m pytest tests/v2/test_baseline_smoke.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/llm/service.py src/oms_hub/transcripts \
  src/oms_hub/ingestion/repository.py src/oms_hub/app.py tests
git commit -m "feat: route transcripts through active LLM provider"
```

---

### Task 5: Add Secure Settings Endpoints and Diagnostics

**Files:**
- Modify: `src/oms_hub/web/settings_routes.py`
- Create: `src/oms_hub/web/llm_schemas.py`
- Modify: `src/oms_hub/app.py`
- Create: `tests/v2/test_llm_settings_routes.py`
- Create: `tests/v2/test_llm_secret_safety.py`

**Interfaces:**
- Consumes: `LLMService`, `LLMSettingsRepository`, `SecretStore`
- Produces:
  - `POST /settings/ai/{provider}/credential`
  - `POST /settings/ai/{provider}/model`
  - `POST /settings/ai/{provider}/test`
  - `POST /settings/ai/active`

- [ ] **Step 1: Write failing endpoint tests**

Cover successful saves, blank-save retention, independent credentials, invalid
providers, empty models, selecting an unconfigured provider, successful tests,
all diagnostic categories, and `Cache-Control: no-store`.

Assert each JSON response has a safe shape:

```json
{
  "provider": "openai",
  "configured": true,
  "state": "connected",
  "tested_at": "2026-07-26T12:00:00+00:00",
  "diagnostic": null
}
```

- [ ] **Step 2: Write failing secret-leak tests**

Use a sentinel credential and assert it is absent from page HTML, JSON bodies,
redirect locations, captured logs, exception text, and safe diagnostics for
both success and failure paths.

- [ ] **Step 3: Run route tests**

Run: `python -m pytest tests/v2/test_llm_settings_routes.py tests/v2/test_llm_secret_safety.py -q`

Expected: FAIL because endpoints do not exist.

- [ ] **Step 4: Add request validation**

Use a strict provider path conversion, bounded credential length, bounded model
length, and explicit allowlists. A whitespace-only credential means retain the
existing credential. Never normalize or echo non-empty secret content.

- [ ] **Step 5: Implement credential and model endpoints**

Save credentials directly through `SecretStore`. Return configured state only.
Persist models through `LLMSettingsRepository`.

- [ ] **Step 6: Implement active-provider endpoint**

Reject activation unless `LLMService.credential_configured(provider)` is true.
Return the active provider and model only.

- [ ] **Step 7: Implement connection-test endpoint**

Generate a Study Hub correlation UUID, call the service, record safe test
metadata, and return a structured connected or failed result. Log only provider,
model, category, HTTP status, safe request ID, and correlation ID.

- [ ] **Step 8: Run route and security tests**

Run:

```bash
python -m pytest tests/v2/test_llm_settings_routes.py tests/v2/test_llm_secret_safety.py -q
python -m ruff check src/oms_hub/web tests/v2
python -m mypy src/oms_hub/web
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/oms_hub/web src/oms_hub/app.py tests/v2
git commit -m "feat: add secure LLM settings APIs"
```

---

### Task 6: Build the Multi-Provider Settings Interface

**Files:**
- Modify: `src/oms_hub/web/templates/settings.html`
- Create: `src/oms_hub/web/static/settings.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/templates/base.html`
- Create: `tests/v2/test_llm_settings_ui.py`
- Create: `tests/js/settings.test.js`

**Interfaces:**
- Consumes: provider summaries rendered by `settings_page()` and the Task 5 endpoints
- Produces: masked credential cards, show/hide controls, save/test actions, active-provider selection, and green/red diagnostics

- [ ] **Step 1: Write failing HTML contract tests**

Assert the Settings page renders three provider cards, password inputs, unique
show/hide buttons with accessible labels, configured state without secret
values, model inputs, test buttons, diagnostic regions with `aria-live`, and an
active-provider selector.

- [ ] **Step 2: Write failing JavaScript behavior tests**

Using Node's built-in test runner and a minimal fake DOM/fetch harness, verify:

- Show/hide toggles only the current input between `password` and `text`.
- Save never places the credential in a URL or rendered status.
- Test enters `Testing...`, then green `Connected` or red
  `Connection failed`.
- The safe diagnostic fields render without `innerHTML`.
- CSRF headers are sent on every mutation.

- [ ] **Step 3: Run UI tests**

Run:

```bash
python -m pytest tests/v2/test_llm_settings_ui.py -q
node --test tests/js/settings.test.js
```

Expected: FAIL because the UI is absent.

- [ ] **Step 4: Render provider cards**

Use server-provided provider summaries. Password inputs must have no `value`
attribute. Show only configured/not configured, selected model, last safe test
state, and test time.

- [ ] **Step 5: Implement accessible show/hide behavior**

Use `textContent`, `setAttribute`, and event listeners from the external
`settings.js`; do not add inline scripts because the Content Security Policy
allows only same-origin script files.

- [ ] **Step 6: Implement save, model, active-provider, and test actions**

Use `fetch` POST requests with the existing CSRF cookie/header. Disable the
relevant button while a request is in flight. Clear the credential field after
a successful save.

- [ ] **Step 7: Add status styling**

Add scoped classes for neutral/testing, connected green, and failed red states.
Keep text labels so state is not communicated by color alone.

- [ ] **Step 8: Run UI tests**

Run:

```bash
python -m pytest tests/v2/test_llm_settings_ui.py -q
node --test tests/js/settings.test.js
python -m ruff check src tests
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/oms_hub/web tests/v2/test_llm_settings_ui.py tests/js
git commit -m "feat: add multi-provider settings interface"
```

---

### Task 7: Complete Regression, Security, and NUC Release Packaging

**Files:**
- Modify: `README.md`
- Create: `docs/v2-multi-provider-nuc-rollout.md`
- Create: `scripts/build-v2-release.py`
- Create: `tests/v2/test_release_package.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: completed multi-provider implementation
- Produces: full V2 source ZIP, minimal hotfix ZIP, SHA-256 checksums, backup/install/rollback instructions

- [ ] **Step 1: Write a failing release-package test**

Assert the hotfix contains every runtime file changed after the reconstructed
baseline, excludes `.env`, databases, caches, credentials, and tests, and uses
paths relative to `C:\Services\oms-study-automation-v2`.

- [ ] **Step 2: Run the package test**

Run: `python -m pytest tests/v2/test_release_package.py -q`

Expected: FAIL because the builder does not exist.

- [ ] **Step 3: Implement deterministic release builder**

Build:

- `Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip`
- `Study-Hub-V2-Source-20260726.zip`
- matching `.sha256` files

Use a fixed sorted file manifest and reject files matching secret or runtime
data patterns.

- [ ] **Step 4: Write NUC rollout and rollback instructions**

Document checksum verification, file backup, scheduled-task stop, bounded port
shutdown, archive expansion, restart, bounded health check, three provider
connection tests, one sample transcript per provider, and rollback.

Explicitly instruct the user to revoke the key stored in `GPT Key.pdf` and enter
only replacement credentials through Settings.

- [ ] **Step 5: Run the complete verification suite**

Run:

```bash
python -m pytest -q
node --test tests/js/settings.test.js
python -m ruff check src tests scripts
python -m mypy src/oms_hub
git diff --check
```

Expected: all commands pass with no warnings.

- [ ] **Step 6: Build and inspect release archives**

Run:

```bash
python scripts/build-v2-release.py
unzip -l dist/Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip
unzip -l dist/Study-Hub-V2-Source-20260726.zip
```

Confirm no credential, `.env`, database, cache, transcript, or user document is
present.

- [ ] **Step 7: Commit release tooling and documentation**

```bash
git add README.md .env.example docs scripts tests/v2/test_release_package.py
git commit -m "docs: add V2 multi-provider NUC rollout"
```

- [ ] **Step 8: Final branch review**

Run:

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: clean worktree with only the planned commits on
`codex/v2-multi-provider-settings`.
