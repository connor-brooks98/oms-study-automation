# Phase 3 Panopto Transcript Automation Implementation Plan

> **Superseded authentication section:** Task 2 and every
> client-credentials/Server Application reference in this historical plan are
> replaced by the approved Server-side Web Application correction in
> `docs/superpowers/specs/2026-07-23-phase-3-panopto-oauth-correction.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe, schedule-aware Panopto caption discovery, immutable transcript revision storage, automatic OpenAI cleaning, canonical transcript filing, and checklist updates to the existing Windows NUC Hub.

**Architecture:** A dedicated `oms_hub.panopto` package implements read-only Panopto OAuth/REST access and a four-stage `discover → download → clean → file` worker. Focused SQLite records make each stage idempotent and recoverable; Windows Credential Manager holds both secrets, the Obsidian note supplies the approved editable prompt, and existing catalog/checklist services remain authoritative.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, APScheduler 3, `httpx`, Pydantic Settings, Windows Credential Manager via `keyring`, SQLite, pytest, respx, Ruff, mypy.

## Global Constraints

- Preserve all Phase 1 and Phase 2 behavior, especially immutable Canvas originals and quarantine/revision handling.
- Panopto tenant URL is `https://lmunet.hosted.panopto.com/Panopto/Pages/Home.aspx`.
- Panopto authentication uses an unattended `Server Application`.
- Panopto access is read-only: never record, upload, edit, delete, share, or publish sessions or captions.
- Poll only Monday-Friday on Outlook-scheduled lecture days, every 15 minutes from 9:20 AM through 7:00 PM in `America/New_York`.
- Run a first-eligible-poll backfill for scheduled lectures still missing `transcript_downloaded`.
- Prefer captions with content language `English_USA`; do not silently use another language.
- Store the Panopto client secret and OpenAI API key only through the existing Windows Credential Manager `SecretStore`.
- Never put secrets or issued tokens in `.env`, SQLite, source control, command arguments, logs, screenshots, documentation, or test fixtures.
- Preserve every raw and cleaned revision under `C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions`.
- Use OpenAI Responses API model `gpt-5.6-terra` with reasoning effort `none` and `store: false`.
- Load the editable prompt from `C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md`.
- Require the current prompt SHA-256 to equal the explicitly approved prompt hash before automatic cleaning.
- Validate cleaned character length ratio from `0.60` through `1.25` of raw text.
- File only validated UTF-8 text under `%USERPROFILE%\Documents\OMS II\<Subject>\Exam <number>\Transcripts`.
- Complete `panopto_recording_found`, `transcript_downloaded`, `transcript_cleaned`, and `transcript_filed` only after the corresponding durable artifact exists.
- Use acceptance session `8796399e-393c-4256-b6e4-b48f0150d156`.
- Use no more than three automatic attempts per failed action; caption-not-ready is waiting, not a failed attempt.
- Keep the dashboard bound to localhost with existing trusted-host and cross-site POST protections.

---

## File Structure

### New production files

- `src/oms_hub/panopto/__init__.py` — package exports only.
- `src/oms_hub/panopto/domain.py` — Panopto enums and immutable value objects.
- `src/oms_hub/panopto/repository.py` — all Phase 3 persistence and job-claim operations.
- `src/oms_hub/panopto/auth.py` — in-memory OAuth client-credentials token provider.
- `src/oms_hub/panopto/client.py` — read-only Panopto REST and caption-download client.
- `src/oms_hub/panopto/matcher.py` — scheduled lecture/recording scoring.
- `src/oms_hub/panopto/discovery.py` — schedule gate, bounded searches, and match persistence.
- `src/oms_hub/panopto/prompt.py` — prompt initialization, hashing, approval validation, and fixed constraints.
- `src/oms_hub/panopto/openai_client.py` — Responses API request/response adapter and usage calculation.
- `src/oms_hub/panopto/pipeline.py` — immutable download, cleaning, filing, and recovery state machine.
- `src/oms_hub/web/panopto_routes.py` — local dashboard setup and review mutations.
- `src/oms_hub/web/templates/panopto_setup.html` — setup, credential-state, prompt, acceptance, and enablement UI.
- `src/oms_hub/web/templates/panopto_review.html` — ambiguous/failed job review UI.
- `docs/phase-3-nuc-rollout.md` — secret-safe rollout, validation, recovery, and rollback guide.

### New test files

- `tests/panopto/__init__.py`
- `tests/panopto/test_domain_repository.py`
- `tests/panopto/test_auth_client.py`
- `tests/panopto/test_matcher_discovery.py`
- `tests/panopto/test_prompt_openai.py`
- `tests/panopto/test_pipeline.py`
- `tests/panopto/test_panopto_web.py`
- `tests/panopto/test_phase3_acceptance.py`

### Existing files modified

- `src/oms_hub/models.py` — five new Phase 3 tables.
- `src/oms_hub/config.py` — validated Panopto/OpenAI/prompt/polling settings.
- `src/oms_hub/repositories.py` — scheduled-lecture and missing-transcript queries.
- `src/oms_hub/scheduler.py` — guarded Panopto poll and worker jobs.
- `src/oms_hub/app.py` — Phase 3 dependency wiring and router registration.
- `src/oms_hub/cli.py` — secret-safe setup and diagnostic commands.
- `src/oms_hub/web/routes.py` — dashboard summary context.
- `src/oms_hub/web/templates/dashboard.html` — Panopto status/review cards.
- `scripts/install-windows.ps1` — create immutable Panopto revision root without overwriting `.env`.
- `.env.example` — non-secret Phase 3 settings only.
- `README.md` — Phase 3 setup, operation, and remaining limitations.
- `tests/test_scheduler.py`, `tests/test_cli.py`, `tests/test_dashboard.py`, and `tests/test_windows_scripts.py` — integration coverage.

---

