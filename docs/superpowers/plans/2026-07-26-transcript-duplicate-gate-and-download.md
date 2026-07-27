# Transcript Duplicate Cost Gate and Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pause a different transcript matched to an already-cleaned lecture before any LLM request, and add a validated, descriptively named `.txt` download to the cleaned-transcript review page.

**Architecture:** Keep duplicate classification authoritative in `IngestionRepository`, with exact-source idempotency checked before the broader current-transcript warning. Route confirmation and staged-file discard through `IngestionService`, which serializes competing actions and delegates path-safe deletion to `StagingService`. Enrich batch JSON with catalog display metadata, render an accessible modal in the upload page, and expose a dedicated attachment route that reuses `ArtifactService` validation.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2, Jinja2, browser JavaScript, Node test runner, pytest.

## Global Constraints

- No LLM request or processing job may exist while an upload is `awaiting_confirmation`.
- Exact source SHA-256 duplicates remain cost-free and do not open the warning.
- Discard deletes only the new staged file and never changes the existing cleaned transcript.
- The warning label is `COURSE · Lecture ## · LECTURE TOPIC`.
- The download filename is `COURSE - Lecture ## - TOPIC - Transcript.txt`.
- Downloaded bytes must be the existing path-contained, checksum-validated UTF-8 cleaned artifact.
- All mutation requests remain CSRF-protected and repeat clicks cannot create multiple jobs.
- No new environment variable, secret, dependency, or database migration is introduced.

---

### Task 1: Server-Side Duplicate Classification and Decisions

**Files:**
- Modify: `src/oms_hub/ingestion/domain.py`
- Modify: `src/oms_hub/ingestion/repository.py`
- Modify: `src/oms_hub/ingestion/staging.py`
- Modify: `src/oms_hub/ingestion/service.py`
- Modify: `src/oms_hub/app.py`
- Create: `tests/v2/test_transcript_duplicate_gate.py`

**Interfaces:**
- Consumes: `StudyRevisionModel.current`, `StudyRevisionModel.source_sha256`, `StagingService.root`, and the unique ingestion-job constraint.
- Produces: `UploadState.AWAITING_CONFIRMATION`, `UploadState.DISCARDED`, `IngestionRepository.confirm_processing(item_id: str) -> StoredUploadItem`, `IngestionRepository.mark_discarded(item_id: str) -> StoredUploadItem`, `StagingService.discard_file(path: Path) -> None`, `IngestionService.confirm_processing(item_id: str) -> StoredUploadItem`, and `IngestionService.discard_item(item_id: str) -> StoredUploadItem`.

- [ ] **Step 1: Write failing repository tests for all three classifications**

Create fixtures that insert a catalog lecture, a current cleaned transcript
revision, and staged upload items. Verify:

```python
assert repository.count_jobs(new_item_id, "process") == 1
assert repository.require_item(exact_item_id).state is UploadState.COMPLETE
assert repository.count_jobs(exact_item_id, "process") == 0
assert (
    repository.require_item(different_item_id).state
    is UploadState.AWAITING_CONFIRMATION
)
assert repository.count_jobs(different_item_id, "process") == 0
```

- [ ] **Step 2: Run the focused tests and confirm the missing state fails**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_duplicate_gate.py -q
```

Expected: fail because `UploadState.AWAITING_CONFIRMATION` does not exist and
the repository currently queues a different transcript.

- [ ] **Step 3: Implement the two states and classification order**

Add:

```python
class UploadState(StrEnum):
    ...
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DISCARDED = "discarded"
```

Change `_enqueue_unless_current_duplicate` to:

```python
exact = session.scalar(
    select(StudyRevisionModel.id).where(
        StudyRevisionModel.lecture_id == item.lecture_id,
        StudyRevisionModel.kind == item.kind,
        StudyRevisionModel.source_sha256 == item.sha256,
        StudyRevisionModel.current.is_(True),
    )
)
if exact is not None:
    item.state = UploadState.COMPLETE.value
    item.error = None
    return
