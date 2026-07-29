# NUC-Local Anki Curation Design

**Status:** Approved architecture, pending implementation-plan review
**Date:** 2026-07-29
**Repository:** `connor-brooks98/oms-study-automation`
**Replaces:** The Mac/Tailscale boundary in the 2026-07-27 Anki curation design

## Goal

Make the Windows NUC the only machine required to curate OMS II Anki decks.
The NUC runs Study Hub, Anki Desktop, AnkiConnect, the AnKing collection, the
Anki index, media storage, and all curation writes. The Mac remains a personal
study client only: it receives completed changes through its ordinary AnkiWeb
sync and runs no curation, agent, export, indexing, or networking process.

The redesigned workflow must not require Tailscale, a tailnet hostname, a
cross-machine bearer credential, or a macOS LaunchAgent.

## Background and decision

The foundation branch currently implements a narrow macOS agent that accesses
AnkiConnect on the Mac and reaches the NUC-hosted Hub through Tailscale Serve.
It exports snapshots and builds the index but intentionally does not yet apply
curated changes. This boundary introduces a second machine, service lifecycle,
credential, and network dependency without providing a needed user-facing
capability.

Three alternatives were considered:

1. **Direct NUC integration (selected):** Study Hub uses a loopback-only
   AnkiConnect client and launches Anki on the NUC as needed.
2. **Windows companion process:** Port the Mac agent to a second NUC scheduled
   task that talks to Study Hub over localhost. This retains needless process,
   protocol, and lifecycle complexity.
3. **Direct collection-file writes:** Manipulate Anki's database outside Anki.
   This is rejected because it is unsafe with a live Anki process and bypasses
   supported application behavior.

## Operating model

### Machine responsibilities

| Component | Windows NUC | Mac |
|---|---|---|
| Study Hub | Runs as the existing interactive scheduled task | Not installed or run for curation |
| Anki Desktop and AnkiConnect | Runs under the NUC interactive Windows user | Not used by curation |
| AnKing collection and AnkiHub account | Canonical curation collection | Receives normal AnkiWeb updates |
| Snapshot, index, LCL, retrieval, judgment, review, media, and apply work | Runs locally | Does not run |
| AnkiHub and AnkiWeb synchronization | Initiated after a successful apply | Normal personal study sync only |

The NUC must be logged into the same interactive Windows account that owns the
Anki profile. The existing Study Hub task already uses an interactive logon;
this is required because AnkiConnect is hosted by the foreground Anki
application rather than a Windows service.

### Local ports and process lifecycle

Study Hub continues binding to `127.0.0.1:8765`; the existing Cloudflare Tunnel
continues exposing only the dashboard through the established Cloudflare Access
policy. AnkiConnect is configured directly in its NUC add-on configuration as:

```json
{
  "webBindAddress": "127.0.0.1",
  "webBindPort": 8766
}
```

Study Hub accepts only `http://127.0.0.1:8766` as its AnkiConnect endpoint.
No configuration can change it to a LAN, Tailscale, or public address.

Before a local Anki operation, Study Hub first probes AnkiConnect. If it is not
available, a NUC-local launcher starts the configured `Anki.exe` without a
shell. It polls the loopback endpoint until a bounded startup deadline. A
failure leaves the job recoverable with an instruction to sign in to the NUC or
start Anki; it does not attempt a network fallback.

## Component design

### Local Anki integration

The useful code from `oms_anki_agent` moves into `src/oms_hub/anki/`:

| Module | Responsibility |
|---|---|
| `ankiconnect.py` | Strict loopback AnkiConnect v6 client for read, tag, media, note, and sync operations. |
| `runtime.py` | NUC Windows launcher, health check, bounded startup wait, and `doctor` checks. |
| `snapshot_export.py` | Full and delta export of `Anking Step Deck`, using the current deterministic content hashes. |
| `ledger.py` | NUC-local SQLite ledger for snapshot hashes and completed local operations. |
| `apply.py` | Idempotent ordered envelope execution, durable operation receipts, post-sync verification, and safe retry classification. |

The snapshot validation and index modules remain NUC-local and retain their
bounded, atomic staging behavior. Network-oriented names and request payloads
are replaced with local validated models where their validation remains useful.

### Hub and worker integration

The application creates one local Anki runtime alongside the existing ingestion
and study-generation services. The future serialized curation worker owns all
calls to it; browser routes and background tasks never make concurrent direct
AnkiConnect calls.

The worker obtains or refreshes a snapshot before curation, records the snapshot
ID on the job, and passes the accepted action envelope directly to the local
executor after the user approves the review. No command polling or HTTP callback
is involved.

### Removed remote-boundary components

The following code and configuration are removed:

- `src/oms_anki_agent/` and all macOS-only tests;
- `scripts/macos/` and LaunchAgent installation instructions;
- `/agent/v1/*` routes, tailnet-host routing, agent bearer authentication, and
  agent-specific CSRF exceptions;
- `anki_agent_hostname`, `anki_agent_token_key`, request-size, heartbeat, and
  agent polling settings;
