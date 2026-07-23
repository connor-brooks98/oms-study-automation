# Phase 3 Panopto Browser Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Panopto API/OAuth access with the existing paired Chrome companion, scan recordings in **Shared with Me**, extract complete rendered English transcripts, and feed the existing immutable cleaning and filing pipeline.

**Architecture:** The local Hub owns scheduling, commands, matching, validation, immutable storage, cleaning, and checklist state. The existing Chrome companion owns one temporary inactive Panopto tab, uses the browser's authenticated session without reading cookies, and returns only bounded recording metadata or transcript text through strict bearer-authenticated localhost endpoints.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite, pytest, Chrome Manifest V3 JavaScript, Node's built-in test runner, OpenAI Responses API.

## Global Constraints

- Panopto origin is exactly `https://lmunet.hosted.panopto.com`.
- Automatic polling is every 15 minutes, Monday-Friday, 09:20-19:00 America/New_York, and only on scheduled lecture days.
- The companion polls local commands once per minute.
- Never request Chrome's `cookies` permission or serialize cookies, authorization headers, Panopto HTML, or unrelated page text.
- Host permissions remain limited to LMU Canvas, LMU Panopto, and `http://127.0.0.1:8765`.
- Panopto browser actions are read-only.
- Only extension-owned tabs may be navigated or closed.
- Preserve existing ProgramData immutable revisions, verified atomic writes, quarantine behavior, automatic `gpt-5.6-terra` cleaning, and checklist transitions.
- Preserve the untracked user file `src/oms_hub/panopto/auth 2.py`; never stage, edit, or delete it.
- Keep implementation on the feature branch until live NUC acceptance passes.

## File structure

- `src/oms_hub/panopto/browser_domain.py` — strict browser command, recording, extraction, and reason-code value objects.
- `src/oms_hub/panopto/browser_service.py` — command orchestration, schedule gating, matching, discovery dispositions, and transcript ingestion.
- `src/oms_hub/panopto/api.py` — paired-companion JSON endpoints and request validation.
- `src/oms_hub/panopto/pipeline.py` — accept validated transcript bytes directly; retain immutable/clean/file stages.
- `src/oms_hub/panopto/repository.py` — command persistence, heartbeat/state, recording deduplication, and existing job persistence.
- `src/oms_hub/models.py` — additive browser-command and recording-source tables; no destructive migration.
- `src/oms_hub/app.py` — wire browser service/API and remove active OAuth client wiring.
- `src/oms_hub/scheduler.py` — queue browser scan commands rather than performing REST calls.
- `src/oms_hub/web/panopto_routes.py` and templates — nontechnical browser-session setup and controls.
- `extension/canvas-hub/lib/panopto-page.js` — isolated rendered-page adapters and completeness checks.
- `extension/canvas-hub/lib/panopto-runner.js` — extension-owned tab lifecycle and content-script messaging.
- `extension/canvas-hub/panopto-content.js` — Panopto DOM bridge.
- Existing companion files — command polling, popup status, manifest host access, and notices.

---

### Task 1: Browser command domain and durable queue

**Files:**
- Create: `src/oms_hub/panopto/browser_domain.py`
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/panopto/repository.py`
- Test: `tests/panopto/test_browser_repository.py`

**Interfaces:**
- Produces: `BrowserCommandKind`, `BrowserCommand`, `BrowserRecording`, `TranscriptExtraction`, `BrowserDisposition`.
- Produces: `PanoptoRepository.queue_browser_command(kind, payload, now_utc) -> str`.
- Produces: `PanoptoRepository.claim_browser_command(now_utc) -> BrowserCommand | None`.
- Produces: `complete_browser_command`, `fail_browser_command`, `heartbeat`, and `mark_waiting_for_transcript`.
- Produces: `set_recording_source(recording_id, viewer_url)` and
  `get_recording_source(recording_id) -> str | None`.
- Produces: `recover_stale_browser_commands(now_utc, timeout_seconds=300) -> int`.

- [x] **Step 1: Write failing domain and repository tests**

```python
def test_browser_command_queue_coalesces_and_claims_once(database):
    repo = PanoptoRepository(database)
    first = repo.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )
    second = repo.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )
    assert first == second
    claimed = repo.claim_browser_command(NOW)
    assert claimed and claimed.id == first
    assert repo.claim_browser_command(NOW) is None


