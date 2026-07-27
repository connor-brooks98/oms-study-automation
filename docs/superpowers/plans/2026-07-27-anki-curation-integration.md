# Anki Curation Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, interactive, single-lecture Anki curation workflow to Study Hub that searches the complete AnKing deck, proposes existing and custom cards, supports optional images, and applies one reviewed, idempotent envelope through a Mac companion agent.

**Architecture:** Extend the existing FastAPI/SQLAlchemy modular monolith on the Windows NUC with an `oms_hub.anki` module and a second serialized worker. Package a narrow `oms-anki-agent` process in the same repository for macOS; it is the only process allowed to call AnkiConnect. The Hub and agent communicate over Tailscale Serve using versioned Pydantic contracts and a bearer token held in the OS credential stores. Existing AnKing notes are read-only except for adding the owned lecture tag.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Jinja2, browser JavaScript, `httpx`, `python-pptx`, PyMuPDF, `selectolax`, NumPy, FastEmbed, Pillow, AnkiConnect v6, Tailscale Serve, pytest/respx, Ruff, and strict mypy.

**Implementation baseline:** Rebase this plan against the repository's current `main` before starting. The inspected baseline was commit `02d13cc6a1cb77b321a2aca482ba9d6941b66edb`.

## Global Constraints

- Keep the Hub bound to `127.0.0.1:8765`. Use `tailscale serve --bg 8765` to expose it only inside the tailnet.
- Reject `/agent/*` through the configured Cloudflare public hostname even if Cloudflare authentication succeeds.
- Authenticate every `/agent/v1/*` request with a constant-time checked bearer token. Store the token in Windows Credential Manager and macOS Keychain; never persist or log it in SQLite, files, URLs, templates, or error bodies.
- Never expose AnkiConnect beyond `127.0.0.1:8765` on the Mac.
- Never edit, move, suspend, delete, or retarget an existing AnKing note. Existing-note operations may only add the owned lecture tag.
- Use strict AMBOSS syntax: one or more `nid:<digits>` terms separated by uppercase `OR`, with surrounding whitespace allowed.
- Derive paths centrally:
  - Deck: `OMS-II_Custom_Cards::<Course_With_Underscores>::Exam_<N>::Lec<N>_<Topic>`
  - Tag: `AnkiHub_Optional::LMU_OMS_II::<CourseWithoutSpaces>::Block<N>::Lec<N>_<Topic>`
- Use source deck `Anking Step Deck`.
- Use note type `AnKingOverhaul (OMS_II_Extra/JCBrooks)` and discover its fields at runtime. Populate `Text` and `Extra`; supply every other required field as an empty string.
- Search the entire indexed AnKing deck for every lecture. Domain filtering only changes ranking and the size of the focused pass; it never removes the whole-deck safety-net pass.
- Send every retrieved candidate to Claude during shadow mode. Do not activate deterministic AUTO-INCLUDE/AUTO-DROP until two consecutive lectures meet both approved calibration thresholds.
- Treat coverage below 10 kept cards or above 40% gaps as acknowledgement warnings, not hard failures.
- Generated gap cards may be accepted without an image. GPT Image 2 runs only after an explicit per-card button click and a second confirmation.
- Preserve current transcript-cleaning behavior and provider settings. New structured and image capabilities are additive.
- Persist workflow truth in `hub.db`; treat the card index and vectors as rebuildable.
- Store all job artifacts under a path resolved beneath the configured Study Hub data directory. Reject traversal and symlink escape.
- Use immutable input fingerprints and stage-specific invalidation. Never invalidate completed upstream work because an unrelated Anki note changed.
- Every mutation is idempotent at the Hub envelope, envelope-operation, Mac ledger, media filename, and generated-note marker layers.

## Public Interfaces to Freeze First

Create these names before implementing stage internals so tests, web routes, the worker, and the Mac agent build against one contract.

```python
# src/oms_hub/anki/domain.py
from enum import StrEnum


class CurationState(StrEnum):
    QUEUED = "queued"
    BUILDING_LCL = "building_lcl"
    RETRIEVING = "retrieving"
    JUDGING = "judging"
    DEDUPING = "deduping"
    PROPOSING_GAPS = "proposing_gaps"
    READY_FOR_REVIEW = "ready_for_review"
    ENVELOPE_PENDING = "envelope_pending"
    APPLYING = "applying"
    COMPLETE = "complete"
    FAILED = "failed"


class CurationStage(StrEnum):
    LCL = "lcl"
    RETRIEVAL = "retrieval"
    JUDGMENT = "judgment"
    DEDUPE = "dedupe"
    GAPS = "gaps"
    MEDIA = "media"
    ENVELOPE = "envelope"


class Verdict(StrEnum):
    INCLUDE = "include"
    UNCERTAIN = "uncertain"
    DROP = "drop"


class AgentCommandType(StrEnum):
    FULL_SNAPSHOT = "full_snapshot"
    DELTA_SNAPSHOT = "delta_snapshot"
    FETCH_MEDIA = "fetch_media"
    APPLY_ENVELOPE = "apply_envelope"


class EnvelopeOperationType(StrEnum):
    STORE_MEDIA = "store_media"
    ADD_TAGS = "add_tags"
    ADD_NOTES = "add_notes"
    SYNC = "sync"
    VERIFY = "verify"
```

```python
# src/oms_hub/anki/paths.py
from dataclasses import dataclass


@dataclass(frozen=True)
class LectureIdentity:
    course: str
    exam_number: int
    lecture_number: int
    topic: str


def target_deck(value: LectureIdentity) -> str:
    course = canonical_component(value.course, separator="_")
    topic = canonical_component(value.topic, separator="_")
    return (
        f"OMS-II_Custom_Cards::{course}::Exam_{value.exam_number}"
        f"::Lec{value.lecture_number}_{topic}"
    )


def target_tag(value: LectureIdentity) -> str:
    course = canonical_component(value.course, separator="")
    topic = canonical_component(value.topic, separator="_")
    return (
        f"AnkiHub_Optional::LMU_OMS_II::{course}::Block{value.exam_number}"
        f"::Lec{value.lecture_number}_{topic}"
    )
```

The canonicalizer must normalize Unicode, collapse whitespace and punctuation, reject an empty result, and cap each component at 80 characters. Tests below define the accepted examples.

---

## Task 1: Package Shape, Dependencies, and Configuration

