# NUC-Local Anki Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the disposable Mac/Tailscale Anki bridge with a safe NUC-local Anki runtime that exports and indexes AnKing notes, applies idempotent curation envelopes, synchronizes through AnkiHub and AnkiWeb, and verifies the result.

**Architecture:** Study Hub remains on `127.0.0.1:8765` and calls AnkiConnect directly at the fixed loopback endpoint `http://127.0.0.1:8766`. A single serialized local runtime launches Anki on the interactive Windows NUC when necessary, owns snapshot and apply operations, and stores its ledger beneath the existing Anki data directory. All Mac-agent HTTP, authentication, command, heartbeat, packaging, and Tailscale code is removed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic Settings, `httpx`, AnkiConnect v6, Anki Desktop on Windows, AnkiHub add-on, AnkiWeb, pytest/respx, Ruff, and strict mypy.

## Global Constraints

- The Windows NUC is the only curation runtime. The Mac receives completed changes through ordinary AnkiWeb sync and runs no curation process.
- Keep Study Hub on `127.0.0.1:8765`.
- Accept only `http://127.0.0.1:8766` as the AnkiConnect URL.
- Launch only an absolute configured `Anki.exe` path, with `shell=False`, in the scheduled task's interactive Windows session.
- Keep `Anking Step Deck` as the indexed source deck.
- Keep `AnKingOverhaul (OMS_II_Extra/JCBrooks)` as the generated-card note type and discover all fields at runtime.
- Existing AnKing notes may only receive the owned lecture tag. Never edit, move, suspend, delete, or retarget them.
- Apply operations in this order: approved media, existing-note tags, generated notes, AnkiHub/AnkiWeb sync, verification.
- Configure AnkiHub with `auto_sync: "on_ankiweb_sync"`; the AnkiHub add-on runs before AnkiWeb when Anki's normal sync is invoked.
- Never report an envelope complete until post-sync tag and generated-card verification succeeds.
- Preserve existing curation jobs, index data, artifacts, envelopes, and receipts. Only the disposable `anki_agent_state` and `anki_agent_commands` tables may be dropped.
- Remove `/agent/v1/*`, Tailscale configuration, shared bearer credentials, the `oms_anki_agent` package, and macOS LaunchAgent files.
- Use test-driven development for behavior changes: add one failing test, observe the expected failure, implement the minimum behavior, and rerun focused plus relevant regression tests.

---

## Task 1: NUC-Local Configuration Boundary

**Files:**

- Modify: `src/oms_hub/config.py`
- Modify: `.env.example`
- Modify: `tests/anki/test_settings.py`

**Interfaces:**

- Produces `Settings.anki_connect_url: Literal["http://127.0.0.1:8766"]`.
- Produces `Settings.anki_executable_path: Path | None`.
- Produces `Settings.anki_startup_timeout_seconds: float` and `Settings.anki_startup_poll_seconds: float`.
- Removes every `anki_agent_*` setting.

- [ ] **Step 1: Replace agent-setting assertions with failing local-setting tests.**

```python
def test_anki_settings_use_fixed_nuc_loopback_boundary(tmp_path: Path) -> None:
    executable = tmp_path / "Anki.exe"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        anki_executable_path=executable,
    )

    assert settings.anki_connect_url == "http://127.0.0.1:8766"
    assert settings.anki_executable_path == executable
    assert settings.anki_startup_timeout_seconds == 60
    assert settings.anki_startup_poll_seconds == 1
    assert not hasattr(settings, "anki_agent_hostname")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8766",
        "http://0.0.0.0:8766",
        "https://study-hub.example.ts.net",
    ],
)
def test_anki_settings_reject_noncanonical_connect_url(url: str) -> None:
    with pytest.raises(ValidationError, match="anki_connect_url"):
        Settings(_env_file=None, anki_connect_url=url)


def test_anki_settings_reject_relative_executable_path() -> None:
    with pytest.raises(ValidationError, match="anki_executable_path"):
        Settings(_env_file=None, anki_executable_path=Path("Anki.exe"))
```