def test_browser_heartbeat_never_stores_unbounded_error(database):
    repo = PanoptoRepository(database)
    repo.heartbeat("panopto_login_required", NOW, "x" * 5000)
    connection = repo.connection()
    assert connection.state == "panopto_login_required"
    assert len(connection.last_error or "") <= 1000


def test_recording_viewer_url_is_kept_in_additive_source_table(database):
    repo = PanoptoRepository(database)
    repo.set_recording_source(
        1,
        "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id="
        "8796399e-393c-4256-b6e4-b48f0150d156",
    )
    assert "Viewer.aspx" in (repo.get_recording_source(1) or "")


def test_stale_running_command_is_requeued(database):
    repo = PanoptoRepository(database)
    command_id = repo.queue_browser_command(
        BrowserCommandKind.SCAN, {"manual": False}, NOW
    )
    repo.claim_browser_command(NOW)
    assert repo.recover_stale_browser_commands(LATER, 300) == 1
    assert repo.claim_browser_command(LATER).id == command_id
```

- [x] **Step 2: Run tests and verify they fail**

Run: `pytest tests/panopto/test_browser_repository.py -q`

Expected: collection fails because `browser_domain` and browser command methods do not exist.

- [x] **Step 3: Add immutable browser value objects**

```python
class BrowserCommandKind(StrEnum):
    CONNECTION_CHECK = "connection_check"
    SCAN = "scan"
    ACCEPTANCE = "acceptance"


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    id: str
    kind: BrowserCommandKind
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class BrowserRecording:
    session_id: str
    name: str
    created_utc: datetime
    duration_seconds: float
    folder_name: str
    viewer_url: str


@dataclass(frozen=True, slots=True)
class TranscriptExtraction:
    command_id: str
    recording_id: int
    session_id: str
    viewer_url: str
    language: str
    line_count: int
    complete: bool
    text: str
```

- [x] **Step 4: Add the additive command table and repository operations**

```python
class PanoptoBrowserCommandModel(Base):
    __tablename__ = "panopto_browser_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    claimed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class PanoptoRecordingSourceModel(Base):
    __tablename__ = "panopto_recording_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("panopto_recordings.id"), unique=True
    )
    viewer_url: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now)
```

Use a transaction to return the existing pending/running command of the same
kind before inserting a UUID. Claim only the oldest `pending` row and set it to
`running`. Requeue a `running` command after five minutes without completion.
Deserialize only a JSON object. Store only allowlisted connection states and
truncate errors to 1,000 characters. Validate the exact LMU viewer origin
before writing `PanoptoRecordingSourceModel`.

- [x] **Step 5: Run focused tests**

Run: `pytest tests/panopto/test_browser_repository.py tests/panopto/test_domain_repository.py -q`

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/panopto/browser_domain.py src/oms_hub/panopto/repository.py tests/panopto/test_browser_repository.py
git commit -m "feat: add durable Panopto browser commands"
```

### Task 2: Browser discovery coordinator and schedule matching

**Files:**
- Create: `src/oms_hub/panopto/browser_service.py`
- Modify: `src/oms_hub/panopto/domain.py`
- Modify: `src/oms_hub/panopto/repository.py`
- Test: `tests/panopto/test_browser_service.py`
- Modify test: `tests/panopto/test_matcher_discovery.py`

**Interfaces:**
- Consumes: Task 1 browser objects and queue methods, `RecordingMatcher`, `PollingPolicy`, `CatalogRepository`.
- Produces: `PanoptoBrowserService.queue_scheduled_scan(now) -> str | None`.
- Produces: `process_discovery(command_id, recordings) -> list[BrowserDisposition]`.
- Produces: `report_browser_result(command_id, reason_code) -> None`.

- [x] **Step 1: Write failing schedule/discovery tests**

```python
def test_scheduled_scan_queues_only_in_eligible_window(service, repo):
    repo.set_enabled(True)
    assert service.queue_scheduled_scan(utc(2026, 7, 23, 13, 19)) is None
    command_id = service.queue_scheduled_scan(utc(2026, 7, 23, 13, 20))
    assert command_id


def test_discovery_returns_extract_only_for_confident_match(service):
    result = service.process_discovery("command-id", [shoulder_recording()])
    assert result[0].action == "extract_transcript"
    assert result[0].viewer_url.startswith(
        "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id="
    )


def test_discovery_rejects_wrong_origin(service):
    with pytest.raises(ValueError, match="LMU Panopto"):
        service.process_discovery(
            "command-id",
            [replace(shoulder_recording(), viewer_url="https://evil.example/x")],
        )
```

