# Integrated Setup Center and Panopto Caption Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Panopto claim-and-lease command workflow and transcript-panel scraping with one-click Hub-triggered caption downloads, recoverable request state, managed immutable ingestion, and a live integrated Setup Center.

**Architecture:** The Hub stores idempotent desired-state requests and exposes authenticated request/progress/download endpoints to the existing Chrome companion. A same-origin Hub bridge triggers manual tests immediately, while alarms recover missed work. The extension opens Panopto, discovers the newest or scheduled recording, downloads Panopto's English (United States) `.txt` caption file into a managed inbox, and reports completion; the Hub validates, isolates tests, preserves production originals, and streams status to the combined Setup Center.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, Jinja2, server-sent events, Manifest V3 Chrome extension, vanilla JavaScript ES modules, Node test runner, pytest.

## Global Constraints

- Use the existing paired Chrome companion and normal LMU Panopto session; do not add Panopto OAuth, API credentials, cookie export, or cookie extraction.
- Manual connection testing is one Hub click and opens a visible active Chrome tab immediately.
- Select the newest Shared with Me recording for connection testing.
- Download English (United States) captions through Panopto's built-in caption download; do not scrape transcript-panel lines or play the lecture.
- Missing captions are `waiting_for_captions`, retried every 15 minutes only on scheduled lecture weekdays from 9:20 AM through 7:00 PM Eastern.
- Preserve immutable ProgramData originals, revision history, idempotency, review/quarantine behavior, automatic cleaning, canonical routing, and lecture checklist updates.
- Keep Canvas behavior and extension pairing backward compatible.
- Never expose, log, render, or commit secrets, cookies, authorization headers, raw page HTML, or transcript excerpts.
- Keep work on `feat/panopto-browser-companion`; do not merge `main` until live NUC acceptance passes.
- Never modify, delete, or stage the untracked `src/oms_hub/panopto/auth 2.py`.

---

## File Structure

### Backend

- `src/oms_hub/models.py`: additive SQLAlchemy model for recoverable browser requests.
- `src/oms_hub/config.py`: managed Panopto inbox and quarantine roots.
- `src/oms_hub/panopto/browser_domain.py`: request kinds, states, progress, and download metadata value objects.
- `src/oms_hub/panopto/repository.py`: request persistence, retry eligibility, legacy supersession, status snapshots, and recording wait state.
- `src/oms_hub/panopto/browser_service.py`: scheduled/manual request creation, discovery, waiting-caption disposition, and download completion orchestration.
- `src/oms_hub/panopto/download_ingestion.py`: path confinement, stabilization, `.txt` validation, temporary-test cleanup, quarantine, and immutable production ingestion.
- `src/oms_hub/panopto/api.py`: authenticated companion request, progress, discovery, download-complete, and result endpoints.
- `src/oms_hub/web/setup_routes.py`: integrated Setup Center page, one-click test request, status snapshot, and SSE stream.
- `src/oms_hub/app.py`: service construction, legacy supersession, router registration.
- `src/oms_hub/cli.py`: scheduler queues desired-state scans instead of legacy commands.
- `src/oms_hub/web/templates/base.html`: single Setup navigation entry.
- `src/oms_hub/web/templates/setup.html`: overview and Canvas/Panopto detail panels.
- `src/oms_hub/web/static/setup.js`: test submission, bridge event, SSE rendering, and polling fallback.
- `src/oms_hub/web/static/app.css`: Setup Center cards, tabs, progress, and live states.
- `scripts/install-windows.ps1`: create and permission managed Panopto inbox/quarantine directories.

### Extension

- `extension/canvas-hub/manifest.json`: local Hub bridge content script and updated extension identity.
- `extension/canvas-hub/hub-bridge.js`: exact-origin custom-event validation and service-worker messaging.
- `extension/canvas-hub/lib/hub-client.js`: new desired-state and download-completion API methods.
- `extension/canvas-hub/lib/command-poller.js`: poll recoverable Panopto requests instead of legacy commands.
- `extension/canvas-hub/lib/panopto-page.js`: newest-recording selection and caption-download descriptor discovery.
- `extension/canvas-hub/panopto-content.js`: page commands for discovery and caption download, without transcript scraping.
- `extension/canvas-hub/lib/panopto-downloads.js`: confined download naming, Chrome download tracking, and persistent recovery metadata.
- `extension/canvas-hub/lib/panopto-runner.js`: visible manual test, login continuation, scheduled background scans, progress, and retry results.
- `extension/canvas-hub/background.js`: immediate bridge trigger, alarm recovery, and Panopto download completion dispatch.
- `extension/canvas-hub/popup.html`, `extension/canvas-hub/popup.js`, `extension/canvas-hub/lib/popup-status.js`: popup becomes pairing/repair diagnostics only.