### Task 1: Phase 3 domain, configuration, schema, and repository

**Files:**
- Create: `src/oms_hub/panopto/__init__.py`
- Create: `src/oms_hub/panopto/domain.py`
- Create: `src/oms_hub/panopto/repository.py`
- Create: `tests/panopto/__init__.py`
- Create: `tests/panopto/test_domain_repository.py`
- Modify: `src/oms_hub/models.py:1-186`
- Modify: `src/oms_hub/config.py:9-34`
- Modify: `src/oms_hub/repositories.py:28-149`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `Database`, `CatalogRepository`, `LectureStepName`, `StepStatus`, and `models.utc_now`.
- Produces:
  - `PanoptoSession(session_id: str, name: str, created_utc: datetime, duration_seconds: float, folder_name: str, content_language: str | None, caption_download_url: str | None)`.
  - `RecordingMatch(lecture_id: int | None, confidence: float, evidence: tuple[str, ...], needs_review: bool)`.
  - `PanoptoRepository.connection() -> PanoptoConnectionModel`.
  - `PanoptoRepository.upsert_recording(session: PanoptoSession, match: RecordingMatch) -> RecordingDisposition`.
  - `PanoptoRepository.create_raw_revision(recording_id: int, raw_sha256: str, raw_path: str) -> TranscriptRevisionModel`.
  - `PanoptoRepository.queue_job(revision_id: int, action: TranscriptAction) -> None`.
  - `PanoptoRepository.claim_next_job(now_utc: datetime) -> TranscriptJobModel | None`.
  - `CatalogRepository.list_scheduled_between(start_utc: datetime, end_utc: datetime) -> list[LectureModel]`.
  - `CatalogRepository.list_missing_transcripts_before(end_utc: datetime) -> list[LectureModel]`.

- [ ] **Step 1: Write failing schema and repository tests**

```python
from datetime import UTC, datetime

from oms_hub.domain import LectureStepName
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch, TranscriptAction
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository, LectureInput


def test_recording_and_raw_revision_are_idempotent(database, tmp_path):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("MSK", 1, 6, "Shoulder Disease Injury and Treatment", "Silvers", None)
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM")
    repository = PanoptoRepository(database)
    session = PanoptoSession(
        "8796399e-393c-4256-b6e4-b48f0150d156",
        "6H. MSK Shoulder Disease Injury and Treatment",
        datetime(2026, 7, 23, 12, tzinfo=UTC),
        3600.0,
        "MSK",
        "English_USA",
        "https://captions.example/download",
    )
    disposition = repository.upsert_recording(
        session,
        RecordingMatch(lecture_id, 0.98, ("schedule", "topic"), False),
    )
    raw_path = tmp_path / "raw.txt"
    raw_path.write_text("shoulder transcript", encoding="utf-8")
    first = repository.create_raw_revision(disposition.recording_id, "a" * 64, str(raw_path))
    second = repository.create_raw_revision(disposition.recording_id, "a" * 64, str(raw_path))
    repository.queue_job(first.id, TranscriptAction.CLEAN)
    repository.queue_job(first.id, TranscriptAction.CLEAN)

    assert first.id == second.id
    assert repository.job_count(first.id, TranscriptAction.CLEAN) == 1
    assert catalog.get_lecture(lecture_id).steps[5].name == LectureStepName.PANOPTO_RECORDING_FOUND
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/panopto/test_domain_repository.py tests/test_config.py -q`

Expected: collection fails because `oms_hub.panopto.domain` and the Phase 3 settings/models do not exist.

- [ ] **Step 3: Add domain contracts and validated settings**

```python
# src/oms_hub/panopto/domain.py
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TranscriptAction(StrEnum):
    CLEAN = "clean"
    FILE = "file"


class TranscriptJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class PanoptoSession:
    session_id: str
    name: str
    created_utc: datetime
    duration_seconds: float
    folder_name: str
    content_language: str | None
    caption_download_url: str | None


@dataclass(frozen=True, slots=True)
class RecordingMatch:
    lecture_id: int | None
    confidence: float
    evidence: tuple[str, ...]
    needs_review: bool


@dataclass(frozen=True, slots=True)
class RecordingDisposition:
    recording_id: int
    created: bool
    needs_review: bool
```

Add these exact settings to `Settings`:

```python
panopto_tenant_url: str = "https://lmunet.hosted.panopto.com"
panopto_client_id: str | None = None
panopto_revision_root: Path = Path(
    r"C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions"
)
panopto_acceptance_session_id: str = "8796399e-393c-4256-b6e4-b48f0150d156"
panopto_poll_minutes: int = Field(default=15, ge=15, le=15)
panopto_poll_start: str = "09:20"
panopto_poll_end: str = "19:00"
panopto_max_caption_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
transcript_prompt_path: Path = Path(
    r"C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md"
)
transcript_min_clean_ratio: float = Field(default=0.60, ge=0.1, le=1.0)
transcript_max_clean_ratio: float = Field(default=1.25, ge=1.0, le=2.0)
openai_model: str = "gpt-5.6-terra"
openai_input_usd_per_million: float = Field(default=2.50, ge=0)
openai_output_usd_per_million: float = Field(default=15.00, ge=0)
```

- [ ] **Step 4: Add Phase 3 tables and repository transactions**

Add five tables with these uniqueness rules:

```python
class PanoptoConnectionModel(Base):
    __tablename__ = "panopto_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_url: Mapped[str] = mapped_column(String(300), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="disabled")
    enabled: Mapped[bool] = mapped_column(default=False)
    acceptance_validated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_successful_poll: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PanoptoRecordingModel(Base):
    __tablename__ = "panopto_recordings"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), unique=True)
    name: Mapped[str] = mapped_column(String(500))
    created_utc: Mapped[str] = mapped_column(String(40))
    duration_seconds: Mapped[float]
    folder_name: Mapped[str] = mapped_column(String(300), default="")
    content_language: Mapped[str | None] = mapped_column(String(60), nullable=True)
    lecture_id: Mapped[int | None] = mapped_column(ForeignKey("lectures.id"), nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    review_state: Mapped[str] = mapped_column(String(30), default="none")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class TranscriptRevisionModel(Base):
    __tablename__ = "transcript_revisions"
    __table_args__ = (UniqueConstraint("recording_id", "raw_sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("panopto_recordings.id"))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    raw_path: Mapped[str] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleaned_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleaned_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="downloaded")
    current: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class TranscriptJobModel(Base):
    __tablename__ = "transcript_jobs"
    __table_args__ = (UniqueConstraint("revision_id", "action"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("transcript_revisions.id"))
    action: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class OpenAIUsageModel(Base):
    __tablename__ = "openai_usage"
    __table_args__ = (UniqueConstraint("revision_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("transcript_revisions.id"))
    model: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_microusd: Mapped[int]
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
```

Implement repository methods as single-session transactions, truncate stored errors to 1,000 characters, JSON-encode only concise evidence, and never persist `caption_download_url`.

- [ ] **Step 5: Run tests, static checks, and commit**

Run:

```bash
pytest tests/panopto/test_domain_repository.py tests/test_config.py tests/test_repositories.py -q
ruff check src/oms_hub/panopto src/oms_hub/models.py src/oms_hub/config.py
mypy
```

Expected: all commands pass.

Commit:

```bash
git add src/oms_hub/panopto src/oms_hub/models.py src/oms_hub/config.py src/oms_hub/repositories.py tests/panopto tests/test_config.py tests/test_repositories.py
git commit -m "feat: add Panopto transcript persistence"
```

---

### Task 2: Panopto OAuth and read-only REST client

**Files:**
- Create: `src/oms_hub/panopto/auth.py`
- Create: `src/oms_hub/panopto/client.py`
- Create: `tests/panopto/test_auth_client.py`

**Interfaces:**
- Consumes: `SecretStore`, `PanoptoSession`, `httpx.Client`.
- Produces:
  - `PanoptoTokenProvider.access_token() -> str`.
  - `PanoptoClient.search_sessions(search_query: str, max_pages: int = 3) -> list[PanoptoSession]`.
  - `PanoptoClient.get_session(session_id: str) -> PanoptoSession`.
  - `PanoptoClient.download_captions(download_url: str, max_bytes: int) -> bytes`.
  - `PanoptoAuthenticationError`, `PanoptoPermissionError`, and `CaptionNotReady`.

- [ ] **Step 1: Write failing HTTP contract tests**

```python
import httpx
import respx

from oms_hub.panopto.auth import PanoptoTokenProvider
from oms_hub.panopto.client import CaptionNotReady, PanoptoClient


class MemorySecrets:
    def __init__(self):
        self.values = {"panopto-client-secret": "secret"}
    def get(self, key: str) -> str | None:
        return self.values.get(key)
    def set(self, key: str, value: str) -> None:
        self.values[key] = value
    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@respx.mock
def test_client_credentials_and_caption_download():
    token = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    session = respx.get(
        "https://lmunet.hosted.panopto.com/Panopto/api/v1/sessions/"
        "8796399e-393c-4256-b6e4-b48f0150d156"
    ).mock(return_value=httpx.Response(200, json={
        "Id": "8796399e-393c-4256-b6e4-b48f0150d156",
        "Name": "6H. MSK Shoulder",
        "CreatedDate": "2026-07-23T12:00:00Z",
        "Duration": 3600,
        "Folder": {"Name": "MSK"},
        "ContentLanguage": "English_USA",
        "Urls": {"CaptionDownloadUrl": "https://captions.example/file.txt"},
    }))
    captions = respx.get("https://captions.example/file.txt").mock(
        return_value=httpx.Response(200, text="shoulder transcript")
    )
    tokens = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
    )
    client = PanoptoClient("https://lmunet.hosted.panopto.com", tokens)

    value = client.get_session("8796399e-393c-4256-b6e4-b48f0150d156")
    assert client.download_captions(value.caption_download_url or "", 1024) == b"shoulder transcript"
    assert token.call_count == session.call_count == captions.call_count == 1


@respx.mock
def test_missing_caption_url_is_waiting():
    respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    respx.get(
        "https://lmunet.hosted.panopto.com/Panopto/api/v1/sessions/"
        "8796399e-393c-4256-b6e4-b48f0150d156"
    ).mock(return_value=httpx.Response(200, json={
        "Id": "8796399e-393c-4256-b6e4-b48f0150d156",
        "Name": "6H. MSK Shoulder",
        "CreatedDate": "2026-07-23T12:00:00Z",
        "Duration": 3600,
        "Folder": {"Name": "MSK"},
        "ContentLanguage": "English_USA",
        "Urls": {"CaptionDownloadUrl": None},
    }))
    tokens = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
    )
    client = PanoptoClient("https://lmunet.hosted.panopto.com", tokens)
    value = client.get_session("8796399e-393c-4256-b6e4-b48f0150d156")
    with pytest.raises(CaptionNotReady):
        client.download_captions(value.caption_download_url or "", 1024)
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `pytest tests/panopto/test_auth_client.py -q`

Expected: import failure because the token provider and client do not exist.

- [ ] **Step 3: Implement OAuth with an in-memory expiry cache**

```python
class PanoptoTokenProvider:
    def __init__(self, tenant_url: str, client_id: str, secrets: SecretStore, http=None):
        self.tenant_url = tenant_url.rstrip("/")
        self.client_id = client_id
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        secret = self.secrets.get("panopto-client-secret")
        if not secret:
            raise PanoptoAuthenticationError("Panopto client secret is not configured")
        response = self.http.post(
            f"{self.tenant_url}/Panopto/oauth2/connect/token",
            auth=(self.client_id, secret),
            data={"grant_type": "client_credentials", "scope": "api"},
        )
        if response.status_code in {400, 401, 403}:
            raise PanoptoAuthenticationError("Panopto client credentials were rejected")
        response.raise_for_status()
        payload = response.json()
        self._token = str(payload["access_token"])
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 300))
        return self._token
