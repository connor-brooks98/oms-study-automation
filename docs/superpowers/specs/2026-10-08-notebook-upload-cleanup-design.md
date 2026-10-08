# Upload completion and notebook delivery cleanup

Date: 2026-10-08
Status: proposed design for Connor's review; implementation has not started.

## Intent

Finish Hub uploads when their Hub processing is finished, remove retired NotebookLM generation/connectors, preserve independent quiz builders and imports, simplify ExtendLM delivery, add direct file transfer to Clinical Notebook, and make mistaken unmatched uploads removable.

Notebook delivery is an explicit action separate from uploading and filing. Defaults in this proposal: remember one existing destination notebook per exam; send on the user's click; open Clinical Notebook on the current Mac. Connor was also offered queue-until-open behavior; that preference remains open until answered. No automatic whole-course transfer or automatic quiz/lesson generation is part of this work.

## Verified baseline

Read-only inspection on October 8 found:

- Hub checkout: `codex/review-prep-2026-10-07`, `983f7162`; the preexisting untracked `Claude outputs/` is unrelated.
- Live NUC: `C:\Services\oms-study-automation-v2`, release `0a2d6f526bd4e896bbd10044b38460f1aad9ad34`, tree `4fa01e48187cc8e94a14a8559e175c83985da0f0`, schema 42. Readiness is `ok`; the three existing workers are alive and idle; one listener owns port 8765.
- 96 legacy notebook sources have `needs_review` plus `notebook_upload_unavailable`. Sampled records have zero attempts and say filing succeeded but optional NotebookLM delivery was held. Every row must pass the retirement conditions below; 96 is an observed count, not a hardcoded migration target.
- Legacy source operations are terminal. Ingestion also has real outstanding work: 22 jobs needing review, one quarantined, and five queued. These must remain visible.
- The unmatched quarantined file is `Pulmonary Name That Disease - LMU - draft October 1, 2025 (1).pptx`; its lecture assignment is null. This design does not itself delete that file.
- Clinical Notebook source: `/Users/connor/Documents/Codex/2026-09-24/i/work/notebook-app`, clean `codex/background-imports` at `bf38562`. The selected Codex project folder contains evidence rather than the application source.
- Clinical Notebook already has a manual slide-mirror importer and a proposed text-only transfer schema. Neither is a working Hub-to-app file bridge. Its native API uses an ephemeral loopback port and a runtime-owned bearer, so the Windows Hub cannot call it as a stable remote service.

## 1. Hub completion and retirement of old NotebookLM work

Materials progress comprises the existing eleven intake steps, without `QUIZ_PUBLISHED`. Quiz availability remains independent and is still shown where useful. This changes the material completion calculation, not individual ingestion failures, missing material requirements, or quiz history.

Remove the ingestion `LectureNotebookSync` hook. Remove legacy connect/test UI and disallow old connect, source-add, and generation entry points before they can perform provider/auth activity. Stop legacy remote worker dispatch while retaining local import and GPT paths. Remove the legacy NotebookLM source/generation tab from Quiz Builder and make import the default. Keep file, pasted-text, and URL imports, including sources such as Dr Wang's blog; keep manual review, images, publication, and existing GPT builder behavior.

Imports without supplied answers must remain private and visibly require review. They must not upload references to an obsolete backend in an attempt to fill the answers. Keep shared encrypted storage because ExtendLM uses it. Preserve existing notebooks, received sources, generated artifacts, and published quizzes.

Retire abandoned optional queue records with an explicit terminal skipped/retired state and reason. Apply this only when the record is a legacy notebook source with the known unavailable diagnostic, zero attempts, no active claim/lease, no remote source identifier, and no associated source operation. Recheck all predicates transactionally. Preserve the previous state/reason and a retirement timestamp for audit. Do not mark these records uploaded, attached, indexed, or successfully generated. Keep a history view; exclude retired records from ongoing-work totals. Ambiguous or previously attempted remote work remains visible for reconciliation.

Primary implementation seams: `domain.py`, `progress.py`, `web/routes.py`, `app.py`, `study_generation/studio_repository.py`, `study_generation/studio_worker.py`, `study_generation/quiz_import_worker.py`, `processes.py`, legacy generation/settings routes, and Quiz Builder templates/scripts.

## 2. ExtendLM after one-time setup

Settings retains ExtendLM sign-in and explicit browser/account selection. Save an existing notebook destination by stable exam identity inside the selected account/grant context. Never choose by notebook title alone. Account changes invalidate the destination; expired authorization gives a reconnect action without silently selecting another account.

