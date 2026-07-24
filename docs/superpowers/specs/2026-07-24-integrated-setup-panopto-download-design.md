# Integrated Setup Center and Panopto Caption Download Design

Date: 2026-07-24  
Status: Approved in conversation; awaiting written-spec review  
Branch: `feat/panopto-browser-companion`

## Objective

Replace the fragile two-step Panopto browser-command workflow with a single Hub action that immediately uses the existing Chrome companion to test Panopto. Replace transcript-panel scraping with Panopto's built-in English caption download. Consolidate Canvas, Panopto, OpenAI, and prompt health into one live-updating Setup Center.

The design preserves the existing Phase 3 requirements:

- use the user's normal LMU Panopto Chrome session;
- scan recordings shared with the user;
- poll on scheduled lecture days every 15 minutes from 9:20 AM through 7:00 PM Eastern;
- preserve immutable raw transcript revisions under ProgramData;
- clean automatically with the configured OpenAI model and approved Obsidian prompt;
- route cleaned transcripts into the OMS II hierarchy;
- update lecture checklists;
- quarantine malformed or conflicting artifacts;
- never expose or commit cookies, bearer tokens, or API keys.

## Problem Being Corrected

The current implementation has three coupled failure modes:

1. The Hub queues a persistent command.
2. The extension separately claims that command through its popup or background alarm.
3. A transient Manifest V3 service worker and a persistent `running` database row must remain synchronized.

An interrupted tab, suspended worker, extension reload, or Hub restart can leave the database and browser disagreeing about whether work is still running. The dashboard then redirects successfully but provides no useful progress, while the extension may report that no command is pending.

Transcript extraction also depends on Panopto's transcript-panel DOM and rendering behavior. This is unnecessary because Panopto already exposes a caption download that immediately produces a consistent `.txt` file.

## User Experience

### Navigation

The top navigation has one **Setup** entry. Existing Canvas and Panopto setup controls move into a single Setup Center. The Setup Center always opens on its overview rather than remembering the last detail view.

### Setup overview

The overview shows current health and the relevant recovery action for:

- Hub service;
- Chrome companion;
- Canvas browser session;
- Canvas automation;
- Panopto browser session;
- Panopto automation;
- OpenAI credential and last successful cleaning request;
- transcript-cleaning prompt.

Canvas and Panopto rows open their detailed setup panels. The overview remains the default landing view.

### One-click Panopto test

The Panopto detail panel contains:

1. **Open Panopto sign-in**, which opens the LMU Panopto home page in Chrome.
2. **Test Panopto Connection**, which performs the complete test without requiring the user to open or click the extension popup.

The test displays live progress:

```text
Opening Shared with Me
→ Opening newest recording
→ Looking for English captions
→ Downloading transcript
→ Validating transcript
→ Connected
```

The test opens the newest Shared with Me recording in a visible active tab. The video is not played.

If Panopto redirects to Microsoft sign-in, the tab remains open and the request becomes `awaiting_login`. After the user signs in and Panopto returns to the recording, the same test continues automatically without another Hub action.

On successful validation, the test tab closes. If the user closes the tab during sign-in, the Setup Center reports that sign-in was interrupted. Other terminal outcomes close the test tab after reporting their result.

If the newest recording exists but its caption download is not ready, the test reports:

```text
Panopto connected · newest recording captions pending
Next check in 15 minutes
```

This is a waiting state, not a connection failure. The request remains eligible for retry until captions become available.

## Architecture

### Immediate Hub-to-extension bridge

The Hub page and companion extension communicate through a narrowly scoped local bridge:

1. The Setup Center posts a new Panopto test request to the Hub.
2. The Hub stores the request and returns an opaque request ID.
3. Page JavaScript emits a same-page event containing only that request ID.
4. A companion content script installed only on `http://127.0.0.1:8765/*` validates the exact Hub origin and forwards the ID to the extension service worker.
5. The service worker retrieves the request from the authenticated local API and begins immediately.

The dashboard never receives or reads the extension bearer token. The bridge cannot provide arbitrary URLs or commands; it can only request the predefined Panopto connection-test operation for a server-issued opaque ID.