current = session.scalar(
    select(StudyRevisionModel.id).where(
        StudyRevisionModel.lecture_id == item.lecture_id,
        StudyRevisionModel.kind == item.kind,
        StudyRevisionModel.current.is_(True),
    )
)
if current is not None and item.kind == UploadKind.TRANSCRIPTS.value:
    item.state = UploadState.AWAITING_CONFIRMATION.value
    item.error = None
    return
self._enqueue(session, item.id, "process")
```

Place `AWAITING_CONFIRMATION` in batch-state priority before queued processing
states and treat `DISCARDED` as terminal.

- [ ] **Step 4: Write failing tests for confirm, repeat confirm, and discard**

Verify:

```python
first = service.confirm_processing(item_id)
second = service.confirm_processing(item_id)
assert first.state is UploadState.QUEUED
assert second.state is UploadState.QUEUED
assert repository.count_jobs(item_id, "process") == 1

discarded = service.discard_item(other_item_id)
assert discarded.state is UploadState.DISCARDED
assert not staged_path.exists()
assert repository.count_jobs(other_item_id, "process") == 0
```

Also test that a path outside the staging root raises `UploadRejected`, leaves
the file untouched, and leaves the item awaiting confirmation.

- [ ] **Step 5: Implement serialized confirmation and safe discard**

Use a private `threading.RLock` in `IngestionService`. Inside the lock:

```python
def confirm_processing(self, item_id: str) -> StoredUploadItem:
    with self._decision_lock:
        return self.repository.confirm_processing(item_id)

def discard_item(self, item_id: str) -> StoredUploadItem:
    with self._decision_lock:
        item = self.repository.require_item(item_id)
        if item.state is UploadState.DISCARDED:
            return item
        if item.state is not UploadState.AWAITING_CONFIRMATION:
            raise ValueError("upload is not awaiting confirmation")
        self.staging.discard_file(item.staged_path)
        return self.repository.mark_discarded(item_id)
```

`StagingService.discard_file` must resolve both the candidate and staging root,
reject paths outside the root, require a regular file, and call `unlink()`.
Construct `StagingService` before `IngestionService` in `create_app` and inject
it into the service.

`IngestionRepository.confirm_processing` queues only when the current state is
`awaiting_confirmation`; already-queued requests return the current item.
`mark_discarded` transitions only from `awaiting_confirmation`, syncs the
batch, and never creates a job.

- [ ] **Step 6: Run the server-side gate tests**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_duplicate_gate.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the server gate**

```bash
git add src/oms_hub/ingestion src/oms_hub/app.py tests/v2/test_transcript_duplicate_gate.py
git commit -m "feat: gate duplicate transcript processing"
```

---

### Task 2: Confirmation API and Upload Warning Interface

**Files:**
- Modify: `src/oms_hub/web/upload_routes.py`
- Modify: `src/oms_hub/web/templates/uploads.html`
- Modify: `src/oms_hub/web/static/uploads.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `tests/v2/test_transcript_duplicate_gate.py`
- Create: `tests/js/uploads.test.js`

**Interfaces:**
- Consumes: the Task 1 service decision methods and `CatalogRepository.get_lecture(lecture_id)`.
- Produces: `POST /api/upload-items/{item_id}/confirm`, `POST /api/upload-items/{item_id}/discard`, and batch item `duplicate_warning` metadata with `subject`, `lecture_number`, and `topic`.

- [ ] **Step 1: Write failing route tests**

Build a paused item and assert:

```python
batch = client.get(f"/api/upload-batches/{batch_id}").json()
warning = batch["items"][0]["duplicate_warning"]
assert warning == {
    "subject": "Cardiology",
    "lecture_number": 7,
    "topic": "Heart Failure",
}

confirmed = client.post(f"/api/upload-items/{item_id}/confirm")
assert confirmed.json()["state"] == "queued"
assert repository.count_jobs(item_id, "process") == 1
```

Create a second paused item, call its discard endpoint, and assert state
`discarded`, no staged file, and no job. Assert an item not awaiting
confirmation receives `409`.