- [x] **Step 2: Run tests and verify failure**

Run: `pytest tests/panopto/test_browser_service.py -q`

Expected: collection fails because `PanoptoBrowserService` does not exist.

- [x] **Step 3: Implement schedule queueing and discovery**

```python
class PanoptoBrowserService:
    def queue_scheduled_scan(self, now: datetime) -> str | None:
        lectures = self._today(now)
        connection = self.repository.connection()
        if not self.policy.eligible(now, lectures, connection.enabled):
            return None
        return self.repository.queue_browser_command(
            BrowserCommandKind.SCAN, {"manual": False}, now
        )

    def process_discovery(
        self, command_id: str, recordings: list[BrowserRecording]
    ) -> list[BrowserDisposition]:
        lectures = self._today_and_previous_day()
        dispositions = []
        for item in recordings[:100]:
            validate_panopto_viewer_url(item.viewer_url, item.session_id)
            session = item.as_panopto_session()
            match = self.matcher.match(session, lectures)
            stored = self.repository.upsert_recording(session, match)
            self.repository.set_recording_source(
                stored.recording_id, item.viewer_url
            )
            dispositions.append(
                BrowserDisposition.from_match(stored, item, match)
            )
        return dispositions
```

`as_panopto_session()` sets `content_language=None` and carries the viewer URL
in the renamed in-memory `viewer_url` field. Remove the old behavior that forces
unknown discovery language into review; language is validated after viewer
extraction.

- [x] **Step 4: Make manual review queue extraction after remap**

Change `remap_recording(recording_id, lecture_id)` to return the recording and
queue an extraction command only when a validated viewer URL is available in
`PanoptoRecordingSourceModel`. If an old API-era record has no viewer URL, show a
sanitized "rescan required" state instead of inventing a URL.

- [x] **Step 5: Run focused tests**

Run: `pytest tests/panopto/test_browser_service.py tests/panopto/test_matcher_discovery.py -q`

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/oms_hub/panopto/browser_service.py src/oms_hub/panopto/domain.py src/oms_hub/panopto/repository.py tests/panopto/test_browser_service.py tests/panopto/test_matcher_discovery.py
git commit -m "feat: coordinate Panopto browser discovery"
```

### Task 3: Direct immutable transcript ingestion

**Files:**
- Modify: `src/oms_hub/panopto/pipeline.py`
- Modify: `src/oms_hub/panopto/browser_service.py`
- Modify: `src/oms_hub/panopto/repository.py`
- Test: `tests/panopto/test_pipeline.py`
- Test: `tests/panopto/test_browser_service.py`

**Interfaces:**
- Produces: `TranscriptPipeline.ingest_transcript(recording_id: int, payload: bytes) -> int`.
- Consumes: `TranscriptExtraction`; produces an immutable revision or an idempotent existing revision.

- [x] **Step 1: Write failing direct-ingestion tests**

```python
def test_ingest_transcript_writes_immutable_raw_and_deduplicates(pipeline):
    first = pipeline.ingest_transcript(1, b"00:01\\nShoulder transcript")
    second = pipeline.ingest_transcript(1, b"00:01\\nShoulder transcript")
    assert first == second
    assert pipeline.repository.get_revision(first).raw_path.endswith("raw.txt")


def test_browser_service_rejects_incomplete_extraction(service):
    with pytest.raises(TranscriptValidationError, match="complete"):
        service.ingest_extraction(replace(extraction(), complete=False))
```

- [x] **Step 2: Run tests and verify failure**

Run: `pytest tests/panopto/test_pipeline.py tests/panopto/test_browser_service.py -q`

Expected: fail because `ingest_transcript` and `ingest_extraction` do not exist.

- [x] **Step 3: Replace caption-download dependency with direct bytes**

```python
def ingest_transcript(self, recording_id: int, payload: bytes) -> int:
    validate_raw_caption(payload, self.settings.panopto_max_caption_bytes)
    raw_sha256 = hashlib.sha256(payload).hexdigest()
    revision = self.repository.create_raw_revision(
        recording_id, raw_sha256, ""
    )
    if revision.raw_path and Path(revision.raw_path).is_file():
        return revision.id
    raw_path = self._revision_root(revision.id) / "raw.txt"
    self._write_immutable(raw_path, payload, raw_sha256)
    self.repository.finalize_download(revision.id, str(raw_path))
    return revision.id
