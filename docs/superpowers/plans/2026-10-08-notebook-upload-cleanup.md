# Notebook upload cleanup implementation plan

> Execution authorized by Connor on 2026-10-08: “looks good, go ahead and implement.” Use focused parallel workers and independent review; no additional design approval round.

**Goal:** Complete the approved Hub workflow cleanup and deliver a reusable Clinical Notebook source API with Study Hub as its first client.

**Architecture:** Hub material completion is independent from quiz and notebook delivery. Clinical Notebook owns a versioned, scoped source API; clients own destination mapping and delivery retries. Reuse existing storage, extraction, credentials, and UI patterns.

**Specs:** `../specs/2026-10-08-notebook-upload-cleanup-design.md` and `../specs/2026-10-08-clinical-notebook-integration-api-design.md`.

## Constraints and verification focus

- Preserve independent quiz import/GPT paths and historical artifacts.
- Do not stop, restart, mutate the production database, upload real lectures, or replace the running native app during local implementation.
- Preserve unrelated `Claude outputs/` files. Isolate Clinical Notebook changes from its existing work branch.
- Tests must cover real state transitions, permissions, repeated requests, partial failures, and source identity, not only mocks or source strings.
- New source API tokens never authorize administrative routes; grants constrain metadata, content, file and receipt access.
- No AI-provider credentials or calls are required for local import/read/export.
- Screenshots and end-to-end synthetic checks supplement unit tests; report live acceptance separately.

## Tasks and ownership

- [x] **1. Hub lifecycle and legacy retirement.** Worker owns domain/progress, app wiring, legacy Studio/settings routes/templates/scripts, import worker, process status and related tests. Remove quiz completion dependency and legacy dispatch; retire only unattempted optional pending sources with exact predicates. Test imports and true unfinished jobs remain visible. Avoid ExtendLM and quarantine files.
- [x] **2. Unmatched upload deletion.** Worker owns ingestion repository/service, quarantine routes/template/script, discard routes and related tests. Commit a tombstone before safe staged cleanup; serialize assignment and deletion; preserve shared batches and downstream-linked material. Verify owner/CSRF and race/retry cases. Coordinate shared app wiring with task 1.
- [x] **3. ExtendLM friction.** Worker owns ExtendLM service/routes/template/script and dedicated tests. Persist exam/account-scoped destination, expose one-click lecture send and lecture-only receipts, and bounded automatic indexing checks. Own lecture action markup only; coordinate shared lecture template with root's Clinical action.
- [x] **4. Clinical API contract and implementation.** Root initially owns the isolated Clinical Notebook checkout. Publish one OpenAPI contract for capabilities, authorized notebooks, file/text imports, import receipts, metadata/text/original reads. Add named revocable grants, notebook authorization, safe DTOs and namespaced idempotency. Preserve private native auth. Test with no provider configuration and real isolated storage.
- [x] **5. Clinical Connected Apps and lifecycle.** Implement owner UI to issue/revoke scoped connections, endpoint discovery, and documented local/remote setup. Native app and app-private services remain protected. Verify wide/compact management views and local API connection lifecycle.
- [x] **6. Hub Clinical client.** Implement encrypted configurable connection settings, notebook mapping, immutable source selection and per-file send receipts/retries against the canonical contract. Keep delivery separate from ingestion. Use synthetic inputs and an isolated Clinical runtime for end-to-end proof; preserve actual remote deployment constraints.
- [ ] **7. Integration and final review.** Run affected Python/JS/Swift suites and builds, inspect screenshots, and independently review both diffs for data deletion and auth flaws. Fix and recheck findings. Commit bounded changes and deliver exact evidence, remaining deployment requirements, and rollback-ready source identity.

## Execution ledger

- Baseline: Hub `d6bb4813`; Clinical Notebook `bf38562`. No product changes yet; execution authorized.
- Ruling: execute now after the user's explicit implementation approval; the plan records work rather than introducing another approval round.

- Hub commits: `d63a32e6` guarded unmatched deletion; `463b9389` ExtendLM destinations/receipts; `b4e2e28f` material lifecycle/retirement; `96f7f7d7` ExtendLM review fixes; `3dbe13ee` Clinical client; `3c286e9c` SDK/login removal.
- Clinical isolated branch `codex/source-integration-api`: `83786fa` native Connected Apps/discovery; `c61e1ed` scoped source API/gateway/contract/example. Working checkout: `/Users/connor/Developer/clinical-notebook-source-api`.
- Canonical contract and Hub snapshot match SHA-256 `426845aa6641e9856a7ec9060d5f3bbda493c92e61287d5ad7984d63d735748f`. Chose generated OpenAPI JSON instead of proposed YAML to use the repository's existing Python tooling without a new dependency.
- Clinical final affected backend run: **205 passed**. Native Swift build succeeded; suite reported **348 tests, 34 opt-in skipped, zero failures**. New Connected Apps/discovery/offscreen checks passed.
- Hub focused verification: deletion72 Python/28 JS; legacy lifecycle239 passed then corrected obsolete fixture and51 affected checks passed; SDK cleanup111+2 immutable-release checks passed; final Clinical/ExtendLM50 Python/12 JS; root full JS318 passed before final copy-only check22 passed. Full repository Python survey is being classified separately.
- Independent review found and fixed: ExtendLM paginated cache invalidation; missing selected-role admission; snapshot/revision digest binding; receipt/source identity and pending aggregation; invalid uploads reserving external identities; browser preflight ordering; original descriptors/provenance. A concurrency test also exposed native partial-write visibility, fixed by atomic publication and request coalescing. Fresh review verified 16 concurrent native writers and the 50 Hub regression tests without remaining findings.
- Synthetic actual HTTP evidence: Hub PDF/transcript pair through external-only gateway both readable, hashes matched, repeated send no duplicates. Independent generic Python client used a separate connection. Windows PowerShell client on another computer imported/read/exported a nonmedical file via an SSH loopback forward; original SHA-256 matched. No production data or AI-provider credentials used.
- Screenshot evidence at `/Users/connor/Developer/release-evidence/2026-10-08-notebook-upload-cleanup-ui` and `...-native`; cross-computer evidence at `...-api`. Actual Hub templates checked 1440/430 widths; native settings checked 480/660 points, light/dark.
- Full-suite baseline diagnosis: rehearsal registry stops at schema 40 while existing latest schema is 42 (both unchanged from `d6bb4813`). Focused capsule/export/process run produced 28 failures from that preexisting guard plus 2 from the current Python runtime lacking stdlib `venv`. A process-local diagnostic registry patch made 38 capsule tests pass; no repository or live state was changed by that diagnosis.
- Deployment boundary: source changes, fixtures, builds and tests only. Production Hub/DB, installed native app, real ExtendLM authorization and real uploaded material remain untouched. Permanent connection setup and production installation/restart need the separate rollout step.
