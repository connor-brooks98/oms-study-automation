# Phase 2 Canvas and File Pipeline — Design Specification

Date: July 21, 2026  
Status: Approved design, pending written-spec review

## 1. Objective

Extend the OMS II Study Automation Hub so newly posted or revised Canvas
lecture PowerPoints and professor practice-question files reach the correct
local and iCloud staging folders exactly once. Preserve every source revision,
automatically process high-confidence new files, and require approval before a
changed lecture replaces the current version.

Phase 2 must use the user's existing signed-in Chrome session without a Canvas
API token, password storage, SSO automation, or attempts to bypass MFA and
LockDown Browser session resets.

## 2. Scope

Phase 2 includes:

- A private Chrome extension derived from the MIT-licensed Canvas Course
  Downloader project.
- One-time extension pairing and Canvas course-to-subject mapping.
- Module, lecture-page, and attachment discovery every 30 minutes.
- Classification of lecture PowerPoints and professor practice questions.
- Catalog matching using Canvas context and the Phase 1 lecture catalog.
- Incremental download and immutable source revision storage.
- PowerPoint-to-PDF and Word-to-PDF conversion through installed Microsoft
  Office desktop applications.
- PDF validation, canonical naming, and automatic folder creation.
- Local filing under the user's Documents folder.
- Mirrored PDF staging in a visible iCloud Drive folder for later Goodnotes
  import.
- Dashboard setup, connection status, discovery preview, review queues,
  revision approval, retry, and audit details.
- Chrome startup recovery when the NUC user signs in.

Phase 2 excludes Panopto, transcript cleaning, NotebookLM, Gemini, Google Docs,
actual Goodnotes UI import, and Anki.

## 3. Selected Approach

Use a private Hub companion extension rather than the unmodified Chrome Web
Store extension or full browser automation.

The Web Store extension supports session-based Canvas REST calls and
incremental downloads, but it requires manual triggering and exports far more
course content than this workflow needs. Full browser automation would be more
fragile and would risk contention with the user's normal Chrome profile.

The private extension will reuse only the relevant session, pagination,
module-discovery, and download patterns. It will retain required MIT license and
attribution files. It will not include teacher exports, grades, quizzes,
submissions, discussions, full-course archives, or general-page injection.

Initial upstream reference:

- Project: `jasp-nerd/canvas-course-downloader`
- Baseline reviewed: release `v2.10.0`
- License: MIT
- Source: <https://github.com/jasp-nerd/canvas-course-downloader>

## 4. Trust Boundary and Permissions

The unpacked extension lives in `extension/canvas-hub/` and is loaded through
Chrome Developer Mode. Its host access is restricted to:

```text
https://lmunet.instructure.com/*
http://127.0.0.1:8765/*
```

It does not inject into every HTTPS page. It uses the active Canvas session and
never reads, exports, or sends Canvas cookies to the Hub.

The dashboard creates a short-lived one-time pairing code. After pairing, the
extension and Hub use a random local bearer credential. The extension stores
its copy in `chrome.storage.local`; the Hub stores its copy through Windows
Credential Manager. The database stores only a non-reversible credential
fingerprint and pairing audit metadata. Pairing can be revoked from the
dashboard.

The Canvas ingestion API remains bound to localhost. It rejects missing or
invalid credentials, unexpected content types, oversized requests, and file
paths outside the configured Canvas inbox.

## 5. Canvas Configuration

The existing Chrome profile is used. Chrome normally remains open, but the
Windows startup script also starts Chrome when no Chrome process is running.
It must not launch a duplicate instance.

The Canvas base URL is fixed to:

```text
https://lmunet.instructure.com/
```

The setup screen fetches active Fall 2026 courses and asks the user to select
and map the eight relevant course IDs:

| Canvas course | Catalog subject |
|---|---|
| Clinical Neuroscience | Neuro |
| Musculoskeletal | MSK |
| Osteopathic Principles & Practice III | OPP |
| Essentials Patient Care III | EPC |
| Hematology & Lymph | Heme/Lymph |
| Cardiovascular | Cardio |
| Renal | Renal |
| Respiratory | Resp |