```

Remove `CaptionClient` from the pipeline constructor. The browser service
requires `complete=True`, `language == "English_USA"`, positive bounded line
count, matching command/recording/session IDs, and a validated viewer URL
before UTF-8 encoding the text and calling `ingest_transcript`.

- [x] **Step 4: Preserve corrected-revision behavior**

Add a test that a changed transcript creates a second immutable directory,
queues a new clean job, leaves the first raw file unchanged, and makes neither
revision current until filing succeeds.

- [x] **Step 5: Run focused tests**

Run: `pytest tests/panopto/test_pipeline.py tests/panopto/test_browser_service.py -q`

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/oms_hub/panopto/pipeline.py src/oms_hub/panopto/browser_service.py src/oms_hub/panopto/repository.py tests/panopto/test_pipeline.py tests/panopto/test_browser_service.py
git commit -m "feat: ingest browser transcripts immutably"
```

### Task 4: Paired Panopto companion API

**Files:**
- Create: `src/oms_hub/panopto/api.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/panopto/test_browser_api.py`
- Modify test: `tests/canvas/test_api.py`

**Interfaces:**
- `POST /api/panopto/heartbeat`
- `GET /api/panopto/command`
- `POST /api/panopto/discover`
- `POST /api/panopto/transcript`
- `POST /api/panopto/result`
- All endpoints consume the existing companion bearer verified by `PairingService.verify`.

- [x] **Step 1: Write failing API contract tests**

```python
def test_panopto_api_requires_existing_companion_bearer(client):
    assert client.get("/api/panopto/command").status_code == 401


def test_discover_rejects_extra_fields_and_wrong_origin(paired_client):
    response = paired_client.post(
        "/api/panopto/discover",
        json={"command_id": COMMAND, "recordings": [{
            **recording_json(), "cookie": "must-not-be-accepted"
        }]},
    )
    assert response.status_code == 422


def test_transcript_body_obeys_configured_limit(paired_client):
    response = paired_client.post(
        "/api/panopto/transcript",
        json={**extraction_json(), "text": "x" * (5 * 1024 * 1024 + 1)},
    )
    assert response.status_code in {413, 422}
```

- [x] **Step 2: Run tests and verify 404 failures**

Run: `pytest tests/panopto/test_browser_api.py -q`

Expected: endpoints return 404.

- [x] **Step 3: Implement strict API models and authentication**

Use `ConfigDict(extra="forbid")`, UUID validation, maximum 100 discovery items,
title/folder/URL bounds, positive duration, maximum 100,000 transcript lines,
and a transcript text bound equal to `panopto_max_caption_bytes`. Reuse the
pairing verifier but return "OMS companion bearer required" rather than a
Canvas-specific error.

Return `204` when no command is pending. `discover` returns only
`recording_id`, `session_id`, `action`, `viewer_url`, and sanitized reason.
Never echo transcript text.

- [x] **Step 4: Allow paired extension POSTs through cross-site middleware**

```python
is_companion_api = request.url.path.startswith(
    ("/api/canvas/", "/api/panopto/")
)
```

Do not weaken the origin checks for dashboard routes.

- [x] **Step 5: Run API and Canvas regression tests**

Run: `pytest tests/panopto/test_browser_api.py tests/canvas/test_api.py -q`

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add src/oms_hub/panopto/api.py src/oms_hub/app.py tests/panopto/test_browser_api.py tests/canvas/test_api.py
git commit -m "feat: add paired Panopto companion API"
```

### Task 5: Panopto rendered-page adapter

**Files:**
- Create: `extension/canvas-hub/lib/panopto-page.js`
- Create: `extension/canvas-hub/tests/panopto-page.test.js`
- Modify: `extension/canvas-hub/NOTICE.md`

**Interfaces:**
- Produces: `isLoginRequired(document, location) -> boolean`.
- Produces: `readSharedRecordings(document) -> BrowserRecording[]`.
- Produces: `readTranscript(document, options) -> Promise<TranscriptResult>`.
- Produces: stable reason codes `panopto_login_required`, `transcript_processing`, `english_captions_missing`, `transcript_incomplete`, and `page_structure_changed`.

- [x] **Step 1: Write failing adapter tests with synthetic documents**

```javascript
test("missing list container fails closed", () => {
  assert.throws(
    () => readSharedRecordings(fakeDocument({})),
    /page_structure_changed/
  );
});

