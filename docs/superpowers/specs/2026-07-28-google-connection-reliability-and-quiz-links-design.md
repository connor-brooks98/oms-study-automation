# Google Connection Reliability and Quiz Link Design

**Date:** 2026-07-28

## Goal

Make the existing native Study Hub quiz workflow reliably use the Google
credentials shown on Settings, and make each course Google Doc display a clean
quiz hyperlink instead of a raw URL.

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
- retrying a paused job remains idempotent; and
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
7. confirm the course document displays a linked `Lecture N Quiz` label.

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