The extension popup remains available for pairing, repair, and diagnostics, but it is not part of normal Canvas or Panopto operation.

### Persistent desired state

The Panopto workflow uses persistent desired/request state rather than a claim-and-lease command queue.

Each request records:

- opaque request ID;
- operation type;
- requested, started, and completed timestamps;
- current state;
- bounded reason code;
- last progress update;
- retry eligibility and next eligible time.

Operations are safe for at-least-once execution. A Chrome suspension, extension reload, or Hub restart leaves the request eligible to resume. There is no user-visible `pending` versus orphaned `running` distinction.

Manual tests use the immediate bridge. The extension also checks outstanding desired state during its normal alarm so a missed bridge signal still recovers automatically.

Scheduled scans use the same request-state model. They remain background operations and do not steal browser focus unless user sign-in is required.

The legacy `panopto_browser_commands` table remains in place to avoid a destructive migration, but new Panopto work no longer uses it. On first startup after migration, legacy `pending` or `running` rows are marked failed with a bounded `superseded_command_model` reason. Existing completed history remains intact.

### Live Setup status

The Setup Center subscribes to a same-origin server-sent event stream. State changes are sent when:

- a test or scan is requested;
- the extension begins work;
- a browser tab opens or reaches sign-in;
- Shared with Me or the newest recording loads;
- caption availability is determined;
- a download begins or completes;
- validation, ingestion, cleaning, routing, or checklist work changes state;
- a Canvas or Panopto heartbeat changes health.

If the event stream disconnects, the page falls back to a bounded five-second status poll and reconnects automatically. A manual page reload is never required to observe a completed test or service outage.

## Panopto Caption Download

### Recording discovery

The extension opens the standard Panopto Shared with Me list and reads bounded recording metadata. It sorts by Panopto's recording creation timestamp and selects the newest recording for the connection test.

Scheduled discovery continues to examine up to 100 Shared with Me recordings and matches relevant recent recordings to the Outlook-derived lecture schedule.

### Caption retrieval

The extension does not read transcript lines from the transcript panel and does not wait through lecture playback.

For a selected recording, it:

1. opens the Panopto viewer;
2. locates Panopto's built-in caption download control;
3. selects **English (United States)** when a language choice is present;
4. obtains or invokes the authenticated caption download;
5. saves the `.txt` file into the managed Panopto download location;
6. waits for Chrome to report download completion;
7. reports only bounded download metadata to the local Hub.

Transcript-panel DOM extraction is not a production fallback. If Panopto's caption download control is absent, the recording becomes `waiting_for_captions`. If the expected control structure changes despite captions being available, the recording becomes `needs_review` with `page_structure_changed`.

### Connection-test isolation

The connection test downloads into a temporary, request-specific test location. The Hub validates:

- the download belongs to the expected request and recording;
- the file is a bounded `.txt` file;
- the selected language is English (United States);
- the content is nonempty and passes raw-caption validation.

The test does not clean, route, update a lecture checklist, or create a production revision. Its temporary file is removed after validation or bounded failure handling.

### Production ingestion

For a matched scheduled lecture, the Hub:

1. validates the completed download;
2. copies the raw transcript into the immutable ProgramData revision hierarchy;
3. records its hash and source metadata;
4. avoids duplicate work when the same recording and raw hash are seen again;
5. quarantines malformed, conflicting, oversized, or unexpected files;
6. cleans the validated raw revision with the approved Obsidian prompt and configured OpenAI model;
7. writes the cleaned transcript under `OMS II/Subject/Exam ##/Transcripts`;
8. updates the lecture checklist only after the canonical cleaned artifact is committed.

ProgramData originals are never overwritten.

## Caption Waiting and Polling

If a matched recording is released before captions are generated:

- store or update the recording as `waiting_for_captions`;
- do not create an empty transcript revision;
- do not send anything to OpenAI;
- do not create a review item merely because captions are still processing;
- set the next eligible check to the next 15-minute polling slot;
- retry only on scheduled lecture weekdays between 9:20 AM and 7:00 PM Eastern;
- retain the recording in the polling lineup until caption download succeeds.