### Tests and docs

- `tests/panopto/test_browser_repository.py`
- `tests/panopto/test_browser_service.py`
- `tests/panopto/test_browser_api.py`
- `tests/panopto/test_download_ingestion.py`
- `tests/panopto/test_panopto_web.py`
- `tests/panopto/test_phase3_acceptance.py`
- `tests/test_windows_scripts.py`
- `extension/canvas-hub/tests/background-contract.test.js`
- `extension/canvas-hub/tests/command-poller.test.js`
- `extension/canvas-hub/tests/hub-bridge.test.js`
- `extension/canvas-hub/tests/panopto-page.test.js`
- `extension/canvas-hub/tests/panopto-downloads.test.js`
- `extension/canvas-hub/tests/panopto-runner.test.js`
- `extension/canvas-hub/tests/popup-status.test.js`
- `docs/phase-3-nuc-rollout.md`

---

### Task 1: Add recoverable Panopto request state

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/panopto/browser_domain.py`
- Modify: `src/oms_hub/panopto/repository.py`
- Test: `tests/panopto/test_browser_repository.py`

**Interfaces:**
- Produces: `BrowserRequestKind`, `BrowserRequest`, `PanoptoRepository.create_browser_request()`, `next_browser_request()`, `update_browser_request()`, `complete_browser_request()`, `supersede_legacy_browser_commands()`.
- Consumes: existing `Database`, `PanoptoBrowserCommandModel`, and UTC-aware datetimes.

- [ ] **Step 1: Write failing repository tests**

Add tests proving request reads do not claim or hide work, request progress survives repeated reads, retry time gates waiting captions, completed requests disappear, and legacy active commands are superseded:

```python
def test_browser_request_remains_visible_until_terminal(database):
    repository = PanoptoRepository(database)
    request_id = repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST, {}, NOW
    )
    first = repository.next_browser_request(NOW)
    second = repository.next_browser_request(NOW)
    assert first is not None and first.id == request_id
    assert second is not None and second.id == request_id
    repository.update_browser_request(request_id, "opening_shared", NOW)
    assert repository.next_browser_request(NOW).progress == "opening_shared"
    repository.complete_browser_request(request_id, NOW)
    assert repository.next_browser_request(NOW) is None


def test_waiting_caption_request_obeys_next_eligible_time(database):
    repository = PanoptoRepository(database)
    request_id = repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST, {}, NOW
    )
    repository.wait_browser_request(
        request_id, "captions_pending", NOW + timedelta(minutes=15), NOW
    )
    assert repository.next_browser_request(NOW + timedelta(minutes=14)) is None
    assert repository.next_browser_request(NOW + timedelta(minutes=15)).id == request_id
```

- [ ] **Step 2: Run the repository tests and verify RED**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_browser_repository.py -q
```

Expected: failures because `BrowserRequestKind`, the new model, and repository methods do not exist.

- [ ] **Step 3: Add the model and domain types**

Add an additive `PanoptoBrowserRequestModel` table with `id`, `kind`, `state`, `payload_json`, `progress`, `requested_at`, `started_at`, `completed_at`, `next_eligible_at`, and `error_code`. Add:

```python
class BrowserRequestKind(StrEnum):
    CONNECTION_TEST = "connection_test"
    SCAN = "scan"


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    id: str
    kind: BrowserRequestKind
    state: str
    payload: dict[str, object]
    progress: str
```

- [ ] **Step 4: Implement request persistence**

Implement repository methods with non-destructive desired state. `next_browser_request()` must select `requested`, `running`, `awaiting_login`, or due `waiting_for_captions` rows without mutating them. `complete_browser_request()` and terminal failure remove work from selection. `supersede_legacy_browser_commands()` marks only legacy `pending` and `running` rows failed with `superseded_command_model`.

- [ ] **Step 5: Run focused and full repository tests**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_browser_repository.py -q
```

Expected: all repository tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/panopto/browser_domain.py src/oms_hub/panopto/repository.py tests/panopto/test_browser_repository.py
git commit -m "feat: add recoverable Panopto browser requests"
```

