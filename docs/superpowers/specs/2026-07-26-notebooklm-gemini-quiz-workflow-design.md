# NotebookLM Outline and Gemini Quiz Workflow Design

Date: 2026-07-26  
Status: Approved for implementation planning

## Purpose

Extend Study Hub with a button-driven workflow that creates grounded lecture
outlines and interactive Gemini quizzes from the existing lecture artifacts.
The workflow must:

- create one Gemini Notebook (NotebookLM notebook) for each course exam;
- keep all lectures for that exam in the notebook;
- use only the selected lecture's current PDF and cleaned transcript for each
  outline or quiz generation;
- read the outline and quiz prompts from user-managed Obsidian files;
- save each lecture outline as one locally routed PDF;
- run the existing Gemini Quiz Gem and publish its interactive quiz;
- create one master Google Doc per course with one tab per exam;
- put an ordered, linked lecture entry in the appropriate exam tab; and
- expose the current outline and quiz from the individual lecture page.

The first release is deliberately button-driven. Outline and quiz generation
are separate actions and can be rerun independently.

## Existing System Context

Study Hub is a private FastAPI application hosted on a Windows NUC. It already
tracks courses, exams, lectures, immutable source revisions, current derived
artifacts, background ingestion jobs, and validated artifact downloads. It
also has:

- current lecture PDFs converted from PowerPoint;
- current cleaned transcript text files;
- Windows Credential Manager integration;
- a Settings page with established provider-card patterns;
- durable SQLite migrations and repository abstractions;
- per-lecture pages with file cards and pipeline status;
- checksum-validated artifact routes; and
- Cloudflare Access and CSRF protection.

The implementation must extend these patterns rather than introduce a separate
application or visual system.

## Chosen Integration Approach

Use a hybrid Google integration:

1. A shared, persistent Google browser profile on the NUC supplies the signed-in
   sessions used by Gemini Notebook and the Gemini web application.
2. `notebooklm-py` supplies the programmatic notebook, source, chat, and report
   operations.
3. Browser automation invokes the user's existing Gemini Quiz Gem, waits for
   the interactive quiz, enables link sharing, and captures the final URL.
4. The official Google Docs API creates and updates the course documents and
   exam tabs.

This preserves the user's existing Quiz Gem while using the official Docs API
for deterministic document editing. A browser-only implementation was rejected
because Google Docs UI automation would add unnecessary fragility. Recreating
the Gem with the Gemini API was rejected because it would not run the actual
Gem requested by the user.

`notebooklm-py` is unofficial and uses undocumented Google interfaces. It must
be isolated behind an application-owned adapter and pinned to a tested version.

## Google Connection Experience

Settings gains a Google workspace section using the same typography, cards,
buttons, status pills, spacing, and responsive behavior as the existing AI
provider settings.

### Connect Google

The **Connect Google** action starts a guided connection on the NUC:

1. Launch or reuse the dedicated persistent browser profile.
2. Open Google sign-in on the NUC desktop.
3. Let the user complete sign-in locally or through Remote Desktop.
4. Verify access to Gemini Notebook and the Gemini application.
5. Complete Google OAuth consent for Drive and Docs access when required.
6. Verify that all three surfaces resolve to the same Google account.
7. Persist browser state in an owner-only application directory.
8. Store OAuth refresh credentials in Windows Credential Manager.

The settings card shows:

- connected or disconnected state;
- the connected account email;
- Notebook, Gemini, and Docs connection-test results;
- last successful verification time;
- **Reconnect Google** and **Test connection** actions; and
- safe, actionable diagnostics.

If an OAuth desktop-client configuration is required, Settings provides a
clearly labeled upload/control for the Google OAuth client JSON. It is not
described as an API key. The client configuration and resulting tokens must not
be returned to the browser after saving or stored in SQLite.

## Prompt Configuration

Settings stores two non-secret filesystem paths:

- NotebookLM lecture outline prompt;
- NotebookLM lecture quiz prompt.