On a lecture with a destination, show `Send to [notebook name]` and `Change destination` directly beside its material actions. First use opens the notebook selector, then saves the choice for that exam. Reuse the existing validated PDF/cleaned-transcript resolver, immutable job destination, per-file receipts, and retry keys. A repeated click on unchanged material reuses the existing delivery rather than adding duplicate sources.

Show the selected account with the notebook. A deleted/inaccessible notebook requires a new explicit choice. Concurrent destination changes affect future sends only; already created jobs retain their original destination and source revisions. Reauthorization must not silently resurrect mappings under a different account.

Automatically check indexing for a bounded period after acceptance while the page remains open. Stop on ready/failure; if indexing takes longer, retain its truthful status and provide Refresh. Never resend merely because indexing is pending. Filter the displayed receipts to the current lecture. After OAuth, load connection choices automatically, but retain explicit account selection when it is not already valid.

The browser extension remains a provider prerequisite. This change removes repeated Hub clicks; it does not turn ExtendLM into a headless provider. Reuse the existing MCP tools and scopes.

Primary implementation seams: `extendlm_service.py`, `web/extendlm_routes.py`, `web/static/extendlm.js`, `web/templates/extendlm.html`, and the lecture action area. Preserve the path-scoped cookie by keeping API calls under `/settings/extendlm`.

## 3. Direct Clinical Notebook transfer

### Recommended transport

Use an immutable export prepared by the Hub and fetched by the paired Mac app. Pair a device once with a narrowly scoped device credential, held in macOS Keychain and distinct from ExtendLM, Anki, and the native backend bearer. Restrict bridge routes to the intended private/Tailscale transport and its own authorization scope. Revoke a device from Hub Settings.

The Hub action creates a durable transfer for the chosen exam notebook, then opens a registered Clinical Notebook URL carrying only an opaque transfer ID. The app authenticates independently and fetches the transfer from its configured Hub. The URL must not contain reusable credentials or arbitrary fetch URLs. The native app then calls its own local API through its existing supervisor-owned authentication. No new public listener on the Mac is required.

During initial pairing, select the existing Clinical Notebook destination for the Hub exam and save its stable notebook ID under the paired device. Do not automatically create notebooks or match them by title. A deleted or inaccessible destination produces an actionable destination error. The device grant is limited to that owner's targeted transfers and their receipt/destination metadata; it grants no Anki, arbitrary filesystem, or unrelated Hub access. Revocation stops subsequent fetches.

When the app is unavailable, retain the pending transfer and offer Open/Resume. On startup, the paired app may recover explicitly queued transfers for that device. Do not add continuous background library synchronization. If Connor chooses queue-until-open, omit the launch step. Sending from a phone or another computer must say which paired Mac will receive it; a custom URL alone must not imply cross-device delivery.

Pairing needs a short-lived one-use code issued from an authenticated, CSRF-protected Hub action. Exchange and retrieval must use a reviewed private bridge route rather than reusing the Anki agent bearer or relaxing public Cloudflare Access checks. Confirm the actual Mac-to-NUC private route during implementation; browser Cloudflare cookies cannot simply be reused by the native client. If that route is unavailable, show a connection requirement rather than silently weakening authentication.

### Transfer contract

Define a versioned file-transfer contract before implementing either side. Keep the existing proposed text-only `study-hub-send-v1.schema.json` unchanged; actual file attachments require a new contract. One canonical schema plus fixed contract fixtures must be checked by both repositories.

The new contract carries transfer ID, originating Hub identity, stable course/exam/lecture IDs and display names, device and notebook identity, and a per-file manifest: stable document ID, role (`slides` or `transcript`), material revision, original filename, MIME type, byte length, and SHA-256. Use validated slide PDF and cleaned transcript files from current approved Hub material. Preserve the PDF itself for figures/page citation and the cleaned transcript content; do not send merely a text summary. Carry existing cleanup provenance when available without inventing timestamps or page-level verification.

Only selected available material is sent. A slides-only lecture may be imported as slides-only with the missing transcript explicit; it must not pretend to satisfy the older paired-source lesson contract. No quiz content, quarantined files, draft revisions, arbitrary local paths, credentials, or executable flags enter a transfer. Hash and revalidate the exact exported bytes; source changes during preparation must fail cleanly rather than mix revisions.

Use independent bounded binary file downloads, bounded by the receiver's file size/type limits; validate actual bytes as well as names and MIME declarations. Do not reuse the text-only contract's draft-character budget as a file-size or import limit. Snapshot the selected immutable material so a later lecture edit cannot change an already queued transfer.

Identical transfer retries return the same source IDs. A reused key with different content is a conflict. Changed document revisions require an explicit replacement preview and expected source revision before replacing an existing source. Preserve source provenance and history. Deleting an upload in the Hub never deletes a Clinical Notebook or NotebookLM source implicitly.