**Files:**

- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `src/oms_hub/config.py`
- Create: `src/oms_hub/anki/__init__.py`
- Create: `src/oms_hub/anki/domain.py`
- Create: `src/oms_hub/anki/paths.py`
- Create: `src/oms_anki_agent/__init__.py`
- Create: `src/oms_anki_agent/config.py`
- Create: `tests/anki/test_paths.py`
- Create: `tests/agent/test_config.py`

- [ ] **Step 1: Write failing path and agent configuration tests.**

```python
def test_confirmed_paths_are_derived_from_one_identity():
    identity = LectureIdentity(
        course="Heme Lymph",
        exam_number=1,
        lecture_number=4,
        topic="Anemia I",
    )
    assert target_deck(identity) == (
        "OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I"
    )
    assert target_tag(identity) == (
        "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
    )


def test_agent_requires_https_hub_url_and_keyring_token_name():
    config = AgentSettings(
        hub_url="https://study-hub.tailnet-name.ts.net",
        hub_token_key="anki-agent-token",
    )
    assert config.ankiconnect_url == "http://127.0.0.1:8765"
```

- [ ] **Step 2: Run the focused tests and confirm import failures.**

```text
python -m pytest tests/anki/test_paths.py tests/agent/test_config.py -q
```

Expected: collection fails because the new packages and types do not exist.

- [ ] **Step 3: Add dependencies and package both source trees.**

Add compatible ranges:

```toml
"python-pptx>=1.0,<2",
"PyMuPDF>=1.26,<2",
"selectolax>=0.3,<1",
"numpy>=2,<3",
"fastembed>=0.7,<1",
"Pillow>=11,<13",
```

Set Hatch packages to both `src/oms_hub` and `src/oms_anki_agent`, and add:

```toml
[project.scripts]
oms-study-hub = "oms_hub.cli:main"
oms-anki-agent = "oms_anki_agent.cli:main"
```

- [ ] **Step 4: Add Hub and agent settings with safe defaults.**

Hub settings include `anki_enabled`, `anki_data_dir`, `anki_agent_hostname`, agent heartbeat age, snapshot age, worker polling interval, embedding model, focused/global retrieval limits, and image price estimates. The shared secret is addressed by key name only.

- [ ] **Step 5: Implement the frozen enums and path builder, then pass focused tests.**

```text
python -m pytest tests/anki/test_paths.py tests/agent/test_config.py -q
python -m ruff check src/oms_hub/anki src/oms_anki_agent tests/anki tests/agent
python -m mypy src/oms_hub/anki src/oms_anki_agent
```

- [ ] **Step 6: Commit.**

```text
git add pyproject.toml .env.example src/oms_hub/config.py src/oms_hub/anki src/oms_anki_agent tests/anki tests/agent
git commit -m "feat(anki): establish curator package and path contracts"
```

## Task 2: Durable Schema, Migrations, and Repository State Machine

**Files:**

- Create: `src/oms_hub/anki/models.py`
- Create: `src/oms_hub/anki/repository.py`
- Modify: `src/oms_hub/migrations.py`
- Create: `tests/anki/test_migrations.py`
- Create: `tests/anki/test_repository.py`

- [ ] **Step 1: Write migration and state-transition tests.**

Tests must prove:

- A schema-v3 database upgrades without losing current lecture or LLM rows.
- Re-running migration is idempotent.
- All ten approved Anki tables exist.
- `claim_next_job()` atomically claims only one oldest queued job.
- Invalid transitions raise `InvalidCurationTransition`.
- Startup recovery changes interrupted pre-review jobs back to queued and leaves immutable envelopes pending.
- Job creation snapshots the instruction text/hash, target paths, prompt versions, and index snapshot.

```python
ALLOWED_TRANSITIONS = {
    CurationState.QUEUED: {CurationState.BUILDING_LCL, CurationState.FAILED},
    CurationState.BUILDING_LCL: {CurationState.RETRIEVING, CurationState.FAILED},
    CurationState.RETRIEVING: {CurationState.JUDGING, CurationState.FAILED},
    CurationState.JUDGING: {CurationState.DEDUPING, CurationState.FAILED},
    CurationState.DEDUPING: {CurationState.PROPOSING_GAPS, CurationState.FAILED},
    CurationState.PROPOSING_GAPS: {
        CurationState.READY_FOR_REVIEW,
        CurationState.FAILED,
    },
    CurationState.READY_FOR_REVIEW: {CurationState.ENVELOPE_PENDING},
    CurationState.ENVELOPE_PENDING: {CurationState.APPLYING, CurationState.FAILED},
    CurationState.APPLYING: {CurationState.COMPLETE, CurationState.FAILED},
}
```

- [ ] **Step 2: Run tests and confirm missing models/repository.**

```text
python -m pytest tests/anki/test_migrations.py tests/anki/test_repository.py -q
```

- [ ] **Step 3: Add all approved durable models.**

Use the existing `Base` and `utc_now` from `oms_hub.models`. Store JSON as text with repository-level Pydantic validation so SQLite migrations remain simple. Add unique constraints for:

- one instruction row per lecture;
- one stage row per job/stage;
- one candidate row per job/note;
- one verdict cache row per complete cache key;
- one operation UUID and content hash per envelope;
- one stage setting per stage.

Add indexes on job state/creation time, candidate job/verdict/selection, cache key, envelope state, and agent command state.

- [ ] **Step 4: Register Anki metadata and bump the additive schema version.**

Import `oms_hub.anki.models` before `Base.metadata.create_all()` in migrations. Follow the current explicit, repeat-safe migration style.

- [ ] **Step 5: Implement repository methods with returned detached domain objects.**

Minimum public methods:

```python
create_job(request: CreateCurationJob) -> CurationJob
claim_next_job(now: datetime) -> CurationJob | None
transition(
    job_id: UUID,
    expected_state: CurationState,
    target_state: CurationState,
    detail: str | None = None,
) -> CurationJob
start_stage(
    job_id: UUID,
    stage: CurationStage,
    provider: ProviderName | None = None,
    model: str | None = None,
) -> JobStage
finish_stage(
    job_id: UUID,
    stage: CurationStage,
    usage: StageUsage | None = None,
    cache_hits: int = 0,
) -> JobStage
fail_stage(job_id: UUID, stage: CurationStage, safe_error: str) -> JobStage
replace_candidates(job_id: UUID, candidates: Sequence[Candidate]) -> None
save_gap_cards(job_id: UUID, cards: Sequence[GapCard]) -> None
save_review(job_id: UUID, change_set: ReviewChangeSet) -> SavedReview
create_envelope(job_id: UUID, envelope: ActionEnvelope) -> StoredEnvelope
record_receipt(envelope_id: UUID, receipt: EnvelopeReceipt) -> StoredEnvelope
recover_interrupted_jobs() -> int
```