Both point to the user's existing Obsidian files. Settings displays path
validity and last-modified status and provides a non-generating validation
action.

At the start of each job, Study Hub:

1. resolves and validates the configured path;
2. reads the latest UTF-8 contents;
3. rejects a missing, unreadable, or empty prompt;
4. calculates a SHA-256 fingerprint; and
5. records the path, fingerprint, and modification time with the run.

Editing the Obsidian file therefore affects the next generation without an
application restart. Prompt contents are not written to logs.

## Notebook and Source Organization

Study Hub creates or reuses exactly one Gemini Notebook per course and exam.
The display name follows:

`<Course> · Exam <number>`

For example:

`Clinical Neuroscience · Exam 1`

Each notebook can contain the current lecture PDF and current cleaned
transcript for every lecture in that exam. Practice questions, outlines,
quizzes, and files belonging to another exam are not uploaded by this
workflow.

The derived PDF is preferred over the PowerPoint because Study Hub already
produces and checksum-validates it, it is stable across Office versions, and
NotebookLM supports PDF text and images. The cleaned transcript is uploaded as
text.

Study Hub records:

- notebook ID for the course/exam;
- NotebookLM source ID;
- lecture ID;
- source kind (`lecture_pdf` or `cleaned_transcript`);
- Study Hub revision ID and SHA-256;
- NotebookLM processing state; and
- created and last-verified timestamps.

An unchanged Study Hub revision reuses the existing source mapping. A changed
current revision creates a new mapping and makes the stale mapping ineligible
for generation. Cleanup of obsolete remote sources may occur only after the
replacement source is ready and no active job references the obsolete source.

## Hard Source-Isolation Contract

Every outline and quiz request must include an explicit list containing exactly
two NotebookLM source IDs:

1. the selected lecture's current PDF source; and
2. the selected lecture's current cleaned-transcript source.

The adapter must reject:

- a missing source;
- more or fewer than two sources;
- duplicate source IDs;
- a source mapped to another lecture;
- a source mapped to a stale Study Hub revision; or
- a source that NotebookLM has not finished processing.

No generation path may fall back to all notebook sources. Source selection is
validated both when the job is queued and immediately before the remote prompt
is submitted.

## Lecture Outline Workflow

The lecture page provides **Generate Outline** when both required source
artifacts are current and the Google connection and outline prompt are valid.

The durable job stages are:

1. validate lecture artifacts and prompt;
2. create or resolve the course/exam notebook;
3. upload or resolve the two lecture sources;
4. wait for both sources to become ready;
5. revalidate the exact two-source selection;
6. run the current outline prompt through NotebookLM;
7. render the response as a single PDF;
8. checksum and atomically route the PDF; and
9. promote it as the current lecture-outline artifact.

The canonical user-facing folder is:

`<Course> / Exam <number> / Lecture Outlines`

The filename contains the course, lecture number, topic, and `Lecture Outline`.
Unsafe filename characters use Study Hub's existing normalization rules.

Only one user-facing PDF is created. The run record may retain the remote
response needed for a bounded retry until the PDF is safely promoted, but no
second text artifact is exposed.

A successful lecture page shows **Open Lecture Outline** and **Regenerate
Outline**. Opening the file uses the same path-containment and checksum
validation standards as existing slide and transcript artifacts.

## Lecture Quiz Workflow

The lecture page provides **Generate Quiz** independently of outline status.
An outline is not a prerequisite.

The durable job stages are:

1. validate lecture artifacts and quiz prompt;
2. create or resolve the course/exam notebook;
3. upload or resolve the two lecture sources;
4. wait for both sources to become ready;
5. revalidate the exact two-source selection;
6. run the current quiz prompt through NotebookLM;
7. open the dedicated Google browser profile;
8. invoke the user's existing Gemini Quiz Gem;
9. submit the NotebookLM-produced quiz content;
10. wait for the interactive quiz/Canvas result;
11. enable anyone-with-link access where Gemini permits it;
12. capture and validate the shared quiz URL;
13. synchronize the course Google Doc; and
14. promote the URL as the current lecture quiz.