Mappings use stable Canvas course IDs. Display names and course codes are
retained as evidence but do not become routing authorities.

## 6. Discovery Protocol

Chrome's extension alarm runs every 30 minutes while Chrome is available. The
extension also supports **Scan now** from the dashboard and extension popup.
Only configured courses are scanned.

For each configured course, the extension:

1. Lists modules and their items using Canvas session-authenticated REST calls.
2. Opens only module items that can contain lecture-page attachments or direct
   files.
3. Extracts course ID, module ID/title, item ID/title/type, page URL/title,
   attachment file ID, filename, content type, size, modified timestamp, and
   authenticated download URL.
4. Posts metadata to the local Hub before downloading anything.
5. Downloads only items the Hub marks `download` through Chrome's downloads
   API into a managed inbox.
6. Reports the completed Chrome download ID and absolute path.

The Hub accepts a completed path only when its resolved location is underneath
`%USERPROFILE%\Downloads\OMSStudyHub\CanvasInbox`. It waits for the file size
to stabilize, validates the file type, calculates a SHA-256 checksum, and
promotes the source into immutable revision storage.

The extension does not crawl the Canvas Files area, assignments, quizzes,
grades, announcements, discussions, submissions, or external LTI content.

HTTP 401/403 responses, redirects to login, or HTML login responses change the
connection state to `canvas_login_required`. Authentication errors do not retry
continuously. The extension resumes only after the user signs back into Canvas
and runs a successful scan.

## 7. Classification and Matching

### 7.1 Standard courses

Most courses provide:

- A course that identifies the subject.
- A module such as `Exam 1 Lectures` that identifies the exam.
- A page such as `Lecture 1: General CNS Pathology` that identifies the lecture
  number and topic.
- PPT/PPTX lecture attachments and optional PQ attachments one page deeper.

Course ID, exam number, and lecture number must agree with one catalog record.
Topic similarity is supporting evidence and highlights unexpected title drift.

### 7.2 EPC exception

EPC modules are organized by topic and contain readings, rubrics, assignments,
lecture pages, and LockDown Browser items. Lecture numbers and exam labels may
be absent from the module.

For EPC, the matcher uses the configured EPC course plus normalized module,
item, page, and catalog topic titles. A unique strong topic match may continue
automatically and derives its exam and lecture number from the catalog. Missing,
weak, or competing matches require review.

### 7.3 Lecture classification

- Supported lecture source types are `.ppt` and `.pptx`.
- A PowerPoint attachment on a matched lecture page is a lecture candidate.
- Professor PDF copies of lectures are always ignored, including when a
  PowerPoint is temporarily absent.
- Macro-enabled or encrypted Office documents require review and are never
  opened automatically.

### 7.4 Practice-question classification

Only lectures and professor practice questions are collected. Readings,
objectives, rubrics, articles, lab instructions, general handouts, assignments,
and LockDown Browser items are ignored.

A non-lecture document becomes a PQ candidate only when its filename, link
text, page text, or module/item title contains positive evidence such as
`practice question`, `practice qs`, `question set`, `review question`, `case
questions`, or an approved configurable alias. Negative content categories
override weak positive matches. Uncertain documents require review.

PQ files with `.pdf`, `.doc`, `.docx`, `.ppt`, or `.pptx` extensions can process
automatically. Word and PowerPoint documents are converted to PDF. Existing
PDFs are validated and copied. Every other extension enters review rather than
being mislabeled as PDF-ready.

PQ files inherit the matched lecture when page context is strong. Otherwise,
they route at the subject/exam level when the module provides an exam. Items
without a reliable exam destination require review.

## 8. Idempotency and Revisions

The Hub records remote Canvas identifiers and modified timestamps when
available, plus the downloaded checksum. A source signature includes:

- Provider and Canvas course ID.
- Canvas file ID.
- Modified timestamp or remote revision hint.
- Content size.
- SHA-256 checksum after download.

An unchanged signature is skipped. Repeated scans must not create duplicate
sources, artifacts, jobs, or review entries.

Replacement detection is based on the matched lecture and artifact role, not
only the remote file ID. A different checksum proposed as the current lecture
PowerPoint is a replacement even when Canvas assigns it a new file ID.