```

- [ ] **Step 4: Implement only verified Panopto GET operations**

Use the generated Panopto public schema endpoints:

```python
GET /Panopto/api/v1/sessions/search
GET /Panopto/api/v1/sessions/{id}
GET <Session.Urls.CaptionDownloadUrl>
```

`search_sessions` sends `searchQuery`, `sortField=CreatedDate`,
`sortOrder=Desc`, and zero-based `pageNumber`, stopping after an empty
`Results` list or `max_pages`. Every Panopto API request has
`Authorization: Bearer <token>`. The caption URL is used only in memory.
Reject redirects to `/Pages/Auth/`, HTML content types, responses above
`max_bytes`, 401/403 with sanitized typed errors, and non-`English_USA`
content in the discovery layer.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
pytest tests/panopto/test_auth_client.py -q
ruff check src/oms_hub/panopto/auth.py src/oms_hub/panopto/client.py
mypy
```

Expected: all commands pass and tests assert that secrets/tokens do not appear in exception text.

Commit:

```bash
git add src/oms_hub/panopto/auth.py src/oms_hub/panopto/client.py tests/panopto/test_auth_client.py
git commit -m "feat: add read-only Panopto client"
```

---

### Task 3: Schedule gate, recording matcher, and bounded discovery

**Files:**
- Create: `src/oms_hub/panopto/matcher.py`
- Create: `src/oms_hub/panopto/discovery.py`
- Create: `tests/panopto/test_matcher_discovery.py`

**Interfaces:**
- Consumes: `CatalogRepository.list_scheduled_between`, `PanoptoClient`, `PanoptoRepository`, `LectureModel`, `PanoptoSession`.
- Produces:
  - `PollingPolicy.eligible(now: datetime, scheduled: list[LectureModel], enabled: bool) -> bool`.
  - `RecordingMatcher.match(session: PanoptoSession, lectures: list[LectureModel]) -> RecordingMatch`.
  - `PanoptoDiscovery.poll(now: datetime, manual_session_id: str | None = None) -> DiscoverySummary`.

- [ ] **Step 1: Write failing schedule and matching tests**

```python
from datetime import UTC, datetime

from oms_hub.panopto.discovery import PollingPolicy
from oms_hub.panopto.matcher import RecordingMatcher


def test_polling_window_and_weekday_gate(scheduled_lecture):
    policy = PollingPolicy("America/New_York", "09:20", "19:00")
    assert not policy.eligible(
        datetime(2026, 7, 23, 13, 19, tzinfo=UTC), [scheduled_lecture], True
    )
    assert policy.eligible(
        datetime(2026, 7, 23, 13, 20, tzinfo=UTC), [scheduled_lecture], True
    )
    assert policy.eligible(
        datetime(2026, 7, 23, 23, 0, tzinfo=UTC), [scheduled_lecture], True
    )
    assert not policy.eligible(
        datetime(2026, 7, 23, 23, 1, tzinfo=UTC), [scheduled_lecture], True
    )


def test_matcher_requires_schedule_and_topic_evidence(msk_session, scheduled_lecture):
    match = RecordingMatcher().match(msk_session, [scheduled_lecture])
    assert match.lecture_id == scheduled_lecture.id
    assert match.confidence >= 0.90
    assert not match.needs_review


def test_only_unmatched_recording_is_not_enough(msk_session, scheduled_lecture):
    unrelated = replace(msk_session, name="Unrelated Grand Rounds")
    match = RecordingMatcher().match(unrelated, [scheduled_lecture])
    assert match.lecture_id is None
    assert match.needs_review
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/panopto/test_matcher_discovery.py -q`

Expected: import failure because matcher and discovery policy do not exist.

- [ ] **Step 3: Implement deterministic eligibility and scoring**

Use `ZoneInfo`, existing title normalization helpers, and these score weights:

```python
SCHEDULE_SAME_DAY = 0.35
SCHEDULE_WITHIN_TWO_HOURS = 0.20
SUBJECT_EVIDENCE = 0.20
LECTURE_NUMBER_EVIDENCE = 0.10
TOPIC_SIMILARITY_MAX = 0.10
LECTURER_EVIDENCE = 0.05
AUTO_MATCH_THRESHOLD = 0.90
REVIEW_MARGIN = 0.10
```

Require same-local-day evidence and at least one title/subject/lecturer signal.
If the best candidate is below `0.90` or within `0.10` of the runner-up, return
`lecture_id=None` and `needs_review=True`.

- [ ] **Step 4: Implement bounded discovery and checklist transition**

For each scheduled lecture, construct at most three nonempty searches:
`"<subject> <topic>"`, `"<lecture_number> <topic>"`, and `"<lecturer> <topic>"`.
Deduplicate results by session ID, discard results outside the local scheduled
date plus one-day backfill window, fetch each candidate by ID to obtain
`CaptionDownloadUrl`, and cap a poll at 100 distinct sessions.