test("recordings are normalized without unrelated text", () => {
  const result = readSharedRecordings(sharedWithMeFixture());
  assert.deepEqual(Object.keys(result[0]).sort(), [
    "created_utc", "duration_seconds", "folder_name",
    "name", "session_id", "viewer_url",
  ]);
});

test("virtualized transcript must reach a stable complete set", async () => {
  const result = await readTranscript(transcriptFixture(), {
    maxScrolls: 200, stablePasses: 3,
  });
  assert.equal(result.complete, true);
  assert.equal(result.lines.length, 3);
});
```

- [x] **Step 2: Run tests and verify module-not-found failure**

Run: `cd extension/canvas-hub && npm test`

Expected: fail because `lib/panopto-page.js` does not exist.

- [x] **Step 3: Implement isolated selectors and parsers**

Use selector arrays with the referenced rendered structures as fallbacks:

```javascript
const LIST_CONTAINERS = ["#listViewContainer", "[data-testid='session-list']"];
const LIST_ROWS = ["tbody tr.list-view-row", "[data-session-id]"];
const TRANSCRIPT_PANES = [
  "div.event-tab-scroll-pane",
  "[data-testid='transcript-scroll-pane']",
];
const TRANSCRIPT_LINES = ["li.index-event", "[data-testid='transcript-line']"];
```

Extract a UUID only from a validated LMU viewer URL. Normalize timestamps to
ISO strings and durations to seconds. For transcript completeness, scroll the
pane until its scroll height and ordered line signature are stable for three
passes; reject duplicate timestamps with conflicting text and a nonzero
scroll gap.

- [x] **Step 4: Add MIT attribution**

Update `NOTICE.md` to cite the project URL and state that its rendered Panopto
transcript extraction approach informed `panopto-page.js`. Do not claim copied
code if none is copied.

- [x] **Step 5: Run extension tests**

Run: `cd extension/canvas-hub && npm test`

Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add extension/canvas-hub/lib/panopto-page.js extension/canvas-hub/tests/panopto-page.test.js extension/canvas-hub/NOTICE.md
git commit -m "feat: parse rendered Panopto transcripts"
```

### Task 6: Extension-owned tab runner and command loop

**Files:**
- Create: `extension/canvas-hub/lib/panopto-runner.js`
- Create: `extension/canvas-hub/panopto-content.js`
- Create: `extension/canvas-hub/tests/panopto-runner.test.js`
- Modify: `extension/canvas-hub/lib/hub-client.js`
- Modify: `extension/canvas-hub/background.js`
- Modify: `extension/canvas-hub/manifest.json`
- Modify: `extension/canvas-hub/popup.html`
- Modify: `extension/canvas-hub/popup.js`
- Test: `extension/canvas-hub/tests/scanner.test.js`

**Interfaces:**
- Consumes Task 4 endpoints and Task 5 adapters.
- Produces: `runPanoptoCommand(command, dependencies)`.
- Content messages: `panopto:connection-check`, `panopto:discover`, `panopto:extract`.

- [x] **Step 1: Write failing tab-ownership and command tests**

```javascript
test("runner creates and removes only its own inactive tab", async () => {
  const tabs = fakeTabs();
  await runPanoptoCommand(scanCommand(), {tabs, hub, waitForReady});
  assert.equal(tabs.created[0].active, false);
  assert.deepEqual(tabs.removed, [tabs.created[0].id]);
  assert.equal(tabs.updatedUserTabs.length, 0);
});

test("login result is reported without throwing retry loop", async () => {
  const result = await runPanoptoCommand(scanCommand(), loginFixtureDeps());
  assert.equal(result.reason_code, "panopto_login_required");
});
```

- [x] **Step 2: Run tests and verify failure**

Run: `cd extension/canvas-hub && npm test`

Expected: module-not-found failure for `panopto-runner.js`.