A first high-confidence lecture source processes automatically. A changed or
re-uploaded lecture source is downloaded, validated, and converted in revision
staging, but the current final PPTX/PDF and iCloud staging file remain unchanged.
The daily review offers:

- **Approve replacement** — archive the prior current artifacts and atomically
  promote the validated revision.
- **Keep current** — retain the proposed revision without promoting it and
  suppress repeated prompts for that exact signature.
- **Remap** — choose a different lecture and recalculate destinations.

No source revision is deleted automatically.

## 9. Data Model

Phase 2 adds focused records rather than expanding external-event payloads:

- `CanvasConnection`: base URL, state, last heartbeat, last successful scan,
  last error, and paired extension identity.
- `CanvasCourseMapping`: Canvas course ID/name/code and catalog subject.
- `CanvasSourceItem`: remote context, classification, matched lecture or exam,
  confidence, evidence, and review state.
- `SourceRevision`: immutable remote signature, checksum, original filename,
  source path, discovery time, and current/proposed/rejected state.
- `Artifact`: role, canonical path, checksum, source revision, validation state,
  and promotion time.
- `ProcessingJob`: bounded action, state, attempt count, timestamps, and concise
  error details.

Full Canvas page bodies are not retained. Stored evidence contains only the
small excerpts needed to explain classification and matching.

## 10. Office Conversion and Validation

The NUC has Microsoft PowerPoint and Word installed. Conversion uses isolated
Office COM adapter boundaries:

- PowerPoint exports matched PPT/PPTX lecture or PQ sources as PDF.
- Word exports DOC/DOCX PQ sources as PDF.
- Existing PQ PDFs bypass Office conversion.

One Office job runs at a time in the signed-in interactive Windows session.
Each adapter starts a distinct Office automation instance, disables expected
alerts, records the process it owns, and applies a bounded timeout. Cleanup may
close only the instance the Hub started; it must never close a pre-existing user
window.

Before Office opens a source, the Hub verifies its extension, signature, size,
and safe staging location. After export, the Hub verifies that the PDF exists,
is nonempty, opens successfully, and has at least one page. Conversion failures
remain staged and enter review. No invalid or partial output reaches a final
folder.

## 11. Naming and Folder Routing

The canonical local root is:

```text
C:\Users\conbr\Documents\OMS II
```

The configured setting stores `%USERPROFILE%\Documents\OMS II` and expands it
for the signed-in Windows user at runtime.

Example local output:

```text
%USERPROFILE%\Documents\OMS II\
  Neuro\
    Exam 1\
      Lectures\
        Lecture 01 - General CNS Pathology.pptx
        Lecture 01 - General CNS Pathology.pdf
      Practice Questions\
        Lecture 01 - General CNS Pathology - Practice Questions.pdf
```

Missing subject, exam, Lectures, and Practice Questions folders are created
automatically. Windows-invalid characters and reserved names are sanitized by
the established naming rules. Multiple PQ sets receive a stable sanitized
descriptive suffix and never overwrite one another.

Immutable originals and prior revisions live outside the study tree:

```text
C:\ProgramData\OMSStudyHub\artifacts\revisions\
```

## 12. iCloud and Goodnotes Staging

Goodnotes iCloud sync uses app data that is not exposed as an ordinary iCloud
Drive folder. Phase 2 therefore uses a normal visible iCloud Drive staging
folder. The setup screen detects likely iCloud Drive roots and requires the user
to confirm one before enabling delivery.

Example:

```text
<iCloud Drive>\OMS II Goodnotes Inbox\
  Neuro\
    Exam 1\
      Lecture 01 - General CNS Pathology.pdf
      Practice Questions\
        Lecture 01 - General CNS Pathology - Practice Questions.pdf
```

Only validated PDFs are staged. Staging is atomic and checksum-verified. The
existing `goodnotes_delivered` checklist step becomes complete with detail
`Staged for import: <path>`; this does not claim that the Goodnotes library has
imported the document.

When an approved lecture replacement supersedes a previously staged file, the
new PDF replaces the iCloud staging copy and the checklist detail changes to
`Updated PDF staged; Goodnotes re-import may be required`.