- [ ] **Step 2: Run the focused test and observe the missing local fields.**

Run: `python -m pytest tests/anki/test_settings.py -q`

Expected: FAIL because `anki_connect_url`, `anki_executable_path`, and startup settings do not exist and agent settings still exist.

- [ ] **Step 3: Implement the local-only settings.**

Add:

```python
from typing import Literal

anki_connect_url: Literal["http://127.0.0.1:8766"] = "http://127.0.0.1:8766"
anki_executable_path: Path | None = None
anki_startup_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
anki_startup_poll_seconds: float = Field(default=1.0, ge=0.1, le=10.0)

@field_validator("anki_executable_path")
@classmethod
def validate_anki_executable_path(cls, value: Path | None) -> Path | None:
    if value is not None and not value.is_absolute():
        raise ValueError("anki_executable_path must be absolute")
    return value
```

Remove `anki_agent_hostname`, `anki_agent_token_key`, heartbeat age, agent request size, and their validators. Replace the matching `.env.example` entries with:

```text
OMS_HUB_ANKI_CONNECT_URL=http://127.0.0.1:8766
OMS_HUB_ANKI_EXECUTABLE_PATH=C:\Users\conbr\AppData\Local\Programs\Anki\anki.exe
OMS_HUB_ANKI_STARTUP_TIMEOUT_SECONDS=60
OMS_HUB_ANKI_STARTUP_POLL_SECONDS=1
```

- [ ] **Step 4: Run focused and configuration regressions.**

Run: `python -m pytest tests/anki/test_settings.py tests/v2/test_generation_settings.py tests/v2/test_baseline_smoke.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the configuration boundary.**

```bash
git add .env.example src/oms_hub/config.py tests/anki/test_settings.py
git commit -m "refactor(anki): configure NUC-local AnkiConnect"
```

## Task 2: Local AnkiConnect Client and Windows Runtime

**Files:**

- Create: `src/oms_hub/anki/ankiconnect.py`
- Create: `src/oms_hub/anki/runtime.py`
- Create: `tests/anki/test_ankiconnect.py`
- Create: `tests/anki/test_runtime.py`

**Interfaces:**

- Produces `AnkiConnectClient` with `version`, `find_notes`, `notes_info`, `model_field_names`, `retrieve_media_file`, `store_media_file`, `add_tags`, `add_notes`, and `sync`.
- Produces `WindowsAnkiLauncher.launch() -> None`.
- Produces `LocalAnkiRuntime.ensure_available() -> int` and `LocalAnkiRuntime.doctor() -> AnkiDoctorResult`.

- [ ] **Step 1: Add failing client tests at the NUC endpoint.**

Port the existing AnkiConnect behavior tests to `tests/anki/test_ankiconnect.py`, import from `oms_hub.anki.ankiconnect`, and assert every request uses:

```python
assert request.url == "http://127.0.0.1:8766"
assert request.json() == {"action": "version", "version": 6, "params": {}}
```

Also assert constructing with any other URL raises `ValueError`.

- [ ] **Step 2: Run the client tests and observe the missing Hub module.**

Run: `python -m pytest tests/anki/test_ankiconnect.py -q`

Expected: collection/import FAIL because `oms_hub.anki.ankiconnect` does not exist.

- [ ] **Step 3: Implement the strict local client.**

Move the tested client behavior into `src/oms_hub/anki/ankiconnect.py`, change the default and validator to the fixed `8766` URL, and keep the existing safe error classes:

```python
class AnkiConnectClient:
    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:8766",
        http: httpx.Client | None = None,
    ) -> None:
        if url != "http://127.0.0.1:8766":
            raise ValueError(
                "AnkiConnect must use the loopback URL http://127.0.0.1:8766"
            )
        self.url = url
        self.http = http or httpx.Client(timeout=30.0)