- [ ] **Step 2: Run the route tests and verify failure**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_duplicate_gate.py -q
```

Expected: fail with missing routes and missing `duplicate_warning`.

- [ ] **Step 3: Add safe batch metadata and decision endpoints**

In `batch_status`, start from `batch.public_dict()`, then attach
`duplicate_warning` only to awaiting transcript items whose lecture exists:

```python
item_payload["duplicate_warning"] = {
    "subject": lecture.subject,
    "lecture_number": lecture.lecture_number,
    "topic": lecture.topic,
}
```

The confirm/discard endpoints call `request.app.state.ingestion_service` and
return:

```python
{"item_id": item.id, "state": item.state.value}
```

Map missing items to `404`, invalid/stale decisions to `409`, and safe staged
file rejection or deletion failure to `409`.

- [ ] **Step 4: Write failing HTML and JavaScript tests**

The Python page test asserts one native dialog, the warning copy, detected
lecture output, and both action buttons. Node tests import exported helper
functions and assert:

```javascript
assert.equal(formatLecture({
  subject: "Cardiology",
  lecture_number: 7,
  topic: "Heart Failure",
}), "Cardiology · Lecture 07 · Heart Failure");

assert.equal(nextConfirmation(batch).id, "paused-item");
```

Also assert decision requests are `POST`, include the CSRF header, and never
put item data in a query string.

- [ ] **Step 5: Implement the accessible modal and polling continuation**

Add a `<dialog data-duplicate-dialog>` with text-only lecture fields and
buttons carrying `data-confirm-duplicate` and `data-discard-duplicate`.
In `uploads.js`:

- export pure helpers under `module.exports` when running in Node;
- format lecture numbers with `String(number).padStart(2, "0")`;
- stop batch polling when an awaiting item is encountered;
- open the dialog with `showModal()` and focus the safe default
  **Discard upload** action;
- prevent `cancel` and backdrop dismissal;
- disable both buttons while posting;
- on success, close the dialog and resume polling;
- on failure, keep the dialog open and display the safe response detail;
- render exact duplicates as “already processed” without opening the dialog.

Use `textContent` exclusively for lecture metadata and error output.

- [ ] **Step 6: Run route and JavaScript tests**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_duplicate_gate.py -q
node --test tests/js/uploads.test.js
```

Expected: all tests pass.

- [ ] **Step 7: Commit the interface**

```bash
git add src/oms_hub/web/upload_routes.py src/oms_hub/web/templates/uploads.html src/oms_hub/web/static/uploads.js src/oms_hub/web/static/app.css tests/v2/test_transcript_duplicate_gate.py tests/js/uploads.test.js
git commit -m "feat: add duplicate transcript confirmation"
```

---

### Task 3: Validated Cleaned-Transcript Download

**Files:**
- Modify: `src/oms_hub/web/artifact_routes.py`
- Modify: `src/oms_hub/web/templates/artifact_text.html`
- Create: `tests/v2/test_transcript_download.py`

**Interfaces:**
- Consumes: `ArtifactService.resolve(revision_id, ArtifactRole.CLEANED)`,
  `IngestionRepository.get_study_revision(revision_id)`,
  `CatalogRepository.get_lecture(lecture_id)`, and
  `sanitize_filename(value: str) -> str`.
- Produces: `GET /artifacts/{revision_id}/cleaned/download` and a conditional
  `download_url` on cleaned transcript review pages.

- [ ] **Step 1: Write failing download tests**

Create a current transcript revision with a validated canonical cleaned file,
then assert:

```python
response = client.get(f"/artifacts/{revision_id}/cleaned/download")
assert response.status_code == 200
assert response.content == cleaned_bytes
assert response.headers["content-type"].startswith("text/plain")
assert "Cardiology - Lecture 07 - Heart Failure - Transcript.txt" in (
    response.headers["content-disposition"]
)
assert response.headers["cache-control"] == "private, no-store"
```

Assert the cleaned review page contains the endpoint URL and button copy.
Corrupt the file and assert the endpoint returns `409` without returning the
corrupt bytes.