Persist confident matches and complete `panopto_recording_found` with detail
`Panopto session <session-id> matched: <evidence>`. Persist ambiguous records
without downloading captions.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
pytest tests/panopto/test_matcher_discovery.py tests/test_checklist.py -q
ruff check src/oms_hub/panopto/matcher.py src/oms_hub/panopto/discovery.py
mypy
```

Expected: all commands pass.

Commit:

```bash
git add src/oms_hub/panopto/matcher.py src/oms_hub/panopto/discovery.py tests/panopto/test_matcher_discovery.py
git commit -m "feat: discover scheduled Panopto recordings"
```

---

### Task 4: Obsidian prompt contract and OpenAI Responses adapter

**Files:**
- Create: `src/oms_hub/panopto/prompt.py`
- Create: `src/oms_hub/panopto/openai_client.py`
- Create: `tests/panopto/test_prompt_openai.py`

**Interfaces:**
- Consumes: `SecretStore`, `Settings`, `httpx.Client`.
- Produces:
  - `PromptLoader.current() -> ApprovedPrompt`.
  - `PromptLoader.initialize() -> Path`.
  - `ApprovedPrompt(text: str, sha256: str)`.
  - `OpenAITranscriptCleaner.clean(raw_text: str, prompt: ApprovedPrompt) -> CleanResult`.
  - `CleanResult(text: str, model: str, request_id: str, input_tokens: int, output_tokens: int, cost_microusd: int)`.

- [ ] **Step 1: Write failing prompt and Responses API tests**

```python
import httpx
import respx

from oms_hub.panopto.openai_client import OpenAITranscriptCleaner
from oms_hub.panopto.prompt import PromptLoader, PromptNotApproved


def test_prompt_must_match_approved_hash(tmp_path):
    path = tmp_path / "Transcript Cleaning.md"
    path.write_text("Remove filler words but preserve facts.", encoding="utf-8")
    loader = PromptLoader(path, approved_sha256=None)
    with pytest.raises(PromptNotApproved):
        loader.current()


@respx.mock
def test_responses_request_disables_reasoning_and_records_usage(approved_prompt, secrets):
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json={
            "id": "resp_123",
            "status": "completed",
            "model": "gpt-5.6-terra",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "Cleaned shoulder transcript."}],
            }],
            "usage": {"input_tokens": 11000, "output_tokens": 9000},
        })
    )
    cleaner = OpenAITranscriptCleaner(
        secrets, "gpt-5.6-terra", 2.50, 15.00
    )
    result = cleaner.clean("Raw shoulder transcript.", approved_prompt)
    request = route.calls[0].request

    assert b'"effort":"none"' in request.content
    assert b'"store":false' in request.content
    assert result.cost_microusd == 162_500
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/panopto/test_prompt_openai.py -q`

Expected: import failure because prompt and OpenAI adapters do not exist.

- [ ] **Step 3: Implement the prompt initializer and approval check**

Use this starter body and do not overwrite an existing note:

```markdown
# Transcript Cleaning

Remove verbal filler and false starts. Correct obvious transcription errors,
especially medical terminology, only when the intended wording is clear from
context. Add readable paragraphs and headings while preserving every
substantive fact, qualification, example, caution, and question.
```

`PromptLoader.current()` reads at most 64 KiB, rejects empty/non-UTF-8 notes,
computes SHA-256 over the exact bytes, and raises `PromptNotApproved` unless the
hash equals `approved_sha256`.

- [ ] **Step 4: Implement the raw Responses API adapter**

Send:

```python
payload = {
    "model": self.model,
    "store": False,
    "reasoning": {"effort": "none"},
    "instructions": FIXED_TRANSCRIPT_CONSTRAINTS,
    "input": [{
        "role": "user",
        "content": [{
            "type": "input_text",
            "text": (
                "<editable_prompt>\n" + prompt.text + "\n</editable_prompt>\n"
                "<raw_transcript>\n" + raw_text + "\n</raw_transcript>"
            ),
        }],
    }],
}
```

Read `openai-api-key` from `SecretStore`, set `Authorization: Bearer`, reject
non-`completed` responses, concatenate only `output_text` items from assistant
message output, and calculate:

```python
cost_microusd = round(
    input_tokens * input_usd_per_million
    + output_tokens * output_usd_per_million
)
```

Because one token at one USD per million equals one micro-USD, no additional
million divisor is applied.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
pytest tests/panopto/test_prompt_openai.py -q
ruff check src/oms_hub/panopto/prompt.py src/oms_hub/panopto/openai_client.py
mypy
```

Expected: all commands pass; tests cover 401, 429, timeout, incomplete status,
missing output text, missing usage, and sanitized errors.

Commit:

```bash
git add src/oms_hub/panopto/prompt.py src/oms_hub/panopto/openai_client.py tests/panopto/test_prompt_openai.py
git commit -m "feat: clean transcripts with OpenAI"
```

---

### Task 5: Immutable transcript pipeline, filing, retries, and recovery

**Files:**
- Create: `src/oms_hub/panopto/pipeline.py`
- Create: `tests/panopto/test_pipeline.py`

**Interfaces:**
- Consumes: `PanoptoClient`, `PanoptoRepository`, `CatalogRepository`, `PromptLoader`, `OpenAITranscriptCleaner`, `Settings`, `verified_atomic_copy`, `sha256_file`, and `artifact_names`.
- Produces:
  - `TranscriptPipeline.ingest_captions(recording_id: int, download_url: str) -> int`.
  - `TranscriptPipeline.run_next(now: datetime | None = None) -> bool`.
  - `TranscriptPipeline.recover_abandoned_jobs() -> RecoverySummary`.
  - `TranscriptPipeline.retry_job(job_id: int) -> None`.

- [ ] **Step 1: Write failing end-to-end pipeline tests**