```

- [ ] **Step 4: Run the client tests and confirm they pass.**

Run: `python -m pytest tests/anki/test_ankiconnect.py -q`

Expected: PASS.

- [ ] **Step 5: Add failing runtime tests.**

```python
def test_runtime_launches_anki_once_and_waits_to_bounded_deadline(tmp_path: Path) -> None:
    anki = SequencedAnki([AnkiConnectUnavailable("offline"), 6])
    launcher = RecordingLauncher()
    clock = FakeClock()
    runtime = LocalAnkiRuntime(
        anki=anki,
        launcher=launcher,
        startup_timeout_seconds=3,
        startup_poll_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert runtime.ensure_available() == 6
    assert launcher.calls == 1
    assert clock.sleeps == [1]


def test_windows_launcher_uses_absolute_executable_without_shell(tmp_path: Path) -> None:
    executable = tmp_path / "Anki.exe"
    popen = RecordingPopen()

    WindowsAnkiLauncher(executable, popen=popen).launch()

    assert popen.calls == [([str(executable)], False)]
```

- [ ] **Step 6: Run runtime tests and observe the missing runtime module.**

Run: `python -m pytest tests/anki/test_runtime.py -q`

Expected: collection/import FAIL because `oms_hub.anki.runtime` does not exist.

- [ ] **Step 7: Implement runtime launch, wait, and doctor behavior.**

```python
@dataclass(frozen=True, slots=True)
class AnkiDoctorResult:
    ankiconnect_version: int
    source_note_count: int
    note_type_fields: tuple[str, ...]


class LocalAnkiRuntime:
    def ensure_available(self) -> int:
        try:
            return self.anki.version()
        except AnkiConnectUnavailable:
            self.launcher.launch()
        deadline = self.monotonic() + self.startup_timeout_seconds
        while self.monotonic() < deadline:
            self.sleep(min(self.startup_poll_seconds, deadline - self.monotonic()))
            try:
                return self.anki.version()
            except AnkiConnectUnavailable:
                continue
        raise AnkiConnectUnavailable(
            "AnkiConnect did not become available before the startup deadline"
        )
```

`doctor()` must call `ensure_available`, verify a nonempty `deck:"Anking Step Deck"`, discover the target note type fields, and require both `Text` and `Extra`. AnkiHub login and `auto_sync` remain explicit live-rollout checks because AnkiConnect does not expose AnkiHub's private authentication state.

- [ ] **Step 8: Run all local client/runtime tests.**

Run: `python -m pytest tests/anki/test_ankiconnect.py tests/anki/test_runtime.py -q`

Expected: PASS.

- [ ] **Step 9: Commit the local runtime.**

```bash
git add src/oms_hub/anki/ankiconnect.py src/oms_hub/anki/runtime.py tests/anki/test_ankiconnect.py tests/anki/test_runtime.py
git commit -m "feat(anki): add NUC-local Anki runtime"
```

## Task 3: Move Snapshot Export and Ledger Into Study Hub

**Files:**

- Create: `src/oms_hub/anki/ledger.py`
- Create: `src/oms_hub/anki/snapshot_export.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Create: `tests/anki/test_ledger.py`
- Create: `tests/anki/test_snapshot_export.py`
- Create: `tests/anki/test_delta_snapshot.py`
- Modify: `tests/anki/test_agent_contracts.py`

**Interfaces:**

- Produces `AnkiLedger(path: Path)` with snapshot and operation idempotency methods.
- Produces `FullSnapshotExporter.export(destination, exported_at=None) -> SnapshotManifest`.
- Produces `DeltaSnapshotPlanner.plan(...) -> DeltaSnapshotPlan`.
- Removes agent/command identity from snapshot types while preserving strict Pydantic validation and canonical hashes.

- [ ] **Step 1: Add failing Hub-local ledger tests.**

Copy the three existing ledger behaviors into `tests/anki/test_ledger.py`, import `AnkiLedger` and `OperationIdentityConflict` from `oms_hub.anki.ledger`, and keep the exact replay assertions:

```python
assert ledger.record_operation(operation_id, "a" * 64, first_result) == first_result
assert ledger.record_operation(operation_id, "a" * 64, second_result) == first_result
with pytest.raises(OperationIdentityConflict):
    ledger.record_operation(operation_id, "b" * 64, second_result)
```

- [ ] **Step 2: Run the ledger tests and observe the missing module.**

Run: `python -m pytest tests/anki/test_ledger.py -q`

Expected: collection/import FAIL for `oms_hub.anki.ledger`.

- [ ] **Step 3: Move the ledger implementation and rename it.**

Create `AnkiLedger` with the existing `snapshot_notes`, `completed_operations`, and `metadata` tables. Preserve `record_operation` conflict behavior and deterministic JSON.

- [ ] **Step 4: Run ledger tests to green.**

Run: `python -m pytest tests/anki/test_ledger.py -q`

Expected: PASS.

- [ ] **Step 5: Add failing Hub-local full/delta export tests.**

Port the existing snapshot and delta planner tests under `tests/anki/`, change imports to `oms_hub.anki.snapshot_export`, replace `agent_version` with `hub_version`, and assert the manifest contains `producer_version="test"` rather than `agent_version`.

- [ ] **Step 6: Run export tests and observe the missing module/schema fields.**

Run: `python -m pytest tests/anki/test_snapshot_export.py tests/anki/test_delta_snapshot.py -q`

Expected: FAIL because the local exporter and `producer_version` field do not exist.

- [ ] **Step 7: Move the exporter/planner and localize snapshot contracts.**

Implement:

```python
class SnapshotManifest(ContractModel):
    snapshot_id: Annotated[str, Field(min_length=1, max_length=200)]
    source_deck: Literal["Anking Step Deck"]
    note_count: Annotated[int, Field(ge=0)]
    id_set_sha256: Sha256
    content_sha256: Sha256
    export_version: Annotated[str, Field(min_length=1, max_length=50)]
    producer_version: Annotated[str, Field(min_length=1, max_length=100)]
    ankiconnect_version: Annotated[int, Field(ge=6)]
    exported_at: datetime
    payload_sha256: Sha256
```

Move full export, note parsing, delta planning, and streamed payload preparation into `snapshot_export.py`. Use `AnkiLedger`; remove `AgentCommandType`, `AgentCommand`, and HTTP upload concepts.

- [ ] **Step 8: Run snapshot, normalization, and index regressions.**

Run: `python -m pytest tests/anki/test_snapshot_export.py tests/anki/test_delta_snapshot.py tests/anki/test_ledger.py tests/anki/test_normalize.py tests/anki/test_domains.py tests/anki/test_index.py tests/anki/test_embeddings.py -q`

Expected: PASS.

- [ ] **Step 9: Commit local snapshot ownership.**

```bash
git add src/oms_hub/anki/contracts.py src/oms_hub/anki/ledger.py src/oms_hub/anki/snapshot_export.py tests/anki
git commit -m "refactor(anki): move snapshots onto the NUC"
```

## Task 4: Idempotent Local Envelope Apply, Sync, and Verification

**Files:**

- Create: `src/oms_hub/anki/apply.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/domain.py`
- Modify: `src/oms_hub/anki/repository.py`
- Create: `tests/anki/test_apply.py`
- Modify: `tests/anki/test_anki_repository.py`

**Interfaces:**

- Produces `LocalEnvelopeExecutor.execute(envelope: ActionEnvelope) -> EnvelopeReceipt`.
- Produces repository methods `start_envelope_operation`, `complete_envelope_operation`, `fail_envelope_operation`, and `operation_results`.
- Produces `StoredEnvelopeOperation` as the detached result of repository operation transitions.
- Replaces receipt `agent_id` with `executor_id: Literal["nuc-local"]`.

- [ ] **Step 1: Add failing repository operation-state tests.**

```python
def test_repository_records_idempotent_envelope_operation_results(repository, envelope):
    operation = envelope.operations[0]
    claimed = repository.start_envelope_operation(envelope.envelope_id, operation.operation_id)
    completed = repository.complete_envelope_operation(
        envelope.envelope_id,
        operation.operation_id,
        {"filename": "lecture.png"},
    )

    assert claimed.state == "applying"
    assert completed.state == "complete"
    assert repository.operation_results(envelope.envelope_id)[operation.operation_id] == {
        "filename": "lecture.png"
    }
```

- [ ] **Step 2: Run the repository test and observe missing methods.**

Run: `python -m pytest tests/anki/test_anki_repository.py -q`

Expected: FAIL with missing repository operation-state methods.

- [ ] **Step 3: Implement atomic operation state transitions.**

Each start increments attempts and transitions `pending` or `retryable` to `applying`. Completion writes canonical JSON and `complete`; failure writes a bounded safe error and either `retryable` or `failed`. A complete operation returns its stored result without rerunning.

- [ ] **Step 4: Run repository tests to green.**

Run: `python -m pytest tests/anki/test_anki_repository.py -q`

Expected: PASS.

- [ ] **Step 5: Add failing executor tests for order and idempotency.**

Use a stateful fake Anki implementation and real temporary `AnkiLedger`. Cover:

```python
def test_executor_applies_media_tags_notes_sync_and_verify_in_order(...):
    receipt = executor.execute(envelope)
    assert anki.actions == ["store_media", "add_tags", "add_notes", "sync", "notes_info"]
    assert receipt.executor_id == "nuc-local"
    assert receipt.sync_status == "complete"
    assert receipt.verified is True


def test_executor_replay_does_not_duplicate_generated_notes(...):
    first = executor.execute(envelope)
    second = executor.execute(envelope)
    assert second == first
    assert anki.add_notes_calls == 1


def test_executor_refuses_stale_existing_note_before_any_write(...):
    with pytest.raises(StaleEnvelopeError):
        executor.execute(envelope)
    assert anki.write_actions == []


def test_executor_does_not_complete_when_post_sync_verification_fails(...):
    receipt = executor.execute(envelope)
    assert receipt.sync_status == "complete"
    assert receipt.verified is False
    assert receipt.safe_error == "post-sync verification failed"
```

- [ ] **Step 6: Run executor tests and observe the missing module.**

Run: `python -m pytest tests/anki/test_apply.py -q`

Expected: collection/import FAIL for `oms_hub.anki.apply`.

- [ ] **Step 7: Implement the minimal local executor.**

`execute` must:

1. Validate every `touched_note_hashes` entry against fresh `notesInfo` before writes.
2. Run envelope operations in their validated phase order.
3. Add `f"OMSStudyHub_Operation::{operation.operation_id}"` as a deterministic generated-note marker tag and query it before `addNotes` to close the crash/retry duplication window.
4. Store completed results in both the Hub repository and local ledger.
5. Invoke `sync` exactly once for a new envelope. AnkiHub's configured wrapper performs AnkiHub before AnkiWeb.
6. Re-read tagged existing notes and generated note IDs after sync.
7. Record a complete receipt only when verification passes; otherwise store a failed verification receipt and keep the job recoverable.

- [ ] **Step 8: Run executor and contract tests.**

Run: `python -m pytest tests/anki/test_apply.py tests/anki/test_agent_contracts.py tests/anki/test_anki_repository.py -q`

Expected: PASS. The contract test keeps its old filename until Task 5 renames it.

- [ ] **Step 9: Commit local apply behavior.**

```bash
git add src/oms_hub/anki/apply.py src/oms_hub/anki/contracts.py src/oms_hub/anki/repository.py tests/anki
git commit -m "feat(anki): apply and verify envelopes on the NUC"
```

## Task 5: Remove Mac, Agent API, Tailscale, and Obsolete Tables

**Files:**

- Delete: `src/oms_anki_agent/`
- Delete: `tests/agent/`
- Delete: `scripts/macos/`
- Delete: `src/oms_hub/web/anki_agent_routes.py`
- Delete: `tests/v2/test_agent_access.py`
- Rename: `tests/anki/test_agent_contracts.py` to `tests/anki/test_contracts.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/security/csrf.py`
- Modify: `src/oms_hub/anki/domain.py`
- Modify: `src/oms_hub/anki/contracts.py`
- Modify: `src/oms_hub/anki/models.py`
- Modify: `src/oms_hub/anki/repository.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `pyproject.toml`
- Create: `tests/v2/test_local_anki_boundary.py`
- Modify: `tests/anki/test_migrations.py`

**Interfaces:**

- `create_app` no longer exposes or authenticates `/agent/v1/*`.
- Schema version 7 drops only `anki_agent_commands` and `anki_agent_state` when upgrading from version 6.
- `oms-anki-agent` is no longer packaged or installed.

- [ ] **Step 1: Add failing boundary and migration tests.**

```python
def test_agent_api_is_absent_on_every_host(tmp_path: Path) -> None:
    client = prepared_client(tmp_path)
    assert client.get("/agent/v1/health").status_code == 404
    assert client.post("/agent/v1/heartbeat", json={}).status_code == 404


def test_schema_upgrade_retires_only_disposable_agent_tables(database) -> None:
    database.migrate()
    tables = set(inspect(database.engine).get_table_names())
    assert "anki_agent_commands" not in tables
    assert "anki_agent_state" not in tables
    assert "anki_curation_jobs" in tables
    assert "anki_envelopes" in tables
```

Add a source-surface test asserting `pyproject.toml` contains neither `oms-anki-agent` nor `oms_anki_agent`, and the `scripts/macos` path is absent.

- [ ] **Step 2: Run the boundary tests and observe the exposed agent API/tables.**

Run: `python -m pytest tests/v2/test_local_anki_boundary.py tests/anki/test_migrations.py -q`

Expected: FAIL because agent routes, package metadata, and tables still exist.

- [ ] **Step 3: Remove runtime agent wiring and types.**

Delete the agent router import/inclusion and the agent-host branch from `app.py`. Remove `/agent/v1/` from CSRF special cases. Remove `AgentCommandType`, `AgentState`, `StoredAgentCommand`, agent contracts, agent models, and repository methods.

- [ ] **Step 4: Add the targeted schema migration.**

Before metadata creation can recreate removed tables, remove their model definitions. In migration version 7, inspect existing table names and execute:

```python
for table_name in ("anki_agent_commands", "anki_agent_state"):
    if table_name in existing_tables:
        connection.execute(text(f"DROP TABLE {table_name}"))
```

The table names are fixed constants, never user input. Preserve all other Anki tables.

- [ ] **Step 5: Remove disposable files and package entry points.**

Delete the Mac package, tests, scripts, and agent route. Change Hatch and mypy package lists to `oms_hub` only.

- [ ] **Step 6: Run boundary, migration, security, and baseline tests.**

Run: `python -m pytest tests/v2/test_local_anki_boundary.py tests/anki/test_migrations.py tests/anki/test_contracts.py tests/v2/test_baseline_smoke.py tests/v2/test_llm_migration.py -q`

Expected: PASS.

- [ ] **Step 7: Commit removal of the remote bridge.**

```bash
git add -A
git commit -m "refactor(anki): remove Mac and Tailscale bridge"
```

## Task 6: Wire Local Doctor and Snapshot Commands

**Files:**

- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/cli.py`
- Create: `src/oms_hub/anki/service.py`
- Create: `tests/anki/test_local_service.py`
- Modify: `tests/v2/test_baseline_smoke.py`

**Interfaces:**

- Produces `LocalAnkiService.doctor() -> AnkiDoctorResult`.
- Produces `LocalAnkiService.export_full() -> SnapshotManifest`.
- Adds `oms-hub anki-doctor` and `oms-hub anki-snapshot --full`.
- Exposes `app.state.anki_service` and `app.state.anki_executor` without starting Anki during application construction.

- [ ] **Step 1: Add failing service and CLI parser tests.**

```python
def test_local_service_exports_to_nuc_data_directory_and_commits_ledger(...):
    manifest = service.export_full(exported_at=NOW)
    assert manifest.note_count == 3
    assert service.ledger.note_hashes().keys() == {101, 102, 103}
    assert (tmp_path / "snapshots" / "current.jsonl.gz").exists()


def test_cli_exposes_nuc_local_anki_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["anki-doctor"]).handler is anki_doctor
    assert parser.parse_args(["anki-snapshot", "--full"]).handler is anki_snapshot
```

- [ ] **Step 2: Run service tests and observe missing service/commands.**

Run: `python -m pytest tests/anki/test_local_service.py -q`

Expected: FAIL because the local service and CLI commands do not exist.

- [ ] **Step 3: Implement the service composition root.**

`LocalAnkiService` owns runtime, exporter, ledger, and snapshot paths. It performs a full export into a temporary file, atomically replaces `snapshots/current.jsonl.gz`, then updates the ledger only after the accepted file is durable.

Application construction creates clients and services but performs no Anki I/O. If `anki_enabled` is false or no executable path is configured, CLI commands exit with a clear configuration message.

- [ ] **Step 4: Implement CLI output.**

`anki-doctor` prints only safe health facts: AnkiConnect version, source-note count, and availability of `Text`/`Extra`. `anki-snapshot --full` prints the snapshot ID, note count, and NUC destination; neither command prints credentials or collection content.

- [ ] **Step 5: Run local service, CLI, and app regressions.**

Run: `python -m pytest tests/anki/test_local_service.py tests/v2/test_baseline_smoke.py tests/anki -q`

Expected: PASS.

- [ ] **Step 6: Commit local service integration.**

```bash
git add src/oms_hub/app.py src/oms_hub/cli.py src/oms_hub/anki/service.py tests
git commit -m "feat(anki): expose NUC-local doctor and snapshots"
```

## Task 7: Windows Installation, Release, and NUC Acceptance Documentation

**Files:**

- Modify: `scripts/install-windows.ps1`
- Modify: `scripts/build-v2-release.py`
- Modify: `tests/v2/test_release_package.py`
- Modify: `README.md`
- Create: `docs/anki-nuc-rollout.md`

**Interfaces:**

- Windows installation validates the NUC-local Anki settings when enabled.
- Release archives include all `src/oms_hub/anki/` runtime files and no Mac agent files.
- Documentation contains no Anki curation dependency on Tailscale.

- [ ] **Step 1: Add failing release-surface tests.**

```python
def test_release_contains_nuc_anki_runtime_and_no_mac_bridge(tmp_path):
    hotfix, source = builder.build_releases(root, tmp_path, "20260729")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
    assert "src/oms_hub/anki/ankiconnect.py" in names
    assert "src/oms_hub/anki/runtime.py" in names
    assert "src/oms_hub/anki/apply.py" in names
    assert not any(name.startswith("src/oms_anki_agent/") for name in names)
    assert not any(name.startswith("scripts/macos/") for name in names)
```

Add text assertions that the README's Anki section contains `127.0.0.1:8766`, `anki-doctor`, and `auto_sync`, and contains neither `tailscale serve` nor `LaunchAgent`.

- [ ] **Step 2: Run release tests and observe stale documentation/package surface.**

Run: `python -m pytest tests/v2/test_release_package.py -q`

Expected: FAIL on stale Mac/Tailscale README text and missing local modules in the hotfix allowlist.

- [ ] **Step 3: Update installation and release packaging.**

Include all tracked `src/oms_hub/anki/` files in the hotfix archive. Keep secret filters. Update `install-windows.ps1` to run `oms-hub anki-doctor` only when `OMS_HUB_ANKI_ENABLED=true` after `validate-config` succeeds.

- [ ] **Step 4: Replace the README bridge section and add the rollout guide.**

Document:

1. Install Anki Desktop, AnkiConnect, and AnkiHub on the NUC interactive account.
2. Set AnkiConnect bind address `127.0.0.1` and port `8766`.
3. Set AnkiHub `auto_sync` to `on_ankiweb_sync` and sign in to AnkiHub and AnkiWeb.
4. Configure the absolute `Anki.exe` path and enable Anki in `.env`.
5. Run `oms-hub anki-doctor` and `oms-hub anki-snapshot --full`.
6. Complete live apply, combined sync, Mac receipt, restart recovery, and rollback checks.
7. State explicitly that Tailscale is not used by Anki curation.

- [ ] **Step 5: Run release and configuration tests.**

Run: `python -m pytest tests/v2/test_release_package.py tests/anki/test_settings.py tests/anki/test_local_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit deployment and operations changes.**

```bash
git add README.md docs/anki-nuc-rollout.md scripts/install-windows.ps1 scripts/build-v2-release.py tests/v2/test_release_package.py
git commit -m "docs(anki): add NUC-only rollout"
```

## Task 8: Whole-Repository Verification and Migration Audit

**Files:**

- Modify only files required by failures proven during this task.

**Interfaces:**

- Produces a branch with no Mac/Tailscale Anki dependency and a verified local runtime surface.

- [ ] **Step 1: Search for stale runtime references.**

Run:

```bash
rg -n -i 'oms_anki_agent|oms-anki-agent|anki_agent_|/agent/v1|tailscale serve|tailnet|LaunchAgent|connor-mac' . \
  -g '!docs/superpowers/specs/2026-07-27-anki-curation-integration-design.md' \
  -g '!docs/superpowers/plans/2026-07-27-anki-curation-integration.md' \
  -g '!docs/superpowers/specs/2026-07-29-nuc-local-anki-curation-design.md' \
  -g '!docs/superpowers/plans/2026-07-29-nuc-local-anki-curation.md'
```

Expected: no matches in runtime, deployment, current tests, or current README/rollout documentation.

- [ ] **Step 2: Run the complete Python test suite.**

Run: `python -m pytest -q`

Expected: PASS with zero failures and zero warning errors.

- [ ] **Step 3: Run static checks.**

Run:

```bash
python -m ruff check .
python -m mypy src/oms_hub
```

Expected: both exit zero.

- [ ] **Step 4: Build release archives and inspect contents.**

Run:

```bash
python scripts/build-v2-release.py
python -m zipfile -l dist/Study-Hub-V2-Source-20260728.zip
```

Expected: build exits zero; the archive contains the local Anki modules and contains no `oms_anki_agent` or `scripts/macos` path.

- [ ] **Step 5: Review the final diff against the design requirements.**

Run:

```bash
git diff origin/codex/anki-curation-foundation...HEAD --stat
git diff --check
git status --short
```

Expected: only the approved design/plan and NUC-local implementation changes are present; whitespace check passes; worktree is clean after the task commits.

- [ ] **Step 6: Commit any test-proven integration corrections.**

If Step 2 or Step 3 required a code correction, first add a focused failing regression test, implement the minimal correction, rerun the focused and complete checks, then commit only those files:

```bash
git add -u src/oms_hub tests
git commit -m "fix(anki): complete NUC-local integration"
```

If no correction was required, do not create an empty commit.