## 13. Dashboard and Approval Workflow

### 13.1 Canvas Setup

The setup page guides the user through:

1. Extension installation and pairing.
2. Course selection and subject mapping.
3. Local study root confirmation.
4. iCloud staging root confirmation.
5. Discovery-only scan.
6. Representative classification and destination review.
7. Explicit enablement of automatic processing.

### 13.2 Connection status

The dashboard displays:

- Connected and last heartbeat.
- Last successful scan and item counts.
- Scan in progress.
- Canvas login required.
- Extension missing or disconnected.
- Last bounded error with retry or re-pair action.

### 13.3 Daily review

Dedicated queues show:

- Unmatched Canvas items.
- Uncertain lecture/PQ classifications.
- Proposed lecture replacements.
- Conversion or validation failures.
- Missing or conflicting destinations.

Every item includes inspectable evidence, the Canvas source link, proposed
catalog target, proposed canonical paths, and the permitted actions.

## 14. Scheduling and Recovery

The extension alarm owns the 30-minute discovery cadence because it has the
authenticated Canvas session. The Hub owns processing jobs and does not add a
second competing Canvas poller.

The Windows startup script starts Chrome only when no Chrome process exists,
then starts the Hub. The extension resumes alarms after Chrome starts.

Transient Canvas and local network failures use bounded exponential backoff.
Authentication failures pause. A worker crash leaves staged sources and
artifacts intact. On restart, abandoned download or conversion jobs become
queued only when retrying is side-effect safe; otherwise they enter review.

Final promotion uses same-volume atomic replacement where available. If local
or iCloud copy verification fails, the prior final remains current.

## 15. Verification Strategy

### 15.1 Extension tests

- Canvas module and page parsing.
- Standard lecture detection.
- EPC topic-based discovery.
- Attachment classification inputs.
- Restricted host permissions.
- Authentication-page detection.
- Pagination, incremental scan, and revision signatures.
- Hub pairing and disposition contract.

Extension business rules are written as pure JavaScript modules and tested with
Node's built-in test runner. Node is a development dependency only; the unpacked
extension has no runtime build step.

### 15.2 Hub tests

- Course mappings and catalog matching.
- Lecture PPTX versus duplicate professor PDF handling.
- PQ classification and negative categories.
- EPC unique, weak, and competing title matches.
- Duplicate and revised source handling.
- Windows-safe path generation and automatic folder creation.
- Office adapters with fakes, timeouts, and owned-process cleanup.
- PDF validation.
- Atomic local and iCloud staging.
- Approval promotion, rejection, remapping, and rollback.
- Pairing, authentication, schema validation, and path-containment checks.
- Dashboard setup, connection, review, and retry routes.
- Restart recovery and idempotent repeated scans.

### 15.3 NUC rollout

1. Install and pair the unpacked extension.
2. Run discovery-only against Neuro.
3. Verify several lecture pages, a duplicate professor PDF, a PQ file, and an
   ignored document.
4. Enable automatic processing for Neuro.
5. Confirm repeat scans create no duplicates.
6. Expand to the remaining seven courses.
7. Exercise a controlled revision fixture and confirm it waits for approval.
8. Restart the NUC user session and confirm Chrome, Hub, extension heartbeat,
   and safe job recovery.

## 16. Acceptance Criteria

Phase 2 is complete only when:

- The extension scans only the eight configured LMU Canvas courses every 30
  minutes using the active session.
- A new high-confidence lecture PPTX reaches the correct canonical local PPTX,
  local PDF, and iCloud staging path automatically.
- A matched professor PQ reaches the correct local and iCloud Practice
  Questions folders, with DOC/DOCX converted to a validated PDF.
- A professor PDF copy of lecture slides is ignored.
- EPC lecture discovery succeeds by unique topic matching or enters review with
  clear evidence.
- Repeated scans produce no duplicate source, artifact, job, or review records.
- A changed lecture source remains staged until explicitly approved, while the
  prior current version remains intact.
- Expired Canvas authentication pauses scanning and appears clearly on the
  dashboard.
- Every original and prior revision remains recoverable.
- Automated extension and Hub tests, linting, typing, and Phase 1 regression
  tests pass.