- [ ] **Step 6: Pass focused and existing migration tests.**

```text
python -m pytest tests/anki/test_migrations.py tests/anki/test_repository.py tests/v2/test_llm_migration.py -q
```

- [ ] **Step 7: Commit.**

```text
git add src/oms_hub/anki/models.py src/oms_hub/anki/repository.py src/oms_hub/migrations.py tests/anki
git commit -m "feat(anki): persist curator jobs reviews and envelopes"
```

## Task 3: Versioned Agent Contracts and Network Boundary

**Files:**

- Create: `src/oms_hub/anki/contracts.py`
- Create: `src/oms_hub/web/anki_agent_routes.py`
- Modify: `src/oms_hub/security/access.py`
- Modify: `src/oms_hub/security/csrf.py`
- Modify: `src/oms_hub/app.py`
- Create: `tests/anki/test_agent_contracts.py`
- Create: `tests/v2/test_agent_access.py`

- [ ] **Step 1: Write contract round-trip and host/auth matrix tests.**

The matrix must assert:

| Host/path | Credentials | Result |
|---|---|---|
| local dashboard | existing local rules | allowed |
| Cloudflare hostname `/anki` | valid Cloudflare JWT | allowed |
| Cloudflare hostname `/agent/v1/heartbeat` | any | 404 |
| configured tailnet hostname `/agent/v1/heartbeat` | absent/wrong bearer | 401 |
| configured tailnet hostname `/agent/v1/heartbeat` | correct bearer | 200 |
| configured tailnet hostname dashboard path | bearer | 404 |
| unknown host | any | rejected |

Also assert secret values never appear in logs, response bodies, exception strings, or `repr()` output.

- [ ] **Step 2: Define strict Pydantic contracts with `extra="forbid"`.**

Contracts include:

- `AgentHeartbeat`
- `AgentCommand` and command payload variants
- `SnapshotManifest`, `SnapshotNote`, `SnapshotDelta`
- `MediaFetchRequest`, `MediaUpload`
- `ActionEnvelope`, ordered operation variants
- `EnvelopeReceipt`, `OperationReceipt`

Every contract carries `contract_version = 1`; every stored payload carries its SHA-256.

- [ ] **Step 3: Add agent endpoints.**

```text
POST /agent/v1/heartbeat
GET  /agent/v1/commands/next
POST /agent/v1/commands/{command_id}/snapshot
POST /agent/v1/commands/{command_id}/media
POST /agent/v1/commands/{command_id}/receipt
```

Use bounded request sizes, UUID path parameters, repository ownership checks, and safe error messages.

- [ ] **Step 4: Branch access and CSRF handling by route family.**

`/agent/v1/*` bypasses browser CSRF only after tailnet-host and bearer authentication. It must never fall through to Cloudflare or local dashboard authentication.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_agent_contracts.py tests/v2/test_agent_access.py tests/v2/test_baseline_smoke.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki/contracts.py src/oms_hub/web/anki_agent_routes.py src/oms_hub/security src/oms_hub/app.py tests
git commit -m "feat(anki): secure versioned Mac agent API"
```

## Task 4: AnkiConnect Client, Mac Ledger, and CLI Skeleton

**Files:**

- Create: `src/oms_anki_agent/ankiconnect.py`
- Create: `src/oms_anki_agent/hub_client.py`
- Create: `src/oms_anki_agent/ledger.py`
- Create: `src/oms_anki_agent/cli.py`
- Create: `tests/agent/test_ankiconnect.py`
- Create: `tests/agent/test_hub_client.py`
- Create: `tests/agent/test_ledger.py`

- [ ] **Step 1: Write mocked AnkiConnect v6 tests.**

Cover `version`, `findNotes`, `notesInfo`, `modelFieldNames`, `retrieveMediaFile`, `storeMediaFile`, `addTags`, `addNotes`, and `sync`. Assert every call:

```json
{"action": "version", "version": 6, "params": {}}
```

Reject:

- HTTP failures;
- invalid JSON;
- responses without exactly `result` and `error`;
- non-null `error`;
- a version below 6;
- non-loopback AnkiConnect URLs.

- [ ] **Step 2: Write ledger idempotency tests.**

The SQLite ledger records snapshot note hashes and completed operation UUID/content-hash pairs. A UUID reused with a different hash is a fatal protocol error.

- [ ] **Step 3: Implement clients with injected `httpx.Client`.**

Follow current provider adapter conventions: direct HTTP, explicit timeouts, safe diagnostics, and dependency injection for tests. Read Hub bearer token through `KeyringSecretStore`; do not accept a CLI token flag.

- [ ] **Step 4: Add CLI commands without write behavior yet.**

```text
oms-anki-agent doctor
oms-anki-agent run
oms-anki-agent snapshot --full
```

`doctor` verifies Keychain access, Hub health, local AnkiConnect version, source deck presence, target note type presence, and runtime `Text`/`Extra` fields.

- [ ] **Step 5: Pass tests and verify installed entry point.**

```text
python -m pytest tests/agent -q
oms-anki-agent --help
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_anki_agent tests/agent pyproject.toml
git commit -m "feat(agent): add safe AnkiConnect and Hub clients"
```

## Task 5: Full Snapshot Export and Normalization

**Files:**

- Create: `src/oms_anki_agent/snapshot.py`
- Create: `src/oms_hub/anki/normalize.py`
- Create: `src/oms_hub/anki/domains.py`
- Create: `src/oms_hub/anki/snapshot.py`
- Create: `tests/agent/test_snapshot.py`
- Create: `tests/anki/test_normalize.py`
- Create: `tests/anki/test_domains.py`
- Create: `tests/anki/fixtures/anking_notes.json`

- [ ] **Step 1: Add a de-identified fixture containing clozes, HTML, media, hierarchical tags, multiple cards, and multi-domain notes.**

Keep only synthetic text and filenames. Include tags that prove one note can belong to Heme and Pharm simultaneously.

- [ ] **Step 2: Write normalization and full-export tests.**

Assert:

- the query is exactly `deck:"Anking Step Deck"`;
- `notesInfo` calls are chunked;
- HTML/script/style removal is deterministic;
- cloze markers retain visible cloze content but discard ordinals/hints from the search text;
- raw fields remain available for review;
- media references retain field and source order;
- tags retain owned and high-value source hierarchies;
- content hashes are stable under tag-order changes;
- ID-set hash changes on add/delete;
- multi-domain assignments are deterministic.

- [ ] **Step 3: Implement snapshot JSONL streaming and manifests.**

Never load media bytes or the whole exported collection into memory. The manifest includes source deck, note count, ID-set hash, content fingerprint, export version, AnkiConnect version, agent version, and UTC time.

- [ ] **Step 4: Implement Hub-side validation and bounded atomic staging.**

Validate note count, unique note IDs, manifest hash, decompressed byte limit, and JSONL row size before moving a staged snapshot into the job area.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/agent/test_snapshot.py tests/anki/test_normalize.py tests/anki/test_domains.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_anki_agent/snapshot.py src/oms_hub/anki tests/agent tests/anki
git commit -m "feat(anki): export and normalize full AnKing snapshots"
```