- [ ] **Step 2: Run the download tests and verify failure**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_download.py -q
```

Expected: fail with `404` for the missing download route and no review button.

- [ ] **Step 3: Implement the dedicated validated attachment route**

Refactor artifact error mapping into a small private resolver helper so both
display and download use identical `404`/`409` behavior. The download route:

```python
resolved = _resolve(request, revision_id, ArtifactRole.CLEANED)
revision = request.app.state.ingestion_repository.get_study_revision(revision_id)
lecture = request.app.state.catalog_repository.get_lecture(revision.lecture_id)
filename = sanitize_filename(
    f"{lecture.subject} - Lecture {lecture.lecture_number:02d} - "
    f"{lecture.topic} - Transcript"
) + ".txt"
return FileResponse(
    resolved.path,
    media_type="text/plain; charset=utf-8",
    filename=filename,
    content_disposition_type="attachment",
    headers={"Cache-Control": "private, no-store"},
)
```

Read the resolved path as UTF-8 before returning to preserve the design's
readability check. Reject a missing catalog lecture with `409`.

Pass `download_url` only when the review role is `ArtifactRole.CLEANED`, and
render a standard **Download transcript** link above the `<pre>`.

- [ ] **Step 4: Run the download tests**

Run:

```bash
PYTHONPATH=src:. python -m pytest tests/v2/test_transcript_download.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit transcript download**

```bash
git add src/oms_hub/web/artifact_routes.py src/oms_hub/web/templates/artifact_text.html tests/v2/test_transcript_download.py
git commit -m "feat: download cleaned transcripts"
```

---

### Task 4: Regression Verification and Release Refresh

**Files:**
- Modify: `README.md`
- Modify: `docs/v2-multi-provider-nuc-rollout.md`
- Modify: `tests/v2/test_release_package.py`
- Regenerate (ignored): `dist/Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip`
- Regenerate (ignored): `dist/Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip.sha256`
- Regenerate (ignored): `dist/Study-Hub-V2-Source-20260726.zip`
- Regenerate (ignored): `dist/Study-Hub-V2-Source-20260726.zip.sha256`

**Interfaces:**
- Consumes: the completed server, interface, and download behavior from Tasks 1–3.
- Produces: updated operator instructions and deterministic V2 packages containing the new files.

- [ ] **Step 1: Extend release-package expectations**

Assert the hotfix manifest includes the changed upload, artifact, template,
JavaScript, CSS, ingestion, app, and test-independent runtime files and still
excludes `.env`, databases, key material, caches, and bytecode.

- [ ] **Step 2: Update user-facing documentation**

Document:

- exact duplicates do not call an LLM;
- different transcripts for cleaned lectures pause for confirmation;
- discard removes the new staged upload;
- cleaned transcript review pages offer descriptive `.txt` downloads.

- [ ] **Step 3: Run the full verification suite**

Run:

```bash
PYTHONPATH=src:. python -m pytest -q
node --test tests/js/settings.test.js tests/js/uploads.test.js
PYTHONPATH=src:. python -m ruff check src tests scripts
PYTHONPATH=src:. python -m mypy src/oms_hub
git diff --check
```

Expected: every test and static check passes.

- [ ] **Step 4: Rebuild and validate release archives**

Run:

```bash
PYTHONPATH=src:. python scripts/build-v2-release.py
PYTHONPATH=src:. python -m pytest tests/v2/test_release_package.py -q
shasum -a 256 -c dist/Study-Hub-V2-Multi-Provider-Hotfix-20260726.zip.sha256
shasum -a 256 -c dist/Study-Hub-V2-Source-20260726.zip.sha256
```

Expected: both archive checksums report `OK`; package tests pass; no secret or
runtime data appears in either archive.

- [ ] **Step 5: Commit documentation and release metadata**

```bash
git add README.md docs/v2-multi-provider-nuc-rollout.md tests/v2/test_release_package.py
git commit -m "docs: add transcript cost gate rollout"
```

- [ ] **Step 6: Confirm clean branch state**

Run:

```bash
git status -sb
git log -5 --oneline
```

Expected: a clean `codex/v2-multi-provider-settings` branch containing the
design, plan, feature, tests, documentation, and refreshed ignored release
archives.