The Gemini adapter identifies the configured Gem by a stored stable URL or
configured identifier, not by the first similarly named item in the sidebar.
The shared URL must match an allowlisted Google/Gemini host before it is stored.

The lecture page then shows **Take Lecture Quiz** and **Regenerate Quiz**.
**Take Lecture Quiz** opens the current shared URL in a new browser tab.

## Course Google Docs

Study Hub creates or reuses one master Google Doc per course. The document ID is
stored against the normalized course identity rather than rediscovered by title
on every run.

Each exam has a root-level tab titled:

`Exam <number>`

Study Hub uses the Google Docs API tab operations and tab IDs. Within each tab,
the managed content is ordered by lecture number and formatted as:

`Lecture 1: <linked Gemini quiz>`

`Lecture 2: <linked Gemini quiz>`

The user-facing link text may be concise, but the visible prefix remains
`Lecture <number>:`. A rerun replaces that lecture's current URL. It does not
append a duplicate line. Other exam tabs and unmanaged document content are not
modified.

If the document or tab was deleted outside Study Hub, the next synchronization
recreates it and updates the stored mapping. The course document itself is not
automatically made public; it remains under the user's Google sharing control.
The quiz URLs placed in it use anyone-with-link access.

## User Interface

The implementation continues the established Study Hub design:

- IBM Plex Sans and IBM Plex Mono;
- existing page shell, course rail, badges, cards, buttons, progress bars, and
  status pills;
- existing color and spacing tokens;
- existing accessible live-message behavior;
- existing responsive breakpoints; and
- no embedded external Google interface or separate Google-branded theme.

The lecture page adds two file-style cards.

### Lecture Outline Card

- Missing prerequisite: disabled action and concise reason.
- Ready: **Generate Outline**.
- Running: current stage and progress.
- Complete: **Open Lecture Outline** and **Regenerate Outline**.
- Failed: safe diagnostic and **Retry**.

### Lecture Quiz Card

- Missing prerequisite: disabled action and concise reason.
- Ready: **Generate Quiz**.
- Running: NotebookLM, Gemini, sharing, or Docs synchronization stage.
- Complete: **Take Lecture Quiz** and **Regenerate Quiz**.
- Failed: safe diagnostic and stage-aware **Retry**.

Buttons are server-authorized. Browser state alone cannot bypass prerequisite,
source-isolation, or account checks.

## Persistence and Idempotency

New persistence separates remote identity from run history:

- Google connection metadata without secrets;
- prompt path configuration;
- notebook mappings by course/exam;
- source mappings by Study Hub revision;
- course document mappings;
- exam tab mappings;
- generation jobs and stage state;
- outline artifact revisions; and
- quiz revisions and current shared URL.

Creation operations use stored IDs and probe-before-create behavior. Repeated
requests, transient timeouts, page refreshes, and worker restarts must not
create duplicate notebooks, sources, course documents, exam tabs, lecture
entries, or active outputs.

Reruns create a new run record and promote the newest successful result as
current. Prior run metadata remains available for troubleshooting, while the
lecture page and Google Doc expose only the current output.

Only one active outline job and one active quiz job are allowed per lecture.

## Failure Recovery

Jobs persist after every remote side effect and resume from the first
incomplete stage.

- Expired Google browser session: pause and request **Reconnect Google**.
- Missing OAuth access: retain other progress and request Docs reconnection.
- NotebookLM throttling or processing delay: bounded exponential backoff.
- NotebookLM API shape change: fail behind the adapter with a repair-oriented
  diagnostic.
- Missing or changed Gemini Gem: pause without rerunning NotebookLM.
- Gemini quiz completed but sharing failed: retain the quiz and retry sharing.
- Quiz shared but Docs update failed: retain the URL and retry only document
  synchronization.
- Outline response completed but PDF routing failed: retain the response
  temporarily and retry only PDF production.
- NUC or Study Hub restart: resume from persisted stage without duplicating
  completed remote operations.