```python
def test_download_clean_file_and_checklist(
    database, tmp_path, prepared_panopto, fake_panopto, fake_cleaner
):
    pipeline, catalog, lecture_id, recording_id = prepared_panopto(
        fake_panopto=fake_panopto,
        cleaner=fake_cleaner,
        raw_text="Raw shoulder transcript with substantive detail.",
    )
    revision_id = pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")
    assert pipeline.run_next()
    assert pipeline.run_next()

    lecture = catalog.get_lecture(lecture_id)
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses["transcript_downloaded"] == "complete"
    assert statuses["transcript_cleaned"] == "complete"
    assert statuses["transcript_filed"] == "complete"
    revision = pipeline.repository.get_revision(revision_id)
    assert Path(revision.raw_path).read_text(encoding="utf-8").startswith("Raw shoulder")
    assert Path(revision.canonical_path or "").name.endswith("Transcript.txt")


def test_identical_caption_hash_does_not_call_openai_twice(prepared_panopto):
    pipeline, _, _, recording_id = prepared_panopto(raw_text="same transcript")
    first = pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")
    second = pipeline.ingest_captions(recording_id, "https://captions.example/file.txt")
    assert first == second
    assert pipeline.repository.job_count(first, TranscriptAction.CLEAN) == 1
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `pytest tests/panopto/test_pipeline.py -q`

Expected: import failure because `TranscriptPipeline` does not exist.

- [ ] **Step 3: Implement immutable raw download**

Validate the response before storage:

```python
def validate_raw_caption(payload: bytes, max_bytes: int) -> str:
    if not payload or len(payload) > max_bytes:
        raise TranscriptValidationError("caption payload size is invalid")
    prefix = payload[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"{\"error\"")):
        raise TranscriptValidationError("caption response is not plain text")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranscriptValidationError("caption response is not UTF-8") from error
    if not text.strip():
        raise TranscriptValidationError("caption response is empty")
    return text
```

Write to `<panopto_revision_root>/<allocated-revision-id>/raw.txt` through a
temporary sibling, fsync, atomic replace, and SHA-256 verification. Queue one
`clean` job only after the immutable file and database row are durable.

- [ ] **Step 4: Implement cleaning and filing stages**

For `clean`, verify the raw hash, load the approved prompt, call OpenAI, and
enforce:

```python
ratio = len(cleaned_text) / len(raw_text)
if not settings.transcript_min_clean_ratio <= ratio <= settings.transcript_max_clean_ratio:
    raise TranscriptNeedsReview(
        f"cleaned length ratio {ratio:.2f} is outside "
        f"{settings.transcript_min_clean_ratio:.2f}-"
        f"{settings.transcript_max_clean_ratio:.2f}"
    )
```

Store immutable `cleaned.txt`, its hash, prompt hash, and `OpenAIUsageModel` in
one repository completion transaction. Complete `transcript_cleaned` and queue
`file`.

For `file`, resolve:

```python
destination = (
    expanded_study_root
    / lecture.subject
    / f"Exam {lecture.exam_number}"
    / "Transcripts"
    / artifact_names(LectureKey(
        lecture.subject,
        lecture.exam_number,
        lecture.lecture_number,
        lecture.topic,
    )).transcript
)
```

Prove the destination is beneath `study_root`, call `verified_atomic_copy`,
mark earlier revisions non-current, mark this revision current, and complete
`transcript_filed`.

- [ ] **Step 5: Implement retries and startup recovery**

Map:

- Caption unavailable: job/recording remains waiting and consumes no attempt.
- HTTP 429/timeout/5xx: increment attempts and set `next_attempt_at` using
  `min(15 * 2 ** (attempts - 1), 120)` minutes plus deterministic testable
  jitter.
- Third transient failure: `failed`.
- Prompt mismatch, ambiguous match, validation ratio, 401/403, or path
  containment failure: `needs_review`.
- Startup `running` clean jobs: requeue if immutable raw hash matches.
- Startup `running` file jobs: mark complete if canonical hash already matches;
  otherwise requeue.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
pytest tests/panopto/test_pipeline.py tests/test_checklist.py tests/files/test_atomic.py -q
ruff check src/oms_hub/panopto/pipeline.py
mypy
```

Expected: all commands pass, including corrected-caption replacement, failed
clean preservation, path escape, truncation, three-attempt exhaustion, and
restart recovery cases.

Commit:

```bash
git add src/oms_hub/panopto/pipeline.py tests/panopto/test_pipeline.py
git commit -m "feat: process immutable transcript revisions"
```

---

### Task 6: Scheduler, application wiring, and secret-safe CLI

**Files:**
- Modify: `src/oms_hub/scheduler.py:12-47`
- Modify: `src/oms_hub/app.py:11-87`
- Modify: `src/oms_hub/cli.py:1-199`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: all Task 1-5 service constructors.
- Produces:
  - `build_scheduler(timezone: str, sync_once: Callable[[], None] | None, canvas_worker_once: Callable[[], object] | None = None, panopto_poll_once: Callable[[], object] | None = None, panopto_worker_once: Callable[[], object] | None = None) -> BackgroundScheduler`.
  - CLI commands `panopto-set-secret`, `openai-set-key`, `panopto-init-prompt`, `panopto-approve-prompt`, `panopto-status`, `panopto-scan-once`, `panopto-worker-once`, and `panopto-recover`.

- [ ] **Step 1: Write failing scheduler and CLI tests**

```python
def test_scheduler_has_guarded_panopto_jobs():
    scheduler = build_scheduler(
        "America/New_York",
        None,
        None,
        panopto_poll_once=lambda: None,
        panopto_worker_once=lambda: False,
    )
    assert scheduler.get_job("panopto-poll").trigger.interval.total_seconds() == 900
    assert scheduler.get_job("panopto-worker").trigger.interval.total_seconds() == 5


def test_secret_commands_use_getpass_not_arguments(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(["panopto-set-secret"])
    assert not hasattr(args, "secret")
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/test_scheduler.py tests/test_cli.py -q`

Expected: scheduler signature and CLI command assertions fail.

- [ ] **Step 3: Wire services into `create_app`**

Create one shared `KeyringSecretStore` and attach:

```python
app.state.panopto_repository = PanoptoRepository(database)
app.state.panopto_tokens = PanoptoTokenProvider(
    resolved.panopto_tenant_url,
    resolved.panopto_client_id or "",
    secrets,
)
app.state.panopto_client = PanoptoClient(
    resolved.panopto_tenant_url,
    app.state.panopto_tokens,
)
app.state.panopto_prompt = PromptLoader(
    resolved.transcript_prompt_path,
    app.state.panopto_repository.connection().approved_prompt_sha256,
)
app.state.openai_cleaner = OpenAITranscriptCleaner(
    secrets,
    resolved.openai_model,
    resolved.openai_input_usd_per_million,
    resolved.openai_output_usd_per_million,
)
app.state.panopto_pipeline = TranscriptPipeline(
    app.state.panopto_repository,
    CatalogRepository(database),
    app.state.panopto_client,
    app.state.panopto_prompt,
    app.state.openai_cleaner,
    resolved,
)
app.state.panopto_discovery = PanoptoDiscovery(
    CatalogRepository(database),
    app.state.panopto_repository,
    app.state.panopto_client,
    RecordingMatcher(),
    PollingPolicy(
        resolved.timezone,
        resolved.panopto_poll_start,
        resolved.panopto_poll_end,
    ),
)
```

Constructing the app must not fetch tokens or make network calls.

- [ ] **Step 4: Add guarded scheduler jobs**

```python
if panopto_poll_once is not None:
    scheduler.add_job(
        guarded_panopto_poll,
        "interval",
        minutes=15,
        id="panopto-poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
if panopto_worker_once is not None:
    scheduler.add_job(
        panopto_worker_once,
        "interval",
        seconds=5,
        id="panopto-worker",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
```

The discovery policy, not APScheduler, enforces weekday and local clock
eligibility.

- [ ] **Step 5: Add secret-safe CLI handlers**

Use `getpass.getpass()` inside handlers:

```python
def panopto_set_secret(args: argparse.Namespace) -> int:
    del args
    value = getpass.getpass("Panopto client secret: ")
    if not value:
        raise SystemExit("Secret cannot be empty")
    KeyringSecretStore().set("panopto-client-secret", value)
    print("Panopto client secret stored in Windows Credential Manager")
    return 0
```

Implement the OpenAI equivalent, prompt initialization/approval by current
hash, diagnostics, manual acceptance-session scan, one worker action, and
recovery. Never print a secret or token.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
pytest tests/test_scheduler.py tests/test_cli.py tests/test_health.py -q
ruff check src/oms_hub/app.py src/oms_hub/cli.py src/oms_hub/scheduler.py
mypy
```

Expected: all commands pass and existing Canvas/Outlook scheduler tests remain
unchanged except for the explicit new optional arguments.

Commit:

```bash
git add src/oms_hub/app.py src/oms_hub/cli.py src/oms_hub/scheduler.py tests/test_scheduler.py tests/test_cli.py
git commit -m "feat: schedule Panopto transcript jobs"
```

---

### Task 7: Dashboard setup, controls, and review

**Files:**
- Create: `src/oms_hub/web/panopto_routes.py`
- Create: `src/oms_hub/web/templates/panopto_setup.html`
- Create: `src/oms_hub/web/templates/panopto_review.html`
- Create: `tests/panopto/test_panopto_web.py`
- Modify: `src/oms_hub/app.py:21-77`
- Modify: `src/oms_hub/web/routes.py`
- Modify: `src/oms_hub/web/templates/dashboard.html`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `app.state.panopto_repository`, `panopto_discovery`, `panopto_pipeline`, `panopto_prompt`, and existing template helpers.
- Produces:
  - `GET /panopto/setup`
  - `POST /panopto/prompt/initialize`
  - `POST /panopto/prompt/approve`
  - `POST /panopto/acceptance/validate`
  - `POST /panopto/enable`
  - `POST /panopto/pause`
  - `POST /panopto/scan`
  - `GET /panopto/review`
  - `POST /panopto/review/{recording_id}/remap`
  - `POST /panopto/jobs/{job_id}/retry`

- [ ] **Step 1: Write failing local-web workflow tests**

```python
def test_setup_never_renders_secrets(client):
    page = client.get("/panopto/setup")
    assert page.status_code == 200
    assert "Panopto client secret" not in page.text
    assert "OpenAI API key" not in page.text
    assert "Configured" in page.text or "Not configured" in page.text


def test_enable_requires_acceptance_and_current_prompt(client, repository, prompt_file):
    response = client.post("/panopto/enable", follow_redirects=False)
    assert response.status_code == 409
    repository.mark_acceptance_validated()
    repository.approve_prompt(sha256_file(prompt_file))
    response = client.post("/panopto/enable", follow_redirects=False)
    assert response.status_code == 303
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/panopto/test_panopto_web.py tests/test_dashboard.py -q`

Expected: 404 responses because Panopto web routes do not exist.

- [ ] **Step 3: Implement setup and mutation guards**

Keep credentials out of forms. Setup displays only:

- Client ID configured/not configured.
- Credential Manager secret present/not present.
- Prompt path, readable state, current hash, approved hash, and changed state.
- Acceptance session result.
- Discovery enabled/paused status.

`POST /panopto/enable` returns 409 unless acceptance is validated, the current
prompt hash equals the approved hash, both secrets are present, and a Panopto
client ID is configured. All POSTs use existing origin protections and redirect
with a concise status message.

- [ ] **Step 4: Implement review and dashboard cards**

Review rows show session name/ID, proposed lecture evidence, concise error, job
stage/attempts, remap lecture selector, and retry action. They never show raw or
cleaned transcript bodies.

Dashboard cards show connection state, last poll, next window, pending review
count, recent input/output token totals, and cost formatted from
`cost_microusd / 1_000_000`.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
pytest tests/panopto/test_panopto_web.py tests/test_dashboard.py -q
ruff check src/oms_hub/web/panopto_routes.py src/oms_hub/web/routes.py
mypy
```