---

### Task 2: Replace legacy companion commands with request APIs

**Files:**
- Modify: `src/oms_hub/panopto/browser_service.py`
- Modify: `src/oms_hub/panopto/api.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/cli.py`
- Test: `tests/panopto/test_browser_service.py`
- Test: `tests/panopto/test_browser_api.py`

**Interfaces:**
- Consumes: Task 1 request repository.
- Produces: `GET /api/panopto/request`, `POST /api/panopto/request/{id}/progress`, `POST /api/panopto/request/{id}/discover`, and `POST /api/panopto/request/{id}/result`.

- [ ] **Step 1: Write failing API and service tests**

Cover repeated request retrieval, authentication, progress persistence, scheduled request eligibility, and terminal result:

```python
def test_request_is_recoverable_until_complete(tmp_path):
    client, headers = _prepared_client(tmp_path)
    request_id = client.app.state.panopto_browser.queue_connection_test(NOW)
    first = client.get("/api/panopto/request", headers=headers)
    second = client.get("/api/panopto/request", headers=headers)
    assert first.json()["id"] == request_id
    assert second.json()["id"] == request_id
    progress = client.post(
        f"/api/panopto/request/{request_id}/progress",
        headers=headers,
        json={"state": "running", "progress": "opening_shared"},
    )
    assert progress.status_code == 200
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_browser_service.py ../tests/panopto/test_browser_api.py -q
```

Expected: 404s or missing-method failures.

- [ ] **Step 3: Implement request service methods**

Add:

```python
def queue_connection_test(self, now: datetime) -> str:
    return self.repository.create_browser_request(
        BrowserRequestKind.CONNECTION_TEST, {}, now
    )

def queue_manual_scan(self, now: datetime) -> str:
    return self.repository.create_browser_request(
        BrowserRequestKind.SCAN, {"manual": True}, now
    )
```

Change scheduled scans to create `SCAN` requests only when `PollingPolicy.eligible()` is true.

- [ ] **Step 4: Implement strict authenticated request endpoints**

Use `StrictModel(extra="forbid")`, UUID request IDs, bounded progress/reason enums, and the existing companion bearer verification. Return 204 only when no desired work exists. Preserve legacy endpoints temporarily for completed-history compatibility but stop invoking them.

- [ ] **Step 5: Wire startup and scheduler migration**

Call `supersede_legacy_browser_commands()` once during app construction after schema creation. Keep scheduler timing unchanged while switching `queue_scheduled_scan()` to desired requests.

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_browser_service.py ../tests/panopto/test_browser_api.py ../tests/panopto/test_phase3_acceptance.py -q
```

Expected: request tests pass after replacing every acceptance fixture's `command_id` field and legacy `/api/panopto/command` retrieval with the persisted request ID and `/api/panopto/request`.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/panopto/browser_service.py src/oms_hub/panopto/api.py src/oms_hub/app.py src/oms_hub/cli.py tests/panopto/test_browser_service.py tests/panopto/test_browser_api.py tests/panopto/test_phase3_acceptance.py
git commit -m "feat: expose recoverable Panopto request APIs"
```

---

### Task 3: Build managed Panopto caption ingestion

**Files:**
- Create: `src/oms_hub/panopto/download_ingestion.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/panopto/browser_service.py`
- Modify: `src/oms_hub/panopto/api.py`
- Modify: `scripts/install-windows.ps1`
- Create: `tests/panopto/test_download_ingestion.py`
- Modify: `tests/panopto/test_browser_api.py`
- Modify: `tests/test_windows_scripts.py`

**Interfaces:**
- Produces: `PanoptoDownloadIngestion.complete_test_download()` and `complete_recording_download()`.
- Consumes: `TranscriptPipeline.ingest_transcript()`, `validate_raw_caption()`, repository request/recording lookup, and configured inbox/quarantine roots.

- [ ] **Step 1: Write failing confinement, cleanup, ingestion, and quarantine tests**

Tests must prove:

```python
def test_test_download_validates_and_removes_temporary_file(prepared):
    path = prepared.inbox / "test" / "captions.txt"
    path.parent.mkdir(parents=True)
    path.write_text("00:01 First line\n00:03 Second line", encoding="utf-8")
    prepared.ingestion.complete_test_download(prepared.request_id, path)
    assert not path.exists()
    assert prepared.repository.connection().acceptance_validated_at
    assert list(prepared.revision_root.glob("*/raw.txt")) == []


def test_production_download_preserves_immutable_raw_before_removing_inbox(prepared):
    path = prepared.inbox / "scan" / "captions.txt"
    path.parent.mkdir(parents=True)
    path.write_text("00:01 Raw lecture", encoding="utf-8")
    revision_id = prepared.ingestion.complete_recording_download(
        prepared.request_id, prepared.recording_id, path
    )
    assert not path.exists()
    assert (prepared.revision_root / str(revision_id) / "raw.txt").is_file()
```

Also reject paths outside the managed inbox, files over the configured limit, non-`.txt` files, HTML/JSON bodies, and wrong-language metadata. Invalid managed files move beneath the configured quarantine root without overwriting.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_download_ingestion.py -q
```

Expected: import failure because `download_ingestion.py` does not exist.

- [ ] **Step 3: Add settings and installer directories**

Add:

```python
panopto_inbox: Path = Path(
    r"%USERPROFILE%\Downloads\OMSStudyHub\PanoptoInbox"
)
panopto_quarantine_root: Path = Path(
    r"C:\ProgramData\OMSStudyHub\quarantine\panopto"
)
```

Create and permission both directories in `install-windows.ps1`.

- [ ] **Step 4: Implement stable, confined ingestion**

Resolve and require downloads beneath `panopto_inbox`, wait for stable size/mtime, enforce `.txt` and `panopto_max_caption_bytes`, validate UTF-8 plain text, then:

- connection test: validate, mark acceptance, remove temporary file;
- production: ingest immutable raw revision first, then remove inbox file;
- failure: move the managed file atomically to a unique request-specific quarantine path and mark review.

- [ ] **Step 5: Add strict download-complete API**

Accept only request ID, recording/session identifiers, viewer URL, language `English_USA`, Chrome download ID, and an absolute bounded path. Derive test versus production behavior from the persisted request kind; do not trust a client-supplied mode.

- [ ] **Step 6: Run focused tests**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/panopto/test_download_ingestion.py ../tests/panopto/test_browser_api.py ../tests/test_windows_scripts.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/panopto/download_ingestion.py src/oms_hub/config.py src/oms_hub/panopto/browser_service.py src/oms_hub/panopto/api.py scripts/install-windows.ps1 tests/panopto/test_download_ingestion.py tests/panopto/test_browser_api.py tests/test_windows_scripts.py
git commit -m "feat: ingest managed Panopto caption downloads"
```

---

### Task 4: Discover newest recordings and caption download controls

**Files:**
- Modify: `extension/canvas-hub/lib/panopto-page.js`
- Modify: `extension/canvas-hub/panopto-content.js`
- Modify: `extension/canvas-hub/tests/panopto-page.test.js`
- Modify: `extension/canvas-hub/tests/background-contract.test.js`

**Interfaces:**
- Produces: `newestSharedRecording(recordings)` and `readCaptionDownload(document, location)`.
- Returns caption descriptor `{status: "ready", language: "English_USA", download_url, filename}` or `{status: "captions_pending"}`.
- Consumes: standard Shared with Me metadata and LMU-hosted HTTPS caption URLs.

- [ ] **Step 1: Write failing extension tests**

Add:

```javascript
test("selects newest shared recording by created time", () => {
  const newest = newestSharedRecording([
    {session_id: "old", created_utc: "2026-07-24T12:00:00.000Z"},
    {session_id: "new", created_utc: "2026-07-24T13:00:00.000Z"},
  ]);
  assert.equal(newest.session_id, "new");
});

test("returns the built-in English USA caption download", () => {
  const link = node({
    text: "Download Captions",
    attrs: {
      href: "https://lmunet.hosted.panopto.com/Panopto/caption.txt",
      "data-language": "English_USA",
    },
  });
  const result = readCaptionDownload(
    node({many: {"a,button": [link]}}),
    {origin: "https://lmunet.hosted.panopto.com"},
  );
  assert.equal(result.status, "ready");
  assert.equal(result.language, "English_USA");
});
```