- agent heartbeat and command repository APIs and their domain types;
- macOS/Tailscale setup, rotation, and rollback documentation.

The obsolete `anki_agent_state` and `anki_agent_commands` tables are explicitly
dropped in the next additive schema migration. The user confirmed their bridge
state is disposable. The installer continues creating a timestamped `hub.db`
backup before the migration.

## Curation and synchronization flow

1. A user starts or resumes a lecture curation job in Study Hub.
2. The curation worker ensures local Anki is ready and obtains a full snapshot
   or a safe delta refresh of `Anking Step Deck`.
3. Study Hub updates the local search index, builds the lecture concept ledger,
   retrieves and judges candidates, and presents editable gap cards for review.
4. Applying the review creates one immutable envelope with current note hashes.
5. The local executor re-reads selected existing notes and refuses an envelope
   whose target content changed after indexing.
6. It performs the persisted operations in order: store approved media, add the
   owned lecture tags, create generated notes, and invoke Anki's normal sync.
7. The installed AnkiHub add-on must use `auto_sync: "on_ankiweb_sync"`. Its
   synchronization wrapper refreshes AnkiHub/AnKing before the normal AnkiWeb
   sync invoked by AnkiConnect.
8. The executor re-reads the existing tagged notes and generated cards. It
   records success only when the expected tag and generated cards persist after
   the combined synchronization.
9. The Mac obtains the completed collection through its normal AnkiWeb sync.

## Durable state and recovery

Study Hub remains the system of record. Existing curation jobs, snapshots,
index data, artifacts, envelopes, and operation receipts stay beneath the NUC
Study Hub data directory.

Each apply operation has a stable operation ID and content hash. The local
ledger and Hub envelope receipt together prevent duplicate writes across a
process restart. On startup, the curation worker recovers interrupted jobs:

- a pre-apply job returns to review or pending apply;
- an operation with no durable success result is preflighted before retry;
- an already-created generated note is found from its deterministic operation
  marker rather than created again;
- a failed or cancelled AnkiHub/AnkiWeb sync stays retryable and is not reported
  as complete;
- a post-sync mismatch is a failed verification requiring user review and a
  fresh snapshot.

Anki remains loopback-only. Cloudflare protects dashboard access as before, but
neither Cloudflare nor Tailscale can reach AnkiConnect or a separate agent API.

## Configuration and operations

New Hub settings are intentionally narrow:

- `OMS_HUB_ANKI_CONNECT_URL=http://127.0.0.1:8766`
- `OMS_HUB_ANKI_EXECUTABLE_PATH=<absolute path to Anki.exe>`
- `OMS_HUB_ANKI_STARTUP_TIMEOUT_SECONDS=60`
- `OMS_HUB_ANKI_STARTUP_POLL_SECONDS=1`

The settings validator rejects non-loopback hosts, ports other than `8766`,
relative executable paths, and unsupported startup values. `anki_enabled`
remains the feature gate.

The Windows installer verifies the local configuration and adds NUC doctor
checks for AnkiConnect v6, a nonempty `Anking Step Deck`, the generated-note
type and its `Text`/`Extra` fields, and a logged-in AnkiHub profile. The rollout
guide instructs the operator to install Anki, AnkiConnect, and AnkiHub directly
on the NUC and to set AnkiHub auto-sync to `on_ankiweb_sync`.

The old Mac Keychain bearer credential may be deleted manually after the NUC
acceptance run. It is not read, migrated, or required by the redesigned code.

## Testing and acceptance

Automated tests cover:

- NUC-only configuration validation and rejection of network endpoints;
- launcher behavior, bounded wait, and unavailable-Anki recovery;
- full and delta snapshot export, hash validation, and index updates;
- direct local envelope execution, operation idempotency, and restart recovery;
- stale-note refusal, sync failure handling, and post-sync verification;
- absence of `/agent/v1/*`, agent configuration, macOS packaging, and Tailscale
  documentation from the deployment surface.

Live NUC acceptance requires:

1. Start Study Hub under the intended Windows interactive user.
2. Run the NUC Anki doctor successfully with Anki initially closed, confirming
   automatic launch.
3. Export and index `Anking Step Deck` with no Mac or Tailscale connection.
4. Curate one lecture, apply its reviewed result, and confirm the expected tags
   and generated cards in the NUC collection.
5. Confirm the AnkiHub/AnKing update and AnkiWeb sync both complete.
6. Sync the Mac normally through AnkiWeb and confirm the curated deck appears.
7. Restart Study Hub during a controlled pending operation and confirm recovery
   neither duplicates cards nor reports an unverified sync as successful.

## Scope boundaries

This redesign replaces the completed Mac-agent foundation and changes every
remaining Anki curation milestone to target the local executor. It does not
change the approved AnKing source deck, owned tag namespace, generated deck
paths, lecture concept ledger, retrieval, judgment, dedupe, custom-card, image,
or review requirements. Those capabilities remain subject to the detailed
implementation plan that follows this design review.