## Task 6: Rebuildable Search Index and Incremental Refresh

**Files:**

- Create: `src/oms_hub/anki/embeddings.py`
- Create: `src/oms_hub/anki/index.py`
- Modify: `src/oms_hub/anki/snapshot.py`
- Modify: `src/oms_anki_agent/snapshot.py`
- Create: `tests/anki/test_index.py`
- Create: `tests/anki/test_embeddings.py`
- Create: `tests/agent/test_delta_snapshot.py`

- [ ] **Step 1: Write temporary-index tests.**

Cover SQLite tables `notes`, `note_tags`, `note_domains`, `note_media`, `notes_fts`, and `index_meta`. Prove:

- exact note-ID lookup;
- tag-prefix lookup;
- FTS ranking;
- domain-filtered semantic ranking;
- whole-deck semantic ranking;
- atomic vector/order replacement;
- update/add/delete delta behavior;
- a failed rebuild leaves the prior index usable;
- snapshot IDs change only after a complete commit.

- [ ] **Step 2: Implement a deterministic embedding seam.**

```python
class Embedder(Protocol):
    @property
    def model_name(self) -> str:
        pass

    def embed(self, texts: Sequence[str]) -> NDArray[np.float32]:
        pass
```

Production uses FastEmbed; tests use a fixed normalized vector map. Store vectors in a NumPy `float32` file and note IDs in a parallel JSON file, both replaced atomically under a file lock.

- [ ] **Step 3: Implement full rebuild and delta transactions.**

Build into a sibling temporary directory, fsync files, validate counts and vector dimensions, then swap directories. For deltas, write a fresh compact vector matrix rather than mutating rows in place.

- [ ] **Step 4: Implement daily delta selection.**

Use the current full ID set for deletion detection and an edit query based on the last successful export minus a safety margin. Fall back to full export if the ledger is absent, the window is unsafe, counts disagree, or the agent/normalizer/embedding version changed.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_index.py tests/anki/test_embeddings.py tests/agent/test_delta_snapshot.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki src/oms_anki_agent/snapshot.py tests/anki tests/agent
git commit -m "feat(anki): build hybrid AnKing index with delta refresh"
```

## Task 7: Read-Only Agent Service and macOS LaunchAgent

**Files:**

- Create: `src/oms_anki_agent/service.py`
- Create: `scripts/macos/com.omsstudy.anki-agent.plist`
- Create: `scripts/macos/install-anki-agent.sh`
- Modify: `src/oms_anki_agent/cli.py`
- Create: `tests/agent/test_service.py`
- Create: `tests/agent/test_launch_agent.py`
- Modify: `README.md`

- [ ] **Step 1: Write service tests using fake Hub and AnkiConnect clients.**

Prove `run`:

- posts heartbeat without secrets;
- opens Anki only when local AnkiConnect is unavailable;
- waits with a bounded deadline for AnkiConnect;
- polls one command at a time;
- retries only network/service failures with bounded backoff;
- uploads a snapshot and acknowledges its command;
- cannot execute write commands until Task 8 enables an apply handler;
- exits cleanly on SIGTERM.

- [ ] **Step 2: Implement macOS Anki startup.**

Use a small injectable process launcher for `/usr/bin/open -a Anki`; do not use shell interpolation. Leave Anki open.

- [ ] **Step 3: Add a LaunchAgent template and installer.**

The installer resolves the installed `oms-anki-agent` executable, writes logs beneath `~/Library/Logs/OMSStudyHub`, loads the plist, and prints the exact `launchctl` status command. It must not embed the token.

- [ ] **Step 4: Add NUC operator setup documentation.**

Document:

```text
tailscale serve --bg 8765
tailscale serve status
```

Document disabling with `tailscale serve reset`, rotating the bearer token on both machines, and confirming that the Cloudflare hostname returns 404 for `/agent/v1/heartbeat`.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/agent/test_service.py tests/agent/test_launch_agent.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_anki_agent scripts/macos tests/agent README.md
git commit -m "feat(agent): run read-only Mac bridge under launchd"
```

## Task 8: Idempotent Envelope Apply and Post-Sync Verification

**Files:**

- Create: `src/oms_anki_agent/media.py`
- Create: `src/oms_anki_agent/apply.py`
- Create: `src/oms_hub/anki/envelope.py`
- Modify: `src/oms_anki_agent/service.py`
- Create: `tests/agent/test_apply.py`
- Create: `tests/anki/test_envelope.py`
- Create: `tests/contract/test_handwritten_envelope.py`

- [ ] **Step 1: Write envelope hashing and operation-order tests.**

The builder must emit:

1. zero or more `store_media`;
2. `add_tags` chunks of at most 1,000 note IDs;
3. `add_notes`;
4. exactly one `sync`;
5. exactly one `verify`.

The immutable envelope hash excludes mutable delivery state but includes every operation UUID, operation hash, snapshot ID, target deck/tag, touched-note hash, and expected media/note result.

- [ ] **Step 2: Write crash/replay contract tests.**

Cover:

- operation replay with same UUID/hash returns the stored result;
- operation UUID/hash mismatch fails closed;
- existing media with identical bytes is accepted;
- existing media with different bytes fails;
- tagged notes already carrying the target tag succeed;
- a generated note is rediscovered by its invisible marker after a simulated crash;
- Anki dialog conflicts retry twice;
- unrelated collection changes do not reject the envelope;
- a touched-note content-hash change rejects before mutation;
- sync failure records a retryable receipt without replaying completed operations.

- [ ] **Step 3: Implement preflight.**

Preflight checks AnkiConnect version, target note type and fields, all touched note IDs/hashes, target tag namespace, deterministic media conflicts, generated-note markers, and the active snapshot ID.

- [ ] **Step 4: Implement ordered apply and receipts.**

After sync, query all touched and created notes again. Verify lecture-tag persistence, created-note fields/deck, marker uniqueness, and media availability. Record every result in the Mac ledger before sending the receipt.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/agent/test_apply.py tests/anki/test_envelope.py tests/contract/test_handwritten_envelope.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_anki_agent src/oms_hub/anki/envelope.py tests/agent tests/anki tests/contract
git commit -m "feat(anki): apply immutable idempotent Anki envelopes"
```

**Milestone demonstration:** Against a disposable Anki profile, send a handwritten envelope that tags three notes and adds one generated note. Send it twice and confirm one generated note, one set of tags, one media file, and successful post-sync verification.

## Task 9: Capability-Focused LLM Service and Stage Settings

**Files:**

- Modify: `src/oms_hub/llm/domain.py`
- Modify: `src/oms_hub/llm/provider.py`
- Modify: `src/oms_hub/llm/service.py`
- Modify: `src/oms_hub/llm/openai.py`
- Modify: `src/oms_hub/llm/gemini.py`
- Modify: `src/oms_hub/llm/anthropic.py`
- Modify: `src/oms_hub/llm/repository.py`
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Create: `tests/llm/test_structured_generation.py`
- Create: `tests/anki/test_stage_settings.py`

- [ ] **Step 1: Lock existing transcript behavior with the current LLM tests.**

```text
python -m pytest tests/llm tests/v2/test_multi_provider_pipeline.py tests/v2/test_llm_settings_routes.py tests/v2/test_llm_settings_ui.py -q
```

Expected: pass before changes.

- [ ] **Step 2: Write failing structured-generation and routing tests.**

Define:

```python
@dataclass(frozen=True)
class StructuredRequest:
    system: str
    content: Sequence[TextPart | ImagePart]
    response_schema: dict[str, object]
    max_output_tokens: int


@dataclass(frozen=True)
class StructuredResult:
    payload: dict[str, object]
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
```

Test that a stage captures provider/model before the request starts, resolves credentials from the existing secret store, validates JSON against the requested Pydantic schema, records usage, and maps safe diagnostics using existing `LLMRequestError` classifications.

- [ ] **Step 3: Add structured text/image input to Gemini and Anthropic adapters.**

Preserve `clean()`. Add a capability method used by `LLMService.generate_structured()`. Keep provider HTTP behavior in the adapter and stage selection in the Anki layer.

- [ ] **Step 4: Add stage settings.**

Defaults:

| Stage | Provider | Model behavior |
|---|---|---|
| LCL | Gemini | configured Gemini model |
| Judgment | Anthropic | configured Claude Sonnet model |
| Gap cards | Anthropic | configured Claude Sonnet model |
| Image | OpenAI | `gpt-image-2` |

The Settings page edits provider/model per stage, shows credential readiness, and never exposes a key.

- [ ] **Step 5: Pass new and regression tests.**

```text
python -m pytest tests/llm tests/anki/test_stage_settings.py tests/v2/test_multi_provider_pipeline.py tests/v2/test_llm_settings_routes.py tests/v2/test_llm_settings_ui.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/llm src/oms_hub/web tests/llm tests/anki
git commit -m "feat(llm): add structured stage routing for Anki"
```

## Task 10: Lecture Artifact Extraction and Lecture Concept Ledger

**Files:**

- Create: `src/oms_hub/anki/lcl.py`
- Create: `src/oms_hub/anki/prompts/lcl.md`
- Modify: `src/oms_hub/anki/pipeline.py`
- Create: `tests/anki/test_lcl.py`
- Create: `tests/anki/fixtures/lcl_response.json`

- [ ] **Step 1: Write extraction and schema tests.**

Use the current artifact revision repository. Tests must cover:

- PPTX speaker notes and text extraction in slide order;
- embedded image metadata;
- PDF fallback through PyMuPDF;
- image-slide rendering only when text is sparse;
- transcript inclusion when a current transcript revision exists;
- source hashes in the LCL fingerprint;
- persisted lecture-specific comments in the prompt;
- Gemini structured output rejection on missing concepts, bad citations, or invalid domains;
- cache reuse for an identical input manifest.

- [ ] **Step 2: Define the LCL schema.**

Each concept has a stable ID, canonical name, aliases, objective, importance, professor emphasis, red-highlight evidence, source slide/page spans, transcript spans, domains, testability, and exclusion/context notes.

- [ ] **Step 3: Implement extraction with bounded inputs.**

Do not send the raw PPTX binary. Extract text first; attach rendered JPEG slides only for sparse/image-heavy slides. Store `slides-extracted.json` and `lcl.json` beneath the job directory using atomic writes.

- [ ] **Step 4: Implement cache key and invalidation.**

Key on slide/transcript hashes, lecture-instruction hash, LCL prompt/schema version, provider, and model. A change to AMBOSS input must not rebuild the LCL.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_lcl.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki/lcl.py src/oms_hub/anki/prompts/lcl.md src/oms_hub/anki/pipeline.py tests/anki
git commit -m "feat(anki): build cached lecture concept ledgers"
```

## Task 11: Strict AMBOSS Parsing and Hybrid Retrieval

**Files:**

- Create: `src/oms_hub/anki/amboss.py`
- Create: `src/oms_hub/anki/retrieval.py`
- Modify: `src/oms_hub/anki/pipeline.py`
- Create: `tests/anki/test_amboss.py`
- Create: `tests/anki/test_retrieval.py`

- [ ] **Step 1: Write strict parser tests.**

Accepted:

```text
nid:1479430487028
nid:1479430487028 OR nid:1517176548564
  nid:1479430487028   OR   nid:1517176548564
```