Persist receipts per file and retain the distinction between queued, transferring, accepted, reading/indexing, ready, partially failed, and failed. Accepting bytes is not readiness. Only report the whole transfer ready when every requested file is ready. Reuse Clinical Notebook's existing background extraction and source-readiness machinery. A changed source keeps the existing lesson-staleness rules. Transfer never triggers a model call to generate a quiz/lesson or grants automatic adoption.

Retry only missing/failed files after partial success. An expired or unavailable export must be reported explicitly and require preparing a new transfer; never substitute a newer lecture revision under the original transfer ID. Receipt updates are themselves replay safe so interruption after receiver persistence cannot duplicate imported sources.

### Why this approach

Direct NUC-to-Mac push would require exposing and managing a stable authenticated Mac listener even when the app is closed. Extending the existing mirror scan would still omit cleaned transcripts and transfer receipts. A paired outbound pull uses the existing desktop lifecycle, supports exact file identities, and keeps one-click sending possible.

Clinical Notebook implementation seams: `api/routers/study_hub.py`, a separate transfer service, native source processing/persistence, `desktop/GPTLM` app URL handling and existing API client, packaging URL registration, and Keychain-backed pairing. Keep manual mirror import available. Record the new boundary in that repo's decision log.

## 4. Delete mistaken unmatched uploads

Add `Delete upload` next to Assign on eligible Quarantine rows and the corresponding upload detail. Confirmation names the file and says only that uploaded copy will be removed. Cancellation changes nothing.

Eligibility for this new action: quarantined/unmatched, no lecture assignment, no ingestion job, and no study revision or other downstream material reference. A processing failure with linked material instead links to its existing lecture recovery/removal workflow. Do not broaden every quarantined state into deletable material.

Transactionally record a durable discarded tombstone before idempotent staged-file cleanup. Use database serialization and state preconditions shared with assignment/confirmation, so a stale Assign action cannot resurrect deletion and deletion cannot win after processing begins. Refuse unsafe paths and shared-path references. Delete only the individual file within the staging root, never its shared batch directory. Missing files are successful retries; cleanup failures remain visible/retryable. Synchronize the batch state, retaining siblings and audit metadata.

If physical cleanup fails, say `Removed from uploads; file cleanup pending` and retain a retry path. A vanished quarantine row alone is not proof that its bytes were deleted.

Use private-owner authorization and the established CSRF protections. Preserve existing transcript confirmation behavior and processed material recovery semantics.

Primary implementation seams: `ingestion/service.py`, `ingestion/repository.py`, `web/quarantine_routes.py`, `web/templates/quarantine.html`, and upload detail controls.

## Acceptance

- Materials become complete with a waiting/failed/absent quiz; true missing or failed ingestion remains incomplete.
- Filing no longer creates legacy NotebookLM work. Obsolete routes and workers make zero legacy connector calls.
- Only predicate-verified abandoned optional rows retire. All real ingestion work and ambiguous remote history remain visible.
- File, text, and URL quiz imports still reach review/publication; missing answers stay unresolved/private without NotebookLM calls.
- ExtendLM remembers the exam notebook, invalidates it on account changes, reuses repeated sends, isolates lecture history, and reports indexing truthfully.
- Quarantine deletion preserves shared-batch siblings and all filed material; assignment/deletion races, missing files, path traversal/shared references, cleanup failures, CSRF, and owner isolation are exercised.
- Both bridge implementations pass the same contract fixtures, including mismatched hashes, wrong role/type/size, unknown device/notebook, replay/conflicting payloads, changed source revision, and interrupted/partial transfers.
- A synthetic PDF and transcript make a full Hub-to-native-app trip with stable source IDs on retry and actual receiver readiness. Local mocks alone do not establish cross-computer acceptance.
- Inspect relevant Hub pages at wide/compact widths and native import states in an isolated fixture. Record screenshots for action visibility, progress, failure and confirmation states.
- Run affected Python and JavaScript suites plus existing Clinical Notebook import/runtime tests; retain independent final review and distinguish local verification from deployed/provider acceptance.

## Delivery and future scope

Implement the Hub cleanup and Clinical Notebook bridge as independently reviewable changes with a shared contract. Do not mix source changes into the unrelated preexisting files or replace Clinical Notebook's accepted runtime work.

Deployment/restart is a separate final step after verification, with the current live revision rechecked and rollback prepared. This design review does not authorize taking the running Hub offline, pairing accounts, sending real lecture files, or deleting the identified live upload. Those actions should be made concrete and scoped when the candidate is ready.

Clinical Notebook eventually replacing NotebookLM is recorded in `docs/future-implementation-ideas.md`. Full migration, remote-source deletion, chatbot migration, and removal of ExtendLM itself are future decisions, not this implementation.
