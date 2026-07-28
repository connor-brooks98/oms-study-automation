# Google Connection Reliability and Quiz Link Design

**Date:** 2026-07-28

## Goal

Make the existing native Study Hub quiz workflow reliably use the Google
credentials shown on Settings, and make each course Google Doc display a clean
quiz hyperlink instead of a raw URL. At the same time, make NotebookLM sources
readable and revision-safe, prove that each generation uses only its lecture's
two sources, and preserve the structure of NotebookLM lecture outlines in the
saved PDF.

This is a focused follow-up to the native quiz implementation. It does not add
new generation capabilities or replace the Google Docs API.

## Problem

Study Hub currently has two separate Google authorization mechanisms:

- the official Google Docs API uses an installed-app OAuth client and refresh
  token; and
- `notebooklm-py` uses a saved Google browser session because NotebookLM does
  not provide an official public API.

The Settings connection test and the generation worker do not currently test
the same NotebookLM credential path. Settings launches a persistent Chrome
profile and considers NotebookLM connected when the page does not redirect to
Google sign-in. Generation instead opens
`google/notebooklm-storage.json` through
`NotebookLMClient.from_storage()`. Those two credential stores can disagree,
allowing Settings to remain green while generation fails with an expired or
invalid authentication error.

The OAuth client upload has a separate usability problem. The JSON file is
saved under the persistent Study Hub data directory, but the browser cannot
repopulate a file input after a page reload and the page does not otherwise
show that the client is configured. This makes a successfully saved client
look missing.

Finally, Google Docs currently inserts the complete native quiz URL after the
lecture label. The long URL is unnecessary visual noise because Google Docs
can hyperlink readable text.

NotebookLM source uploads currently use internal titles such as
`OMS-1-cleaned_transcript-2ddfc6dcf8905f73`. Those titles expose implementation
details instead of the canonical lecture names already used on disk. The hash
was also serving as an implicit revision identifier, so removing it without a
replacement mapping could cause a later generation to reuse stale content.

The generation adapter already supplies explicit `source_ids` to NotebookLM,
but this isolation is not visible in NotebookLM's browser UI. Opening a
notebook can show all sources checked because that is the website's current
selection state, not the source list sent by the Study Hub API request. The
implementation needs stronger end-to-end tests and durable source bindings so
the worker's actual request, rather than the browser checkbox state, is the
auditable source of truth.

The outline PDF renderer currently strips leading whitespace and escapes
Markdown as literal text. As a result, nested bullets lose indentation and
inline markers such as `**bold**` appear as stars instead of formatting. A line
containing `***` is also treated as a forced page break rather than a visual
divider.

## Decisions

### Continue using the Google Docs API with OAuth

The Google Docs API and OAuth are complementary rather than competing options.
An API key can identify an application accessing public data, but it cannot
authorize Study Hub to create and edit documents owned by the user's Google
account. Study Hub will therefore continue using the official Docs API with an
installed-app OAuth client.

A service account is not used. It would require manually creating and sharing
each course document, or a Google Workspace Shared Drive or administrator
controlled domain-wide delegation. It would also introduce a long-lived
service-account key without removing NotebookLM's separate browser session.

### Keep one primary Connect Google action

Settings retains one **Connect Google** button. It orchestrates both
connections but skips work that is already valid:

1. Confirm that the Google Desktop app OAuth client is saved.
2. Test the existing Google Docs refresh token.
3. Run the Google Docs OAuth consent flow only when the Docs authorization is
   missing or invalid.
4. Run NotebookLM's supported interactive login against the exact storage file
   used by the generation worker.
5. Perform live tests for both services before showing the overall connection
   as connected.

The first connection can therefore show two Google browser interactions. A
later NotebookLM-only expiration does not force the user to repeat Google Docs
consent.

### Use notebooklm-py's supported login and validation

Study Hub will invoke the pinned `notebooklm-py` command-line login from the
current Python environment, without a shell:

```text
notebooklm --storage <app-storage-path> login --browser chrome
```

The command uses the installed system Google Chrome, detects successful
NotebookLM login itself, and writes the exact file subsequently opened by
`NotebookLMClient.from_storage()`. This replaces the custom fixed two-minute
browser wait and generic Playwright `storage_state()` capture.

Study Hub will validate that same file with the library's supported live
authentication check:

```text
notebooklm --storage <app-storage-path> auth check --test --json
```

NotebookLM is connected only when the process succeeds and the JSON reports
both `status: ok` and `checks.token_fetch: true`. Opening the NotebookLM website
without a redirect is not sufficient.

The CLI adapter has bounded timeouts, captures only the small structured
result, and converts output into safe user-facing diagnostics. Cookie values,
OAuth tokens, client secrets, and raw command output never enter HTTP
responses or normal application logs.

## Settings Experience

The Google workspace card continues to show separate NotebookLM and Google Docs
status pills.

It additionally shows an OAuth client configuration state:

- **Client file not saved** before a valid Desktop app client is uploaded; or
- **Client file saved** after `google/oauth-client.json` exists and passes the
  existing structural validation.

The status endpoint includes a secret-safe boolean such as
`oauth_client_configured`; it never includes the file contents, full path,
client ID, or client secret. Uploading a replacement file updates the visible
state immediately. Reloading Settings preserves the **Client file saved**
display.

While connection runs, Settings polls the existing status endpoint and shows
which service is authorizing or being tested. The final result can be:

- both connected;
- Google Docs connected and NotebookLM requiring login;
- NotebookLM connected and Google Docs requiring authorization; or
- both requiring attention.

The message tells the user which action remains without exposing external
service details.

## Live Status and Failure Handling

Persisted status remains useful for rendering Settings quickly, but it is not
treated as proof that a current generation will authenticate.

Before accepting a new outline or quiz generation request, Study Hub performs
a live connection preflight using:

- a refreshable Google Docs credential and user-info request; and
- the NotebookLM token-fetch validation against the worker's storage file.

If either test fails, the job is not queued and the relevant Settings surface
is persisted as needing reconnection.

If authentication expires after queuing and the worker encounters an
authentication error, the worker:

1. pauses the generation job at its durable stage;
2. marks the relevant connection surface as requiring reconnection;
3. keeps the other surface's last independently verified state; and
4. returns an actionable message directing the user to **Connect Google**.

After reconnection, the existing generation retry resumes from its stored
stage. It does not duplicate already uploaded NotebookLM sources or replace a
published quiz unnecessarily.

## Google Docs Quiz Labels

Each exam tab will contain one line per lecture in this form:

```text
Lecture 2 Quiz
```

The entire visible phrase is hyperlinked to the stable native Study Hub quiz
URL. The raw URL is not displayed.

The existing named-range marker remains the idempotency key. On the next sync
for a lecture, Study Hub replaces that lecture's existing raw-URL line with the
new linked label while retaining lecture-number ordering. No short URL,
redirect route, new hostname, or domain purchase is required.

## NotebookLM Source Titles and Revision Bindings

NotebookLM source titles use the stem of each current canonical filed path:

- the lecture PDF uses a title such as
  `Lecture 02 - Pathology of Degenerative and Demyelinating CNS Disease`; and
- its cleaned transcript uses
  `Lecture 02 - Pathology of Degenerative and Demyelinating CNS Disease - Transcript`.

The title does not include `OMS`, a database lecture ID, a source-kind enum, a
fingerprint, or a file extension. The canonical filenames remain the single
source of naming truth.

A durable NotebookLM source binding records, for each exam notebook, lecture,
and source kind:

- NotebookLM notebook ID;
- Study Hub lecture ID;
- source kind;
- current revision ID and SHA-256;
- NotebookLM remote source ID; and
- clean display title.

The existing `notebook_mappings` and `notebook_source_mappings` tables become
the active source registry. The next additive SQLite schema migration adds the
clean display title needed for auditing and title repair. Existing uniqueness
and revision fields retain historical bindings; only one binding per notebook,
lecture, and source kind is kept in the `ready` state. The migration does not
alter or delete existing generation jobs, quiz outputs, outlines, or lecture
files.

On generation, Study Hub lists the notebook's current remote sources and:

1. reuses the bound remote source only when its revision ID and SHA-256 match
   the queued canonical revision and the remote ID still exists;
2. renames a matching bound source when only its display title is outdated;
3. uploads the current canonical file with the clean title when the binding is
   absent, stale, or points to a deleted source;
4. persists the new binding only after NotebookLM reports the upload ready; and
5. removes the superseded source after the replacement and binding are safely
   established.

The first generation after this rollout also recognizes the prior
`OMS-<lecture-id>-<source-kind>-<fingerprint>` title for the selected lecture as
a legacy source. It uploads and binds the canonical replacement before
removing that legacy source. Sources belonging to other lectures are never
renamed or deleted.

This local binding preserves clean NotebookLM titles without losing the
revision identity formerly embedded in the display name. It also prevents
outline and quiz jobs for the same current lecture revision from uploading
duplicate sources.

## Per-Lecture Source Isolation

Every outline and quiz request must use exactly two ready remote source IDs:

1. the current canonical lecture PDF for the selected lecture; and
2. the current canonical cleaned transcript for that same lecture.

The request continues to call NotebookLM chat with an explicit
`source_ids=[pdf_remote_id, transcript_remote_id]`. It never omits
`source_ids`, never passes `None`, and never derives the selection from the
NotebookLM website's checked boxes.

Before the prompt is sent, Study Hub verifies that:

- both bindings match the queued lecture and revision fingerprints;
- both remote IDs still exist in the intended exam notebook;
- both sources are ready;
- the two IDs are distinct; and
- no third source ID is present in the request.