Rejected: lowercase `or`, commas, general Anki search terms, empty terms, negative IDs, duplicate separators, and trailing `OR`. Deduplicate IDs while preserving first appearance.

- [ ] **Step 2: Write retrieval union and scoring tests.**

Every run unions:

1. owned lecture-tag hits;
2. exact AMBOSS note IDs;
3. focused domain FTS/semantic hits;
4. whole-deck safety-net FTS/semantic hits.

Prove exact tags and AMBOSS IDs bypass domain filtering, provenance is retained, missing AMBOSS IDs are reported, one note is emitted once, and a relevant cross-domain note survives through the safety net.

- [ ] **Step 3: Implement deterministic score components.**

Store, do not hide, component scores: exact tag, AMBOSS, alias/title overlap, FTS, semantic similarity, domain affinity, source-tag trust, and context-trap penalty. Use the scores to order candidates and predict bands, but send every candidate to Claude in shadow mode.

- [ ] **Step 4: Persist retrieval JSONL and candidates.**

The cache key includes LCL version, AMBOSS hash, instruction hash, index snapshot, retrieval version, and embedding model.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_amboss.py tests/anki/test_retrieval.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki/amboss.py src/oms_hub/anki/retrieval.py src/oms_hub/anki/pipeline.py tests/anki
git commit -m "feat(anki): retrieve candidates across the full AnKing deck"
```

## Task 12: Claude Judgment, Caching, and Shadow Metrics

**Files:**

- Create: `src/oms_hub/anki/judgment.py`
- Create: `src/oms_hub/anki/prompts/judgment.md`
- Modify: `src/oms_hub/anki/pipeline.py`
- Create: `tests/anki/test_judgment.py`
- Create: `tests/anki/fixtures/judgment_response.json`

- [ ] **Step 1: Write schema, batching, cache, and retry tests.**

Each verdict must include note ID, best concept ID, `include|uncertain|drop`, confidence, short reason, context-trap flag, recall direction, mnemonic classification, and objective alignment.

Tests prove:

- compact candidate content stays under the configured batch budget;
- response note IDs must match the request exactly;
- duplicate/missing verdicts are rejected;
- cached verdicts avoid provider calls;
- cache keys include note hash, lecture ID, LCL version, rubric version, instruction hash, provider, and model;
- transient network/quota/service errors retry;
- authentication/model/schema errors stop with safe diagnostics;
- partial successful batches remain cached;
- deterministic predicted bands and Claude outcomes are both stored.

- [ ] **Step 2: Implement judgment through `LLMService.generate_structured()`.**

Do not send raw unused fields or media bytes. Include normalized Text/Extra, trusted tags, retrieval provenance/scores, best matching LCL concepts, and lecture comments.

- [ ] **Step 3: Calculate shadow metrics without enabling automatic decisions.**

Record per-lecture confusion metrics. A settings flag may enable deterministic triage only when the repository proves the two-lecture gate; the UI must show the evidence used to unlock it.

- [ ] **Step 4: Pass tests.**

```text
python -m pytest tests/anki/test_judgment.py tests/v2/test_worker_llm_retry.py -q
```

- [ ] **Step 5: Commit.**

```text
git add src/oms_hub/anki/judgment.py src/oms_hub/anki/prompts/judgment.md src/oms_hub/anki/pipeline.py tests/anki
git commit -m "feat(anki): judge and cache every retrieved candidate"
```

## Task 13: Deduplication, Mnemonic Preference, and Gap Detection

**Files:**

- Create: `src/oms_hub/anki/dedupe.py`
- Create: `src/oms_hub/anki/gaps.py`
- Create: `src/oms_hub/anki/prompts/dedupe_close_call.md`
- Create: `src/oms_hub/anki/prompts/gap_card.md`
- Modify: `src/oms_hub/anki/pipeline.py`
- Create: `tests/anki/test_dedupe.py`
- Create: `tests/anki/test_gaps.py`

- [ ] **Step 1: Write duplicate-survivor tests.**

Prove:

- forward/reverse notes for one concept produce one survivor by default;
- both survive only when the LCL explicitly requires both recall directions;
- objective alignment outranks generic wording;
- clinically useful recall direction outranks context traps;
- less guessable, trusted, concise cards win close deterministic comparisons;
- a card asking for the mnemonic itself beats a card asking to list every mnemonic component;
- deduplication changes only review selection and never emits a destructive operation.

- [ ] **Step 2: Write gap-detection and custom prompt tests.**

Prompt settings store an absolute `.md` path, preview text, SHA-256, accepted SHA-256, and accepted time. Tests prove:

- path traversal is irrelevant because a validated absolute file is read directly;
- non-Markdown, missing, oversized, and unreadable files fail safely;
- a changed file pauses only gap generation until accepted;
- the accepted filename/hash appears in the job manifest;
- existing retrieval/judgment artifacts remain valid after prompt changes;
- gap proposals populate editable `Text` and `Extra`;
- coverage warnings are acknowledgement gates, not failures.

- [ ] **Step 3: Implement deterministic dedupe first and Claude only for close calls.**

Persist cluster membership, recall direction, mnemonic classification, survivor reason, and disposition for audit.

- [ ] **Step 4: Implement gap proposals.**

Generate only for uncovered LCL concepts. Validate cloze syntax, forbidden unsupported fields, content-hash marker, source attribution, and note-type-compatible `Text`/`Extra`.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_dedupe.py tests/anki/test_gaps.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki/dedupe.py src/oms_hub/anki/gaps.py src/oms_hub/anki/prompts tests/anki
git commit -m "feat(anki): deduplicate recall directions and propose gaps"
```

## Task 14: Existing AnKing Media Discovery and On-Demand Previews

**Files:**

- Create: `src/oms_hub/anki/media.py`
- Modify: `src/oms_anki_agent/media.py`
- Modify: `src/oms_anki_agent/service.py`
- Create: `tests/anki/test_media.py`
- Create: `tests/agent/test_media.py`

- [ ] **Step 1: Write ranking and security tests.**

Prove:

- image candidates come from semantically relevant AnKing notes;
- histology/pathology-compatible evidence and concept similarity affect ranking;
- at most three candidates are shown;
- the best candidate is preselected;
- the user may select another, remove selection, or accept no image;
- media filenames are basenames without traversal;
- only indexed media references can be requested;
- MIME type, byte count, image decode, and maximum dimensions are validated;
- existing media bytes are not copied to the NUC until requested.