- [x] **Step 3: Add Panopto API calls to the Hub client**

```javascript
export function getPanoptoCommand() {
  return request("/api/panopto/command", {}, true, {allowNoContent: true});
}
export function postPanoptoDiscover(payload) {
  return request("/api/panopto/discover", {
    method: "POST", body: JSON.stringify(payload),
  });
}
export function postPanoptoTranscript(payload) {
  return request("/api/panopto/transcript", {
    method: "POST", body: JSON.stringify(payload),
  });
}
```

Extend `request` to handle `204` without trying to decode JSON. Do not log
request bodies.

- [x] **Step 4: Implement the dedicated inactive tab lifecycle**

Create a new inactive tab for each command, wait for the exact Panopto origin,
send a content-script message, submit discoveries, then extract each returned
disposition. Always remove the created tab in `finally`. Never query for or
reuse a user-created Panopto tab.

- [x] **Step 5: Register content script and exact host permission**

```json
"host_permissions": [
  "https://lmunet.instructure.com/*",
  "https://lmunet.hosted.panopto.com/*",
  "http://127.0.0.1:8765/*"
],
"content_scripts": [{
  "matches": ["https://lmunet.hosted.panopto.com/*"],
  "js": ["panopto-content.js"],
  "run_at": "document_idle"
}],
"web_accessible_resources": [{
  "resources": ["lib/panopto-page.js"],
  "matches": ["https://lmunet.hosted.panopto.com/*"]
}]
```

Assert in tests that `permissions` does not contain `cookies` and host
permissions contain no wildcard host beyond the three exact entries.
`panopto-content.js` loads the adapter with
`import(chrome.runtime.getURL("lib/panopto-page.js"))`; it does not duplicate
the page-reading implementation.

- [x] **Step 6: Poll Panopto commands from the existing one-minute alarm**

Canvas's scheduled scan stays unchanged. The command alarm first finishes any
Canvas scan request and then claims at most one Panopto command. Maintain
separate `activeScan` and `activePanopto` promises.

- [x] **Step 7: Update popup status without exposing content**

Rename the popup heading to **OMS Study Hub Companion** and show separate
Canvas and Panopto one-line states. Keep the existing pairing control.

- [x] **Step 8: Run all extension tests**

Run: `cd extension/canvas-hub && npm test`

Expected: all pass.

- [x] **Step 9: Commit**

```bash
git add extension/canvas-hub
git commit -m "feat: run Panopto through the Chrome companion"
```

### Task 7: Remove active OAuth wiring and add browser-session UI

**Files:**
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/cli.py`
- Modify: `src/oms_hub/scheduler.py`
- Modify: `src/oms_hub/web/panopto_routes.py`
- Modify: `src/oms_hub/web/routes.py`
- Modify: `src/oms_hub/web/templates/panopto_setup.html`
- Modify: `src/oms_hub/web/templates/dashboard.html`
- Delete: `src/oms_hub/panopto/auth.py`
- Delete: `src/oms_hub/panopto/client.py`
- Delete: `tests/panopto/test_auth_client.py`
- Modify tests: `tests/panopto/test_panopto_web.py`
- Modify tests: `tests/test_cli.py`
- Modify tests: `tests/test_config.py`
- Modify tests: `tests/test_dashboard.py`
- Modify tests: `tests/test_scheduler.py`

**Interfaces:**
- Consumes Task 2 service, Task 3 pipeline, Task 4 API.
- Produces dashboard controls that queue `CONNECTION_CHECK`, `SCAN`, and
  `ACCEPTANCE` commands.
- Produces CLI command `panopto-clear-legacy-credentials`.

- [x] **Step 1: Replace OAuth web tests with browser-session tests**

```python
def test_setup_has_browser_controls_and_no_api_credentials(client):
    page = client.get("/panopto/setup")
    assert "Sign in to Panopto" in page.text
    assert "Check connection" in page.text
    assert "client secret" not in page.text.lower()
    assert "redirect" not in page.text.lower()


def test_enable_requires_browser_acceptance_prompt_and_openai(client, app):
    response = client.post("/panopto/enable")
    assert response.status_code == 409
    assert "Complete every Panopto setup step" in response.text