This validation applies identically to outline and quiz generation. Automated
tests will construct an exam notebook containing sources for multiple lectures
and capture the final `chat.ask` call, proving that only the chosen lecture's
PDF and transcript IDs are submitted. The NotebookLM browser may still display
all sources selected when opened manually; that website-only state does not
alter the explicit API request.

## Formatted Lecture Outline PDFs

The PDF renderer will interpret the safe Markdown subset produced by
NotebookLM rather than printing Markdown control characters. It supports:

- headings with distinct size, weight, and spacing;
- bold and italic inline emphasis;
- inline code with a monospace face;
- bulleted and numbered lists;
- nested list indentation derived from the original leading whitespace;
- paragraph spacing and line continuations; and
- horizontal rules rendered as visual dividers rather than page breaks.

Markdown markers used for supported formatting do not appear in extracted PDF
text. All source text is escaped before ReportLab markup is emitted, so
NotebookLM output cannot inject arbitrary ReportLab XML. Unsupported Markdown
is retained as readable plain text rather than silently dropped.

The renderer remains deterministic, creates one validated PDF, uses the
existing canonical lecture-outline filename and folder, and continues adding
page numbers after the first page. It does not use a browser, an online
Markdown renderer, or HTML-to-PDF automation.

## Security and Storage

- `oauth-client.json` remains under the persistent Study Hub data directory.
- The Google Docs refresh token remains in the owner-only operating-system
  secret store.
- NotebookLM browser cookies remain in the app-owned storage file with
  owner-only permissions where the operating system supports them.
- Subprocess execution uses an argument list and never invokes PowerShell,
  `cmd.exe`, or a shell.
- Status payloads expose booleans, service names, account email where already
  authorized, and sanitized diagnostics only.
- Google Docs URLs continue to pass the exact configured Study Hub origin
  validator before insertion.

## Testing

Automated coverage will verify:

- the OAuth client configured state survives a Settings reload without
  exposing secret fields;
- NotebookLM validation uses the exact storage path passed to the generation
  gateway;
- a browser profile that opens NotebookLM cannot produce a connected state
  when token fetch fails;
- the login command uses system Chrome, a bounded timeout, and no shell;
- Connect Google skips valid Google Docs authorization while reconnecting
  NotebookLM;
- generation preflight refuses stale NotebookLM authentication and updates the
  stored surface status;
- a worker authentication failure pauses the job and invalidates the correct
  surface;
- Google Docs inserts `Lecture N Quiz` and hyperlinks the entire label;
- retrying a paused job remains idempotent;
- NotebookLM sources use canonical lecture display titles without internal
  IDs or fingerprints;
- an unchanged revision reuses its bound remote source;
- a changed revision uploads and binds the replacement before deleting the
  superseded source;
- legacy hashed source titles are migrated only for the selected lecture;
- a notebook containing several lectures still sends exactly the selected
  lecture's two remote IDs for both outline and quiz prompts;
- source isolation fails closed when either binding is stale, missing, or
  points to a non-ready remote source;
- outline PDFs render headings, emphasis, nested bullets, numbered lists, and
  horizontal rules without exposing their Markdown markers; and
- all existing native quiz, ingestion, security, and JavaScript tests continue
  to pass.

The NUC acceptance test will:

1. confirm **Client file saved** remains visible after reloading Settings;
2. connect Google and complete any browser prompts;
3. select **Test connection** and confirm both surfaces are connected;
4. restart Study Hub and confirm both live tests still pass;
5. generate one lecture quiz;
6. confirm the job completes instead of failing with stale NotebookLM
   authentication; and
7. confirm NotebookLM shows the canonical PDF and transcript titles;
8. spot-check that the outline contains only material from the selected
   lecture, while the automated isolation test verifies the exact two remote
   IDs submitted;
9. open the lecture-outline PDF and confirm headings, bold text, numbered and
   nested bullets, and dividers match the readable NotebookLM structure; and
10. confirm the course document displays a linked `Lecture N Quiz` label.

## Deferred Work

The following are explicitly outside this rollout:

- a Queue tab;
- cancelling queued or running jobs;
- cancellation checkpoints in ingestion, slide conversion, or transcript
  cleaning;
- short quiz URLs or an additional quiz hostname;
- service-account Google Docs access;
- browser automation for editing Google Docs; and
- changing the Cloudflare Access policy.

The Queue tab should be designed as a separate feature after the quiz workflow
has been loaded and observed on the NUC. Any future cancellation mechanism
must be cooperative and stop only at durable stage boundaries; it must not
terminate an external request or file operation mid-process.

## References

- Google Docs API overview:
  https://developers.google.com/workspace/docs/api/how-tos/overview
- Google Workspace authentication and authorization:
  https://developers.google.com/workspace/guides/auth-overview
- Google Docs API scopes:
  https://developers.google.com/workspace/docs/api/auth
- `notebooklm-py` v0.7.3 CLI reference:
  https://github.com/teng-lin/notebooklm-py/blob/v0.7.3/docs/cli-reference.md