- [ ] **Step 2: Implement command-driven preview fetch.**

The Hub queues `FETCH_MEDIA`; the agent calls `retrieveMediaFile`, validates base64 and size, and posts bytes with their SHA-256. Store job-local previews atomically.

- [ ] **Step 3: Implement deterministic chosen-media filenames.**

Use `oms_anki_<sha256-prefix>.<ext>`. Preserve the original extension only after validated MIME agreement.

- [ ] **Step 4: Pass tests.**

```text
python -m pytest tests/anki/test_media.py tests/agent/test_media.py -q
```

- [ ] **Step 5: Commit.**

```text
git add src/oms_hub/anki/media.py src/oms_anki_agent tests/anki tests/agent
git commit -m "feat(anki): suggest and fetch existing AnKing images"
```

## Task 15: Curation Worker and Launch/History Screen

**Files:**

- Create: `src/oms_hub/anki/pipeline.py`
- Create: `src/oms_hub/anki/worker.py`
- Create: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/routes.py`
- Modify: `src/oms_hub/web/templates/anki.html`
- Create: `src/oms_hub/web/static/anki.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/cli.py`
- Create: `tests/anki/test_pipeline.py`
- Create: `tests/anki/test_worker.py`
- Create: `tests/v2/test_anki_launch_ui.py`

- [ ] **Step 1: Write pipeline invalidation and worker tests.**

Prove:

- one curation job runs at a time;
- ingestion worker operation remains independent;
- startup recovers interrupted jobs;
- stages reuse valid artifacts;
- slide/transcript/comment change invalidates LCL and downstream;
- AMBOSS change invalidates retrieval and downstream only;
- judgment rubric/model change invalidates judgment and downstream;
- custom prompt change invalidates gaps and media only;
- review edits never trigger an LLM call;
- cancellation between stages is safe;
- safe job errors contain a recovery action.

- [ ] **Step 2: Write launch/history route tests.**

The `/anki` page must provide Course → Exam/Block → Lecture dependent selections from current lecture records, exact derived deck/tag previews, strict AMBOSS validation, persisted lecture comments, accepted prompt fingerprint, index/agent health, and recent job history.

Starting is blocked only for missing current lecture artifacts, stale/missing index, offline agent beyond the configured threshold, unaccepted prompt revision, or another active curation job.

- [ ] **Step 3: Implement serialized worker lifecycle beside ingestion.**

Construct services in `create_app()`. Start a second daemon thread in `cli.serve()`, use an independent database session per operation, and recover jobs before polling.

- [ ] **Step 4: Replace the placeholder page with accessible progressive enhancement.**

Server-render functional selects and forms; JavaScript enhances dependent options, previews, status polling, and inline validation. Preserve CSRF patterns.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/anki/test_pipeline.py tests/anki/test_worker.py tests/v2/test_anki_launch_ui.py tests/v2/test_baseline_smoke.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/anki src/oms_hub/web src/oms_hub/app.py src/oms_hub/cli.py tests
git commit -m "feat(anki): run single-lecture curation jobs from the Hub"
```

## Task 16: Review, Editing, Warnings, and Apply UI

**Files:**

- Create: `src/oms_hub/web/templates/anki_review.html`
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/static/anki.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/anki/envelope.py`
- Create: `tests/v2/test_anki_review_ui.py`
- Create: `tests/anki/test_review_apply.py`
- Create: `tests/js/anki.test.js`

- [ ] **Step 1: Write review-default and optimistic-lock tests.**

Defaults:

- included existing cards selected;
- gap cards selected;
- dropped and uncertain existing cards unselected;
- dedupe losers unselected;
- best existing image selected when available;
- no image remains valid.

Every save includes the review revision. A stale browser receives 409 and current data instead of overwriting newer edits.

- [ ] **Step 2: Write Apply gating tests.**

Apply requires:

- job in `READY_FOR_REVIEW`;
- no invalid gap cards;
- warning acknowledgement when kept count is below 10 or gap fraction exceeds 40%;
- current accepted custom-prompt fingerprint;
- target deck/tag confirmation;
- no existing immutable envelope.

Double-clicking Apply returns the existing envelope.

- [ ] **Step 3: Implement review sections.**

Show concept coverage summary, kept existing cards, uncertain/dropped cards, duplicate clusters, editable gaps, image choices, per-card rationale/provenance, warnings, provider usage/cost, and an immutable Apply summary.

- [ ] **Step 4: Build the envelope only from a saved review revision.**

Reload candidate/note hashes from the indexed snapshot, create ordered operations, persist envelope and operations in one transaction, then queue `APPLY_ENVELOPE`.

- [ ] **Step 5: Test JavaScript behavior.**

```text
node --test tests/js/anki.test.js
python -m pytest tests/v2/test_anki_review_ui.py tests/anki/test_review_apply.py -q
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/web src/oms_hub/anki/envelope.py tests/v2 tests/anki tests/js
git commit -m "feat(anki): review edit and apply curated decks"
```

## Task 17: Explicit GPT Image 2 Generation

**Files:**

- Modify: `src/oms_hub/llm/domain.py`
- Modify: `src/oms_hub/llm/provider.py`
- Modify: `src/oms_hub/llm/service.py`
- Modify: `src/oms_hub/llm/openai.py`
- Modify: `src/oms_hub/anki/media.py`
- Create: `src/oms_hub/anki/prompts/image.md`
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/templates/anki_review.html`
- Modify: `src/oms_hub/web/static/anki.js`
- Create: `tests/llm/test_openai_images.py`
- Create: `tests/anki/test_image_generation.py`
- Modify: `tests/js/anki.test.js`

- [ ] **Step 1: Write provider payload and response tests with `respx`.**

Use the official OpenAI Images generation API through the existing OpenAI credential. The request includes:

```json
{
  "model": "gpt-image-2",
  "prompt": "the confirmed editable prompt",
  "size": "1024x1024",
  "quality": "medium",
  "output_format": "png"
}
```

Validate base64 response bytes, PNG decoding, request ID, usage when present, and safe provider errors. Do not add OAuth.

- [ ] **Step 2: Write two-step confirmation tests.**