Failures are visible on the relevant lecture card and in safe diagnostics.
Logs include correlation IDs, job IDs, stage names, and redacted provider
metadata. They exclude browser cookies, OAuth tokens, prompt contents, quiz
contents, and full remote responses.

## Security

- Browser state is stored below the application data directory with
  owner-only permissions.
- OAuth refresh tokens and other reusable credentials are stored in Windows
  Credential Manager.
- SQLite stores only non-secret account status and remote resource IDs.
- All mutation routes remain behind existing Cloudflare Access, origin, CSRF,
  and trusted-host controls.
- Remote URLs are validated against allowlisted HTTPS hosts.
- Local file paths are resolved through existing containment checks.
- Prompt and artifact contents are not exposed through diagnostics.
- The dedicated browser profile is used only for Study Hub Google automation.

## Testing

### Automated

Tests must cover:

- notebook creation and reuse by course/exam;
- source upload reuse by revision checksum;
- stale-source replacement;
- exact two-source enforcement for every generation request;
- rejection of cross-lecture and cross-exam source mappings;
- prompt path validation, UTF-8 loading, and fingerprint recording;
- outline PDF rendering, naming, atomic routing, checksums, and artifact access;
- quiz stage persistence and restart recovery;
- browser automation against controlled page fixtures;
- Gemini Gem selection by configured identity;
- share-link capture, host validation, and anyone-with-link steps;
- Google Doc and exam-tab creation;
- ordered lecture entries and rerun replacement;
- idempotency after ambiguous remote failures;
- bounded retries and stage-specific resume;
- credential and log redaction;
- route authorization and CSRF behavior;
- lecture-page states and established visual classes; and
- regression coverage for existing Study Hub functions.

Remote SDKs, Google APIs, and browser pages are replaced by contract fakes in
the default test suite. A separately marked live test is allowed only against
the user's connected test resources.

### Live NUC Acceptance

Use one selected lecture first:

1. Connect the Google account from Settings.
2. Verify Gemini Notebook, Gemini, and Docs access.
3. Generate and open the outline PDF.
4. Verify the run recorded exactly the selected lecture PDF and transcript.
5. Generate and take the interactive Gemini quiz.
6. Open the quiz in a signed-out browser to verify link access.
7. Verify the course document, exam tab, and linked lecture entry.
8. Rerun both actions and confirm replacement without duplicates.
9. Restart Study Hub during a job and confirm stage-aware recovery.

Then test:

- a second lecture in the same exam, confirming notebook and tab reuse; and
- one lecture in another exam, confirming separate notebook and tab boundaries.

The feature remains on a test branch until these checks pass on the NUC.

## Rollout and Compatibility

- Additive database migrations only.
- Pin the tested `notebooklm-py` version.
- Preserve all existing lecture, upload, transcript, slide, provider, and
  security behavior.
- Publish the implementation to a dedicated `codex/` branch.
- Provide NUC update, dependency installation, Google connection, acceptance,
  and rollback instructions.
- Do not merge into `main` until the user completes live acceptance.

## Explicit Non-Goals

- Automatic generation immediately after transcript or slide processing.
- Batch generation for an entire exam in the first release.
- Replacing the user's Gemini Quiz Gem with a Gemini API recreation.
- Uploading practice questions to the generation source set.
- Making course Google Docs public automatically.
- Editing prompts inside Study Hub.
- Sharing or collaborating on the Gemini Notebooks themselves.

## External Capability References

- `notebooklm-py`: https://github.com/teng-lin/notebooklm-py
- NotebookLM supported sources and source selection:
  https://support.google.com/notebooklm/answer/16215270
- Google Docs tab model:
  https://developers.google.com/workspace/docs/api/how-tos/tabs
- Google Docs request types, including add-document-tab:
  https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request
- Gemini Gem link sharing:
  https://support.google.com/gemini/answer/16504957
- Gemini quiz and Canvas sharing:
  https://support.google.com/gemini/answer/16275879
