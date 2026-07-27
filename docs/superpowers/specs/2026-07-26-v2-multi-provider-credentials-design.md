# Study Hub V2 Multi-Provider Credentials Design

Date: 2026-07-26

## Objective

Move API credential setup out of PowerShell and into the Study Hub V2 Settings
page. The dashboard must securely store multiple provider credentials at the
same time and make OpenAI, Google Gemini, and Anthropic Claude fully functional
for transcript cleaning.

The page must provide masked credential entry, a show/hide control for newly
entered text, independent save and connection-test actions, an active transcript
provider selector, and safe diagnostics that distinguish Study Hub failures
from provider failures.

## Repository and Release Baseline

The private `connor-brooks98/oms-study-automation` repository will become the
canonical home for V2. V1 remains recoverable in Git history. V2 work will occur
on a feature branch and will not rewrite or force-push the existing main branch.

The V2 baseline will be reconstructed from the full Study Hub V2 package and
the subsequently deployed CSRF, Windows tracker, and tracker-preview hotfixes.
The deployed NUC location remains:

`C:\Services\oms-study-automation-v2`

The release deliverables will include the updated source, a minimal NUC hotfix,
rollback-safe installation instructions, and a complete V2 source bundle.

## Supported Providers

The first release supports:

- OpenAI
- Google Gemini
- Anthropic Claude

Each provider has a dedicated adapter behind a common `LLMProvider` interface.
The transcript pipeline depends only on that interface. Provider-specific
authentication, endpoints, payloads, response parsing, usage information, and
error codes remain isolated inside the adapters.

This boundary permits another provider to be added without changing upload,
job, transcript, or lecture-progress workflows.

## Credential Storage

Credentials are stored independently in Windows Credential Manager through the
existing `SecretStore` boundary. The existing OpenAI entry
`openai-api-key` remains valid for backward compatibility. Gemini and Anthropic
receive distinct credential entries.

Credentials must never be stored in:

- SQLite
- `.env`
- URLs
- logs
- job records
- rendered HTML
- JSON responses
- browser storage

The server never sends an existing credential back to the browser. A Settings
page reload shows only whether a credential is configured. The password field
is empty.

A blank credential submission leaves the existing credential unchanged. A
non-empty submission replaces only the selected provider's credential. Saving
one provider must not modify any other provider.

## Non-Secret Provider Settings

SQLite stores only non-secret preferences and connection metadata:

- Active transcript provider
- Selected model for each provider
- Last connection-test time
- Last test status
- Sanitized diagnostic category
- Safe provider request identifier, when available

The current OpenAI model setting initializes the OpenAI model preference during
migration. Gemini and Anthropic receive supported defaults. Model selections
can be changed in Settings and take effect for new jobs without restarting
Study Hub.

## Settings Interface

Settings adds an "AI providers" section containing one card for each supported
provider.

Each provider card contains:

- Provider name
- Configured or not configured status
- Model selection
- Password input
- Eye-shaped show/hide control
- Save credential button
- Test connection button
- Last test state and time
- Diagnostic output area

The show/hide control affects only text currently entered in the field. It
cannot reveal a credential already stored in Windows Credential Manager.

The section also contains an active transcript provider selector. Selecting a
provider that has no configured credential is rejected with an actionable
message.

Save and test actions are asynchronous so the rest of the Settings page does
not reload.

### Connection-Test States

The test button and provider card use three states:

- Neutral: `Testing...`
- Green: `Connected`
- Red: `Connection failed`

A test performs a very small real generation request through the selected
provider and model. This verifies the credential, model access, request format,
and response path. It may consume a negligible number of tokens.

A failed test does not delete the credential, switch the active provider, or
change transcript jobs.

## Request and Transcript Flow

1. The user enters a provider credential and submits the provider-specific save
   action.
2. The server validates the request, stores the secret in Windows Credential
   Manager, and returns only configured status.
3. The user selects a model and runs the connection test.
4. The provider adapter performs a minimal real request and normalizes the
   result.
5. Study Hub records only safe test metadata and returns a sanitized result to
   the browser.
