# Notebook delivery cleanup: acceptance and rollout boundary

October 8, 2026. User-approved implementation in `codex/notebook-upload-cleanup` (Study Hub) and `codex/source-integration-api` (Clinical Notebook). Clinical Notebook checkout: `/Users/connor/Developer/clinical-notebook-source-api`, commit `c61e1ed32b2adc151bfd5c8966b15f458feb6ee4`.

## Delivered behavior

- Material intake completes independently of quizzes and notebook delivery. Unattempted abandoned legacy syncs are retired as skipped with history retained; attempted/active work is preserved.
- Retired NotebookLM SDK/login/generation is removed. GPT quiz generation and file/text/URL practice-question imports remain available.
- ExtendLM remembers an explicitly selected account/notebook for the exam and offers one-click lecture delivery with per-file receipts and bounded readiness checks.
- Clinical Notebook offers notebook-scoped, revocable source API connections for independent applications. Import, receipt, metadata, extracted text and original-file export routes share a versioned contract. Study Hub is one client.
- Unmatched staged uploads can be deleted from Quarantine with safeguards for shared batches, assignment races, path containment and retryable file cleanup.

See [the user/operator guide](notebook-delivery.md) and [the consumer contract](../contracts/clinical-notebook-sources-v1.md).

## Verification

The affected Hub acceptance matrix passes 189 Python tests; its JavaScript suite passes 318 tests. Clinical Notebook passes 205 affected backend tests. Native Swift build succeeds, and the suite reports 348 tests with 34 opt-in skips and zero failures. Independent review findings were resolved and checked again, including concurrent import recovery and atomic original-file publication.

Actual Hub templates were checked at 1440 and 430 pixel widths; Connected Apps was checked at 480 and 660 points, light and dark. Synthetic actual HTTP checks sent a PDF/transcript pair through the external-only gateway, verified readable content and original checksums, and confirmed repeat sends reused receipts. An independent Python client used another grant. A Windows PowerShell client on another computer imported, read and exported a generic nonmedical document through a private SSH forward with a matching original checksum. These checks used disposable fixtures, not production library data or AI-provider credentials.

The initial whole-repository Python survey reported 4,057 passed, 79 failed and 12 skipped. After installing the declared document-processing extra, all 37 format-related failures passed. Fresh reruns also resolved seven obsolete fixture/assertion failures affected by this change (four UI, one ingestion fixture and two release-package expectations). The remaining 35 failures concern existing baseline or local-runtime issues:

| Count | Cause |
|---|---|
| 28 | Existing Anki rehearsal path registry supports schema 40 while the existing database schema is 42; relevant source is unchanged from baseline. |
| 2 | This local Python runtime lacks the standard-library `venv` module used by launcher tests. |
| 3 | UI baseline already has a different stylesheet hash/version and lacks the asserted pipeline card. |
| 2 | Existing schema-version and unconfigured-model error-code assertions. |

This is a classified survey with focused reruns, not a claim that a second full-repository run passed. No unrelated baseline repair is included in this release.

Evidence is retained under `/Users/connor/Developer/release-evidence/2026-10-08-notebook-upload-cleanup-{release,ui,native,api}`. The release directory holds immutable Hub archives, checksums, manifest verification and test logs. The UI/API directories hold replayable synthetic checks and screenshots. The canonical contract and Hub snapshot both have SHA-256 `426845aa6641e9856a7ec9060d5f3bbda493c92e61287d5ad7984d63d735748f`.

## Separate production rollout

The running Hub, production database, installed Clinical Notebook app, real ExtendLM connection and real uploaded files have not been changed. Temporary fixture servers and the temporary SSH forward have been stopped. The mistaken quarantined upload has not been deleted on the user's behalf.

Before installation, refresh the running process/source identity, health, worker state and active jobs. Back up the installed source and database plus the Clinical Notebook database, uploads and integration grant store; verify the backup and identify the rollback build. Install the reviewed source with its declared dependencies, apply the application's normal upgrade/startup path, and check health and runtime source identity. A clean release installation must not retain removed legacy login modules from an older source overlay.

Then create a named Clinical Notebook connection, explicitly select its notebooks/scopes, configure a durable reachable gateway or private tunnel, and save that address/token in Hub Settings. Check one explicitly selected real delivery and the ongoing-process statuses after startup. Validate the browser ExtendLM path separately with the user's real authorization. Quarantine deletion remains an explicit user action against the displayed filename.

A Hub restart/replacement requires current-request permission under the standing instruction against taking the main Hub offline. This document records the concrete candidate and verification; it is not deployment authorization.