Also test no control → `captions_pending`, non-LMU URL rejection, HTML/javascript URLs rejection, and transcript-panel selectors no longer being used.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd extension/canvas-hub
npm test
```

Expected: missing-export failures.

- [ ] **Step 3: Implement bounded caption-control discovery**

Search only anchors/buttons and known caption-control attributes/text. Normalize accessible text. Require an HTTPS LMU Panopto URL and English (United States) metadata. If a button reveals a language menu, click it and wait boundedly for the English link. Never read `li.index-event`.

- [ ] **Step 4: Replace content-script transcript command**

Remove `panopto:extract`; add `panopto:caption-download`. Retain `panopto:discover`. Return bounded reason codes only.

- [ ] **Step 5: Run extension tests**

Run:

```bash
cd extension/canvas-hub
npm test
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add extension/canvas-hub/lib/panopto-page.js extension/canvas-hub/panopto-content.js extension/canvas-hub/tests/panopto-page.test.js extension/canvas-hub/tests/background-contract.test.js
git commit -m "feat: discover Panopto caption downloads"
```

---

### Task 5: Add one-click Hub bridge and managed Chrome downloads

**Files:**
- Modify: `extension/canvas-hub/manifest.json`
- Create: `extension/canvas-hub/hub-bridge.js`
- Modify: `extension/canvas-hub/lib/hub-client.js`
- Create: `extension/canvas-hub/lib/panopto-downloads.js`
- Modify: `extension/canvas-hub/background.js`
- Create: `extension/canvas-hub/tests/hub-bridge.test.js`
- Create: `extension/canvas-hub/tests/panopto-downloads.test.js`
- Modify: `extension/canvas-hub/tests/background-contract.test.js`

**Interfaces:**
- Produces: runtime message `{type: "panopto-request-now", request_id}`.
- Produces: `downloadPanoptoCaption(descriptor, metadata)` and `completePanoptoDownload(downloadId, report)`.
- Consumes: Task 2 request endpoints and Task 4 caption descriptor.

- [ ] **Step 1: Write failing bridge and download tests**

Verify exact origin and UUID validation:

```javascript
test("Hub bridge forwards only exact local test events", async () => {
  const sent = [];
  const bridge = createHubBridge({
    origin: "http://127.0.0.1:8765",
    send: async (message) => sent.push(message),
  });
  await bridge({
    type: "oms-study-hub:panopto-test",
    detail: {request_id: REQUEST_ID},
  });
  assert.deepEqual(sent, [{type: "panopto-request-now", request_id: REQUEST_ID}]);
});
```

Verify safe filename confinement beneath `OMSStudyHub/PanoptoInbox/<request-id>/`, session-storage recovery records, completion reporting, and rejection of unsafe URLs/filenames.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd extension/canvas-hub
npm test
```

Expected: module-not-found failures.

- [ ] **Step 3: Implement and register the bridge**

Add a content-script entry matching only `http://127.0.0.1:8765/*`. Listen for the fixed custom event, verify exact origin and UUID request ID, and forward only `panopto-request-now`.

- [ ] **Step 4: Implement authenticated request client methods**

Add `getPanoptoRequest()`, `postPanoptoProgress()`, `postPanoptoDiscovery()`, `postPanoptoDownloadComplete()`, and `postPanoptoResult()` using the existing bearer and 15-second fetch timeout.

- [ ] **Step 5: Implement managed downloads**

Use `chrome.downloads.download()` with `saveAs: false`, `conflictAction: "uniquify"`, an LMU HTTPS URL, and a request/session-derived `.txt` filename. Persist only bounded IDs/metadata in `chrome.storage.session`; never persist transcript content or cookies.

- [ ] **Step 6: Wire background completion**

On `chrome.downloads.onChanged` complete, recover the stored Panopto mapping, search the Chrome download, post the absolute path to the Hub, then remove the mapping. Keep existing Canvas completion behavior unchanged.

- [ ] **Step 7: Run tests and commit**

Run `npm test`; expect all extension tests to pass.

```bash
git add extension/canvas-hub/manifest.json extension/canvas-hub/hub-bridge.js extension/canvas-hub/lib/hub-client.js extension/canvas-hub/lib/panopto-downloads.js extension/canvas-hub/background.js extension/canvas-hub/tests/hub-bridge.test.js extension/canvas-hub/tests/panopto-downloads.test.js extension/canvas-hub/tests/background-contract.test.js
git commit -m "feat: trigger Panopto downloads from the Hub"
```

---

### Task 6: Execute visible tests, login continuation, scans, and caption retries