```

- [x] **Step 2: Run focused tests and verify old UI failures**

Run: `pytest tests/panopto/test_panopto_web.py tests/test_config.py tests/test_scheduler.py -q`

Expected: fail because OAuth controls and configuration still exist.

- [x] **Step 3: Rewire the application**

Remove `PanoptoTokenProvider` and `PanoptoClient` construction. Construct
`TranscriptPipeline` without a Panopto client, then construct
`PanoptoBrowserService`. Register the Panopto companion API router.
`panopto_poll_once` calls `queue_scheduled_scan(datetime.now(UTC))`; the
existing five-second transcript worker remains unchanged.

- [x] **Step 4: Replace web routes**

Keep prompt approval, pause/enable, review/remap, and retry. Replace OAuth
routes with:

```python
@router.post("/browser/check")
def check_browser(request: Request) -> RedirectResponse:
    _service(request).queue_connection_check(datetime.now(UTC))
    return RedirectResponse("/panopto/setup", status_code=303)


@router.post("/browser/acceptance")
def acceptance(request: Request) -> RedirectResponse:
    _service(request).queue_acceptance(datetime.now(UTC))
    return RedirectResponse("/panopto/setup", status_code=303)
```

`Sign in to Panopto` is a normal HTTPS link to
`/Panopto/Pages/Home.aspx`, opened in a new tab with `rel="noopener"`.
`Scan now` queues a manual scan and returns immediately.

- [x] **Step 5: Remove Panopto API settings and add explicit cleanup**

Delete `panopto_client_id` and `panopto_oauth_redirect_uri` from settings and
`.env.example`. Remove `panopto-set-secret`. Add:

```python
def panopto_clear_legacy_credentials(args):
    for key in (
        "panopto-client-secret",
        "panopto-refresh-token",
        "panopto-oauth-state",
    ):
        KeyringSecretStore().delete(key)
    print("Legacy Panopto API credentials removed")
    return 0
```

This command is explicit and never runs during install/startup.

- [x] **Step 6: Delete only tracked OAuth modules**

Delete tracked `src/oms_hub/panopto/auth.py` and
`src/oms_hub/panopto/client.py` after all imports are removed. Confirm
`src/oms_hub/panopto/auth 2.py` remains untracked and untouched. Delete the
tracked OAuth client test file because the transport it specifies no longer
exists.

- [x] **Step 7: Run UI/config/scheduler tests**

Run: `pytest tests/panopto/test_panopto_web.py tests/test_cli.py tests/test_config.py tests/test_dashboard.py tests/test_scheduler.py -q`

Expected: all pass.

- [x] **Step 8: Commit**

```bash
git add -u src/oms_hub tests/panopto
git add tests/panopto/test_panopto_web.py tests/test_cli.py tests/test_config.py tests/test_dashboard.py tests/test_scheduler.py .env.example
git commit -m "feat: replace Panopto OAuth with browser sessions"
```

### Task 8: End-to-end acceptance and security regression

**Files:**
- Rewrite: `tests/panopto/test_phase3_acceptance.py`
- Modify: `tests/test_health.py`
- Modify: `tests/canvas/test_phase2_acceptance.py`
- Modify: `tests/test_windows_scripts.py`
- Modify: `scripts/install-windows.ps1`

**Interfaces:**
- Exercises discovery JSON -> match -> extraction JSON -> immutable raw ->
  Terra clean -> canonical file -> checklist.

- [x] **Step 1: Rewrite the acceptance test around browser payloads**

```python
def test_browser_discovery_to_filed_transcript_acceptance(app, paired_client):
    command = queue_eligible_scan(app)
    dispositions = post_shared_recordings(
        paired_client, command.id, [shoulder_recording_json()]
    )
    assert dispositions[0]["action"] == "extract_transcript"
    post_extraction(
        paired_client,
        command.id,
        dispositions[0]["recording_id"],
        sample_transcript(),
    )
    run_transcript_workers(app)
    assert_checklist_complete(app)
    assert_one_immutable_raw(app)
    assert_one_canonical_transcript(app)
