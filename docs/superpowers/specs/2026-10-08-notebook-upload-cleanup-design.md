# Upload completion and notebook delivery cleanup

Date: 2026-10-08
Status: proposed design for Connor's review; implementation has not started.

## Intent

Finish Hub uploads when their Hub processing is finished, remove retired NotebookLM generation/connectors, preserve independent quiz builders and imports, simplify ExtendLM delivery, add a reusable Clinical Notebook source API and direct Hub transfer, and make mistaken unmatched uploads removable.

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

## 3. Clinical Notebook source API, with Study Hub as its first client

**October 8 clarification:** Clinical Notebook must accept source imports and expose source reads/exports to other applications. The receiver must be reusable by other people who install the app. This supersedes the earlier proposal for a receiver specialized to Study Hub and its tailnet.

The detailed [Clinical Notebook external source API design](2026-10-08-clinical-notebook-integration-api-design.md) owns the generic API boundary. Build a versioned, documented REST interface backed by existing Clinical Notebook services: list granted notebooks, import file/text sources, check imports, read source metadata/extracted text, and download originals. Clinical Notebook issues named, revocable per-application credentials with explicit notebook/capability grants. Import-only and read-only access are distinct. Keep the native administrative bearer private.

General source identity, content revision, byte/hash validation, readiness, provenance, pagination, and replay behavior belong to the shared API. Course/exam/lecture mapping, choosing the current approved PDF/cleaned transcript, and the one-click send workflow belong to Study Hub. Hub-specific metadata is optional and namespaced; generic imports do not require lecture identities or slide/transcript pairs. A second unrelated client must pass the same contract tests without Hub metadata.

Use an OpenAPI contract owned by Clinical Notebook, proposed path `docs/contracts/source-api-v1.openapi.yaml`. Retain the old text-only `study-hub-send-v1.schema.json` as a historical proposal, not as the basis for a supposedly general interface. No application-specific receiver bypass is allowed. Initial external writes create/import sources; a changed existing external document returns a revision conflict for explicit owner review, rather than silently modifying a shared source. Source deletion or replacement does not automatically propagate between apps.

Source transfer and retrieval work without a NotebookLM or AI-provider key. Clinical Notebook access tokens are local integration credentials. Model-assisted extraction/indexing remains an explicitly approved processing policy, with stored/readable/indexed states reported separately. No transfer generates or adopts a lesson/quiz.

### Hub workflow and connection

After one-time connection, remember an existing Clinical Notebook destination for the exam and show `Send to Clinical Notebook`. Send the actual current approved PDF and cleaned transcript, or only the available materials the user selected. Pin the exact bytes/revisions in an immutable export. Maintain per-file receipts and retry only failed/missing transfers; repeated sends of unchanged materials reuse their source IDs. Changed material requires explicit review. Never call partial delivery fully ready.

Use the generic API over the installation's configured reachable connection. Desktop defaults remain local while the app is running; remote/self-hosted access is explicitly configured and authenticated. Tailscale can carry this installation's connection but is not a requirement of the source API or distributed app. Do not expose the entire internal backend or reuse Anki credentials. A supported remote path must be verified before cross-computer delivery is accepted.

If the Mac cannot accept inbound requests, a separate configured outbound adapter can fetch the Hub's immutable export and pass it through the same source import boundary as a named integration, subject to its current notebook/capability grants and revocation. It must not use the desktop administrative identity to bypass those grants. Keep that adapter outside the generic source model. A custom URL may open the app with an opaque transfer ID; never put credentials or arbitrary fetch URLs in it. Pending transfers belong to the sending client and remain visible until the receiver is available. No silent full-library synchronization is introduced.

Opening the Mac app on Send remains the proposed default, with queue-until-open as the alternative already offered. This UI preference does not determine the core API architecture. Explicitly show which installation/notebook receives the material. The receiver's application lifecycle and remote reachability must not be hidden behind a successful browser launch.

Primary Clinical Notebook seams: a new versioned external router and public response models, scoped integration authorization and Connected Apps settings, existing source/native-import/readiness services, and desktop integration endpoint discovery. Keep the manual slide-mirror importer available. Add transport-specific wakeup/pull support only as required by the chosen Hub connection. The exact connection must preserve existing Cloudflare/private host access controls rather than assuming browser cookies are available to a native client.

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
- Clinical Notebook, Study Hub, and an unrelated sample client pass the same external API contract fixtures, including mismatched hashes, wrong type/size, grants and revocation, inaccessible notebooks, replay/conflicting payloads, changed source revision, and interrupted/partial transfers.
- A fresh installation with no Hub configuration or AI-provider keys can issue separate import-only/read-only connections, import a generic document, read its available text, and export an original file within its grants. External reads neither reveal local paths nor change Recently Viewed.
- A synthetic PDF and transcript make a full Hub-to-native-app trip with stable source IDs on retry and actual receiver readiness. Local mocks alone do not establish cross-computer acceptance.
- Inspect relevant Hub pages at wide/compact widths and native import states in an isolated fixture. Record screenshots for action visibility, progress, failure and confirmation states.
- Run affected Python and JavaScript suites plus existing Clinical Notebook import/runtime tests; retain independent final review and distinguish local verification from deployed/provider acceptance.

## Delivery and future scope

Implement the Hub cleanup, reusable Clinical Notebook source API, and Study Hub client adapter as independently reviewable changes with one shared API contract. Do not mix source changes into the unrelated preexisting files or replace Clinical Notebook's accepted runtime work.

Deployment/restart is a separate final step after verification, with the current live revision rechecked and rollback prepared. This design review does not authorize taking the running Hub offline, pairing accounts, sending real lecture files, or deleting the identified live upload. Those actions should be made concrete and scoped when the candidate is ready.

Clinical Notebook eventually replacing NotebookLM is recorded in `docs/future-implementation-ideas.md`. Full migration, public app distribution, multi-user hosting, remote-source deletion, chatbot migration, and removal of ExtendLM itself are future decisions, not this implementation. The reusable external API and non-personal connection setup are part of the current design.