The first per-card `Generate Image` click opens a confirmation panel showing editable prompt, model, size, quality, and configured estimated cost. No provider request occurs until the confirm action. Repeated confirm actions are idempotent by generation fingerprint.

- [ ] **Step 3: Implement server-side generation.**

Revalidate job state, review revision, selected gap card, allowed model/size/quality, prompt length, and CSRF. Store generated PNG atomically under the job media directory with SHA-256 metadata. Never auto-generate during the pipeline.

- [ ] **Step 4: Attach the selected image below the explanation in `Extra`.**

Build sanitized HTML using the deterministic Anki media filename:

```html
<div class="oms-extra-explanation">Validated explanation HTML</div>
<div class="oms-extra-image"><img src="oms_anki_0123456789abcdef.png"></div>
```

If the user clears the image, emit only the explanation.

- [ ] **Step 5: Pass tests.**

```text
python -m pytest tests/llm/test_openai_images.py tests/anki/test_image_generation.py -q
node --test tests/js/anki.test.js
```

- [ ] **Step 6: Commit.**

```text
git add src/oms_hub/llm src/oms_hub/anki src/oms_hub/web tests
git commit -m "feat(anki): add explicit confirmed GPT Image 2 generation"
```

## Task 18: Calibration, Operations, Acceptance, and Release Gate

**Files:**

- Create: `src/oms_hub/anki/calibration.py`
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `src/oms_hub/web/templates/anki.html`
- Modify: `README.md`
- Create: `docs/anki-curation-runbook.md`
- Create: `tests/anki/test_calibration.py`
- Create: `tests/v2/test_anki_release_gate.py`

- [ ] **Step 1: Write calibration metric tests.**

Given a manual lecture truth set, calculate:

- retrieval recall of manually kept note IDs;
- AUTO-INCLUDE agreement;
- AUTO-DROP false-drop rate;
- domain-focused misses recovered by the global safety net;
- consecutive qualifying lecture count.

Retrieval recall must be 100% before judgment calibration is accepted. Deterministic triage remains locked unless AUTO-INCLUDE agreement is at least 95% and AUTO-DROP false-drop is below 2% for two consecutive lectures.

- [ ] **Step 2: Add calibration import and report UI.**

Accept a simple reviewed-note export containing note ID and keep/drop outcome. Validate IDs against the active snapshot. Show misses as retrieval defects with provenance and ranked diagnostic candidates.

- [ ] **Step 3: Write the operations runbook.**

Include:

- first full snapshot;
- daily delta and periodic full reconciliation;
- mandatory full rebuild after AnKing releases;
- agent heartbeat/index-age warnings;
- rotating the shared token;
- pausing and resuming agent work;
- recovering failed envelopes without duplicating effects;
- checking Anki dialogs;
- disposable-profile acceptance;
- backup inclusions/exclusions;
- proving Cloudflare cannot reach `/agent/*`;
- rollback by disabling `anki_enabled` and unloading the LaunchAgent.

- [ ] **Step 4: Run a disposable-profile acceptance checklist.**

Record evidence for:

1. full snapshot count and ID-set reconciliation;
2. handwritten envelope replay;
3. one representative lecture retrieved in shadow mode;
4. all manual keeps present in retrieval;
5. forward/reverse and mnemonic survivor behavior;
6. generated note rendering with existing image;
7. generated note rendering with GPT Image 2 image;
8. generated note rendering with no image;
9. post-sync owned-tag persistence;
10. no existing note content/deck/suspension change.

- [ ] **Step 5: Run the full automated release gate.**

```text
python -m ruff check .
python -m mypy src/oms_hub src/oms_anki_agent
python -m pytest --cov=oms_hub --cov=oms_anki_agent --cov-report=term-missing
node --test tests/js/*.test.js
```

Expected: all tests pass, strict mypy passes, Ruff passes, and coverage does not regress below the repository's existing gate.

- [ ] **Step 6: Perform security and mutation audit.**

```text
rg -n "deleteNotes|suspend|changeDeck|updateNoteFields|removeTags" src/oms_hub/anki src/oms_anki_agent
rg -n "api[_-]?key|bearer|authorization|token" src/oms_hub/anki src/oms_anki_agent
```

Expected: no destructive AnkiConnect action exists; credential references are key names or redacted headers, never secret values.

- [ ] **Step 7: Commit.**

```text
git add src/oms_hub/anki/calibration.py src/oms_hub/web README.md docs/anki-curation-runbook.md tests
git commit -m "feat(anki): gate production curation with calibration and runbook"
```

## Final Implementation Review

- [ ] Trace every approved design statement to a task or global constraint.
- [ ] Confirm all new database tables are covered by migration and repository tests.
- [ ] Confirm every agent request and response uses the versioned contract models.
- [ ] Confirm every provider call records provider, model, request ID, usage, estimated cost, and safe failure source.
- [ ] Confirm comment, artifact, AMBOSS, prompt, rubric, model, and index changes invalidate only dependent stages.
- [ ] Confirm existing AnKing note operations contain only `addTags`.
- [ ] Confirm tag chunks never exceed 1,000 note IDs.
- [ ] Confirm all media and job paths remain under the configured Anki data directory.
- [ ] Confirm review defaults and warning acknowledgements match the approved behavior.
- [ ] Confirm generated cards supply runtime-required fields and put the optional image below the `Extra` explanation.
- [ ] Confirm the agent can recover after every operation boundary without duplicate effects.
- [ ] Confirm public Cloudflare requests cannot reach agent endpoints.
- [ ] Confirm the full-deck safety-net query runs for every lecture.
- [ ] Confirm deterministic triage cannot unlock without stored calibration evidence.
- [ ] Confirm no automatic image-generation code path exists.
- [ ] Scan the implementation for unfinished markers:

```text
rg -n "TBD|TODO|FIXME|NotImplementedError|implement later|fill this in" src tests docs
```

- [ ] Inspect final type and test results, then use `superpowers:requesting-code-review` and `superpowers:verification-before-completion` before declaring the feature complete.

## Recommended Delivery Sequence

Ship behind `anki_enabled = false` through Tasks 1–7. Enable the feature only in a disposable Anki profile after Task 8 proves the write path. Tasks 9–14 build intelligence while all decisions remain shadowed. Tasks 15–17 expose the complete interactive workflow. Task 18 is the production gate; deterministic triage remains separately locked until the two-lecture calibration evidence exists.