6. The user chooses the active transcript provider.
7. Each new transcript job reads the active provider and model at job start.
8. The selected adapter returns the existing normalized cleaned-transcript
   result: text, provider, model, safe request identifier, token usage, and
   estimated cost when calculable.

Changing the active provider affects new jobs. Running jobs keep the provider
and model captured when they started.

## Error Classification and Diagnostics

All adapters normalize failures into these user-facing sources:

- `Study Hub issue`: local configuration, database, keyring, or internal error
- `Network issue`: DNS, TLS, connection, or timeout failure between the NUC and
  the provider
- `Provider authentication issue`: rejected, expired, or unauthorized
  credential
- `Provider model issue`: invalid, unavailable, or unauthorized model
- `Provider quota issue`: rate limit, exhausted quota, or billing restriction
- `Provider service issue`: provider outage or invalid provider response

The diagnostic panel includes:

- Failure source
- Provider
- Selected model
- Safe HTTP status, when available
- Test time
- Study Hub correlation ID
- Provider request ID, when safely available
- Concise suggested next action

The browser receives structured, sanitized diagnostics rather than raw
exceptions or raw provider response bodies.

Server logs include the same correlation ID, diagnostic category, provider,
model, status, and exception information needed for troubleshooting. Logging
must redact authorization headers, credentials, request bodies, transcript
content, and provider response bodies that could contain credential fragments.

## Security Boundaries

All credential, provider-setting, and test endpoints:

- Require the existing Cloudflare Access identity
- Use the existing same-origin request protection
- Accept non-cacheable POST requests
- Apply provider allowlists rather than accepting arbitrary credential names or
  endpoints
- Return `Cache-Control: no-store`
- Never include secrets in redirects or query strings

Credential shape checks may catch obvious paste mistakes but are not treated as
proof that a credential is valid. The real connection test is authoritative.

## Data and Migration Compatibility

The database migration adds provider preferences and safe connection-test
metadata without changing or deleting existing lecture, artifact, upload,
transcript, or usage records.

Existing OpenAI behavior remains available after migration:

- The current `openai-api-key` keyring entry is recognized.
- The configured OpenAI model initializes the stored provider preference.
- OpenAI is initially active when no provider preference exists.
- Existing transcript and usage records remain intact.

New transcript job and usage records identify the provider and model. Existing
records with no provider value are interpreted as OpenAI records for display
and reporting.

## Testing

Automated tests use mocked provider endpoints and must cover:

- Saving and replacing each credential independently
- Blank submissions retaining existing credentials
- One provider save not modifying another provider
- Existing OpenAI credential compatibility
- Secrets absent from HTML, JSON, redirects, logs, and exceptions
- Password show/hide behavior for newly entered values
- Testing, connected, and failed visual states
- Every diagnostic category
- Provider-specific authentication headers and request formats
- Provider-specific success and usage parsing
- Active-provider selection and switching without restart
- Rejection of an unconfigured active provider
- Transcript cleaning through OpenAI, Gemini, and Anthropic
- Running jobs retaining their captured provider and model
- Cloudflare Access and same-origin protections
- Database migration behavior for existing V2 installations

No automated test calls a live provider.

Final NUC acceptance uses:

1. A newly issued credential for each provider.
2. The real Test connection action for each provider.
3. A small sample transcript processed once through each provider.
4. Verification that each result records the correct provider and model.
5. A log and browser-output review confirming that no credential or transcript
   content is exposed.

## Operational Safety

Before installing the hotfix, the release instructions back up the files being
replaced and verify the package checksum. Study Hub is stopped, the update is
applied, and the service is restarted with a bounded health check.

Rollback restores the backed-up files and restarts the prior V2 service. The
database migration is additive so existing application data remains readable by
the updated version.

The plaintext key currently stored in `GPT Key.pdf` should be revoked. The
replacement key should be entered only through the new Settings interface.

## Out of Scope

- Arbitrary user-defined provider endpoints
- Browser retrieval or display of stored credentials
- Automatic credential rotation
- Organization-wide multi-user credential ownership
- Reprocessing completed transcripts when the active provider changes
- NotebookLM or Gemini Notebook automation changes