Authentication failure pauses further Panopto work and updates Setup to **Sign-in required**. Page-structure changes, invalid downloads, ambiguous matching, or conflicting revisions enter review rather than retrying indefinitely.

## Status Semantics

The Setup Center uses explicit states:

| Area | Healthy states | Waiting states | Action states |
|---|---|---|---|
| Companion | Connected | Starting | Repair pairing / unavailable |
| Canvas session | Signed in | Scanning | Sign-in required |
| Panopto session | Connected | Testing / captions pending | Sign-in required / page changed |
| Panopto automation | Enabled | Waiting for schedule or captions | Paused / needs review |
| OpenAI | Configured; last request successful | Not used yet | Missing key / request failed |
| Cleaning prompt | Approved | Changed since approval | Missing / approval required |

Status details include the last successful activity and current operation without exposing secrets or transcript content.

## Error Handling

All browser and Hub boundaries use bounded reason codes. At minimum:

- `companion_unavailable`;
- `panopto_login_required`;
- `panopto_sign_in_interrupted`;
- `shared_recordings_unavailable`;
- `captions_pending`;
- `english_captions_missing`;
- `caption_download_failed`;
- `caption_download_invalid`;
- `page_structure_changed`;
- `hub_request_failed`;
- `needs_review`.

User-facing messages translate these codes into an action. Raw page HTML, cookies, authorization headers, API keys, transcript excerpts, and unbounded exception strings are never stored or rendered.

## Testing

### Automated tests

Backend and extension tests cover:

- exact-origin validation for the local Hub bridge;
- rejection of arbitrary actions, URLs, and request IDs;
- one Hub click immediately starting the extension request;
- recovery when the immediate bridge signal is missed;
- newest-recording selection by timestamp;
- visible active tabs for manual tests;
- automatic continuation after Microsoft/Panopto sign-in;
- caption-control and English-language selection;
- successful `.txt` download completion;
- `captions_pending` behavior when the download control is absent;
- 15-minute retry eligibility within the approved polling window;
- no retry outside that window;
- temporary test-download isolation and cleanup;
- immutable production ingestion;
- duplicate-download idempotency;
- quarantine for invalid or conflicting downloads;
- live status events and polling fallback;
- bounded failure messages;
- tab closure on success and preservation during required sign-in.

### NUC acceptance

Before merging to `main`, verify on the Windows NUC:

1. Setup always opens to the combined overview.
2. Canvas, Panopto, extension, OpenAI, and prompt health update without reloading.
3. A logged-in one-click Panopto test opens the newest recording, downloads captions, validates them, closes the tab, and reports Connected.
4. A logged-out test permits Microsoft sign-in and resumes automatically.
5. A newest recording without captions becomes captions pending and re-enters polling.
6. A real scheduled recording downloads, preserves its immutable raw revision, cleans, routes, and updates its checklist.
7. Hub restart, Chrome restart, and extension reload recover outstanding work without an orphaned command.
8. Malformed downloads and ambiguous matches enter quarantine or review without overwriting originals.

## Rollout and Compatibility

- Continue work on `feat/panopto-browser-companion`.
- Do not merge to `main` until live NUC acceptance passes.
- Retain the existing extension pairing and Canvas behavior.
- Reload the unpacked extension after installing the updated branch.
- Preserve existing Panopto recordings, revisions, usage, review data, and completed command history.
- Do not adopt or depend on a third-party transcript extension.

The design borrows the established one-click authenticated caption-extraction interaction used by Panopto transcript tools while keeping all OMS-specific scheduling, matching, immutable storage, cleaning, routing, and checklist logic inside the local Hub.

## Out of Scope

- Panopto OAuth or API-client credentials;
- cookie export or browser-cookie extraction;
- video or audio download;
- lecture playback or real-time caption capture;
- use of transcript-panel DOM text as a production fallback;
- dependence on a Chrome Web Store extension;
- merging to `main` before live acceptance.