**Files:**
- Modify: `extension/canvas-hub/lib/command-poller.js`
- Modify: `extension/canvas-hub/lib/panopto-runner.js`
- Modify: `extension/canvas-hub/background.js`
- Modify: `extension/canvas-hub/popup.html`
- Modify: `extension/canvas-hub/popup.js`
- Modify: `extension/canvas-hub/lib/popup-status.js`
- Modify: `extension/canvas-hub/tests/command-poller.test.js`
- Modify: `extension/canvas-hub/tests/panopto-runner.test.js`
- Modify: `extension/canvas-hub/tests/popup-status.test.js`

**Interfaces:**
- Consumes: Task 2 desired request API, Task 4 page commands, Task 5 managed downloads.
- Produces: `runPanoptoRequest(request, dependencies)` returning `{status, reason_code?}`.

- [ ] **Step 1: Write failing runner tests**

Cover:

- connection test uses `tabs.create({active: true})`;
- newest recording is selected;
- login redirect posts `awaiting_login`, waits for the tab to return to LMU Panopto, and continues;
- ready captions start a managed download;
- absent captions posts `captions_pending` with a 15-minute retry;
- scheduled scans use inactive tabs and process bounded dispositions;
- success closes the tab;
- sign-in remains visible;
- duplicate calls share one in-memory active request.

- [ ] **Step 2: Run tests and verify RED**

Run `npm test`; expect assertion failures against the legacy command runner.

- [ ] **Step 3: Implement the request runner**

Replace command claiming with recoverable request reads. Manual test sequence:

```javascript
await hub.postProgress(id, "running", "opening_shared");
const tab = await tabs.create({url: SHARED_WITH_ME, active: true});
const recordings = await pageMessage(tabs, tab.id, "panopto:discover");
const newest = newestSharedRecording(recordings.recordings);
await tabs.update(tab.id, {url: newest.viewer_url, active: true});
const captions = await pageMessage(tabs, tab.id, "panopto:caption-download");
```

If redirected to Microsoft login, post `awaiting_login` and wait for the same tab to return to an LMU viewer. If captions are pending, post a retry result; otherwise start the managed download and let download completion finalize the request.

- [ ] **Step 4: Implement scheduled scan execution**

Use inactive tabs, post discovered metadata, process only `download_caption` dispositions, report `captions_pending` per recording without failing the scan, and close owned tabs after each bounded operation.

- [ ] **Step 5: Make popup diagnostic-only**

Remove the normal **Check Panopto command** action. Retain pairing, Canvas manual scan, current companion status, and a repair/retry diagnostic action that invokes the same request poller without creating work.

- [ ] **Step 6: Run extension tests and commit**

Run `npm test`; expect all pass.

```bash
git add extension/canvas-hub/lib/command-poller.js extension/canvas-hub/lib/panopto-runner.js extension/canvas-hub/background.js extension/canvas-hub/popup.html extension/canvas-hub/popup.js extension/canvas-hub/lib/popup-status.js extension/canvas-hub/tests/command-poller.test.js extension/canvas-hub/tests/panopto-runner.test.js extension/canvas-hub/tests/popup-status.test.js
git commit -m "feat: run recoverable Panopto caption requests"
```

---

### Task 7: Build the integrated live Setup Center

**Files:**
- Create: `src/oms_hub/web/setup_routes.py`
- Create: `src/oms_hub/web/templates/setup.html`
- Create: `src/oms_hub/web/static/setup.js`
- Modify: `src/oms_hub/web/templates/base.html`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/web/canvas_routes.py`
- Modify: `src/oms_hub/web/panopto_routes.py`
- Modify: `tests/canvas/test_canvas_web.py`
- Modify: `tests/panopto/test_panopto_web.py`

**Interfaces:**
- Produces: `GET /setup`, `POST /setup/panopto/test`, `GET /api/setup/status`, `GET /api/setup/events`.
- Consumes: request repository, Canvas/Panopto connection rows, secret presence, and prompt inspection.

- [ ] **Step 1: Write failing Setup Center tests**

Test default overview, consolidated navigation, no second extension instruction, JSON request creation, safe snapshot content, and live stream headers:

```python
def test_setup_is_single_default_overview(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)
    page = client.get("/setup")
    assert page.status_code == 200
    assert "Setup Center" in page.text
    assert "Canvas" in page.text
    assert "Panopto" in page.text
    assert "Test Panopto Connection" in page.text
    assert "Check Panopto command" not in page.text


