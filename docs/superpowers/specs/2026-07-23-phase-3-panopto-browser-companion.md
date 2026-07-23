# Phase 3 Panopto Browser Companion Design

**Date:** 2026-07-23
**Status:** Approved

## 1. Decision

Phase 3 will use the existing paired OMS Study Hub Chrome companion to access
Panopto through the user's normal authenticated browser session. It will not
use a Panopto API client, OAuth client secret, refresh token, exported browser
cookies, or a separate Selenium browser profile.

This document supersedes the Panopto authentication, discovery transport, and
caption-download portions of:

- `2026-07-23-phase-3-panopto-transcript-automation-design.md`
- `2026-07-23-phase-3-panopto-oauth-correction.md`
- `2026-07-23-phase-3-panopto-transcript-automation.md`

The existing schedule gate, matching rules, immutable raw revisions, OpenAI
cleaning, canonical filing, quarantine behavior, retry/recovery controls, and
lecture checklist updates remain authoritative unless this document explicitly
changes them.

The design adapts the rendered-page transcript extraction idea demonstrated by
the MIT-licensed
[`minjunminji/panopto-lecture-transcript-scraper`](https://github.com/minjunminji/panopto-lecture-transcript-scraper)
project. It does not copy that project's Selenium process management, local
state file, or direct-output storage model. Any implementation code derived
from that project must retain the required MIT attribution in the extension's
notice file.

## 2. Goals

- Let the user sign into LMU Panopto through a link in the Hub.
- Use the resulting Panopto session only inside the existing Chrome profile.
- Scan recordings visible in Panopto **Shared with Me**.
- Match recent recordings to the Outlook-backed lecture schedule.
- Extract the rendered English transcript from confident matches.
- Start transcript cleaning automatically without dashboard approval.
- Preserve every accepted raw transcript as an immutable ProgramData revision.
- Continue filing cleaned transcripts under
  `OMS II/Subject/Exam ##/Transcripts` and updating the lecture checklist.
- Keep Canvas automation operational and isolated from Panopto failures.

## 3. Non-goals

- Reading, exporting, forwarding, or persisting Chrome cookies.
- Storing a Panopto username, password, API client secret, or OAuth token.
- Calling Panopto mutation endpoints.
- Editing, publishing, sharing, uploading, or deleting Panopto content.
- Scanning all recordings in the LMU tenant.
- Using links from individual Canvas lectures to Panopto recordings.
- Generating a transcript from lecture audio when Panopto captions are absent.
- Replacing the existing transcript cleaner, prompt approval, filing, or
  checklist workflow.

## 4. Components and trust boundaries

### 4.1 Chrome companion

The existing Canvas companion becomes the OMS Study Hub browser companion. Its
current pairing bearer and localhost authentication boundary are reused.
Existing installations should not require a new pairing unless the bearer is
already invalid.

The extension gains host access only to:

- `https://lmunet.instructure.com/*`
- `https://lmunet.hosted.panopto.com/*`
- `http://127.0.0.1:8765/*`

It must not request the Chrome `cookies` permission or a broad host pattern.
Content scripts and browser requests use Chrome's active Panopto session
naturally. Cookies remain within Chrome.

### 4.2 Local Hub

The Hub remains authoritative for:

- whether a scheduled scan is eligible;
- which Canvas course/subject mappings are enabled;
- matching Panopto recordings to lectures;
- deduplication and review decisions;
- validation and immutable storage of raw transcripts;
- OpenAI cleaning, canonical filing, and checklist updates.

The companion is an authenticated browser transport and page extractor. It does
not decide canonical file paths or mark lecture checklist steps complete.

### 4.3 Panopto

All accepted Panopto URLs must use HTTPS and the exact host
`lmunet.hosted.panopto.com`. Redirects or extracted links to any other host are
rejected.

The implementation uses rendered Panopto list and viewer pages. It must not
depend on undocumented internal Panopto JSON endpoints for the initial
implementation.

## 5. User experience

The dashboard replaces API-client setup with a **Panopto Browser Session** card.

Controls:

- **Sign in to Panopto** opens the LMU Panopto home page in Chrome.
- **Check connection** asks the companion to confirm that **Shared with Me** is
  accessible.
- **Scan now** queues a manual discovery scan.
- Existing pause/resume and review controls remain available.
- A separate explicit legacy-credential cleanup action may remove obsolete
  Panopto secrets after live acceptance. Migration never removes secrets
  automatically.

Connection states:

- `companion_unavailable`
- `panopto_login_required`
- `connected`
- `scanning`
- `waiting_for_transcript`
- `needs_review`
- `error`

The card shows last contact, last completed scan, bounded counts for discovered,
matched, waiting, and review items, and a sanitized status message. It never
renders transcript contents, browser cookies, credentials, or raw Panopto page
HTML.

Chrome must be running for browser scans. If it is closed, the Hub retains one
idempotent pending command. The companion consumes that command after Chrome
starts and the extension wakes.

## 6. Scheduling and command flow

Automatic discovery remains eligible only when all of these are true:

- Panopto automation is enabled.
- It is Monday through Friday.
- Local time is between 09:20 and 19:00 America/New_York, inclusive.
- At least one lecture is scheduled for the local day.
- The previous successful scan does not already cover the current 15-minute
  interval.

The Hub scheduler evaluates eligibility every 15 minutes. The companion polls
the local Hub for commands once per minute, as it already does for Canvas.

Only one Panopto scan command may be pending or running. Repeated scheduler
ticks coalesce. A unique command ID prevents a retried extension response from
creating duplicate work.

Canvas and Panopto commands have separate state and failure handling. A Panopto
failure cannot set the Canvas connection state or stop Canvas discovery.

## 7. Discovery

### 7.1 Browser behavior

For a Panopto scan, the companion:

1. Opens or reuses an inactive, extension-owned tab on the LMU Panopto origin.
2. Navigates to **Shared with Me** using a verified Panopto URL or a visible
   Panopto navigation control.
3. Waits for the rendered recording list to reach a stable state.
4. Reads bounded recording metadata from the rendered rows/cards.
5. Traverses bounded visible pagination or lazy loading.
6. Sends normalized metadata to the Hub in batches.
7. Closes its extension-owned tab after the command completes.

The companion never navigates, closes, or repurposes a Panopto tab opened by
the user. Reuse applies only to a tab previously created and tracked by the
extension for the current Panopto command.

Selectors and page-reading logic are isolated in a Panopto adapter. A page
layout change fails closed with a sanitized `page_structure_changed` result.
It must not silently report an empty successful scan when the expected list
container is absent.

### 7.2 Discovery bounds

The initial scan considers recordings created today and the previous local day.
It stops when it reaches older recordings in a newest-first list or reaches a
configured maximum page/recording count. The server also rejects an oversized
batch.

Metadata is limited to:

- Panopto recording/session ID;
- title;
- recorded or created time when displayed;
- duration when displayed;
- owner/presenter and folder labels when displayed;
- exact LMU Panopto viewer URL.

Cookies, authorization headers, arbitrary page text, full HTML, thumbnails,
comments, and viewer analytics are excluded.

### 7.3 Login detection

Microsoft/LMU sign-in pages, Panopto access-denied pages, or the absence of an
authenticated Panopto navigation shell produce `panopto_login_required`.
Automatic scans pause until a later check confirms access. The Hub does not
rapidly retry authentication failures.

## 8. Matching and dispositions

The Hub validates each discovery payload and deduplicates by Panopto recording
ID. Existing matching logic compares the recording with the Outlook-backed
schedule using:

- lecture date and time;
- subject and topic;
- lecture number;
- lecturer/presenter;
- recording duration;
- enabled subject/course mappings.

A confident unique match receives an `extract_transcript` disposition.
Ambiguous or unmatched recordings enter the existing review queue. They do not
trigger OpenAI cleaning.

The Hub does not trust the extension to supply a lecture ID or destination
path. Manual review may attach a recording to a lecture and then queue
extraction.

## 9. Transcript extraction

For an `extract_transcript` disposition, the companion:

1. Opens the validated LMU viewer URL.
2. Confirms that the viewer's recording ID matches the requested ID.
3. Opens the transcript/captions panel if it is not already visible.
4. Selects English (United States) when multiple caption languages are offered.
5. Waits for transcript processing or rendered transcript events.
6. Scrolls the transcript pane in bounded increments until all virtualized
   lines have been loaded and the line set is stable.
7. Extracts ordered transcript entries, retaining displayed timestamps when
   available.
8. Posts a bounded UTF-8 transcript payload and minimal extraction metadata to
   the Hub.

The extractor must detect repeated or missing line ranges caused by virtualized
rendering. It must not submit a transcript merely because at least one line is
visible.

The response includes the recording ID, viewer URL, language, line count,
extraction time, and transcript text. It excludes cookies, page HTML, and
unrelated viewer content.

If the transcript is still processing, the result is
`waiting_for_transcript`; the recording remains eligible for a later scheduled
scan. Missing English captions, denied access, an unstable transcript pane, or
changed page structure enter review with sanitized reason codes.

## 10. Validation and immutable ingestion

Before preserving a transcript, the Hub validates:

- command and recording IDs;
- exact Panopto origin and viewer path;
- expected recording-to-lecture disposition;
- English transcript language;
- UTF-8 encoding;
- configured byte and line-count bounds;
- non-empty, non-HTML content;
- stable extraction/completeness metadata.

The accepted payload becomes the input to the existing transcript pipeline.
The Hub calculates its SHA-256 hash before writing it.

An unchanged hash is idempotent and creates no duplicate revision. Corrected
Panopto captions with a new hash create a new immutable revision. The raw file
is written beneath the configured ProgramData Panopto revision root using the
existing verified atomic-write behavior. It is never overwritten.

Only after the raw artifact and hash are durable may the Hub queue automatic
cleaning. The cleaner continues to use:

- the approved editable prompt at
  `C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md`;
- the OpenAI API key in Windows Credential Manager;
- `gpt-5.6-terra`;
- existing output-length, integrity, retry, usage, and cost validation.

Cleaned output is filed under
`OMS II/Subject/Exam ##/Transcripts`. Existing quarantine and review behavior
applies to unsafe paths, changed immutable inputs, suspicious cleaning output,
and filing conflicts.

## 11. Checklist behavior

Checklist steps change only after their corresponding durable events:

- recording discovered and confidently matched;
- immutable raw transcript stored;
- cleaned transcript validated;
- cleaned transcript filed to the canonical destination.

Waiting, review, extension, and browser-login states never falsely complete a
checklist step. Reprocessing an unchanged transcript is idempotent.

## 12. Security and privacy requirements

- Never request Chrome cookie access.
- Never serialize or transmit cookies or browser authorization headers.
- Never log transcript text, Panopto page HTML, OpenAI keys, pairing bearers, or
  legacy Panopto secrets.
- Authenticate all companion-to-Hub requests with the existing paired bearer.
- Require JSON, strict request schemas, bounded bodies, and `extra="forbid"`.
- Validate all Panopto URLs on both extension and Hub sides.
- Accept connections only on the loopback Hub address.
- Use read-only browser interactions; no Panopto mutations.
- Preserve the existing ProgramData ACL and immutable-artifact protections.
- Keep synthetic Panopto test fixtures free of real student, lecturer, or
  transcript data.

## 13. Errors and recovery

Expected states:

- Chrome closed or extension asleep: command remains pending.
- Panopto login expired: `panopto_login_required`; user signs in again.
- Transcript processing: waiting state; retry during later eligible scans.
- No scheduled lecture match: review.
- Missing English captions: review.
- Page adapter cannot prove completeness: review.
- Browser or localhost transient failure: bounded retry with idempotent command
  ID.
- Immutable raw file missing or changed: existing recovery sends the job to
  review.
- OpenAI transient/rate limit: existing exponential backoff.
- OpenAI authentication or invalid output: existing review behavior.

Status messages use stable, sanitized reason codes with a short user-facing
description. Raw exceptions and response bodies are not sent to the dashboard.

## 14. Migration from Panopto OAuth

The implementation removes Panopto OAuth from the active application wiring,
dashboard, commands, environment example, setup guide, and acceptance flow.
`panopto_client_id`, OAuth redirect configuration, and token-provider objects
are no longer required.

Existing database recording/revision/job tables are retained. Existing
immutable transcript artifacts remain valid.

Legacy Credential Manager entries are ignored. They are not deleted during
installation or startup. After browser-based live acceptance, the user may run
an explicit cleanup action that names the exact entries to remove.

The extension update adds only the LMU Panopto host permission and Panopto
adapter. Chrome may require the user to approve the added site access when the
unpacked extension is refreshed.

## 15. Testing

### 15.1 Extension tests

- Authenticated and unauthenticated **Shared with Me** fixtures.
- List pagination/lazy loading and newest-first cutoff.
- Metadata normalization and URL rejection.
- Viewer transcript panel opening.
- Virtualized transcript scrolling and stable completion.
- Repeated, missing, empty, processing, and non-English transcript cases.
- Page-structure changes fail closed.
- Background tab creation, reuse, cleanup, and command idempotency.
- Canvas scanning regression.

### 15.2 Hub tests

- Strict authenticated Panopto companion endpoints.
- Request-size, item-count, line-count, and URL bounds.
- Command coalescing and one-time consumption.
- Schedule-window and lecture-day eligibility.
- Discovery matching and review dispositions.
- Transcript hash deduplication and corrected revisions.
- Immutable raw writes and recovery.
- Automatic Terra cleaning, filing, and checklist transitions.
- Sanitized logs and dashboard messages.
- No dependency on Panopto client ID or secret.

### 15.3 Live NUC acceptance

1. Update and refresh the existing paired extension.
2. Confirm Canvas scan behavior is unchanged.
3. Use **Sign in to Panopto** and complete LMU Microsoft authentication.
4. Confirm **Check connection** can access **Shared with Me**.
5. Manually test recording
   `8796399e-393c-4256-b6e4-b48f0150d156`.
6. Verify the extracted transcript passes completeness validation without
   logging its content.
7. Verify the immutable raw revision is beneath ProgramData.
8. Verify automatic `gpt-5.6-terra` cleaning uses the approved Obsidian prompt.
9. Verify canonical filing and all corresponding checklist transitions.
10. Verify an unchanged rescan creates no duplicate revision.
11. Verify login-expiration recovery.
12. Process one newly shared lecture during the scheduled polling window.

## 16. Merge and rollback

Implementation remains on the feature branch until automated tests and live NUC
acceptance pass. Only then is it merged into `main`.

Rollback pauses Panopto automation and restores the prior Hub/extension version.
It does not delete the database, immutable ProgramData revisions, canonical
transcripts, Canvas artifacts, or legacy credentials. Canvas must remain
operational throughout rollback.

## 17. Acceptance criteria

Phase 3 browser-based Panopto automation is accepted when the Windows NUC can:

- use the existing paired Chrome companion and the user's normal Panopto login;
- scan only recent recordings visible in **Shared with Me** during the approved
  schedule;
- match and extract a complete English transcript without exporting cookies;
- preserve the raw transcript immutably and idempotently;
- clean it automatically with the approved Obsidian prompt and
  `gpt-5.6-terra`;
- file it to the correct OMS II transcript folder;
- update checklist steps only after durable events;
- recover safely from closed Chrome, expired login, processing captions, page
  changes, and worker interruption;
- preserve Canvas behavior, quarantine behavior, and all immutable originals;
- operate without a Panopto API client ID or secret.