```

Add unchanged-rescan, corrected-revision, expired-login, processing-caption,
wrong-origin, and companion-unavailable cases.

- [x] **Step 2: Run acceptance tests and verify failures**

Run: `pytest tests/panopto/test_phase3_acceptance.py -q`

Expected: fail until any missing orchestration behavior is completed.

- [x] **Step 3: Make the minimal integration corrections**

Correct only contract mismatches exposed by the acceptance test. Do not add
fallback cookie extraction, Selenium, undocumented Panopto endpoints, or
automatic legacy-secret deletion.

- [x] **Step 4: Verify installer preserves data and permissions**

Keep creation and ACL handling for the existing Panopto revision root. Remove
text that requires a Panopto client secret. Assert install does not overwrite
`.env`, the SQLite database, or immutable revisions.

- [x] **Step 5: Run Python and extension regression suites**

Run: `pytest -q`

Expected: all Python tests pass.

Run: `cd extension/canvas-hub && npm test`

Expected: all extension tests pass.

- [x] **Step 6: Run secret and permission scans**

Run:

```bash
rg -n --glob '!auth 2.py' "panopto-client-secret|panopto-refresh-token|oauth/callback|PanoptoTokenProvider|PanoptoClient" src extension README.md docs/phase-3-nuc-rollout.md .env.example
```

Expected: only the explicit legacy cleanup key names and historical
superseded specs may remain; no active wiring or setup instructions.

Run:

```bash
rg -n "\"cookies\"|<all_urls>|https://\\*/|console\\.(log|debug).*transcript|console\\.(log|debug).*bearer" extension/canvas-hub
```

Expected: no matches.

- [x] **Step 7: Commit**

```bash
git add tests scripts/install-windows.ps1
git add -u src extension/canvas-hub
git commit -m "test: verify Panopto browser workflow"
```

### Task 9: Documentation, rollout, and branch gate

**Files:**
- Modify: `README.md`
- Rewrite: `docs/phase-3-nuc-rollout.md`
- Modify: `docs/canvas-extension-install.md`
- Modify: `docs/superpowers/specs/2026-07-23-phase-3-panopto-browser-companion.md`
- Modify: `docs/superpowers/plans/2026-07-23-phase-3-panopto-browser-companion.md`

**Interfaces:**
- Produces the exact NUC update, extension refresh, sign-in, acceptance,
  recovery, legacy cleanup, rollback, and merge instructions.

- [x] **Step 1: Update user-facing setup**

Document:

1. Stop the scheduled task and running Hub processes.
2. Pull the feature branch and run the installer with PowerShell
   `-ExecutionPolicy Bypass`.
3. Refresh the existing unpacked extension and approve only LMU Panopto access.
4. Restart the scheduled task.
5. Open the Hub, choose **Sign in to Panopto**, and complete Microsoft login.
6. Run **Check connection** and the approved recording acceptance.
7. Verify Canvas still scans.
8. Enable Panopto automation.

Remove all instructions to create an API client, enter a client ID/secret, or
configure an OAuth redirect.

- [x] **Step 2: Document operational states**

Explain `companion_unavailable`, `panopto_login_required`,
`waiting_for_transcript`, `needs_review`, and the fact that Chrome must be
running. Include explicit recovery without exposing cookies.

- [x] **Step 3: Document legacy cleanup and rollback**

Run legacy cleanup only after acceptance:

```powershell
.\.venv\Scripts\oms-hub.exe panopto-clear-legacy-credentials
```

Rollback pauses Panopto, restores the previous app/extension version, and never
deletes ProgramData revisions, the database, Canvas artifacts, or canonical
transcripts.

- [x] **Step 4: Run documentation consistency scan**

Run:

```bash
rg -n "Server-side Web Application|panopto-set-secret|oauth/callback|client ID|client secret" README.md docs/phase-3-nuc-rollout.md docs/canvas-extension-install.md .env.example
```

Expected: no obsolete active setup instructions.

- [x] **Step 5: Mark this plan complete and commit**

Update completed task checkboxes only after their commits and change the
browser companion spec status to **Implemented; live NUC acceptance pending**.

```bash
git add README.md docs .env.example
git commit -m "docs: add Panopto browser rollout"
```

- [ ] **Step 6: Final automated verification**

Run: `pytest -q`

Expected: all tests pass.

Run: `cd extension/canvas-hub && npm test`

Expected: all tests pass.

Run: `git status --short`

Expected: only the preserved untracked
`src/oms_hub/panopto/auth 2.py` appears.

- [ ] **Step 7: Push feature branch without merging**

Push the completed feature branch. Do not merge into `main` until the live NUC
steps pass with the approved recording and one newly shared lecture.