def test_one_click_test_returns_bridge_request_id(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)
    response = client.post("/setup/panopto/test")
    assert response.status_code == 200
    assert response.json()["request_id"]
    assert app.state.panopto_repository.next_browser_request(datetime.now(UTC))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests/canvas/test_canvas_web.py ../tests/panopto/test_panopto_web.py -q
```

Expected: `/setup` and its APIs return 404.

- [ ] **Step 3: Implement status snapshot and one-click request**

Return only bounded operational status. Never include secret values, transcript text, cookies, page HTML, or bearer tokens. The test POST returns JSON `{request_id}` for `setup.js` to dispatch.

- [ ] **Step 4: Implement SSE with polling fallback**

The SSE generator emits `event: status` only when the serialized snapshot changes and sends a heartbeat comment at bounded intervals. `setup.js` uses `EventSource`; on error it starts a five-second fetch poll and reconnects.

- [ ] **Step 5: Build overview and detail panels**

Always render the overview initially. Provide service cards, last activity, live progress, actions, Canvas detail controls, Panopto sign-in/test/automation controls, OpenAI status, and prompt approval. Use one top-level Setup link.

- [ ] **Step 6: Preserve legacy route compatibility**

Redirect `/canvas/setup` to `/setup?detail=canvas` and `/panopto/setup` to `/setup?detail=panopto` so bookmarks open the relevant detail panel without changing the next visit's default overview. Remove old top-navigation links.

- [ ] **Step 7: Run web tests and commit**

Run the focused web tests and `git diff --check`; expect all pass.

```bash
git add src/oms_hub/web/setup_routes.py src/oms_hub/web/templates/setup.html src/oms_hub/web/static/setup.js src/oms_hub/web/templates/base.html src/oms_hub/web/static/app.css src/oms_hub/app.py src/oms_hub/web/canvas_routes.py src/oms_hub/web/panopto_routes.py tests/canvas/test_canvas_web.py tests/panopto/test_panopto_web.py
git commit -m "feat: add live integrated Setup Center"
```

---

### Task 8: End-to-end verification and rollout documentation

**Files:**
- Modify: `tests/panopto/test_phase3_acceptance.py`
- Modify: `docs/phase-3-nuc-rollout.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: automated end-to-end proof and exact NUC rollout/acceptance steps.

- [ ] **Step 1: Rewrite the acceptance test around downloaded files**

Exercise:

1. create scan request;
2. post discovery;
3. create a managed `.txt` file beneath the configured Panopto inbox;
4. post download completion;
5. drain cleaning/file jobs;
6. assert immutable raw file, cleaned canonical file, checklist completion, and OpenAI idempotency;
7. repeat same raw file and then a corrected raw file to prove revision behavior.

- [ ] **Step 2: Add a captions-pending acceptance test**

Assert no revision, no OpenAI call, no review item, and a due request exactly 15 minutes later within the approved window.

- [ ] **Step 3: Run all Python and extension tests**

Run:

```bash
cd src
../.venv/bin/python -m pytest ../tests -q
cd ../extension/canvas-hub
npm test
```

Expected: all Python and extension tests pass with no warnings promoted to errors.

- [ ] **Step 4: Run static checks**

Run:

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy
git diff --check
```

Expected: all checks pass. If mypy has pre-existing failures, record the unchanged baseline and ensure no new failures.

- [ ] **Step 5: Update NUC rollout**

Document stopping the scheduled task/processes, pulling the feature branch, running the installer with `-ExecutionPolicy Bypass`, reloading the unpacked extension, opening `/setup`, and running the six approved live checks. Explicitly state that the extension popup is not part of the normal test flow.

- [ ] **Step 6: Final commit**

```bash
git add tests/panopto/test_phase3_acceptance.py docs/phase-3-nuc-rollout.md README.md
git commit -m "test: verify Panopto caption download workflow"
```

- [ ] **Step 7: Push feature branch**

```bash
git push origin feat/panopto-browser-companion
```

Expected: remote feature branch advances; `main` remains untouched.

- [ ] **Step 8: Live NUC gate**

Do not merge. Ask the user to complete the logged-in, logged-out, captions-pending, real-ingestion, live-status, and restart-recovery checks from the approved design. Merge only after those results are accepted.
