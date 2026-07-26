# Transcript Duplicate Cost Gate and Download Design

**Date:** 2026-07-26  
**Status:** Approved interaction design

## Goal

Prevent an unintended second LLM charge when a newly uploaded transcript is
matched to a lecture that already has a cleaned transcript. Also let the user
download an existing cleaned transcript as a descriptively named UTF-8 `.txt`
file from its review page.

## Scope

This change covers transcript uploads and cleaned-transcript downloads only.
Slide ingestion, transcript matching rules, replacement approval, LLM provider
selection, and cleaned-transcript content are unchanged.

## Duplicate Classification

After staging and lecture matching, but before creating a processing job, the
server classifies a transcript upload as one of three cases:

1. **New lecture transcript:** The matched lecture has no current cleaned
   transcript. Queue processing normally.
2. **Exact duplicate:** A current transcript revision for the lecture has the
   same source SHA-256. Do not queue processing and do not call an LLM. Mark the
   upload complete and tell the browser that the transcript was already
   processed.
3. **Different transcript for an already-cleaned lecture:** A current cleaned
   transcript exists for the lecture, but its source SHA-256 differs. Pause the
   upload before creating a processing job and require explicit confirmation.

Only the third case displays the cost warning. This preserves the existing
cost-free idempotency of exact duplicate uploads without asking an unnecessary
question.

## Paused Upload State

Add explicit upload states for `awaiting_confirmation` and `discarded`.

An upload in `awaiting_confirmation`:

- has a successfully detected lecture;
- has no processing job;
- cannot be claimed by the worker;
- remains visible in its upload batch;
- exposes only safe lecture display metadata to the browser.

The batch-status response supplies the warning with:

- course/subject;
- lecture number;
- lecture topic;
- upload item identifier.

Batch aggregation treats `awaiting_confirmation` as actionable and
`discarded` as terminal. Other non-duplicate files in the same batch may
continue processing. Duplicate prompts are handled one upload at a time.

## Warning Interaction

The upload page displays a modal dialog when a batch contains an item awaiting
confirmation:

> A cleaned lecture transcript has already been processed for this lecture.
> Are you sure you want to process another?
>
> **COURSE · Lecture ## · LECTURE TOPIC**

The dialog has two actions:

- **Process anyway:** Submit an authenticated, CSRF-protected request that
  atomically verifies the item is still awaiting confirmation and queues
  exactly one processing job. Polling then resumes.
- **Discard upload:** Submit an authenticated, CSRF-protected request that
  verifies the item state, deletes only its staged file within the configured
  staging root, and marks the database record `discarded`. No processing job,
  revision, or LLM request is created.

The existing current cleaned transcript is never modified by discarding the
new upload. If safe deletion fails, the server returns a conflict response and
keeps the upload paused so the interface cannot claim it was discarded.

Both actions are idempotent against repeat clicks. A second request returns the
current safe state without creating another job or deleting any unrelated
file.

The exact-duplicate case does not open the dialog. Its upload row displays a
clear message such as “This transcript has already been processed. No API
request was made.”

## Server Enforcement

The cost gate is authoritative on the server. Browser code only presents the
server's decision and sends the selected action. A refresh, multiple browser
tabs, a direct request, or a stale modal cannot bypass the paused state or
enqueue multiple jobs.

Manual lecture assignment from Quarantine runs through the same classification
before enqueueing, so manually matched transcripts receive identical
protection.

## Cleaned Transcript Download

The cleaned-transcript review page retains its existing readable transcript
view and adds a prominent **Download transcript** button.

The button targets a dedicated download endpoint for the same revision and
cleaned artifact. The endpoint:

1. resolves the requested revision and confirms it is a cleaned transcript;
2. applies the existing artifact path containment and SHA-256 validation;
3. confirms the file is readable UTF-8;
4. obtains the lecture subject, lecture number, and topic from the catalog;
5. returns the exact saved bytes as a `text/plain` attachment.

The attachment filename is:

`COURSE - Lecture ## - TOPIC - Transcript.txt`

The course, lecture number, and topic are normalized with the Hub's existing
safe filename rules. Lecture numbers use the existing zero-padded format.
Starlette's file-response filename handling supplies a safe
`Content-Disposition` header.

Downloading never invokes an LLM, creates a transcript revision, or records API
usage. The response remains private and non-cacheable. A missing, unreadable,
out-of-root, or checksum-mismatched file produces a clear `404` or `409`
response instead of returning unverified content.

## Interface and Accessibility

The confirmation dialog:

- uses the native dialog pattern or an equivalent accessible modal;
- moves keyboard focus into the dialog when opened;
- labels both choices explicitly;
- prevents accidental dismissal with Escape or backdrop clicks because an
  explicit processing/discard decision is required;
- disables both controls while an action request is in flight;
- reports request failures without closing the dialog.

The download button is a standard link styled consistently with the existing
button system, so it works without client-side file construction.

## Testing

Automated coverage will verify:

- a transcript for a lecture with no current transcript queues normally;
- an exact SHA-256 duplicate completes without a job or LLM call;
- a different transcript for an already-cleaned lecture pauses without a job;
- manual assignment uses the same duplicate classification;
- confirmation creates exactly one job despite repeated requests;
- discard deletes only the staged file, records `discarded`, and creates no
  job;
- an unsafe or failed deletion leaves the upload paused;
- batch JSON contains the safe lecture warning metadata;
- the upload interface renders the detected lecture and sends CSRF-protected
  confirmation/discard requests;
- the cleaned review page exposes the download button;
- download returns the exact validated bytes with the descriptive `.txt`
  filename;
- missing or checksum-mismatched artifacts are not downloaded;
- the full Python and JavaScript regression suites remain green.

## Deployment Compatibility

No new secret, environment variable, or external service is required. The new
upload states are stored in existing string state columns, so no schema
migration is required. The change ships on the existing
`codex/v2-multi-provider-settings` branch for NUC testing before any decision
to merge into `main`.