Expected: all commands pass, including cross-site POST rejection, invalid
lecture remap, prompt changed after approval, pause, retry, and no-secret HTML.

Commit:

```bash
git add src/oms_hub/web src/oms_hub/app.py tests/panopto/test_panopto_web.py tests/test_dashboard.py
git commit -m "feat: add Panopto dashboard controls"
```

---

### Task 8: Phase 3 acceptance, Windows rollout, and regression gate

**Files:**
- Create: `tests/panopto/test_phase3_acceptance.py`
- Create: `docs/phase-3-nuc-rollout.md`
- Modify: `scripts/install-windows.ps1`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_windows_scripts.py`

**Interfaces:**
- Consumes: complete Phase 3 application surface.
- Produces: offline end-to-end acceptance fixture, NUC rollout instructions,
  required non-secret environment settings, and final regression evidence.

- [ ] **Step 1: Write the failing offline acceptance test**

```python
def test_schedule_to_panopto_to_cleaned_transcript_acceptance(
    app, database, tmp_path, fake_panopto, fake_openai
):
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("MSK", 1, 6, "Shoulder Disease Injury and Treatment", "Silvers", None)
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM 101")
    app.state.panopto_discovery.poll(
        datetime(2026, 7, 23, 13, 20, tzinfo=UTC)
    )
    while app.state.panopto_pipeline.run_next():
        pass

    lecture = catalog.get_lecture(lecture_id)
    statuses = {step.name: step.status for step in lecture.steps}
    assert statuses["panopto_recording_found"] == "complete"
    assert statuses["transcript_downloaded"] == "complete"
    assert statuses["transcript_cleaned"] == "complete"
    assert statuses["transcript_filed"] == "complete"
    filed = list((tmp_path / "OMS II" / "MSK" / "Exam 1" / "Transcripts").glob("*.txt"))
    assert len(filed) == 1

    app.state.panopto_discovery.poll(
        datetime(2026, 7, 23, 13, 35, tzinfo=UTC)
    )
    assert fake_openai.call_count == 1
```

- [ ] **Step 2: Run the acceptance test and verify failure**

Run: `pytest tests/panopto/test_phase3_acceptance.py -q`

Expected: failure until all wiring, fixtures, and idempotent transitions are
complete.

- [ ] **Step 3: Complete Windows and environment setup**

Add only non-secret values to `.env.example`:

```dotenv
OMS_HUB_PANOPTO_TENANT_URL=https://lmunet.hosted.panopto.com
OMS_HUB_PANOPTO_CLIENT_ID=
OMS_HUB_PANOPTO_REVISION_ROOT=C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions
OMS_HUB_PANOPTO_ACCEPTANCE_SESSION_ID=8796399e-393c-4256-b6e4-b48f0150d156
OMS_HUB_PANOPTO_POLL_MINUTES=15
OMS_HUB_PANOPTO_POLL_START=09:20
OMS_HUB_PANOPTO_POLL_END=19:00
OMS_HUB_TRANSCRIPT_PROMPT_PATH=C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md
OMS_HUB_OPENAI_MODEL=gpt-5.6-terra
```

Update `install-windows.ps1` to create the Panopto revision directory with the
same ProgramData ownership pattern as Canvas revisions. It must preserve an
existing `.env`, must not create secret files, and must not modify the Obsidian
note.

- [ ] **Step 4: Write rollout and recovery documentation**

Document these exact live steps:

1. Pull and install on `C:\Services\oms-study-automation`.
2. Set `OMS_HUB_PANOPTO_CLIENT_ID`.
3. Run `oms-hub panopto-set-secret`.
4. Run `oms-hub openai-set-key`.
5. Run `oms-hub panopto-init-prompt`, edit the note, then run
   `oms-hub panopto-approve-prompt`.
6. Run acceptance-session discovery in paused/discovery-only mode.
7. Confirm the English caption, MSK match, destination preview, and immutable
   raw path.
8. Enable automatic processing, verify one transcript and an identical rescan.
9. Confirm Canvas Neuro and Heme/Lymph still process unchanged.
10. Explain pause, retry, recovery, credential rotation, and rollback without
    deleting the database or revision roots.

- [ ] **Step 5: Run the full verification gate**

Run:

```bash
pytest -q
ruff check .
mypy
git diff --check
```

Expected: all tests and static checks pass with no whitespace errors.

Run extension regression tests:

```bash
cd extension/canvas-hub
npm test
```

Expected: all Canvas extension tests pass.

- [ ] **Step 6: Perform a secret scan**

Run:

```bash
rg -n -i "client_secret|api[_-]?key|bearer [a-z0-9_-]{12,}|sk-[a-z0-9_-]+" . \
  -g '!docs/superpowers/**' -g '!.git/**'
```

Expected: only key names, safe prompts, tests with obvious dummy values, and
documentation warnings; no real credential-shaped value.

- [ ] **Step 7: Commit the rollout and acceptance gate**

```bash
git add tests/panopto/test_phase3_acceptance.py docs/phase-3-nuc-rollout.md scripts/install-windows.ps1 .env.example README.md tests/test_windows_scripts.py
git commit -m "docs: add Phase 3 rollout and acceptance"
```

---

## Implementation Completion Gate

Before declaring Phase 3 complete:

- Every task commit exists and the worktree is clean.
- `pytest -q`, `ruff check .`, `mypy`, Canvas extension tests, and
  `git diff --check` pass.
- No real Panopto or OpenAI secret appears in repository history or logs.
- The live NUC acceptance session succeeds with read-only Panopto operations.
- The representative MSK transcript produces a validated canonical transcript.
- The second identical scan creates no new revision, OpenAI request, or file.
- A controlled corrected-caption fixture retains the prior immutable revision.
- Outlook unavailable state disables scheduled discovery without disabling
  manual acceptance validation.
- Canvas Neuro and Heme/Lymph acceptance behavior remains unchanged.
