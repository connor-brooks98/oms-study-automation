"""Actual-process, loopback-only A0 rehearsal harness.

This module deliberately keeps its control plane outside FastAPI: the Hub is
always exercised through a recorded trusted Python interpreter and HTTP.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import UUID

from sqlalchemy import func, select

from oms_hub.anki.contracts import (
    AddNotesOperation,
    AddTagsOperation,
    CreateCurationJobRequest,
    RemoveTagsOperation,
)
from oms_hub.anki.domain import (
    ApplyState,
    CurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
)
from oms_hub.anki.models import (
    AnkiEnvelopeModel,
    AnkiReviewChangeSetModel,
    AnkiReviewedReconciliationModel,
)
from oms_hub.anki.pipeline import StageArtifactStore, pipeline_stages
from oms_hub.anki.provider_attempts import (
    ProviderAttemptIndeterminate,
    replay_namespace_from_job_source,
)
from oms_hub.anki.rehearsal.capsule import (
    CapsuleIntegrityError,
    CapsuleManifest,
    _reject_sensitive_path,
    verify_capsule,
)
from oms_hub.anki.rehearsal.capture import (
    CaptureAuthorization,
    CaptureStore,
    _is_indirect,
    evidence_redact,
    serialize_evidence_record,
)
from oms_hub.anki.rehearsal.materialize import MaterializedCapsule, materialize_capsule
from oms_hub.anki.repository import AnkiCurationRepository, _validate_provider_event_append
from oms_hub.db import Database
from oms_hub.llm.anthropic import AnthropicProvider
from oms_hub.llm.gemini import GeminiProvider
from oms_hub.llm.openai import OpenAIProvider
from oms_hub.llm.openrouter import OpenRouterProvider

Mode = Literal["deterministic", "shadow"]
RunGoal = Literal["golden", "first_replay_miss", "capture"]
ProviderCheckpoint = Literal["begun", "dispatched", "response_received", "terminal"]
_FAILURE_INJECTION_STAGES = frozenset(
    {
        CurationStage.CARD_LEDGER,
        CurationStage.CARD_PREFILTER,
        CurationStage.CARD_FAST_CLASSIFY,
        CurationStage.CARD_CLASSIFY,
        CurationStage.CARD_RESIDUAL,
        CurationStage.CARD_GAP_FILL,
        CurationStage.DEDUPE,
    }
)
_SECRET_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "CREDENTIAL",
    "AUTHORIZATION",
)
_MAX_BODY = 4096
_REPLAY_MANIFEST_NAME = "replay-supplement.json"
_STRUCTURED_EMPTY_PACK_ERROR = re.compile(r"missing structured replay response ([0-9a-f]{64})\Z")
_VECTOR_EMPTY_PACK_ERROR = "replay vector validation failed"
_RUNTIME_DEPENDENCY_MODULES = ("fastapi", "sqlalchemy", "starlette", "uvicorn")
_WINDOWS_RUNTIME_PROBE = """import json, sys
payload = {'base_executable': sys._base_executable, 'version': sys.version}
print(json.dumps(payload))
"""
_WINDOWS_CHILD_CAPABILITY_PROBE = """import _overlapped
import asyncio
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path
paths = [Path(value).resolve() for value in json.loads(sys.argv[1])]
sys.path[:0] = [str(path) for path in paths]
dependencies = {}
for name in ('fastapi', 'sqlalchemy', 'starlette', 'uvicorn'):
    module = importlib.import_module(name)
    origin = Path(module.__file__).resolve()
    if not any(origin.is_relative_to(path) for path in paths):
        raise RuntimeError(f'{name} imported outside trusted dependency paths')
    dependencies[name] = {
        'origin': str(origin),
        'version': importlib.metadata.version(name),
    }
print(json.dumps({'schema_version': 1, 'dependencies': dependencies}, sort_keys=True))
"""
_VERIFIED_SOURCE_BOOTSTRAP = """import hashlib, importlib, importlib.metadata, json, os
import signal, subprocess, sys, sysconfig, time
from pathlib import Path
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.ignore_environment):
    raise RuntimeError('verified source bootstrap requires Python -I -S')
source = Path(sys.argv[1]).resolve()
repository = Path(sys.argv[2]).resolve()
expected_commit, expected_tree, run_nonce = sys.argv[3:6]
attestation = Path(sys.argv[6]).resolve()
startup_ready = Path(sys.argv[7]).resolve()
startup_release = Path(sys.argv[8]).resolve()
probed_dependency_paths = json.loads(sys.argv[9])
if not source.is_dir(): raise RuntimeError('verified implementation source is unavailable')
if not repository.is_dir(): raise RuntimeError('verified implementation repository is unavailable')
if source != (repository / 'src').resolve():
    raise RuntimeError('verified source is not the implementation source tree')
if os.name == 'nt':
    # The parent binds this interpreter handle to a KILL_ON_JOB_CLOSE job before
    # releasing it to inspect/import application code.  SIGBREAK makes the
    # normal parent shutdown path graceful instead of relying on job closure.
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
    startup_ready.parent.mkdir(parents=True, exist_ok=True)
    ready = {'schema_version': 1, 'pid': os.getpid(), 'run_nonce': run_nonce}
    temporary = startup_ready.with_suffix(startup_ready.suffix + '.tmp')
    temporary.write_text(json.dumps(ready, sort_keys=True), encoding='utf-8')
    os.replace(temporary, startup_ready)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            release = json.loads(startup_release.read_text(encoding='utf-8'))
        except FileNotFoundError:
            release = None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError('parent startup release is invalid') from exc
        if release == ready:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError('parent did not bind runtime to its job before startup deadline')
def git(*args):
    result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True, text=True)
    if result.returncode: raise RuntimeError('child Git identity cannot be verified')
    return result.stdout.strip()
if git('rev-parse', 'HEAD') != expected_commit or git('rev-parse', 'HEAD^{tree}') != expected_tree:
    raise RuntimeError('child Git identity changed before import')
if git('status', '--porcelain'):
    raise RuntimeError('child implementation repository is dirty before import')
files = {}
for relative in git('ls-files', '--', 'src').splitlines():
    path = repository / relative
    if not path.is_file() or path.is_symlink():
        raise RuntimeError('child source tree is unavailable')
    files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
source_tree_sha256 = hashlib.sha256(
    json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
).hexdigest()
if not isinstance(probed_dependency_paths, list) or not all(
    isinstance(path, str) for path in probed_dependency_paths
):
    raise RuntimeError('trusted dependency-path probe is invalid')
dependency_paths = []
for value in probed_dependency_paths:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise RuntimeError('trusted dependency path is unavailable')
    if value not in dependency_paths:
        dependency_paths.append(value)
sys.path[:0] = [str(source), *dependency_paths]
import oms_hub
import oms_hub.cli
runtime_dependencies = {}
for name in ('fastapi', 'sqlalchemy', 'starlette', 'uvicorn'):
    module = importlib.import_module(name)
    origin = Path(module.__file__).resolve()
    if not any(origin.is_relative_to(Path(value).resolve()) for value in dependency_paths):
        raise RuntimeError(f'{name} imported outside trusted dependency paths')
    runtime_dependencies[name] = {
        'origin': str(origin),
        'version': importlib.metadata.version(name),
    }
def under(path):
    try: Path(path).resolve().relative_to(source); return True
    except ValueError: return False
if not under(oms_hub.__file__) or not under(oms_hub.cli.__file__):
    raise RuntimeError('Hub import is outside verified implementation source')
attestation.parent.mkdir(parents=True, exist_ok=True)
modules = {'oms_hub': str(Path(oms_hub.__file__).resolve())}
modules['oms_hub.cli'] = str(Path(oms_hub.cli.__file__).resolve())
payload = {
    'source': str(source), 'repository': str(repository), 'python_executable': sys.executable
}
payload['python_version'] = sys.version
payload['pid'] = os.getpid()
payload['run_nonce'] = run_nonce
payload['commit'] = expected_commit
payload['tree'] = expected_tree
payload['source_files'] = files
payload['source_tree_sha256'] = source_tree_sha256
payload['modules'] = modules
payload['isolated'] = bool(sys.flags.isolated)
payload['no_site'] = bool(sys.flags.no_site)
payload['ignore_environment'] = bool(sys.flags.ignore_environment)
payload['bootstrap_dependency_paths'] = dependency_paths
payload['runtime_dependencies'] = runtime_dependencies
payload['child_cwd'] = str(Path.cwd().resolve())
attestation.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
server_arguments = oms_hub.cli.build_parser().parse_args(['serve'])
raise SystemExit(server_arguments.handler(server_arguments))
"""


@dataclass(frozen=True, slots=True)
class RehearsalRequest:
    capsule: Path
    overlay: Path
    mode: Mode
    port: int
    evidence_zip: Path
    failed_job_id: UUID
    expected_manifest_sha256: str
    implementation_repository: Path
    expected_implementation_commit: str
    expected_implementation_tree: str
    trusted_python: Path
    trusted_dependency_paths: tuple[Path, ...] = ()
    shadow_egress_pins_json: str | None = None
    timeout_seconds: float = 300.0
    restart_after_durable_boundary: bool = True
    runtime_evidence_nonce: str | None = None
    replay_supplement: Path | None = None
    expected_replay_supplement_manifest_sha256: str | None = None
    replay_supplement_completion: Path | None = None
    expected_replay_supplement_completion_sha256: str | None = None
    failure_injection: tuple[CurationStage, ProviderCheckpoint] | None = None
    run_goal: RunGoal = "golden"
    capture_store: Path | None = None
    capture_authorization_manifest: Path | None = None
    expected_capture_authorization_manifest_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    runtime_pid: int | None
    started_at: str
    ended_at: str | None
    exit_code: int | None
    argv: tuple[str, ...]


class _WindowsJob:
    """Parent-owned Windows Job Object for one attested interpreter tree."""

    def __init__(self, api: Any, handle: int) -> None:
        self._api = api
        self._handle = handle
        self._closed = False

    @classmethod
    def create(cls) -> _WindowsJob:
        api = _windows_job_api()
        return cls(api, api.create_kill_on_close_job())

    @property
    def handle(self) -> int:
        return self._handle

    @property
    def closed(self) -> bool:
        return self._closed

    def assign_process_handle(self, process_handle: int) -> None:
        if self._closed:
            raise RuntimeError("Windows runtime job is already closed")
        self._api.assign_process_handle(self._handle, process_handle)

    def active_processes(self) -> int:
        if self._closed:
            return 0
        return cast(int, self._api.active_processes(self._handle))

    def send_ctrl_break(self, process_group_id: int) -> None:
        if self._closed:
            raise RuntimeError("Windows runtime job is already closed")
        self._api.send_ctrl_break(process_group_id)

    def terminate(self) -> None:
        if self._closed:
            raise RuntimeError("Windows runtime job is already closed")
        self._api.terminate_job(self._handle)

    def close(self) -> None:
        if not self._closed:
            self._api.close_handle(self._handle)
            self._closed = True


@dataclass(slots=True)
class _Child:
    process: subprocess.Popen[bytes]
    observation: ProcessObservation
    stdout_path: Path
    stderr_path: Path
    stdout: BinaryIO
    stderr: BinaryIO
    runtime_pid: int | None = None
    job: _WindowsJob | None = None
    attestation_path: Path | None = None
    startup_ready_path: Path | None = None
    startup_release_path: Path | None = None


@dataclass(slots=True)
class RehearsalResult:
    job_id: UUID
    overlay: Path
    evidence_zip: Path
    timeline: list[dict[str, Any]] = field(default_factory=list)
    local_execution_evidence: str = "Local actual-process rehearsal execution performed"
    native_gate_complete: bool = False
    missing_evidence: tuple[str, ...] = (
        "Native NUC/Windows PowerShell capsule export pending",
        "Native NUC/Windows capsule execution pending",
    )
    run_goal: RunGoal = "golden"
    outcome: Literal["golden_success", "expected_replay_miss", "capture_success"] = "golden_success"
    expected_replay_miss: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FailureInjectionResult:
    stage: CurationStage
    checkpoint: ProviderCheckpoint
    result: Literal["resumed_without_duplicate", "fail_closed_manual", "not_applicable"]
    evidence_zip: Path


def run_failure_injection_matrix(request: RehearsalRequest) -> tuple[FailureInjectionResult, ...]:
    """Run every durable provider boundary in a separate fresh overlay.

    This function performs no work until an operator supplies a verified
    capsule request.  ``dispatched`` is deliberately a fail-closed/manual
    result: it proves that an indeterminate provider call cannot be replayed.
    """
    if request.failure_injection is not None:
        raise ValueError("matrix request must not preselect a checkpoint")
    matrix_evidence = request.evidence_zip.with_name(
        f"{request.evidence_zip.stem}-failure-injection-matrix.json"
    )
    if matrix_evidence.exists():
        raise ValueError("failure-injection matrix evidence destination already exists")
    results: list[FailureInjectionResult] = []
    for stage in _failure_injection_stage_order():
        for checkpoint in ("begun", "dispatched", "response_received", "terminal"):
            suffix = f"{stage.value}-{checkpoint}"
            child_request = replace(
                request,
                overlay=request.overlay.with_name(f"{request.overlay.name}-{suffix}"),
                evidence_zip=request.evidence_zip.with_name(
                    f"{request.evidence_zip.stem}-{suffix}{request.evidence_zip.suffix}"
                ),
                failure_injection=(stage, checkpoint),
            )
            try:
                ProcessRehearsal(child_request).run()
                outcome: Literal[
                    "resumed_without_duplicate", "fail_closed_manual", "not_applicable"
                ] = "resumed_without_duplicate" if checkpoint == "begun" else "not_applicable"
            except ProviderAttemptIndeterminate:
                if checkpoint == "begun":
                    raise
                outcome = "fail_closed_manual"
            if not child_request.evidence_zip.is_file():
                raise RuntimeError("failure-injection run did not package its evidence ZIP")
            _verify_evidence_zip(child_request.evidence_zip)
            results.append(
                FailureInjectionResult(stage, checkpoint, outcome, child_request.evidence_zip)
            )
    matrix_evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner": "ProcessRehearsal.run",
                "execution_kind": "actual_hub_process_rehearsal",
                "actual_process_evidence_required": True,
                "checkpoint_policy": {
                    "begun": "auto_resume_only_after_stable_logical_identity_comparison",
                    "dispatched": "fail_closed_manual",
                    "response_received": "fail_closed_manual_redacted_response_not_reusable",
                    "terminal": "fail_closed_manual_unproven_completed_stage_recognition",
                },
                "results": [
                    {
                        "stage": result.stage.value,
                        "matrix_stage": _failure_injection_stage_label(result.stage),
                        "checkpoint": result.checkpoint,
                        "result": result.result,
                        "evidence_zip": str(result.evidence_zip),
                    }
                    for result in results
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return tuple(results)


class LoopbackHttp:
    """Small HTTP client which retains only the CSRF cookie in memory."""

    def __init__(self, port: int) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self.cookies = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.transcript: list[dict[str, Any]] = []
        self.csrf_token: str | None = None
        self.capture_capability: str | None = None

    def set_capture_capability(self, capability: str) -> None:
        if len(capability) < 32:
            raise ValueError("capture capability is unavailable")
        self.capture_capability = capability

    def bootstrap_csrf(self) -> Any:
        _, health = self.request("GET", "/health")
        for cookie in self.cookies:
            if cookie.name == "study_hub_csrf":
                self.csrf_token = cookie.value
                return health
        raise RuntimeError("loopback GET did not issue a CSRF cookie")

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        encoded = None if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
        headers = {"Accept": "application/json"}
        if self.capture_capability is not None:
            headers["X-OMS-Capture-Capability"] = self.capture_capability
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD"}:
            if self.csrf_token is None:
                raise RuntimeError("CSRF bootstrap is required before unsafe requests")
            headers["X-CSRF-Token"] = self.csrf_token
        request = Request(self.base_url + path, data=encoded, headers=headers, method=method)
        status: int
        raw: bytes
        try:
            with self._opener.open(request, timeout=10) as response:
                status = response.status
                raw = response.read()
        except HTTPError as error:
            status = error.code
            raw = error.read()
        payload = _decode_json(raw)
        self.transcript.append(
            {
                "method": method,
                "path": path,
                "request_body": _bounded(_redact(body)),
                "status": status,
                "response_body": _bounded(_redact(payload)),
            }
        )
        return status, payload


class ProcessRehearsal:
    def __init__(
        self,
        request: RehearsalRequest,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        http_factory: Callable[[int], LoopbackHttp] = LoopbackHttp,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.request = request
        self._popen = popen
        self._http_factory = http_factory
        self._clock = clock
        self._children: list[_Child] = []
        self._timeline: list[dict[str, Any]] = []
        self._runtime_evidence_nonce = request.runtime_evidence_nonce or secrets.token_urlsafe(32)
        self._source_attestation: dict[str, Any] | None = None
        self._source_tree_sha256: str | None = None
        self._windows_runtime_identity: dict[str, Any] | None = None
        self._failure_injection_consumed = False
        self._capture_authorization: CaptureAuthorization | None = None
        self._capture_store: CaptureStore | None = None
        self._capture_capability = (
            secrets.token_urlsafe(32) if request.run_goal == "capture" else None
        )

    def run(self) -> RehearsalResult:
        manifest = self._validate_destinations()
        if self.request.run_goal == "capture":
            self._prepare_capture_store()
        overlay = materialize_capsule(self.request.capsule, self.request.overlay)
        database = Database(f"sqlite:///{overlay.database_path}")
        try:
            self._record("capsule_materialized", overlay=str(overlay.root))
            repository = AnkiCurationRepository(database)
            if (
                self.request.run_goal == "golden"
                and self.request.replay_supplement is not None
                and _supplement_is_populated(self.request.replay_supplement)
            ):
                self._verify_populated_replay_completion(
                    repository.require_job(self.request.failed_job_id)
                )
            self._install_replay_supplement(overlay)
            if self.request.run_goal == "first_replay_miss":
                _validate_empty_overlay_replay(overlay.root)
            client = self._start_and_connect(overlay, manifest)
            failed = repository.require_job(self.request.failed_job_id)
            if self.request.run_goal == "capture":
                self._validate_capture_namespace(failed)
                self._validate_capture_job_routes(failed, repository)
            payload = (
                fresh_job_payload(failed, live_capture=True)
                if self.request.run_goal == "capture"
                else fresh_job_payload(failed)
            )
            status, created = client.request("POST", "/api/anki/jobs", payload)
            if (
                status != 201
                or not isinstance(created, dict)
                or not isinstance(created.get("id"), str)
            ):
                raise RuntimeError(f"fresh curation job was rejected: HTTP {status}")
            job_id = UUID(created["id"])
            self._record("job_created", job_id=str(job_id), source_failed_job_id=str(failed.id))
            before_logical_identity: set[tuple[object, ...]] | None = None
            precrash_event_id_cutoff: int | None = None
            fault_interlock: dict[str, Any] | None = None
            if self.request.failure_injection is not None:
                stage, checkpoint = self.request.failure_injection
                interlock = self._wait_for_child_interlock(
                    repository, job_id, stage, checkpoint, overlay
                )
                self._validate_fault_cutoff(repository, job_id, interlock)
                precrash_rows = repository.list_provider_attempt_events(job_id)
                before_logical_identity = _stable_logical_call_ids(
                    precrash_rows, _replay_namespace_for_job(repository.require_job(job_id))
                )
                precrash_event_id_cutoff = _provider_event_id_cutoff(precrash_rows)
                fault_interlock = interlock
                # Pause is intentionally child-local.  Once its post-commit
                # interlock is present, terminate hard so a provider-blocked
                # worker cannot retain the parent indefinitely.
                self._stop_latest(hard=True)
                adapter_ledger, egress_ledger = self._validate_crash_interlock_evidence(
                    overlay, interlock
                )
                if checkpoint != "begun":
                    self._write_failure_evidence(
                        manifest,
                        overlay,
                        client,
                        repository,
                        job_id,
                        interlock,
                        adapter_ledger,
                        egress_ledger,
                    )
                    _verify_evidence_zip(self.request.evidence_zip)
                    raise ProviderAttemptIndeterminate(
                        f"durable {checkpoint} provider boundary requires manual recovery"
                    )
                self._consume_failure_injection(overlay, interlock)
                client = self._start_and_connect(overlay, manifest)
                restarted_pid = self._children[-1].runtime_pid
                if restarted_pid is None:
                    raise RuntimeError("restarted child lacks an attested runtime PID")
                if restarted_pid == interlock["pid"]:
                    raise RuntimeError("failure-injection recovery did not start a new child PID")
                self._record(
                    "failure_injection_restart",
                    stage=stage.value,
                    checkpoint=checkpoint,
                    stopped_child_pid=interlock["pid"],
                    restarted_child_pid=restarted_pid,
                    recovery="begun_only_pending_terminal_identity_comparison",
                )
                repository.require_no_indeterminate_provider_attempt(job_id, stage)
            elif self.request.restart_after_durable_boundary:
                observed = self._wait_for_durable_boundary(client, repository, job_id)
                self._restart_after_durable_boundary(repository, job_id, observed, overlay=overlay)
                client = self._start_and_connect(overlay, manifest)
                self._record("process_restarted", job_id=str(job_id))
            final = self._poll(client, job_id, terminal=True)
            if self.request.run_goal == "first_replay_miss":
                miss = self._validate_expected_replay_miss(final, repository, job_id)
                self._assert_smoke_http_transcript(client, job_id)
                # The partial smoke claim is only made after every child has
                # exited.  Do not let _stop_child validate a ledger while a
                # sibling child could still be running.
                self._stop_all()
                adapter_ledger, egress_ledger = self._validate_runtime_evidence(overlay)
                self._validate_expected_replay_miss_runtime(adapter_ledger, egress_ledger)
                self._write_expected_replay_miss_evidence(
                    manifest,
                    overlay,
                    client,
                    repository,
                    job_id,
                    final,
                    miss,
                    adapter_ledger,
                    egress_ledger,
                )
                _verify_evidence_zip(self.request.evidence_zip)
                return RehearsalResult(
                    job_id,
                    overlay.root,
                    self.request.evidence_zip,
                    self._timeline,
                    run_goal="first_replay_miss",
                    outcome="expected_replay_miss",
                    expected_replay_miss=miss,
                )
            if self.request.run_goal == "capture":
                if final.get("state") != "ready_for_review":
                    raise RuntimeError(
                        f"capture job did not reach READY_FOR_REVIEW: {final.get('state')!r}"
                    )
                review: dict[str, Any] | None = None
                apply: dict[str, Any] | None = None
                is_v3 = (
                    repository.require_job(job_id).pipeline_contract_version
                    is PipelineContractVersion.CARD_CENTRIC_V3
                )
                if is_v3:
                    review_status, review_body = client.request(
                        "GET", f"/api/anki/jobs/{job_id}/review"
                    )
                    if review_status != 200 or not isinstance(review_body, dict):
                        raise RuntimeError("v3 capture review evidence is unavailable")
                    review = review_body
                    apply_status, apply_body = client.request(
                        "POST",
                        f"/api/anki/jobs/{job_id}/apply",
                        {"review_revision": 0, "confirmation": "APPLY TO ANKI"},
                    )
                    if apply_status != 423 or not isinstance(apply_body, dict):
                        raise RuntimeError("v3 capture apply boundary did not return 423")
                    apply = apply_body
                    self._record("live_acceptance_apply_gate", apply_status=apply_status)
                self._assert_capture_http_transcript(client, job_id, v3=is_v3)
                self._stop_all(overlay)
                adapter_ledger, egress_ledger = self._validate_runtime_evidence(overlay)
                self._validate_capture_runtime(adapter_ledger, egress_ledger)
                server_audit = self._assert_capture_server_audit(client, job_id, v3=is_v3)
                self._validate_capture_ready_for_review_state(repository, job_id)
                self._write_capture_completion(
                    manifest,
                    overlay,
                    client,
                    repository,
                    job_id,
                    adapter_ledger,
                    egress_ledger,
                    server_audit,
                    review=review,
                    apply=apply,
                    apply_status=423 if is_v3 else None,
                )
                native_gate_complete = is_v3 and _is_windows()
                return RehearsalResult(
                    job_id,
                    overlay.root,
                    self.request.evidence_zip,
                    self._timeline,
                    local_execution_evidence=(
                        "NUC-native actual-process live acceptance performed"
                        if native_gate_complete
                        else "Local actual-process rehearsal execution performed"
                    ),
                    native_gate_complete=native_gate_complete,
                    missing_evidence=(
                        ()
                        if native_gate_complete
                        else (
                            "Native NUC/Windows PowerShell capsule export pending",
                            "Native NUC/Windows capsule execution pending",
                        )
                    ),
                    run_goal="capture",
                    outcome="capture_success",
                )
            self._assert_provider_ledger_is_restart_safe(
                repository,
                job_id,
                expected_precrash_logical_identities=before_logical_identity,
                precrash_event_id_cutoff=precrash_event_id_cutoff,
                fault_interlock=fault_interlock,
            )
            if final.get("state") != "ready_for_review":
                raise RuntimeError(f"job did not reach READY_FOR_REVIEW: {final.get('state')!r}")
            review_status, review = client.request("GET", f"/api/anki/jobs/{job_id}/review")
            if review_status != 200 or not isinstance(review, dict):
                raise RuntimeError("review surface is unavailable")
            save_status, saved = client.request(
                "PUT", f"/api/anki/jobs/{job_id}/review", unchanged_review_payload(review)
            )
            if save_status != 200 or not isinstance(saved, dict):
                raise RuntimeError("unchanged review was rejected")
            revision = saved.get("revision")
            if not isinstance(revision, int):
                raise RuntimeError("review response did not return a revision")
            envelope_status, envelope = client.request(
                "POST", f"/api/anki/jobs/{job_id}/envelope", {"review_revision": revision}
            )
            self._validate_envelope_response(
                repository, job_id, revision, envelope_status, envelope
            )
            apply_status, apply = client.request(
                "POST",
                f"/api/anki/jobs/{job_id}/apply",
                {"review_revision": revision, "confirmation": "APPLY TO ANKI"},
            )
            if apply_status != 423:
                raise RuntimeError(f"rehearsal apply must return 423, received {apply_status}")
            self._record(
                "review_and_apply_gate", review_revision=revision, apply_status=apply_status
            )
            self._stop_all(overlay)
            adapter_ledger, egress_ledger = self._validate_runtime_evidence(overlay)
            self._write_evidence(
                manifest,
                overlay,
                client,
                repository,
                job_id,
                review,
                saved,
                envelope_status,
                envelope,
                apply,
                adapter_ledger,
                egress_ledger,
            )
            return RehearsalResult(
                job_id,
                overlay.root,
                self.request.evidence_zip,
                self._timeline,
            )
        except BaseException as primary_error:
            try:
                self._stop_all(overlay)
            except BaseException as cleanup_error:
                self._record("cleanup_failed", phase="run_failure", error=str(cleanup_error))
                primary_error.add_note(f"rehearsal cleanup failed: {cleanup_error}")
            finally:
                database.close()
            raise
        else:
            try:
                self._stop_all(overlay)
            finally:
                database.close()

    def _validate_destinations(self) -> CapsuleManifest:
        if os.path.lexists(self.request.overlay):
            raise ValueError("overlay destination must not already exist")
        if os.path.lexists(self.request.evidence_zip):
            raise ValueError("evidence destination must not already exist")
        if self.request.run_goal == "capture" and os.path.lexists(
            self.request.evidence_zip.with_suffix(".json")
        ):
            raise ValueError("live acceptance JSON destination must not already exist")
        if not 1024 <= self.request.port <= 65535:
            raise ValueError("rehearsal port must be in 1024..65535")
        if self.request.run_goal not in {"golden", "first_replay_miss", "capture"}:
            raise ValueError("rehearsal run goal is invalid")
        if self.request.mode == "shadow" and not self.request.shadow_egress_pins_json:
            raise ValueError("shadow rehearsal requires pinned egress JSON")
        if self.request.mode == "deterministic" and self.request.shadow_egress_pins_json:
            raise ValueError("deterministic rehearsal cannot configure external egress")
        self._verify_implementation_identity()
        observed_manifest_sha256 = _sha256_file(self.request.capsule / "capsule.json")
        if observed_manifest_sha256 != self.request.expected_manifest_sha256:
            raise ValueError(
                "capsule manifest SHA-256 does not match the operator-supplied identity"
            )
        manifest = verify_capsule(self.request.capsule)
        if (
            self.request.replay_supplement is None
            and self.request.expected_replay_supplement_manifest_sha256 is not None
        ):
            raise ValueError("replay supplement SHA-256 requires a replay supplement directory")
        if self.request.replay_supplement is not None:
            _verify_replay_supplement(
                self.request.replay_supplement,
                self.request.expected_replay_supplement_manifest_sha256,
            )
            if self.request.run_goal == "golden" and _supplement_is_populated(
                self.request.replay_supplement
            ):
                _verify_replay_completion(
                    self.request.replay_supplement_completion,
                    self.request.expected_replay_supplement_completion_sha256,
                    supplement_root=self.request.replay_supplement,
                    expected_manifest_sha256=self.request.expected_replay_supplement_manifest_sha256,
                    expected_commit=self.request.expected_implementation_commit,
                    expected_tree=self.request.expected_implementation_tree,
                    expected_capsule_sha256=observed_manifest_sha256,
                    expected_pack_sha256=self.request.expected_replay_supplement_manifest_sha256,
                )
        if self.request.mode == "deterministic" and self.request.replay_supplement is None:
            raise ValueError("deterministic rehearsal requires a verified replay supplement")
        if self.request.run_goal == "first_replay_miss":
            if self.request.mode != "deterministic":
                raise ValueError("first replay miss requires deterministic mode")
            if self.request.restart_after_durable_boundary:
                raise ValueError("first replay miss must disable restart")
            if self.request.failure_injection is not None:
                raise ValueError("first replay miss cannot use failure injection")
            if self.request.replay_supplement is None:
                raise ValueError("first replay miss requires a replay supplement")
            _validate_empty_replay_supplement(
                self.request.replay_supplement,
                self.request.expected_replay_supplement_manifest_sha256,
            )
            if (
                self.request.replay_supplement_completion is not None
                or self.request.expected_replay_supplement_completion_sha256 is not None
            ):
                raise ValueError("first replay miss forbids a completion manifest")
        if self.request.run_goal == "capture":
            if self.request.mode != "shadow":
                raise ValueError("capture requires shadow mode")
            if self.request.restart_after_durable_boundary:
                raise ValueError("capture must disable restart")
            if self.request.failure_injection is not None:
                raise ValueError("capture cannot use failure injection")
            if self.request.replay_supplement is not None:
                raise ValueError("capture creates, not consumes, a replay supplement")
            if (
                self.request.replay_supplement_completion is not None
                or self.request.expected_replay_supplement_completion_sha256 is not None
            ):
                raise ValueError("capture cannot consume a completion manifest")
            values = (
                self.request.capture_store,
                self.request.capture_authorization_manifest,
                self.request.expected_capture_authorization_manifest_sha256,
            )
            if any(value is None for value in values):
                raise ValueError(
                    "capture requires a private store and exact authorization manifest"
                )
            assert self.request.capture_store is not None
            if os.path.lexists(self.request.capture_store):
                raise ValueError("capture store destination must be absent")
            assert self.request.capture_authorization_manifest is not None
            assert self.request.expected_capture_authorization_manifest_sha256 is not None
            self._capture_authorization = CaptureAuthorization.load(
                self.request.capture_authorization_manifest,
                self.request.expected_capture_authorization_manifest_sha256,
                commit=self.request.expected_implementation_commit,
                tree=self.request.expected_implementation_tree,
                capsule_sha256=observed_manifest_sha256,
                failed_job_id=str(self.request.failed_job_id),
            )
            if self._capture_authorization.document["egress_pins"] != json.loads(
                self.request.shadow_egress_pins_json or "{}"
            ):
                raise ValueError("capture authorization egress pins do not match the launcher pins")
        self._validate_destination_topology()
        return manifest

    def _validate_destination_topology(self) -> None:
        outputs = [
            ("overlay", self.request.overlay),
            ("evidence", self.request.evidence_zip),
        ]
        if self.request.run_goal == "capture":
            outputs.append(("live acceptance JSON", self.request.evidence_zip.with_suffix(".json")))
        if self.request.capture_store is not None:
            outputs.append(("capture store", self.request.capture_store))
        canonical_outputs = [(name, _canonical_output_path(path)) for name, path in outputs]
        for index, (name, destination) in enumerate(canonical_outputs):
            for other_name, other in canonical_outputs[index + 1 :]:
                if _path_contains(destination, other) or _path_contains(other, destination):
                    raise ValueError(
                        f"{name} and {other_name} destinations must be distinct and non-nested"
                    )
        immutable_inputs = [
            self.request.capsule,
            self.request.implementation_repository,
        ]
        if self.request.replay_supplement is not None:
            immutable_inputs.append(self.request.replay_supplement)
        if self.request.capture_authorization_manifest is not None:
            immutable_inputs.append(self.request.capture_authorization_manifest)
        if self.request.replay_supplement_completion is not None:
            immutable_inputs.append(self.request.replay_supplement_completion)
        canonical_inputs = [_canonical_input_path(path) for path in immutable_inputs]
        for name, destination in canonical_outputs:
            if any(
                _path_contains(destination, source) or _path_contains(source, destination)
                for source in canonical_inputs
            ):
                raise ValueError(
                    f"{name} destination must not contain or be inside immutable input"
                )

    def _prepare_capture_store(self) -> None:
        if self._capture_authorization is None or self.request.capture_store is None:
            raise RuntimeError("capture authorization was not validated")
        store = CaptureStore(self.request.capture_store, self._capture_authorization)
        store.prepare()
        self._capture_store = store
        self._record(
            "capture_store_prepared", authorization_sha256=self._capture_authorization.sha256
        )

    def _validate_capture_namespace(self, job: CurationJob) -> None:
        if self._capture_authorization is None:
            raise RuntimeError("capture authorization is unavailable")
        actual = _replay_namespace_for_job(job)
        if actual != self._capture_authorization.document["replay_namespace"]:
            raise RuntimeError(
                "capture authorization replay namespace does not match the job source"
            )

    def _validate_capture_job_routes(
        self,
        job: CurationJob,
        repository: AnkiCurationRepository | None = None,
    ) -> None:
        if self._capture_authorization is None:
            raise RuntimeError("capture authorization is unavailable")
        config = job.resolved_model_config
        pipeline_version = getattr(
            job,
            "pipeline_contract_version",
            PipelineContractVersion.CARD_CENTRIC_V2,
        )
        if pipeline_version is PipelineContractVersion.CARD_CENTRIC_V3:
            stages = [
                config.scope_r3,
                config.cheap_classify_r7,
                config.thorough_classify_r7,
                config.generation_r9,
            ]
            if any(stage is None for stage in stages):
                raise RuntimeError("capture v3 structured routes are incomplete")
        else:
            stages = [config.ledger_s2, config.classify_s4, config.residual_s6, config.gap_fill_s7]
            if config.fast_classify_s4b is not None:
                stages.append(config.fast_classify_s4b)
        reachable = {
            (stage.provider, stage.model, _provider_endpoint(stage.provider, stage.model))
            for stage in stages
            if stage is not None
        }
        authorized = {
            (row["provider"], row["model"], row["endpoint"])
            for row in self._capture_authorization.document["structured"]
        }
        if reachable != authorized:
            raise RuntimeError(
                "capture authorization structured routes do not exactly close the frozen job plan"
            )
        if (
            pipeline_version is PipelineContractVersion.CARD_CENTRIC_V3
            and repository is not None
        ):
            if job.policy_sha256 is None:
                raise RuntimeError("capture v3 policy pin is unavailable")
            policy = repository.get_policy_by_sha256(job.policy_sha256)
            authorized_cost = self._capture_authorization.maxima["total_reserved_microusd"]
            if not (
                0 < authorized_cost <= policy.ordinary_cost_limit_microusd
                <= policy.hard_stop_cost_limit_microusd
            ):
                raise RuntimeError("capture cost authorization exceeds the frozen policy")

    @staticmethod
    def _validate_envelope_response(
        repository: AnkiCurationRepository,
        job_id: UUID,
        revision: int,
        status: int,
        body: Any,
    ) -> None:
        """Require a persisted, job-bound plan before proving apply is denied."""
        if status != 201 or not isinstance(body, dict):
            raise RuntimeError(f"envelope creation was rejected: HTTP {status}")
        required = {"job_id", "envelope_id", "payload_sha256", "summary", "reconciliation"}
        if set(body) != required or body.get("job_id") != str(job_id):
            raise RuntimeError("envelope response identity is malformed or mismatched")
        try:
            envelope_id = UUID(str(body["envelope_id"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("envelope response has an invalid envelope identity") from exc
        payload_sha256 = body.get("payload_sha256")
        if not isinstance(payload_sha256, str) or not _is_sha256(payload_sha256):
            raise RuntimeError("envelope response has an invalid payload digest")
        stored = repository.get_job_envelope(job_id)
        if (
            stored is None
            or stored.id != envelope_id
            or stored.job_id != job_id
            or stored.payload_sha256 != payload_sha256
        ):
            raise RuntimeError("envelope response is not bound to the persisted job plan")
        persisted = repository.get_envelope(envelope_id)
        if (
            getattr(persisted, "review_revision", None) != revision
            or getattr(persisted, "job_id", None) != job_id
        ):
            raise RuntimeError("envelope review identity does not match the saved review")
        expected_summary = _envelope_summary(persisted)
        if body.get("summary") != expected_summary:
            raise RuntimeError("envelope response summary does not match the persisted plan")
        reconciliation = body.get("reconciliation")
        if not isinstance(reconciliation, dict) or not reconciliation.get(
            "can_render_envelope", False
        ):
            raise RuntimeError("envelope response lacks renderable reconciliation evidence")
        persisted_reconciliation = repository.reviewed_reconciliation(job_id, revision)
        if not isinstance(persisted_reconciliation, dict):
            raise RuntimeError("persisted revision-bound reconciliation is unavailable")
        if _canonical_json(reconciliation) != _canonical_json(persisted_reconciliation):
            raise RuntimeError(
                "envelope reconciliation does not exactly match the persisted review"
            )
        if str(persisted_reconciliation.get("contract_version", "")) != getattr(
            persisted, "reconciliation_contract_version", None
        ):
            raise RuntimeError("envelope reconciliation does not match the persisted plan")
        expected_acknowledgement = (
            persisted_reconciliation.get("selection", {}).get("overflow_acknowledgement")
            if isinstance(persisted_reconciliation.get("selection"), dict)
            else None
        ) or {"required": False}
        if (
            getattr(persisted, "overflow_acknowledgement_provenance", None)
            != expected_acknowledgement
        ):
            raise RuntimeError("envelope acknowledgement is not bound to persisted reconciliation")

    def _install_replay_supplement(self, overlay: MaterializedCapsule) -> None:
        supplement = self.request.replay_supplement
        if supplement is None:
            return
        verified = _verify_replay_supplement(
            supplement,
            self.request.expected_replay_supplement_manifest_sha256,
        )
        replay = overlay.root / "replay"
        for relative in verified:
            destination = replay / relative
            if destination.exists() or destination.is_symlink():
                if relative != "structured.json" or not _is_empty_structured_placeholder(
                    destination
                ):
                    raise RuntimeError("fresh overlay replay destination is not empty")
                destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(supplement / relative, destination)
            # The source may have changed after the initial supplement audit.
            # Validate the copied bytes against the operator-bound manifest
            # before a child process can consume them.
            expected = _replay_supplement_entries(
                supplement,
                self.request.expected_replay_supplement_manifest_sha256,
            )[relative]
            if (
                destination.is_symlink()
                or destination.stat().st_size != expected["bytes"]
                or _sha256_file(destination) != expected["sha256"]
            ):
                raise RuntimeError("copied replay supplement bytes do not match operator manifest")
        self._record("replay_supplement_installed", files=len(verified))

    def _verify_populated_replay_completion(self, failed: CurationJob) -> None:
        """Bind a populated pack to the exact failed job in the disposable overlay."""
        supplement = self.request.replay_supplement
        if (
            self.request.run_goal != "golden"
            or supplement is None
            or not _supplement_is_populated(supplement)
        ):
            return
        _verify_replay_completion(
            self.request.replay_supplement_completion,
            self.request.expected_replay_supplement_completion_sha256,
            supplement_root=supplement,
            expected_manifest_sha256=self.request.expected_replay_supplement_manifest_sha256,
            expected_commit=self.request.expected_implementation_commit,
            expected_tree=self.request.expected_implementation_tree,
            expected_capsule_sha256=self.request.expected_manifest_sha256,
            expected_pack_sha256=self.request.expected_replay_supplement_manifest_sha256,
            expected_failed_job_id=str(self.request.failed_job_id),
            expected_replay_namespace=_replay_namespace_for_job(failed),
        )

    def _environment(
        self, overlay: MaterializedCapsule, manifest: CapsuleManifest
    ) -> dict[str, str]:
        anki = overlay.root / "anki"
        replay = overlay.root / "replay"
        try:
            repository_root = overlay.root / manifest.logical_roots["repository"]
            data = overlay.root / manifest.logical_roots["a0data"]
        except KeyError as exc:
            raise ValueError(f"capsule has no materialized {exc.args[0]} root") from exc
        if not data.is_dir() or data.is_symlink():
            raise ValueError("materialized a0data root is unavailable")
        study = data / "study-root"
        icloud_staging = data / "icloud-staging"
        prompt_directory = repository_root / "src" / "oms_hub" / "anki" / "prompt_assets"
        if not prompt_directory.is_dir():
            raise ValueError("materialized repository prompt assets are unavailable")
        data.mkdir(parents=True, exist_ok=True)
        anki.mkdir(parents=True, exist_ok=True)
        replay.mkdir(parents=True, exist_ok=True)
        study.mkdir(parents=True, exist_ok=True)
        icloud_staging.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "OMS_HUB_DATABASE_URL": f"sqlite:///{overlay.database_path}",
            "OMS_HUB_DATA_DIR": str(data),
            "OMS_HUB_STUDY_ROOT": str(study),
            "OMS_HUB_ICLOUD_STAGING_ROOT": str(icloud_staging),
            "OMS_HUB_ANKI_DATA_DIR": str(anki),
            "OMS_HUB_ANKI_ENABLED": "true",
            "OMS_HUB_ANKI_REHEARSAL_MODE": self.request.mode,
            "OMS_HUB_ANKI_REHEARSAL_OVERLAY_DIR": str(overlay.root),
            "OMS_HUB_ANKI_REHEARSAL_RUN_NONCE": self._runtime_evidence_nonce,
            "OMS_HUB_ANKI_REHEARSAL_SOURCE_ROOT": str(
                self.request.implementation_repository.resolve() / "src"
            ),
            "OMS_HUB_ANKI_REHEARSAL_SOURCE_TREE_SHA256": self._required_source_tree_sha256(),
            "OMS_HUB_ANKI_REHEARSAL_SOURCE_COMMIT": self.request.expected_implementation_commit,
            "OMS_HUB_ANKI_REHEARSAL_SOURCE_TREE": self.request.expected_implementation_tree,
            "OMS_HUB_ANKI_REHEARSAL_REPLAY_DIR": str(replay),
            "OMS_HUB_ANKI_PROMPT_DIRECTORY": str(prompt_directory),
            "OMS_HUB_ANKI_PROMPT_GIT_SYNC": "false",
            "OMS_HUB_DASHBOARD_HOST": "127.0.0.1",
            "OMS_HUB_DASHBOARD_PORT": str(self.request.port),
            "OMS_HUB_ANKI_WORKER_POLL_SECONDS": "0.5",
            "OMS_HUB_ANKI_CONNECT_URL": "http://127.0.0.1:8765",
            "OMS_HUB_PUBLIC_HOSTNAME": "",
            "OMS_HUB_ANKI_AGENT_HOSTNAME": "",
            "VOYAGE_API_KEY": "",
            "OMS_HUB_CLOUDFLARE_ACCESS_ISSUER": "",
            "OMS_HUB_CLOUDFLARE_ACCESS_AUDIENCE": "",
            "OMS_HUB_CLOUDFLARE_ACCESS_ALLOWED_EMAIL": "",
        }
        if self.request.mode == "shadow":
            env["OMS_HUB_ANKI_REHEARSAL_EGRESS_PINS_JSON"] = (
                self.request.shadow_egress_pins_json or ""
            )
        if self.request.run_goal == "capture":
            if (
                self._capture_authorization is None
                or self.request.capture_store is None
                or self.request.capture_authorization_manifest is None
                or self.request.expected_capture_authorization_manifest_sha256 is None
            ):
                raise RuntimeError("capture environment lacks validated authorization")
            capture_store = str(self.request.capture_store)
            capture_manifest = str(
                self.request.capture_authorization_manifest.resolve(strict=True)
            )
            capture_sha256 = self.request.expected_capture_authorization_manifest_sha256
            capture_commit = self.request.expected_implementation_commit
            capture_tree = self.request.expected_implementation_tree
            capsule_sha256 = self.request.expected_manifest_sha256
            failed_job_id = str(self.request.failed_job_id)
            env.update(
                {
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_STORE": capture_store,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_AUTHORIZATION_MANIFEST": capture_manifest,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_AUTHORIZATION_SHA256": capture_sha256,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_CANDIDATE_COMMIT": capture_commit,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_CANDIDATE_TREE": capture_tree,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_CAPSULE_MANIFEST_SHA256": capsule_sha256,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_FAILED_JOB_ID": failed_job_id,
                    "OMS_HUB_ANKI_REHEARSAL_CAPTURE_CAPABILITY": self._capture_capability or "",
                }
            )
        if self.request.failure_injection is not None and not self._failure_injection_consumed:
            stage, event = self.request.failure_injection
            env.update(
                {
                    "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE": stage.value,
                    "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT": event,
                    "OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE": "1",
                    "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR": str(
                        overlay.root / "rehearsal" / "runtime-evidence"
                    ),
                    # Dispatched is deliberately an uncatchable child-side
                    # cutoff.  Other durable boundaries wait for this parent
                    # to terminate the paused child.
                    "OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION": (
                        "hard_exit" if event == "dispatched" else "pause"
                    ),
                }
            )
        if _is_windows():
            env["SYSTEMROOT"] = _windows_system_root()
        return env

    def _consume_failure_injection(
        self, overlay: MaterializedCapsule, interlock: dict[str, Any]
    ) -> None:
        """Archive first-child proof before launching an unarmed replacement."""
        source = overlay.root / "rehearsal" / "runtime-evidence" / "provider-fault-interlock.json"
        archived = source.with_name("provider-fault-interlock.initial.json")
        if not source.is_file() or archived.exists():
            raise RuntimeError("failure-injection interlock cannot be consumed safely")
        try:
            persisted = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("failure-injection interlock cannot be consumed safely") from exc
        if persisted != interlock:
            raise RuntimeError("failure-injection interlock changed before restart")
        source.replace(archived)
        self._failure_injection_consumed = True
        self._record(
            "failure_injection_disarmed_for_restarted_child",
            archived_interlock=archived.name,
            stopped_child_pid=interlock["pid"],
        )

    def _command(
        self,
        *,
        attestation_path: Path | None = None,
        startup_ready_path: Path | None = None,
        startup_release_path: Path | None = None,
        runtime_executable: Path | None = None,
    ) -> list[str]:
        attestation_path = attestation_path or self._attestation_path()
        startup_ready_path = startup_ready_path or self._startup_ready_path(0)
        startup_release_path = startup_release_path or self._startup_release_path(0)
        return [
            str((runtime_executable or self.request.trusted_python).resolve()),
            "-I",
            "-S",
            "-B",
            "-c",
            _VERIFIED_SOURCE_BOOTSTRAP,
            str(self.request.implementation_repository.resolve() / "src"),
            str(self.request.implementation_repository.resolve()),
            self.request.expected_implementation_commit,
            self.request.expected_implementation_tree,
            self._runtime_evidence_nonce,
            str(attestation_path),
            str(startup_ready_path),
            str(startup_release_path),
            _canonical_json(
                self._windows_runtime_identity["dependency_paths"]
                if self._windows_runtime_identity is not None
                else _bootstrap_dependency_paths()
            ),
        ]

    def _attestation_path(self) -> Path:
        return (
            self.request.overlay / "rehearsal" / "runtime-evidence" / "implementation-source.json"
        )

    def _startup_ready_path(self, serial: int) -> Path:
        return (
            self.request.overlay / "rehearsal" / "runtime-evidence" / f"startup-{serial}.ready.json"
        )

    def _startup_release_path(self, serial: int) -> Path:
        return (
            self.request.overlay
            / "rehearsal"
            / "runtime-evidence"
            / f"startup-{serial}.release.json"
        )

    def _child_attestation_path(self, serial: int) -> Path:
        return (
            self.request.overlay
            / "rehearsal"
            / "runtime-evidence"
            / f"implementation-source-{serial}.json"
        )

    def _verify_implementation_identity(self) -> None:
        repository = self.request.implementation_repository.resolve()
        if not repository.is_dir():
            raise ValueError("implementation repository is unavailable")
        trusted_python = self.request.trusted_python.resolve()
        if not trusted_python.is_file() or not os.access(trusted_python, os.X_OK):
            raise ValueError("trusted Python interpreter is unavailable or not executable")
        commit = self._git_output(repository, "rev-parse", "HEAD")
        tree = self._git_output(repository, "rev-parse", "HEAD^{tree}")
        dirty = self._git_output(repository, "status", "--porcelain")
        if commit != self.request.expected_implementation_commit:
            raise ValueError("implementation commit does not match the supplied identity")
        if tree != self.request.expected_implementation_tree:
            raise ValueError("implementation tree does not match the supplied identity")
        if dirty:
            raise ValueError("implementation repository must be clean")
        self._source_tree_sha256 = self._source_tree_identity(repository)
        if _is_windows():
            self._windows_runtime_identity = self._probe_windows_runtime()

    def _probe_windows_runtime(self) -> dict[str, Any]:
        completed = subprocess.run(
            [
                str(self.request.trusted_python.absolute()),
                "-I",
                "-S",
                "-B",
                "-c",
                _WINDOWS_RUNTIME_PROBE,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            raise ValueError("trusted Python Windows runtime probe failed")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("trusted Python Windows runtime probe is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"base_executable", "version"}
            or not isinstance(value.get("base_executable"), str)
            or not isinstance(value.get("version"), str)
        ):
            raise ValueError("trusted Python Windows runtime probe is invalid")
        executable = Path(value["base_executable"])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("trusted Python base executable is unavailable")
        dependency_paths = self._trusted_dependency_paths()
        return {
            "base_executable": str(executable),
            "base_executable_sha256": _sha256_file(executable),
            "python_version": value.get("version"),
            "dependency_paths": [str(path) for path in dependency_paths],
        }

    def _trusted_dependency_paths(self) -> list[Path]:
        dependency_paths: list[Path] = []
        for supplied in self.request.trusted_dependency_paths:
            path = Path(supplied)
            if not path.is_absolute() or path.is_symlink() or not path.is_dir():
                raise ValueError("trusted Python dependency paths are unavailable or indirect")
            resolved = path.resolve(strict=True)
            if resolved.is_symlink() or not resolved.is_dir():
                raise ValueError("trusted Python dependency paths are unavailable or indirect")
            if resolved not in dependency_paths:
                dependency_paths.append(resolved)
        if not dependency_paths:
            raise ValueError("trusted Python dependency paths are unavailable")
        return dependency_paths

    def _windows_runtime_executable(self) -> Path:
        if self._windows_runtime_identity is None:
            raise RuntimeError("Windows runtime identity was not preflighted")
        executable = Path(cast(str, self._windows_runtime_identity["base_executable"]))
        if _sha256_file(executable) != self._windows_runtime_identity["base_executable_sha256"]:
            raise RuntimeError("Windows direct runtime changed after preflight")
        return executable

    def _probe_windows_child_capability(self, environment: dict[str, str]) -> None:
        """Fail before containment if the allowlisted runtime closure is unavailable."""
        if self._windows_runtime_identity is None:
            raise RuntimeError("Windows runtime identity was not preflighted")
        dependency_paths = cast(list[str], self._windows_runtime_identity["dependency_paths"])
        try:
            completed = subprocess.run(
                [
                    str(self._windows_runtime_executable()),
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    _WINDOWS_CHILD_CAPABILITY_PROBE,
                    _canonical_json(dependency_paths),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Windows child capability preflight timed out") from exc
        if completed.returncode != 0:
            raise RuntimeError("Windows child capability preflight failed before Hub startup")
        try:
            evidence = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Windows dependency closure evidence is malformed") from exc
        dependencies = _validate_runtime_dependency_closure(evidence, dependency_paths)
        self._windows_runtime_identity["dependency_modules"] = dependencies

    def _required_source_tree_sha256(self) -> str:
        if self._source_tree_sha256 is None:
            raise RuntimeError("implementation source tree was not verified")
        return self._source_tree_sha256

    @staticmethod
    def _source_tree_identity(repository: Path) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), "ls-files", "--", "src"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("implementation source tree cannot be verified")
        files: dict[str, str] = {}
        for relative in completed.stdout.splitlines():
            path = repository / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError("implementation source tree is unavailable")
            files[relative] = _sha256_file(path)
        if not files:
            raise ValueError("implementation source tree is unavailable")
        return hashlib.sha256(_canonical_json(files).encode()).hexdigest()

    @staticmethod
    def _git_output(repository: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("implementation Git identity cannot be verified")
        return completed.stdout.strip()

    def _start_and_connect(
        self, overlay: MaterializedCapsule, manifest: CapsuleManifest
    ) -> LoopbackHttp:
        self._assert_loopback_port_is_free()
        job: _WindowsJob | None = None
        stdout: BinaryIO | None = None
        stderr: BinaryIO | None = None
        try:
            serial = len(self._children) + 1
            attestation_path = self._child_attestation_path(serial)
            startup_ready_path = self._startup_ready_path(serial)
            startup_release_path = self._startup_release_path(serial)
            environment = self._environment(overlay, manifest)
            runtime_executable = self._windows_runtime_executable() if _is_windows() else None
            if _is_windows():
                self._probe_windows_child_capability(environment)
            logs = overlay.root / "rehearsal" / "process-logs"
            logs.mkdir(parents=True, exist_ok=True)
            job = _WindowsJob.create() if _is_windows() else None
            for path in (attestation_path, startup_ready_path, startup_release_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            argv = self._command(
                attestation_path=attestation_path,
                startup_ready_path=startup_ready_path,
                startup_release_path=startup_release_path,
                runtime_executable=runtime_executable,
            )
            stdout_path = logs / f"serve-{serial}.stdout.log"
            stderr_path = logs / f"serve-{serial}.stderr.log"
            stdout = stdout_path.open("wb")
            stderr = stderr_path.open("wb")
            workdir = overlay.root / "rehearsal" / "runtime-cwd"
            workdir.mkdir(parents=True, exist_ok=True)
            if workdir.is_symlink():
                raise RuntimeError("rehearsal child cwd is an indirect path")
            if (workdir / ".env").exists() or (workdir / ".env").is_symlink():
                raise RuntimeError("rehearsal child cwd contains dotenv configuration")
            resolved_workdir = workdir.resolve()
            popen_kwargs: dict[str, Any] = {
                "env": environment,
                "stdout": stdout,
                "stderr": stderr,
                "cwd": str(resolved_workdir),
            }
            if _is_windows():
                if job is None:
                    raise RuntimeError("Windows runtime Job Object is unavailable")
                windows_subprocess = cast(Any, subprocess)
                popen_kwargs["creationflags"] = windows_subprocess.CREATE_NEW_PROCESS_GROUP
            child = self._popen(argv, **popen_kwargs)
        except BaseException as primary_error:
            if job is not None:
                try:
                    job.close()
                except BaseException as cleanup_error:
                    primary_error.add_note(f"Windows Job Object close failed: {cleanup_error}")
            for stream_name, stream in (("stdout", stdout), ("stderr", stderr)):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as cleanup_error:
                        primary_error.add_note(
                            f"startup {stream_name} close failed: {cleanup_error}"
                        )
            raise
        if stdout is None or stderr is None:
            raise RuntimeError("rehearsal process streams are unavailable")
        observation = ProcessObservation(child.pid, None, _timestamp(), None, None, tuple(argv))
        tracked = _Child(
            child,
            observation,
            stdout_path,
            stderr_path,
            stdout,
            stderr,
            attestation_path=attestation_path,
            startup_ready_path=startup_ready_path,
            startup_release_path=startup_release_path,
            job=job,
        )
        self._children.append(tracked)
        self._record(
            "process_started",
            pid=child.pid,
            argv=_redact_argv(argv),
            cwd=str(resolved_workdir),
        )
        client = self._http_factory(self.request.port)
        if self.request.run_goal == "capture":
            if self._capture_capability is None or not hasattr(client, "set_capture_capability"):
                raise RuntimeError("capture loopback client cannot install its control capability")
            client.set_capture_capability(self._capture_capability)
        deadline = self._clock() + min(30.0, self.request.timeout_seconds)
        if _is_windows():
            self._release_windows_runtime_after_handshake(tracked, deadline)
        while True:
            if child.poll() is not None:
                raise RuntimeError(f"oms-hub serve exited during startup: {child.returncode}")
            try:
                health = client.bootstrap_csrf()
                self._validate_source_attestation(tracked.attestation_path)
                runtime_pid = self._attested_runtime_pid()
                if _is_windows() and runtime_pid != child.pid:
                    raise RuntimeError(
                        "Windows source attestation PID does not match direct runtime"
                    )
                tracked.runtime_pid = runtime_pid
                tracked.observation = replace(tracked.observation, runtime_pid=runtime_pid)
                self._validate_child_health(health, runtime_pid)
                self._record(
                    "runtime_child_attested", process_pid=child.pid, runtime_pid=runtime_pid
                )
                return client
            except (URLError, TimeoutError, ConnectionError) as exc:
                if self._clock() >= deadline:
                    raise RuntimeError(
                        "oms-hub serve did not become reachable on loopback"
                    ) from exc
                time.sleep(0.1)

    def _release_windows_runtime_after_handshake(self, child: _Child, deadline: float) -> None:
        if (
            child.job is None
            or child.startup_ready_path is None
            or child.startup_release_path is None
        ):
            raise RuntimeError("Windows runtime startup handshake paths are unavailable")
        try:
            while self._clock() < deadline:
                try:
                    ready = json.loads(child.startup_ready_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    ready = None
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Windows runtime startup handshake is invalid") from exc
                if ready is not None:
                    if (
                        not isinstance(ready, dict)
                        or ready.get("schema_version") != 1
                        or ready.get("run_nonce") != self._runtime_evidence_nonce
                        or not isinstance(ready.get("pid"), int)
                        or ready["pid"] <= 0
                    ):
                        raise RuntimeError(
                            "Windows runtime startup handshake has the wrong identity"
                        )
                    runtime_pid = cast(int, ready["pid"])
                    if runtime_pid != child.process.pid:
                        raise RuntimeError(
                            "Windows ready handshake PID does not match direct runtime"
                        )
                    process_handle = _windows_popen_handle(child.process)
                    child.job.assign_process_handle(process_handle)
                    if child.job.active_processes() != 1:
                        raise RuntimeError("Windows runtime Job Object has the wrong active count")
                    child.runtime_pid = runtime_pid
                    child.observation = replace(child.observation, runtime_pid=runtime_pid)
                    _write_json_atomically(child.startup_release_path, ready)
                    self._record(
                        "runtime_job_bound_and_released",
                        process_pid=child.process.pid,
                        runtime_pid=runtime_pid,
                    )
                    return
                # A virtual-environment launcher may have exited after spawning
                # the real interpreter.  The child bootstrap remains the source
                # of truth until the bounded handshake deadline expires.
                time.sleep(0.05)
            raise RuntimeError("Windows runtime did not complete its startup handshake")
        except BaseException:
            if child.process.poll() is None:
                try:
                    _terminate_rehearsal_process(child.process, hard=True)
                except BaseException as cleanup_error:
                    self._record(
                        "cleanup_failed", phase="startup_process_stop", error=str(cleanup_error)
                    )
            try:
                child.job.close()
            except BaseException as cleanup_error:
                self._record("cleanup_failed", phase="startup_job_close", error=str(cleanup_error))
            raise

    def _validate_source_attestation(self, path: Path | None = None) -> None:
        path = path or self._attestation_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Hub source import attestation is unavailable") from exc
        if not isinstance(value, dict) or value.get("source") != str(
            self.request.implementation_repository.resolve() / "src"
        ):
            raise RuntimeError("Hub source import attestation has the wrong verified source")
        modules = value.get("modules")
        if not isinstance(modules, dict) or set(modules) != {"oms_hub", "oms_hub.cli"}:
            raise RuntimeError("Hub source import attestation is malformed")
        source = self.request.implementation_repository.resolve() / "src"
        try:
            for module_path in modules.values():
                if not isinstance(module_path, str):
                    raise ValueError
                Path(module_path).resolve().relative_to(source)
        except ValueError as exc:
            raise RuntimeError("Hub imported outside verified implementation source") from exc
        if not isinstance(value.get("python_executable"), str) or not isinstance(
            value.get("python_version"), str
        ):
            raise RuntimeError("Hub source import attestation lacks interpreter identity")
        if _is_windows():
            if self._windows_runtime_identity is None:
                raise RuntimeError("Windows runtime identity was not preflighted")
            if (
                Path(value["python_executable"]).resolve()
                != Path(self._windows_runtime_identity["base_executable"]).resolve()
            ):
                raise RuntimeError("Hub source import attestation has the wrong runtime executable")
            if value["python_version"] != self._windows_runtime_identity["python_version"]:
                raise RuntimeError("Hub source import attestation has the wrong runtime version")
        if not all(
            value.get(key) is True for key in ("isolated", "no_site", "ignore_environment")
        ) or not isinstance(value.get("bootstrap_dependency_paths"), list):
            raise RuntimeError("Hub source import attestation lacks isolated bootstrap evidence")
        expected_dependency_paths = (
            self._windows_runtime_identity["dependency_paths"]
            if _is_windows() and self._windows_runtime_identity is not None
            else _bootstrap_dependency_paths()
        )
        if value.get("bootstrap_dependency_paths") != expected_dependency_paths:
            raise RuntimeError("Hub source import attestation has the wrong dependency paths")
        if _is_windows():
            if self._windows_runtime_identity is None:
                raise RuntimeError("Windows runtime identity was not preflighted")
            if value.get("runtime_dependencies") != self._windows_runtime_identity.get(
                "dependency_modules"
            ):
                raise RuntimeError("Hub source import attestation has the wrong dependency closure")
        if (
            value.get("pid") is None
            or value.get("run_nonce") != self._runtime_evidence_nonce
            or value.get("commit") != self.request.expected_implementation_commit
            or value.get("tree") != self.request.expected_implementation_tree
            or value.get("source_tree_sha256") != self._required_source_tree_sha256()
            or not isinstance(value.get("source_files"), dict)
        ):
            raise RuntimeError("Hub source import attestation has an invalid source identity")
        child_cwd = value.get("child_cwd")
        expected_cwd = (self.request.overlay / "rehearsal" / "runtime-cwd").resolve()
        if not isinstance(child_cwd, str) or Path(child_cwd).resolve() != expected_cwd:
            raise RuntimeError("Hub source import attestation has the wrong child cwd")
        self._source_attestation = value

    def _attested_runtime_pid(self) -> int:
        if self._source_attestation is None:
            raise RuntimeError("Hub source import attestation is unavailable")
        pid = self._source_attestation.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("Hub source import attestation has an invalid runtime PID")
        return pid

    def _assert_loopback_port_is_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.bind(("127.0.0.1", self.request.port))
            except OSError as exc:
                raise RuntimeError("rehearsal loopback port is already in use") from exc

    def _validate_child_health(self, health: Any, pid: int) -> None:
        expected = {
            "rehearsal_nonce": self._runtime_evidence_nonce,
            "rehearsal_pid": str(pid),
            "rehearsal_source": str(self.request.implementation_repository.resolve() / "src"),
            "rehearsal_source_tree_sha256": self._required_source_tree_sha256(),
            "rehearsal_commit": self.request.expected_implementation_commit,
            "rehearsal_tree": self.request.expected_implementation_tree,
        }
        if not isinstance(health, dict) or any(
            health.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeError("rehearsal health attestation does not identify the launched child")

    def _poll(self, client: LoopbackHttp, job_id: UUID, *, terminal: bool) -> dict[str, Any]:
        deadline = self._clock() + self.request.timeout_seconds
        latest: dict[str, Any] = {}
        while self._clock() < deadline:
            status, payload = client.request("GET", f"/api/anki/jobs/{job_id}")
            if status != 200 or not isinstance(payload, dict):
                raise RuntimeError(f"job status failed: HTTP {status}")
            latest = payload
            state = payload.get("state")
            self._record("job_state", state=state, attempts=payload.get("attempts"))
            if terminal:
                if state in {"ready_for_review", "failed", "canceled", "removed"}:
                    return latest
            elif state not in {"queued", "preflight"}:
                return latest
            time.sleep(0.25)
        raise TimeoutError("timed out waiting for the curation worker")

    def _wait_for_durable_boundary(
        self, client: LoopbackHttp, repository: AnkiCurationRepository, job_id: UUID
    ) -> dict[str, Any]:
        deadline = self._clock() + self.request.timeout_seconds
        while self._clock() < deadline:
            status, payload = client.request("GET", f"/api/anki/jobs/{job_id}")
            if status != 200 or not isinstance(payload, dict):
                raise RuntimeError(f"job status failed: HTTP {status}")
            artifacts = repository.list_stage_artifacts(job_id)
            if artifacts:
                for stage in CurationStage:
                    repository.require_no_indeterminate_provider_attempt(job_id, stage)
                self._record(
                    "durable_stage_boundary", state=payload.get("state"), artifacts=len(artifacts)
                )
                return payload
            if payload.get("state") in {"failed", "canceled", "removed"}:
                raise RuntimeError("job ended before a restartable durable stage boundary")
            time.sleep(0.25)
        raise TimeoutError("timed out waiting for a durable stage boundary")

    def _wait_for_child_interlock(
        self,
        repository: AnkiCurationRepository,
        job_id: UUID,
        stage: CurationStage,
        checkpoint: ProviderCheckpoint,
        overlay: MaterializedCapsule,
    ) -> dict[str, Any]:
        """Wait for a child-written post-commit interlock, not a DB polling race."""
        deadline = self._clock() + self.request.timeout_seconds
        path = overlay.root / "rehearsal" / "runtime-evidence" / "provider-fault-interlock.json"
        while self._clock() < deadline:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                payload = None
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("child failure interlock evidence is invalid") from exc
            if isinstance(payload, dict):
                required = {
                    "schema_version": 1,
                    "run_nonce": self._runtime_evidence_nonce,
                    "stage": stage.value,
                    "occurrence": 1,
                    "boundary_selector": checkpoint,
                }
                if any(payload.get(key) != value for key, value in required.items()):
                    raise RuntimeError("child failure interlock evidence has the wrong identity")
                event = payload.get("event")
                is_terminal = event in {
                    "accepted",
                    "validation_failed",
                    "transport_failed",
                    "contract_failed",
                }
                if (checkpoint == "terminal" and not is_terminal) or (
                    checkpoint != "terminal" and event != checkpoint
                ):
                    raise RuntimeError("child failure interlock evidence has the wrong boundary")
                if not isinstance(payload.get("pid"), int) or not isinstance(
                    payload.get("call_index"), int
                ):
                    raise RuntimeError("child failure interlock evidence is malformed")
                self._record(
                    "failure_injection_child_interlocked",
                    stage=stage.value,
                    checkpoint=checkpoint,
                    actual_event=event,
                    pid=payload["pid"],
                    action=payload.get("action"),
                )
                return payload
            latest = self._children[-1].process
            if latest.poll() is not None:
                raise RuntimeError("child exited before writing a durable failure interlock")
            time.sleep(0.10)
        raise TimeoutError("timed out waiting for requested provider checkpoint")

    def _validate_fault_cutoff(
        self,
        repository: AnkiCurationRepository,
        job_id: UUID,
        interlock: dict[str, Any],
    ) -> None:
        """Prove the target call stopped at the requested durable boundary."""
        rows = [
            row
            for row in repository.list_provider_attempt_events(job_id)
            if row.get("stage") == interlock["stage"]
            and row.get("call_index") == interlock["call_index"]
            and row.get("subcall_ordinal") == interlock.get("subcall_ordinal", 0)
        ]
        events = [str(row["event"]) for row in rows]
        boundary = str(interlock["event"])
        if not events or events[-1] != boundary or events.count(boundary) != 1:
            raise RuntimeError("provider fault interlock did not preserve an exact event cutoff")
        if boundary == "begun" and "dispatched" in events:
            raise RuntimeError("begun fault interlock allowed provider dispatch")
        if boundary == "dispatched" and len(events) != 2:
            raise RuntimeError("dispatched fault interlock has later durable provider evidence")
        self._record(
            "failure_injection_cutoff_verified",
            stage=interlock["stage"],
            checkpoint=interlock["boundary_selector"],
            actual_event=boundary,
            events=events,
        )

    def _validate_crash_interlock_evidence(
        self, overlay: MaterializedCapsule, interlock: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate a killed child without pretending its shutdown was graceful."""
        if not self._children or self._children[-1].runtime_pid != interlock.get("pid"):
            raise RuntimeError("failure interlock did not identify the active child")
        if self._source_attestation is None or self._source_attestation.get("pid") != interlock.get(
            "pid"
        ):
            raise RuntimeError("failure interlock PID is not bound to the verified source child")
        expected_action = "hard_exit" if interlock.get("event") == "dispatched" else "pause"
        if interlock.get("action") != expected_action:
            raise RuntimeError("failure interlock has an unexpected child action")
        directory = overlay.root / "rehearsal" / "runtime-evidence"
        adapter_path = directory / "read-only-anki-mutation-ledger.json"
        adapter = (
            _load_runtime_ledger(adapter_path)
            if adapter_path.is_file()
            else {"schema_version": 1, "run_nonce": self._runtime_evidence_nonce, "records": []}
        )
        egress = _load_runtime_ledger(directory / "egress-decisions.json")
        _validate_adapter_ledger(adapter, self._runtime_evidence_nonce)
        _validate_egress_ledger(
            egress,
            self._runtime_evidence_nonce,
            self.request.mode,
            require_clean_lifecycle=False,
        )
        # Validators above are intentionally runtime-only; narrow their JSON
        # fields again here before count/index use so malformed crash evidence
        # cannot become a type error or be treated as a valid checkpoint.
        adapter_records = adapter.get("records")
        egress_records = egress.get("records")
        if not isinstance(adapter_records, list) or not isinstance(egress_records, list):
            raise RuntimeError("crash evidence records are malformed")
        authorizations = _require_no_denied_egress_authorizations(egress_records, "crash")
        if self.request.mode == "deterministic" and any(
            str(row["host"]).casefold().rstrip(".") not in {"localhost", "127.0.0.1", "::1"}
            for row in authorizations
        ):
            raise RuntimeError("deterministic crash evidence records non-loopback egress")
        markers = [row["kind"] for row in egress_records if row["kind"] != "authorization"]
        if markers != ["startup"]:
            raise RuntimeError("crash evidence must have one unbalanced startup marker")
        self._record(
            "crash_interlock_evidence_verified",
            child_pid=interlock["pid"],
            checkpoint=interlock["event"],
            adapter_records=len(adapter_records),
            egress_records=len(egress_records),
        )
        return adapter, egress

    def _restart_after_durable_boundary(
        self,
        repository: AnkiCurationRepository,
        job_id: UUID,
        observed: dict[str, Any],
        *,
        overlay: MaterializedCapsule | None = None,
    ) -> None:
        before = _durable_terminal_attempt_keys(repository.list_provider_attempt_events(job_id))
        self._record(
            "restart_after_observed_durable_boundary",
            observed_state=observed.get("state"),
            unsupported_finer_checkpoints=False,
        )
        self._stop_latest(overlay)
        after = _durable_terminal_attempt_keys(repository.list_provider_attempt_events(job_id))
        if before != after:
            raise RuntimeError("provider-attempt ledger changed while process was stopped")

    def _assert_provider_ledger_is_restart_safe(
        self,
        repository: AnkiCurationRepository,
        job_id: UUID,
        *,
        expected_precrash_logical_identities: set[tuple[object, ...]] | None = None,
        precrash_event_id_cutoff: int | None = None,
        fault_interlock: dict[str, Any] | None = None,
    ) -> None:
        for stage in CurationStage:
            repository.require_no_indeterminate_provider_attempt(job_id, stage)
        rows = repository.list_provider_attempt_events(job_id)
        replay_namespace_sha256 = _replay_namespace_for_job(repository.require_job(job_id))
        logical = _stable_logical_call_ids(rows, replay_namespace_sha256)
        event_groups = _stable_logical_call_event_groups(rows, replay_namespace_sha256)
        for events in event_groups.values():
            _validate_recovered_provider_lifecycle(events)
        # Do not derive a final outcome from evidence until every execution
        # stream has passed the repository's append-only lifecycle contract.
        canonical_outcomes = {
            identity: _canonical_provider_outcome(events)
            for identity, events in event_groups.items()
        }
        if expected_precrash_logical_identities is not None:
            if precrash_event_id_cutoff is None or fault_interlock is None:
                raise RuntimeError("begun recovery lacks an append-only pre-crash cutoff")
            cutoff = precrash_event_id_cutoff
            if not expected_precrash_logical_identities.issubset(logical):
                raise RuntimeError("restart changed a pre-crash stable logical provider identity")
            post_restart_rows = [row for row in rows if _provider_event_id(row) > cutoff]
            if not post_restart_rows:
                raise RuntimeError("begun recovery has no newly appended provider evidence")
            precrash_target_rows = [
                row
                for row in rows
                if _provider_event_id(row) <= cutoff
                and row.get("stage") == fault_interlock["stage"]
                and row.get("call_index") == fault_interlock["call_index"]
                and row.get("subcall_ordinal", 0) == fault_interlock.get("subcall_ordinal", 0)
            ]
            if [row.get("event") for row in precrash_target_rows] != ["begun"]:
                raise RuntimeError("begun recovery pre-crash target was dispatched")
            target_identity = _stable_logical_call_id(
                precrash_target_rows[0], replay_namespace_sha256
            )
            target_events = event_groups[target_identity]
            post_target_rows = [
                row
                for row in post_restart_rows
                if _stable_logical_call_id(row, replay_namespace_sha256) == target_identity
            ]
            if sum(row.get("event") == "dispatched" for row in target_events) != 1:
                raise RuntimeError("begun recovery duplicated or omitted provider dispatch")
            if not any(row.get("event") in _TERMINAL_PROVIDER_EVENTS for row in post_target_rows):
                raise RuntimeError("begun recovery terminal outcome was not appended after restart")
            self._record(
                "begun_recovery_stable_identity_verified",
                before=len(expected_precrash_logical_identities),
                after=len(logical),
                post_restart_events=len(post_restart_rows),
                target_dispatches=1,
                target_final_outcome=canonical_outcomes[target_identity],
            )
        self._record(
            "restart_provider_ledger_verified",
            stable_logical_calls=len(event_groups),
            canonical_final_outcomes=len(canonical_outcomes),
        )

    def _stop_latest(
        self, overlay: MaterializedCapsule | None = None, *, hard: bool = False
    ) -> None:
        if not self._children:
            return
        self._stop_child(self._children[-1], overlay, hard=hard)

    def _stop_child(
        self, child: _Child, overlay: MaterializedCapsule | None = None, *, hard: bool = False
    ) -> None:
        preinitialization_exit = (
            overlay is not None
            and not hard
            and self._is_windows_preinitialization_exit(child, overlay)
        )
        if preinitialization_exit:
            # This bootstrap has already exited after the parent released its
            # exact identity handshake. Closing its empty KILL_ON_JOB_CLOSE
            # job preserves containment without sending CTRL_BREAK to a dead
            # process (which can itself fail before this state is classified).
            cleanup_error: BaseException | None = None
            if child.job is not None and not child.job.closed:
                try:
                    child.job.close()
                except BaseException as exc:
                    cleanup_error = exc
            child.observation = ProcessObservation(
                child.observation.pid,
                child.runtime_pid,
                child.observation.started_at,
                _timestamp(),
                child.process.returncode,
                child.observation.argv,
            )
            for stream_name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
                try:
                    stream.close()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    else:
                        cleanup_error.add_note(f"startup {stream_name} close failed: {exc}")
            self._record(
                "process_stopped",
                process_pid=child.process.pid,
                runtime_pid=child.runtime_pid,
                exit_code=child.process.returncode,
                hard_termination=hard,
            )
            self._record(
                "windows_preinitialization_exit_without_runtime_ledgers",
                process_pid=child.process.pid,
                exit_code=child.process.returncode,
            )
            if cleanup_error is not None:
                raise cleanup_error
            return
        if _is_windows() and child.job is not None and not child.job.closed:
            self._stop_windows_job_child(child, hard=hard)
        elif child.process.poll() is None:
            _terminate_rehearsal_process(child.process, hard=hard)
        child.observation = ProcessObservation(
            child.observation.pid,
            child.runtime_pid,
            child.observation.started_at,
            _timestamp(),
            child.process.returncode,
            child.observation.argv,
        )
        child.stdout.close()
        child.stderr.close()
        self._record(
            "process_stopped",
            process_pid=child.process.pid,
            runtime_pid=child.runtime_pid,
            exit_code=child.process.returncode,
            hard_termination=hard,
        )
        if overlay is not None and not hard:
            self._validate_runtime_evidence(overlay)

    def _is_windows_preinitialization_exit(
        self, child: _Child, overlay: MaterializedCapsule
    ) -> bool:
        """Recognize a released Windows bootstrap that failed before runtime ledgers."""
        if not _is_windows() or child.process.returncode in {None, 0}:
            return False
        if (
            child.startup_ready_path is None
            or child.startup_release_path is None
            or child.attestation_path is None
        ):
            return False
        expected = {
            "schema_version": 1,
            "pid": child.process.pid,
            "run_nonce": self._runtime_evidence_nonce,
        }
        ready = _load_exact_json_identity(child.startup_ready_path, expected)
        release = _load_exact_json_identity(child.startup_release_path, expected)
        directory = overlay.root / "rehearsal" / "runtime-evidence"
        ledgers = (
            directory / "read-only-anki-mutation-ledger.json",
            directory / "egress-decisions.json",
        )
        return (
            ready
            and release
            and all(not path.exists() and not path.is_symlink() for path in ledgers)
        )

    def _stop_windows_job_child(self, child: _Child, *, hard: bool) -> None:
        job = child.job
        if job is None:
            return
        try:
            if hard:
                job.terminate()
                if not self._wait_for_windows_job_empty(job, child.process, timeout=10.0):
                    raise RuntimeError("Windows runtime job did not empty after hard termination")
            else:
                job.send_ctrl_break(child.process.pid)
                if not self._wait_for_windows_job_empty(job, child.process, timeout=10.0):
                    self._record(
                        "process_graceful_shutdown_failed",
                        process_pid=child.process.pid,
                        runtime_pid=child.runtime_pid,
                    )
                    job.terminate()
                    if not self._wait_for_windows_job_empty(job, child.process, timeout=10.0):
                        raise RuntimeError(
                            "Windows runtime job did not empty after forced termination"
                        )
                    raise RuntimeError(
                        "Windows runtime required forced job termination after CTRL_BREAK"
                    )
        except BaseException as primary_error:
            # A signal/query error must not leave a child-owned runtime behind.
            # Closing the parent-owned KILL_ON_JOB_CLOSE handle is the final,
            # handle-bound containment step; preserve the primary failure.
            try:
                job.close()
            except BaseException as cleanup_error:
                primary_error.add_note(f"Windows Job Object close failed: {cleanup_error}")
            raise
        else:
            # Closing is idempotent. It also handles an exited launcher whose
            # attested runtime is still alive.
            job.close()

    def _wait_for_windows_job_empty(
        self, job: _WindowsJob, process: subprocess.Popen[bytes], *, timeout: float
    ) -> bool:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if job.active_processes() == 0:
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    return False
                return True
            time.sleep(0.05)
        if job.active_processes() != 0:
            return False
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _stop_all(self, overlay: MaterializedCapsule | None = None) -> None:
        failures: list[BaseException] = []
        for child in reversed(self._children):
            if child.observation.ended_at is None:
                try:
                    self._stop_child(child, overlay)
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]

    def _record(self, event: str, **values: Any) -> None:
        self._timeline.append({"at": _timestamp(), "event": event, **values})

    def _write_evidence(
        self,
        manifest: CapsuleManifest,
        overlay: MaterializedCapsule,
        client: LoopbackHttp,
        repository: AnkiCurationRepository,
        job_id: UUID,
        review: dict[str, Any],
        saved: dict[str, Any],
        envelope_status: int,
        envelope: Any,
        apply: Any,
        adapter_ledger: dict[str, Any],
        egress_ledger: dict[str, Any],
    ) -> None:
        records: dict[str, Any] = {
            "capsule.json": manifest.model_dump(mode="json"),
            "implementation.json": {
                "repository": str(self.request.implementation_repository.resolve()),
                "commit": self.request.expected_implementation_commit,
                "tree": self.request.expected_implementation_tree,
                "trusted_python": str(self.request.trusted_python.resolve()),
                "trusted_python_sha256": _sha256_file(self.request.trusted_python),
                "windows_runtime_identity": self._windows_runtime_identity,
                "source_attestation": self._source_attestation,
            },
            "overlay.json": {
                "root": str(overlay.root),
                "database": str(overlay.database_path),
                "path_audit": [asdict(row) for row in overlay.path_audit],
            },
            "processes.json": [
                asdict(obs) | {"argv": _redact_argv(list(obs.argv))}
                for item in self._children
                for obs in [item.observation]
            ],
            "environment.json": _environment_evidence(self._environment(overlay, manifest)),
            "http-transcript.json": client.transcript,
            "timeline.json": self._timeline,
            "provider-attempt-ledger.json": repository.list_provider_attempt_events(job_id),
            "read-only-anki-mutation-ledger.json": adapter_ledger,
            "egress-decisions.json": egress_ledger,
            "process-logs.json": {
                f"{child.stdout_path.name}": _file_descriptor(child.stdout_path)
                for child in self._children
            }
            | {
                f"{child.stderr_path.name}": _file_descriptor(child.stderr_path)
                for child in self._children
            },
            "restart-observations.json": [
                item for item in self._timeline if "restart" in str(item.get("event"))
            ],
            "review.json": review,
            "review-save.json": saved,
            "envelope.json": {"status": envelope_status, "body": envelope},
            "apply.json": {"status": 423, "body": apply},
        }
        _write_deterministic_zip(self.request.evidence_zip, records)

    def _write_failure_evidence(
        self,
        manifest: CapsuleManifest,
        overlay: MaterializedCapsule,
        client: LoopbackHttp,
        repository: AnkiCurationRepository,
        job_id: UUID,
        interlock: dict[str, Any],
        adapter_ledger: dict[str, Any],
        egress_ledger: dict[str, Any],
    ) -> None:
        """Package a crash checkpoint without implying a successful rehearsal."""
        records: dict[str, Any] = {
            "capsule.json": manifest.model_dump(mode="json"),
            "failure.json": {
                "result": "FAIL_CLOSED_MANUAL_RECOVERY_REQUIRED",
                "ready_for_review": False,
                "reason": "crash-bound provider checkpoint is not authorized for automatic replay",
                "interlock": interlock,
                "execution_kind": "actual_hub_process_rehearsal",
            },
            "processes.json": [
                asdict(item.observation) | {"argv": _redact_argv(list(item.observation.argv))}
                for item in self._children
            ],
            "timeline.json": self._timeline,
            "http-transcript.json": client.transcript,
            "provider-attempt-ledger.json": repository.list_provider_attempt_events(job_id),
            "read-only-anki-mutation-ledger.json": adapter_ledger,
            "egress-decisions.json": egress_ledger,
            "process-logs.json": {
                f"{child.stdout_path.name}": _file_descriptor(child.stdout_path)
                for child in self._children
            }
            | {
                f"{child.stderr_path.name}": _file_descriptor(child.stderr_path)
                for child in self._children
            },
        }
        _write_deterministic_zip(self.request.evidence_zip, records)

    def _validate_runtime_evidence(
        self, overlay: MaterializedCapsule
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        directory = overlay.root / "rehearsal" / "runtime-evidence"
        adapter = _load_runtime_ledger(directory / "read-only-anki-mutation-ledger.json")
        egress = _load_runtime_ledger(directory / "egress-decisions.json")
        _validate_adapter_ledger(adapter, self._runtime_evidence_nonce)
        _validate_egress_ledger(egress, self._runtime_evidence_nonce, self.request.mode)
        records = egress.get("records")
        if not isinstance(records, list):
            raise RuntimeError("runtime egress evidence records are malformed")
        _require_no_denied_egress_authorizations(records, "runtime")
        self._record(
            "runtime_evidence_verified",
            adapter_records=len(adapter["records"]),
            egress_records=len(egress["records"]),
        )
        return adapter, egress

    def _assert_smoke_http_transcript(self, client: LoopbackHttp, job_id: UUID) -> None:
        """Prove the partial goal never exercised review or mutation routes."""
        allowed = {
            ("GET", "/health"),
            ("POST", "/api/anki/jobs"),
            ("GET", f"/api/anki/jobs/{job_id}"),
        }
        if not client.transcript:
            raise RuntimeError("expected replay miss has no HTTP transcript")
        for item in client.transcript:
            if not isinstance(item, dict):
                raise RuntimeError("expected replay miss HTTP transcript is malformed")
            method, path, status = item.get("method"), item.get("path"), item.get("status")
            if not isinstance(status, int):
                raise RuntimeError("expected replay miss HTTP transcript is malformed")
            if (method, path) not in allowed:
                raise RuntimeError("expected replay miss exercised a forbidden HTTP route")
        health = [
            item
            for item in client.transcript
            if item.get("method") == "GET" and item.get("path") == "/health"
        ]
        if not health or any(item.get("status") != 200 for item in health):
            raise RuntimeError("expected replay miss lacks a healthy loopback attestation")
        created = [
            item
            for item in client.transcript
            if item.get("method") == "POST" and item.get("path") == "/api/anki/jobs"
        ]
        if len(created) != 1 or created[0].get("status") != 201:
            raise RuntimeError("expected replay miss lacks the required HTTP job creation")
        statuses = [
            item
            for item in client.transcript
            if item.get("method") == "GET" and item.get("path") == f"/api/anki/jobs/{job_id}"
        ]
        if not statuses or any(item.get("status") != 200 for item in statuses):
            raise RuntimeError("expected replay miss lacks a successful job-status poll")

    def _assert_capture_http_transcript(
        self, client: LoopbackHttp, job_id: UUID, *, v3: bool = False
    ) -> None:
        """Capture stops at READY_FOR_REVIEW and never persists a review artifact."""
        allowed = {
            ("GET", "/health"),
            ("POST", "/api/anki/jobs"),
            ("GET", f"/api/anki/jobs/{job_id}"),
        }
        if v3:
            allowed |= {
                ("GET", f"/api/anki/jobs/{job_id}/review"),
                ("POST", f"/api/anki/jobs/{job_id}/apply"),
            }
        if not client.transcript:
            raise RuntimeError("capture has no HTTP transcript")
        for item in client.transcript:
            if not isinstance(item, dict) or (item.get("method"), item.get("path")) not in allowed:
                raise RuntimeError("capture exercised a forbidden review, envelope, or apply route")
        created = [
            item
            for item in client.transcript
            if item.get("method") == "POST" and item.get("path") == "/api/anki/jobs"
        ]
        if len(created) != 1 or created[0].get("status") != 201:
            raise RuntimeError("capture lacks exactly one successful job creation")
        if not any(
            item.get("method") == "GET"
            and item.get("path") == f"/api/anki/jobs/{job_id}"
            and item.get("status") == 200
            for item in client.transcript
        ):
            raise RuntimeError("capture lacks a successful job status poll")
        if v3:
            review = [
                item
                for item in client.transcript
                if (item.get("method"), item.get("path"))
                == ("GET", f"/api/anki/jobs/{job_id}/review")
            ]
            apply = [
                item
                for item in client.transcript
                if (item.get("method"), item.get("path"))
                == ("POST", f"/api/anki/jobs/{job_id}/apply")
            ]
            if len(review) != 1 or review[0].get("status") != 200:
                raise RuntimeError("v3 capture lacks one successful review read")
            if len(apply) != 1 or apply[0].get("status") != 423:
                raise RuntimeError("v3 capture lacks one apply-disabled proof")

    def _assert_capture_server_audit(
        self, client: LoopbackHttp, job_id: UUID, *, v3: bool = False
    ) -> dict[str, object]:
        """Require private server observation, not merely the parent transcript."""
        if self._capture_store is None:
            raise RuntimeError("capture server audit is unavailable")
        audit = self._capture_store.server_audit()
        if self._capture_capability is None or audit.get("capability_sha256") != hashlib.sha256(
            self._capture_capability.encode("utf-8")
        ).hexdigest():
            raise RuntimeError("capture server audit capability binding is invalid")
        entries = audit.get("entries")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("capture server audit is empty")
        if any(
            not isinstance(entry, dict)
            or entry.get("authenticated") is not True
            or entry.get("allowed") is not True
            for entry in entries
        ):
            raise RuntimeError("capture server audit contains denied or unauthenticated traffic")
        if any(entry.get("query_state") != "empty" for entry in entries):
            raise RuntimeError("capture server audit contains a nonempty query string")
        observed = [
            (item.get("method"), item.get("path"), item.get("status"))
            for item in client.transcript
        ]
        authoritative = [
            (entry.get("method"), entry.get("canonical_path"), entry.get("status"))
            for entry in entries
        ]
        if observed != authoritative:
            raise RuntimeError("capture server audit does not cover the HTTP transcript")
        health = [entry for entry in entries if entry.get("canonical_path") == "/health"]
        created = [
            entry
            for entry in entries
            if entry.get("canonical_path") == "/api/anki/jobs"
        ]
        statuses = [
            entry
            for entry in entries
            if entry.get("canonical_path") == f"/api/anki/jobs/{job_id}"
        ]
        review = [
            entry
            for entry in entries
            if entry.get("canonical_path") == f"/api/anki/jobs/{job_id}/review"
        ]
        apply = [
            entry
            for entry in entries
            if entry.get("canonical_path") == f"/api/anki/jobs/{job_id}/apply"
        ]
        if (
            len(health) != 1
            or health[0].get("method") != "GET"
            or health[0].get("raw_path") != "/health"
            or health[0].get("status") != 200
            or len(created) != 1
            or created[0].get("method") != "POST"
            or created[0].get("raw_path") != "/api/anki/jobs"
            or created[0].get("status") != 201
            or created[0].get("job_id") != str(job_id)
            or not statuses
            or any(
                entry.get("method") != "GET"
                or entry.get("raw_path") != f"/api/anki/jobs/{job_id}"
                or entry.get("status") != 200
                or entry.get("job_id") != str(job_id)
                for entry in statuses
            )
            or (
                v3
                and (
                    len(review) != 1
                    or review[0].get("method") != "GET"
                    or review[0].get("status") != 200
                    or review[0].get("job_id") != str(job_id)
                    or len(apply) != 1
                    or apply[0].get("method") != "POST"
                    or apply[0].get("status") != 423
                    or apply[0].get("job_id") != str(job_id)
                )
            )
            or (not v3 and (review or apply))
            or len(entries)
            != len(health) + len(created) + len(statuses) + len(review) + len(apply)
        ):
            raise RuntimeError("capture server audit violates the control-plane closure")
        return audit

    def _validate_capture_ready_for_review_state(
        self, repository: AnkiCurationRepository, job_id: UUID
    ) -> None:
        """Read the persisted domain state after the child has stopped."""
        job = repository.require_job(job_id)
        if (
            job.state is not CurationState.READY_FOR_REVIEW
            or job.review_revision != 0
            or job.apply_state is not ApplyState.PENDING
        ):
            raise RuntimeError("capture job has review, envelope, apply, or non-review-ready state")
        with repository.database.session() as session:
            artifacts = (
                session.scalar(
                    select(func.count())
                    .select_from(AnkiReviewChangeSetModel)
                    .where(AnkiReviewChangeSetModel.job_id == str(job_id))
                ),
                session.scalar(
                    select(func.count())
                    .select_from(AnkiReviewedReconciliationModel)
                    .where(AnkiReviewedReconciliationModel.job_id == str(job_id))
                ),
                session.scalar(
                    select(func.count())
                    .select_from(AnkiEnvelopeModel)
                    .where(AnkiEnvelopeModel.job_id == str(job_id))
                ),
            )
        if any(value != 0 for value in artifacts):
            raise RuntimeError("capture job has a persisted review or envelope artifact")

    def _validate_capture_runtime(
        self, adapter_ledger: dict[str, Any], egress_ledger: dict[str, Any]
    ) -> None:
        if adapter_ledger.get("records") != []:
            raise RuntimeError("capture requires an empty read-only Anki mutation ledger")
        if self._capture_authorization is None:
            raise RuntimeError("capture authorization is unavailable")
        records = egress_ledger.get("records")
        if not isinstance(records, list):
            raise RuntimeError("capture egress evidence is malformed")
        authorizations = _require_no_denied_egress_authorizations(records, "capture")
        permitted = set(self._capture_authorization.document["egress_pins"])
        for row in authorizations:
            host = row.get("host")
            if host not in {"localhost", "127.0.0.1", "::1"} and host not in permitted:
                raise RuntimeError("capture runtime egress is outside the authorization manifest")

    def _write_capture_completion(
        self,
        manifest: CapsuleManifest,
        overlay: MaterializedCapsule,
        client: LoopbackHttp,
        repository: AnkiCurationRepository,
        job_id: UUID,
        adapter_ledger: dict[str, Any],
        egress_ledger: dict[str, Any],
        server_audit: dict[str, object],
        *,
        review: dict[str, Any] | None = None,
        apply: dict[str, Any] | None = None,
        apply_status: int | None = None,
    ) -> None:
        if self._capture_store is None or self._capture_authorization is None:
            raise RuntimeError("capture store is unavailable")
        if server_audit != self._capture_store.server_audit():
            raise RuntimeError("capture server audit changed before publication")
        server_audit_projection = self._capture_store.server_audit_evidence_projection()
        server_audit_sha256 = self._capture_store.server_audit_sha256()
        observed_audit_sha256 = hashlib.sha256(
            serialize_evidence_record(server_audit_projection)
        ).hexdigest()
        if server_audit_sha256 != observed_audit_sha256:
            raise RuntimeError("capture server audit changed before publication")
        self._validate_capture_namespace(repository.require_job(job_id))
        provider_rows = repository.list_provider_attempt_events(job_id)
        self._reconcile_capture_calls(provider_rows)
        job = repository.require_job(job_id)
        is_v3 = (
            getattr(job, "pipeline_contract_version", None)
            is PipelineContractVersion.CARD_CENTRIC_V3
        )
        if is_v3 and (review is None or apply is None or apply_status != 423):
            raise RuntimeError("v3 capture lacks exact review and apply-disabled evidence")
        artifacts = repository.list_stage_artifacts(job_id) if is_v3 else []
        if is_v3:
            expected_stages = [
                definition.stage
                for definition in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V3)
                if definition.stage is not CurationStage.V3_R12_APPLY
            ]
            if [artifact.stage for artifact in artifacts] != expected_stages:
                raise RuntimeError("v3 capture stage products are not the exact R0-R11 chain")
        stage_products = [
            {
                "stage": artifact.stage.value,
                "kind": artifact.kind,
                "input_sha256": artifact.input_sha256,
                "content_sha256": artifact.content_sha256,
            }
            for artifact in artifacts
        ]
        cost_ledger: list[dict[str, Any]] = []
        cost_ledger_sha256: str | None = None
        if is_v3:
            r11 = next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.stage is CurationStage.V3_R11_REVIEW
                ),
                None,
            )
            if r11 is None:
                raise RuntimeError("v3 capture lacks the committed R11 artifact")
            document = StageArtifactStore(overlay.root / "anki" / "artifacts").read(r11, job=job)
            payload = cast(dict[str, Any], document["payload"])
            raw_ledger = payload.get("cost_ledger")
            raw_sha256 = payload.get("cost_ledger_sha256")
            if (
                not isinstance(raw_ledger, list)
                or not isinstance(raw_sha256, str)
                or not _is_sha256(raw_sha256)
                or hashlib.sha256(_canonical_json(raw_ledger).encode()).hexdigest() != raw_sha256
            ):
                raise RuntimeError("v3 capture R11 cost ledger is invalid")
            cost_ledger = cast(list[dict[str, Any]], raw_ledger)
            cost_ledger_sha256 = raw_sha256
        lineage = {
            "schema_version": 1,
            "authorization_sha256": self._capture_authorization.sha256,
            "failed_job_id": str(self.request.failed_job_id),
            "source_tree_sha256": self._required_source_tree_sha256(),
            "replay_namespace_sha256": _replay_namespace_for_job(job),
            "server_audit_sha256": server_audit_sha256,
        }
        self._capture_store.write_lineage(lineage)
        pack_manifest, pack = self._capture_store.build_pack_manifest()
        completion = {
            "schema_version": 1,
            "authorization_sha256": self._capture_authorization.sha256,
            "candidate": {
                "commit": self.request.expected_implementation_commit,
                "tree": self.request.expected_implementation_tree,
            },
            "capsule_manifest_sha256": self.request.expected_manifest_sha256,
            "failed_job_id": str(self.request.failed_job_id),
            "job_id": str(job_id),
            "source_tree_sha256": lineage["source_tree_sha256"],
            "replay_namespace_sha256": lineage["replay_namespace_sha256"],
            "server_audit_sha256": server_audit_sha256,
            **pack,
        }
        records = {
            "outcome.json": {
                "result": "CAPTURE_READY_FOR_REVIEW",
                "ready_for_review": True,
                "run_goal": "capture",
                "native_gate_complete": is_v3 and _is_windows(),
            },
            "capture-completion.json": completion,
            "capsule.json": manifest.model_dump(mode="json"),
            "implementation.json": {
                "commit": self.request.expected_implementation_commit,
                "tree": self.request.expected_implementation_tree,
                "source_attestation": self._source_attestation,
            },
            "job.json": {"id": str(job_id), "state": "ready_for_review"},
            "stage-products.json": stage_products,
            "cost-ledger.json": {
                "cost_ledger": cost_ledger,
                "cost_ledger_sha256": cost_ledger_sha256,
            },
            "review-gate.json": {
                "review": review,
                "apply": apply,
                "apply_status": apply_status,
            },
            "processes.json": [
                asdict(item.observation) | {"argv": _redact_argv(list(item.observation.argv))}
                for item in self._children
            ],
            "timeline.json": self._timeline,
            "http-transcript.json": _smoke_http_transcript(client.transcript),
            "provider-attempt-ledger.json": _capture_provider_ledger_projection(provider_rows),
            "read-only-anki-mutation-ledger.json": adapter_ledger,
            "egress-decisions.json": egress_ledger,
            "capture-server-audit.json": server_audit_projection,
            "private-pack.json": {
                "path": str(self._capture_store.pack),
                "manifest_sha256": pack["pack_manifest_sha256"],
            },
            "process-logs.json": {
                f"{child.stdout_path.name}": _file_descriptor(child.stdout_path)
                for child in self._children
            }
            | {
                f"{child.stderr_path.name}": _file_descriptor(child.stderr_path)
                for child in self._children
            },
        }
        _write_deterministic_zip(self.request.evidence_zip, records)
        _verify_evidence_zip(self.request.evidence_zip)
        with zipfile.ZipFile(self.request.evidence_zip, "r") as archive:
            published_audit_sha256 = hashlib.sha256(
                archive.read("capture-server-audit.json")
            ).hexdigest()
        if published_audit_sha256 != server_audit_sha256:
            raise RuntimeError("published capture server audit digest does not match completion")
        self._capture_store.publish_pack_manifest(pack_manifest)
        self._capture_store.write_completion(completion)
        if is_v3:
            _write_json_atomically(
                self.request.evidence_zip.with_suffix(".json"),
                {
                    "schema_version": 1,
                    "candidate": completion["candidate"],
                    "job_id": str(job_id),
                    "runtime_pid": self._attested_runtime_pid(),
                    "native_gate_complete": _is_windows(),
                    "stage_products": stage_products,
                    "cost_ledger": cost_ledger,
                    "cost_ledger_sha256": cost_ledger_sha256,
                    "no_anki_mutation": adapter_ledger.get("records") == [],
                    "apply_status": apply_status,
                    "evidence_zip": str(self.request.evidence_zip),
                    "evidence_zip_sha256": _sha256_file(self.request.evidence_zip),
                },
            )

    def _reconcile_capture_calls(self, rows: list[dict[str, object]]) -> None:
        if self._capture_store is None:
            raise RuntimeError("capture store is unavailable")
        groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
        for row in rows:
            key = (
                row.get("provider"),
                row.get("model"),
                row.get("request_sha256"),
                row.get("stage"),
                row.get("kind"),
                row.get("batch_index"),
                row.get("batch_note_ids_sha256"),
                row.get("subcall_ordinal"),
            )
            groups.setdefault(key, []).append(row)
        dispatched = {
            key: events
            for key, events in groups.items()
            if any(event.get("event") == "dispatched" for event in events)
        }
        calls = self._capture_store.calls()
        if len(dispatched) != len(calls):
            raise RuntimeError("capture private store and provider dispatch ledger differ")
        for call in calls:
            if call.get("stored") is not True or call.get("observed_microusd") is None:
                raise RuntimeError("capture private call is not durably stored")
            replay = call.get("replay_identity")
            if not isinstance(replay, dict):
                raise RuntimeError("capture private call lacks replay identity")
            if call["kind"] == "structured":
                expected_kind = replay.get("call_kind")
            elif call["kind"] == "query_embedding":
                expected_kind = "query_embedding"
            elif call["kind"] == "proposal_embedding":
                expected_kind = "embedding"
            else:
                raise RuntimeError("capture private call kind is invalid")
            if not isinstance(expected_kind, str):
                raise RuntimeError("capture private call lacks an exact call kind")
            matches = [
                events
                for key, events in dispatched.items()
                if key[0] == call["provider"]
                and key[1] == call["model"]
                and key[2] == call["request_sha256"]
                and key[3] == replay.get("stage")
                and key[5] == replay.get("batch_ordinal")
                and key[6] == replay.get("batch_note_ids_sha256")
                and key[7] == replay.get("subcall_ordinal")
                and key[4] == expected_kind
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "capture private call does not match exactly one provider dispatch"
                )
            events = sorted(matches[0], key=_provider_event_id)
            names = [event.get("event") for event in events]
            supported = {
                ("begun", "dispatched", "response_received", "accepted"),
                ("begun", "dispatched", "response_received", "validation_failed"),
                ("begun", "dispatched", "response_received", "contract_failed"),
                (
                    "begun",
                    "dispatched",
                    "response_received",
                    "accepted",
                    "contract_failed",
                ),
            }
            response_sha256 = events[2].get("response_sha256")
            if (
                tuple(names) not in supported
                or not isinstance(response_sha256, str)
                or not _is_sha256(response_sha256)
                or call.get("response_sha256") != response_sha256
            ):
                raise RuntimeError(
                    "capture provider lifecycle is not a usable response-backed topology"
                )
            replay_request = call.get("replay_request")
            if not isinstance(replay_request, dict):
                raise RuntimeError("capture private call lacks a pre-dispatch replay request")
            if call["kind"] == "structured" and replay_request.get("replay_identity") != replay:
                raise RuntimeError("capture structured replay identity changed after dispatch")
            if not self._capture_store.private_response_matches(call, events[2]):
                raise RuntimeError("capture private response does not match provider evidence")

    def _validate_expected_replay_miss(
        self, final: dict[str, Any], repository: AnkiCurationRepository, job_id: UUID
    ) -> dict[str, Any]:
        """Accept only the deliberate empty-pack miss, never a generic failure."""
        if final.get("state") != "failed":
            raise RuntimeError("expected replay miss did not reach a failed terminal job")
        error = final.get("error")
        if not isinstance(error, str):
            raise RuntimeError("expected replay miss has no safe terminal job error")
        rows = repository.list_provider_attempt_events(job_id)
        namespace = _replay_namespace_for_job(repository.require_job(job_id))
        return _expected_empty_replay_miss(rows, namespace, error)

    def _write_expected_replay_miss_evidence(
        self,
        manifest: CapsuleManifest,
        overlay: MaterializedCapsule,
        client: LoopbackHttp,
        repository: AnkiCurationRepository,
        job_id: UUID,
        final: dict[str, Any],
        miss: dict[str, Any],
        adapter_ledger: dict[str, Any],
        egress_ledger: dict[str, Any],
    ) -> None:
        """Package a redacted, explicitly partial actual-process smoke result."""
        if self._source_attestation is None:
            raise RuntimeError("expected replay miss lacks source-process attestation")
        if not self._children or any(item.observation.ended_at is None for item in self._children):
            raise RuntimeError("expected replay miss has live children during evidence packaging")
        records: dict[str, Any] = {
            "outcome.json": {
                "result": "EXPECTED_REPLAY_MISS",
                "ready_for_review": False,
                "execution_kind": "actual_hub_process_rehearsal",
                "run_goal": "first_replay_miss",
                "native_phase_b_acceptance_complete": False,
                "a0_ready": False,
                "native_prerequisite": (
                    "Phase A native capsule materialization must pass before Phase B native "
                    "execution can be accepted"
                ),
            },
            "job.json": {
                "id": str(job_id),
                "state": final.get("state"),
                "error": final.get("error"),
            },
            "expected-replay-miss.json": miss,
            "capsule.json": manifest.model_dump(mode="json"),
            "implementation.json": {
                "repository": str(self.request.implementation_repository.resolve()),
                "commit": self.request.expected_implementation_commit,
                "tree": self.request.expected_implementation_tree,
                "trusted_python": str(self.request.trusted_python.resolve()),
                "trusted_python_sha256": _sha256_file(self.request.trusted_python),
                "windows_runtime_identity": self._windows_runtime_identity,
                "source_attestation": self._source_attestation,
            },
            "overlay.json": {
                "root": str(overlay.root),
                "database": str(overlay.database_path),
                "path_audit": [asdict(row) for row in overlay.path_audit],
            },
            "processes.json": [
                asdict(item.observation) | {"argv": _redact_argv(list(item.observation.argv))}
                for item in self._children
            ],
            "timeline.json": self._timeline,
            "http-transcript.json": _smoke_http_transcript(client.transcript),
            "provider-attempt-ledger.json": repository.list_provider_attempt_events(job_id),
            "read-only-anki-mutation-ledger.json": adapter_ledger,
            "egress-decisions.json": egress_ledger,
            "process-logs.json": {
                f"{child.stdout_path.name}": _file_descriptor(child.stdout_path)
                for child in self._children
            }
            | {
                f"{child.stderr_path.name}": _file_descriptor(child.stderr_path)
                for child in self._children
            },
        }
        _write_deterministic_zip(self.request.evidence_zip, records)

    def _validate_expected_replay_miss_runtime(
        self, adapter_ledger: dict[str, Any], egress_ledger: dict[str, Any]
    ) -> None:
        """The smoke goal permits no Anki mutation attempt or non-loopback egress."""
        adapter_records = adapter_ledger.get("records")
        egress_records = egress_ledger.get("records")
        if adapter_records != [] or not isinstance(egress_records, list):
            raise RuntimeError(
                "expected replay miss requires an empty read-only Anki mutation ledger"
            )
        authorizations = _require_no_denied_egress_authorizations(egress_records, "runtime")
        if any(
            str(row.get("host", "")).casefold().rstrip(".") not in {"localhost", "127.0.0.1", "::1"}
            for row in authorizations
        ):
            raise RuntimeError("expected replay miss runtime evidence records non-loopback egress")


def fresh_job_payload(failed: CurationJob, *, live_capture: bool = False) -> dict[str, Any]:
    """Create a new API request from the domain object, never a copied DB row."""
    if failed.pipeline_contract_version not in {
        PipelineContractVersion.CARD_CENTRIC_V2,
        PipelineContractVersion.CARD_CENTRIC_V3,
    }:
        raise ValueError("preserved job must use a card-centric contract")
    v3 = failed.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3
    request = CreateCurationJobRequest(
        lecture_id=failed.lecture_id,
        block_id=failed.block_id,
        source_revision_ids=failed.source_revision_ids,
        deck_allowlist=failed.deck_allowlist,
        tag_allowlist=failed.tag_allowlist,
        instruction_text=failed.instruction_text,
        target_deck=failed.target_deck,
        target_tag=failed.target_tag,
        index_snapshot_id=failed.index_snapshot_id,
        lcl_prompt_version=failed.lcl_prompt_version,
        judgment_rubric_version=failed.judgment_rubric_version,
        gap_prompt_version=failed.gap_prompt_version,
        provider=cast(Literal["openai", "gemini", "anthropic", "openrouter"], failed.provider),
        model=failed.model,
        pipeline_contract_version=failed.pipeline_contract_version.value,
        resolved_model_config=failed.resolved_model_config.canonical_document(),
        source_revision_hashes=failed.source_revision_hashes,
        summary_outline_id=failed.summary_outline_id,
        summary_outline_sha256=failed.summary_outline_sha256,
        semantic_generation=failed.semantic_generation,
        companion_generation=failed.companion_generation,
        policy_sha256=getattr(failed, "policy_sha256", None),
        rate_table_document=getattr(failed, "rate_table_document", None),
        offline_replay_only=v3 and not live_capture,
    )
    return request.model_dump(mode="json")


def _envelope_summary(envelope: Any) -> dict[str, int]:
    """Independent summary check matching the API's published plan summary."""
    created = added = removed = 0
    retagged: set[int] = set()
    for operation in envelope.operations:
        if isinstance(operation, AddNotesOperation):
            created += len(operation.notes)
        elif isinstance(operation, AddTagsOperation):
            added += len(operation.note_ids)
            retagged.update(operation.note_ids)
        elif isinstance(operation, RemoveTagsOperation):
            removed += len(operation.note_ids)
            retagged.update(operation.note_ids)
    return {
        "notes_created": created,
        "existing_notes_retagged": len(retagged),
        "tags_added": added,
        "tags_removed": removed,
    }


def unchanged_review_payload(review: dict[str, Any]) -> dict[str, Any]:
    job = review.get("job")
    groups = review.get("groups")
    if (
        not isinstance(job, dict)
        or not isinstance(groups, dict)
        or not isinstance(job.get("review_revision"), int)
    ):
        raise ValueError("review response is malformed")
    candidates = [*groups.get("pass_1_matches", []), *groups.get("recovered_in_pass_2", [])]
    selections = {
        str(item["note_id"]): bool(item.get("selected"))
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("note_id"), int)
    }
    gaps = groups.get("generated_cards", [])
    edits = [
        {
            "card_id": item["card_id"],
            "concept_id": item["concept_id"],
            "text": item["text"],
            "extra": item.get("extra", ""),
            "selected": bool(item.get("selected")),
        }
        for item in gaps
        if isinstance(item, dict) and all(key in item for key in ("card_id", "concept_id", "text"))
    ]
    return {
        "expected_revision": job["review_revision"],
        "reviewer": "a0-rehearsal",
        "candidate_selections": selections,
        "gap_edits": edits,
        "tag_patches": [],
    }


_TERMINAL_PROVIDER_EVENTS = frozenset(
    {"accepted", "validation_failed", "transport_failed", "contract_failed"}
)


def _durable_terminal_attempt_keys(rows: list[dict[str, object]]) -> set[tuple[object, ...]]:
    """Compare the persisted audit rows while stopping an ordinary run."""
    return {
        (
            row["stage"],
            row["stage_attempt"],
            row["mode"],
            row["call_index"],
            row.get("subcall_ordinal", 0),
            row["event"],
        )
        for row in rows
        if row.get("event") in _TERMINAL_PROVIDER_EVENTS
    }


def _stable_logical_call_ids(
    rows: list[dict[str, object]], replay_namespace_sha256: str
) -> set[tuple[object, ...]]:
    """Return a stage-attempt-independent logical provider identity.

    Stage attempts and process PIDs are deliberately excluded: recovery is only
    safe to label duplicate-free when frozen call material compares identically
    across the killed process and the restart.
    """
    return {_stable_logical_call_id(row, replay_namespace_sha256) for row in rows}


def _stable_logical_call_id(
    row: dict[str, object], replay_namespace_sha256: str
) -> tuple[object, ...]:
    """Stable key: exclude job, stage attempt, mode, and audit request hash."""
    return (
        replay_namespace_sha256,
        row["stage"],
        row["kind"],
        row["batch_index"],
        row["batch_note_ids_sha256"],
        row.get("subcall_ordinal", 0),
        _stable_provider_payload_sha256(row, replay_namespace_sha256),
    )


def _stable_logical_call_event_groups(
    rows: list[dict[str, object]], replay_namespace_sha256: str
) -> dict[tuple[object, ...], list[dict[str, object]]]:
    """Group append-only evidence by the stable logical provider call.

    The durable event name, stage attempt, process mode, audit call index, and
    request hash identify an execution observation, not a new provider call.
    Recovery must account for all of those observations as one logical call.
    """
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(_stable_logical_call_id(row, replay_namespace_sha256), []).append(row)
    for events in groups.values():
        events.sort(key=_provider_event_id)
    return groups


def _canonical_provider_outcome(events: list[dict[str, object]]) -> str:
    """Return the one final outcome for a recovered stable logical call.

    A provider response can be accepted before a later caller-level contract
    check rejects it.  That append-only ``accepted -> contract_failed`` history
    remains one call with a final failed outcome.  Any other multiple terminal
    chains are inconsistent evidence and fail closed.
    """
    terminal_events = [
        str(row.get("event")) for row in events if row.get("event") in _TERMINAL_PROVIDER_EVENTS
    ]
    if not terminal_events:
        raise RuntimeError("recovered provider logical call lacks a terminal outcome")
    if len(terminal_events) == 1:
        return terminal_events[0]
    if len(terminal_events) == 2 and terminal_events == ["accepted", "contract_failed"]:
        return terminal_events[1]
    raise RuntimeError("recovered provider logical call has inconsistent final chains")


def _validate_recovered_provider_lifecycle(events: list[dict[str, object]]) -> None:
    """Validate one stable logical call across its pre- and post-restart executions.

    A restart can append a fresh ``begun`` observation for the same frozen
    provider material, so append-only lifecycle validation applies separately
    to each durable execution identity.  Dispatch cardinality applies across
    the stable logical call: recovery may not turn one provider call into two.
    """

    executions: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in events:
        execution = (
            row["stage_attempt"],
            row["mode"],
            row["call_index"],
            row.get("subcall_ordinal", 0),
        )
        executions.setdefault(execution, []).append(row)
    dispatches = sum(row.get("event") == "dispatched" for row in events)
    if dispatches > 1:
        raise RuntimeError("recovered provider logical call duplicated provider dispatch")
    requires_dispatch = any(
        row.get("event") in {"dispatched", "response_received", *_TERMINAL_PROVIDER_EVENTS}
        for row in events
    )
    if requires_dispatch and dispatches != 1:
        raise RuntimeError("recovered provider logical call omitted provider dispatch")
    for execution_events in executions.values():
        prior: list[str] = []
        for row in execution_events:
            event = str(row.get("event"))
            try:
                _validate_provider_event_append(prior, event)
            except ValueError as exc:
                raise RuntimeError("recovered provider lifecycle is invalid") from exc
            prior.append(event)


def _stable_provider_payload_sha256(row: dict[str, object], replay_namespace_sha256: str) -> str:
    """Canonical digest of frozen provider material, separate from audit request identity."""
    return hashlib.sha256(
        _canonical_json(
            {
                "replay_namespace_sha256": replay_namespace_sha256,
                "provider": row["provider"],
                "model": row["model"],
                "instruction_sha256": row["instruction_sha256"],
                "input_sha256": row["input_sha256"],
                "output_schema_sha256": row["output_schema_sha256"],
                "generation_parameters_sha256": row["generation_parameters_sha256"],
                "cache_prefix_sha256": row["cache_prefix_sha256"],
            }
        ).encode()
    ).hexdigest()


def _replay_namespace_for_job(job: CurationJob) -> str:
    """Return the pipeline/provider replay identity without hashing it again."""
    return replay_namespace_from_job_source(
        configuration_sha256=job.configuration_sha256,
        pipeline_contract_version=job.pipeline_contract_version.value,
        model_config_sha256=job.model_config_sha256,
        source_revision_hashes=job.source_revision_hashes,
        index_snapshot_id=job.index_snapshot_id,
        companion_generation=job.companion_generation,
        semantic_generation=job.semantic_generation,
        source_index_generation=job.source_index_generation,
    )


def _replay_namespace_sha256(job: CurationJob) -> str:
    """Compatibility alias for tests; returns the canonical provider identity."""
    return _replay_namespace_for_job(job)


def _provider_event_id_cutoff(rows: list[dict[str, object]]) -> int:
    return max((_provider_event_id(row) for row in rows), default=0)


def _provider_event_id(row: dict[str, object]) -> int:
    identifier = row.get("id")
    if not isinstance(identifier, int) or identifier <= 0:
        raise RuntimeError("provider-attempt evidence lacks append-only event identifiers")
    return identifier


def _provider_endpoint(provider: str, model: str) -> str:
    if provider == "openai":
        return OpenAIProvider.url
    if provider == "anthropic":
        return AnthropicProvider.url
    if provider == "openrouter":
        return OpenRouterProvider.chat_url
    if provider == "gemini":
        return f"{GeminiProvider.base_url}/{quote(model, safe='')}:generateContent"
    raise ValueError("capture structured provider has no code endpoint")


class _CtypesWindowsJobApi:
    """Small stdlib-only wrapper; tests replace it with a deterministic fake."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        ctypes_module: Any = ctypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class _BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes = ctypes_module
        self._wintypes = wintypes
        self._extended_limit = _ExtendedLimit
        self._basic_accounting = _BasicAccounting
        self._kernel32 = ctypes_module.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _checked(self, result: Any, action: str) -> None:
        if not result:
            raise self._ctypes.WinError(self._ctypes.get_last_error(), action)

    def create_kill_on_close_job(self) -> int:
        handle = self._kernel32.CreateJobObjectW(None, None)
        self._checked(handle, "CreateJobObjectW")
        limits = self._extended_limit()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self._checked(
                self._kernel32.SetInformationJobObject(
                    handle,
                    self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    self._ctypes.byref(limits),
                    self._ctypes.sizeof(limits),
                ),
                "SetInformationJobObject",
            )
        except BaseException as primary_error:
            try:
                self.close_handle(handle)
            except BaseException as cleanup_error:
                primary_error.add_note(f"Job Object handle close failed: {cleanup_error}")
            raise
        return int(handle)

    def assign_process_handle(self, job_handle: int, process_handle: int) -> None:
        self._checked(
            self._kernel32.AssignProcessToJobObject(job_handle, process_handle),
            "AssignProcessToJobObject direct runtime handle",
        )

    def active_processes(self, job_handle: int) -> int:
        accounting = self._basic_accounting()
        self._checked(
            self._kernel32.QueryInformationJobObject(
                job_handle,
                self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                self._ctypes.byref(accounting),
                self._ctypes.sizeof(accounting),
                None,
            ),
            "QueryInformationJobObject",
        )
        return int(accounting.ActiveProcesses)

    def terminate_job(self, job_handle: int) -> None:
        self._checked(self._kernel32.TerminateJobObject(job_handle, 1), "TerminateJobObject")

    def send_ctrl_break(self, process_group_id: int) -> None:
        self._checked(
            self._kernel32.GenerateConsoleCtrlEvent(1, process_group_id),
            "GenerateConsoleCtrlEvent CTRL_BREAK_EVENT",
        )

    def close_handle(self, handle: int) -> None:
        self._checked(self._kernel32.CloseHandle(handle), "CloseHandle")


def _windows_job_api() -> _CtypesWindowsJobApi:
    if not _is_windows():
        raise RuntimeError("Windows Job Objects are unavailable on this platform")
    return _CtypesWindowsJobApi()


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_system_root() -> str:
    """Return the sole Windows host value admitted into a rehearsal child."""
    value = next((item for key, item in os.environ.items() if key.casefold() == "systemroot"), None)
    if not value:
        raise ValueError("Windows SYSTEMROOT is unavailable")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Windows SYSTEMROOT must be an existing absolute directory")
    return str(root.resolve())


def _load_exact_json_identity(path: Path, expected: dict[str, object]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value == expected


def _validate_runtime_dependency_closure(
    evidence: object, dependency_paths: list[str]
) -> dict[str, dict[str, str]]:
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"schema_version", "dependencies"}
        or evidence.get("schema_version") != 1
        or not isinstance(evidence.get("dependencies"), dict)
    ):
        raise RuntimeError("Windows dependency closure evidence is malformed")
    dependencies = cast(dict[object, object], evidence["dependencies"])
    if set(dependencies) != set(_RUNTIME_DEPENDENCY_MODULES):
        raise RuntimeError("Windows dependency closure evidence is incomplete")
    roots = [Path(value).resolve() for value in dependency_paths]
    validated: dict[str, dict[str, str]] = {}
    for name in _RUNTIME_DEPENDENCY_MODULES:
        item = dependencies.get(name)
        if not isinstance(item, dict) or set(item) != {"origin", "version"}:
            raise RuntimeError("Windows dependency closure evidence is malformed")
        origin_value = item.get("origin")
        version = item.get("version")
        if not isinstance(origin_value, str) or not isinstance(version, str) or not version:
            raise RuntimeError("Windows dependency closure evidence is malformed")
        origin = Path(origin_value)
        if (
            not origin.is_absolute()
            or not origin.is_file()
            or origin.is_symlink()
            or not any(origin.resolve().is_relative_to(root) for root in roots)
        ):
            raise RuntimeError("Windows dependency closure imported outside trusted paths")
        validated[name] = {"origin": str(origin.resolve()), "version": version}
    return validated


def _windows_popen_handle(process: subprocess.Popen[bytes]) -> int:
    handle = getattr(process, "_handle", None)
    if not isinstance(handle, int) or handle <= 0:
        raise RuntimeError("direct Windows runtime Popen handle is unavailable")
    return handle


def _terminate_rehearsal_process(process: subprocess.Popen[bytes], *, hard: bool) -> None:
    if hard:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_canonical_json(value), encoding="utf-8")
    os.replace(temporary, path)


def _bootstrap_dependency_paths() -> list[str]:
    import sysconfig

    paths: list[str] = []
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value and Path(value).is_dir() and value not in paths:
            paths.append(value)
    return paths


def _decode_json(value: bytes) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.decode("utf-8", errors="replace")


def _is_empty_structured_placeholder(path: Path) -> bool:
    """Allow only the semantic empty-JSON placeholder across native newlines."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return raw.strip() == "{}" and _decode_json(raw.encode("utf-8")) == {}


def _redact(value: Any) -> Any:
    return evidence_redact(value)


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, sort_keys=True, default=str)
    return (
        value
        if len(encoded) <= _MAX_BODY
        else {
            "truncated": True,
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "bytes": len(encoded),
        }
    )


def _redact_argv(argv: list[str]) -> list[str]:
    return [
        "[REDACTED]" if any(marker.lower() in item.lower() for marker in _SECRET_MARKERS) else item
        for item in argv
    ]


def _environment_evidence(environment: dict[str, str]) -> dict[str, Any]:
    return {
        key: {
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
            "value": "[REDACTED]"
            if any(marker in key.upper() for marker in _SECRET_MARKERS)
            else value,
        }
        for key, value in sorted(environment.items())
    }


def _write_deterministic_zip(destination: Path, records: dict[str, Any]) -> None:
    if destination.exists():
        raise ValueError("evidence destination already exists")
    entries = {name: serialize_evidence_record(value) for name, value in records.items()}
    digest_manifest = {
        name: hashlib.sha256(value).hexdigest() for name, value in sorted(entries.items())
    }
    entries["sha256-manifest.json"] = (
        json.dumps(digest_manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])


def _verify_evidence_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if archive.testzip() is not None:
                raise RuntimeError("failure-injection evidence ZIP CRC verification failed")
            manifest = json.loads(archive.read("sha256-manifest.json"))
            if not isinstance(manifest, dict):
                raise ValueError
            for name, expected in manifest.items():
                if not isinstance(name, str) or not isinstance(expected, str):
                    raise ValueError
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise RuntimeError("failure-injection evidence ZIP digest verification failed")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("failure-injection evidence ZIP is invalid") from exc


def _failure_injection_stage_order() -> tuple[CurationStage, ...]:
    return (
        CurationStage.CARD_LEDGER,
        CurationStage.CARD_PREFILTER,
        CurationStage.CARD_FAST_CLASSIFY,
        CurationStage.CARD_CLASSIFY,
        CurationStage.CARD_RESIDUAL,
        CurationStage.CARD_GAP_FILL,
        CurationStage.DEDUPE,
    )


def _failure_injection_stage_label(stage: CurationStage) -> str:
    labels = {
        CurationStage.CARD_LEDGER: "S2 CARD_LEDGER",
        CurationStage.CARD_PREFILTER: "S4a CARD_PREFILTER",
        CurationStage.CARD_FAST_CLASSIFY: "S4b CARD_FAST_CLASSIFY",
        CurationStage.CARD_CLASSIFY: "S4c CARD_CLASSIFY",
        CurationStage.CARD_RESIDUAL: "S6 CARD_RESIDUAL",
        CurationStage.CARD_GAP_FILL: "S7 CARD_GAP_FILL",
        CurationStage.DEDUPE: "S8 DEDUPE",
    }
    return labels[stage]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_output_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("operator mutable outputs must be absolute paths")
    canonical = Path(os.path.abspath(path))
    _reject_indirect_ancestors(canonical)
    return canonical


def _canonical_input_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("immutable input is unavailable or cannot be canonicalized") from exc


def _reject_indirect_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if os.path.lexists(current) and _is_indirect(current):
            raise ValueError("operator mutable output has a symlink or reparse-point ancestor")


def _path_contains(container: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(container)
    except ValueError:
        return False
    return True


def _file_descriptor(path: Path) -> dict[str, Any]:
    return {"sha256": _sha256_file(path), "bytes": path.stat().st_size}


def _capture_provider_ledger_projection(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Keep capture audit metadata while excluding raw provider response payloads."""
    return [{key: value for key, value in row.items() if key != "response_text"} for row in rows]


def _verify_replay_supplement(root: Path, expected_manifest_sha256: str | None) -> tuple[str, ...]:
    return tuple(sorted(_replay_supplement_entries(root, expected_manifest_sha256)))


def _supplement_is_populated(root: Path) -> bool:
    try:
        return json.loads((root / "structured.json").read_text(encoding="utf-8")) != {} or any(
            path.name.endswith(".npy") for path in (root / "vectors").rglob("*")
        )
    except (OSError, ValueError):
        return True


def _verify_replay_completion(
    path: Path | None,
    expected_sha256: str | None,
    *,
    supplement_root: Path,
    expected_manifest_sha256: str | None,
    expected_commit: str,
    expected_tree: str,
    expected_capsule_sha256: str,
    expected_pack_sha256: str | None,
    expected_failed_job_id: str | None = None,
    expected_replay_namespace: str | None = None,
) -> None:
    if path is None or expected_sha256 is None or not _is_sha256(expected_sha256):
        raise ValueError("populated replay supplement requires an exact completion manifest")
    components = (path.absolute(), *path.absolute().parents)
    if (
        any(_is_indirect(component) for component in components if component.exists())
        or not path.is_file()
        or _sha256_file(path) != expected_sha256
    ):
        raise ValueError("replay completion manifest is unavailable or mismatched")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("replay completion manifest is invalid") from exc
    required = {
        "schema_version",
        "authorization_sha256",
        "candidate",
        "capsule_manifest_sha256",
        "failed_job_id",
        "job_id",
        "source_tree_sha256",
        "replay_namespace_sha256",
        "server_audit_sha256",
        "pack_manifest_sha256",
        "ledger_sha256",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 1:
        raise ValueError("replay completion manifest is invalid")
    candidate = value.get("candidate")
    hashes = {
        "authorization_sha256",
        "capsule_manifest_sha256",
        "source_tree_sha256",
        "replay_namespace_sha256",
        "server_audit_sha256",
        "pack_manifest_sha256",
        "ledger_sha256",
    }
    if (
        candidate != {"commit": expected_commit, "tree": expected_tree}
        or value.get("capsule_manifest_sha256") != expected_capsule_sha256
        or value.get("pack_manifest_sha256") != expected_pack_sha256
        or any(
            not isinstance(value.get(key), str) or not _is_sha256(cast(str, value.get(key)))
            for key in hashes
        )
    ):
        raise ValueError("replay completion manifest bindings do not match")
    for key in ("failed_job_id", "job_id"):
        raw = value.get(key)
        try:
            if not isinstance(raw, str) or str(UUID(raw)) != raw:
                raise ValueError
        except ValueError as exc:
            raise ValueError("replay completion manifest has invalid job identities") from exc
    entries = _replay_supplement_entries(supplement_root, expected_manifest_sha256)
    lineage_path = supplement_root / "capture-lineage.json"
    if (
        "capture-lineage.json" not in entries
        or lineage_path.is_symlink()
        or not lineage_path.is_file()
    ):
        raise ValueError("replay completion lineage is unavailable")
    try:
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("replay completion lineage is invalid") from exc
    required_lineage = {
        "schema_version",
        "authorization_sha256",
        "failed_job_id",
        "source_tree_sha256",
        "replay_namespace_sha256",
        "server_audit_sha256",
    }
    if (
        not isinstance(lineage, dict)
        or set(lineage) != required_lineage
        or lineage.get("schema_version") != 1
        or any(
            not isinstance(lineage.get(key), str) or not _is_sha256(cast(str, lineage.get(key)))
            for key in (
                "authorization_sha256",
                "source_tree_sha256",
                "replay_namespace_sha256",
                "server_audit_sha256",
            )
        )
        or lineage.get("failed_job_id") != value.get("failed_job_id")
        or any(
            lineage.get(key) != value.get(key)
            for key in (
                "authorization_sha256",
                "source_tree_sha256",
                "replay_namespace_sha256",
                "server_audit_sha256",
            )
        )
    ):
        raise ValueError("replay completion lineage does not match the verified pack")
    if (
        expected_failed_job_id is not None
        and (
            value.get("failed_job_id") != expected_failed_job_id
            or lineage.get("failed_job_id") != expected_failed_job_id
        )
    ):
        raise ValueError("replay completion failed-job lineage does not match the overlay")
    if (
        expected_replay_namespace is not None
        and (
            value.get("replay_namespace_sha256") != expected_replay_namespace
            or lineage.get("replay_namespace_sha256") != expected_replay_namespace
        )
    ):
        raise ValueError("replay completion namespace lineage does not match the overlay")


def _validate_empty_replay_supplement(root: Path, expected_manifest_sha256: str | None) -> None:
    """Require semantic emptiness for the one authorized deterministic miss."""
    entries = _replay_supplement_entries(root, expected_manifest_sha256)
    structured = root / "structured.json"
    try:
        structured_value = json.loads(structured.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("first replay miss structured replay is malformed") from exc
    if structured_value != {}:
        raise ValueError("first replay miss requires an empty structured replay")
    if "capture-lineage.json" in entries:
        raise ValueError("first replay miss cannot consume capture lineage")
    vector_entries = [name for name in entries if name.startswith("vectors/")]
    if set(vector_entries).difference({"vectors/manifest.json"}):
        raise ValueError("first replay miss replay vectors must have no payload files")
    if "vectors/manifest.json" in entries:
        try:
            vector_manifest = json.loads((root / "vectors" / "manifest.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("first replay miss vector manifest is malformed") from exc
        if vector_manifest != {}:
            raise ValueError("first replay miss requires an empty vector manifest")


def _validate_empty_overlay_replay(root: Path) -> None:
    """Fail closed unless the materialized replay tree is exactly empty-pack safe."""
    replay = root / "replay"
    if replay.is_symlink() or not replay.is_dir():
        raise ValueError("first replay miss overlay replay directory is unavailable or indirect")
    files: set[str] = set()
    directories: set[str] = set()
    for path in replay.rglob("*"):
        if path.is_symlink():
            raise ValueError("first replay miss overlay replay contains a symbolic link")
        relative = path.relative_to(replay).as_posix()
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            files.add(relative)
        else:
            raise ValueError("first replay miss overlay replay contains an indirect entry")
    if files.difference({"structured.json", "vectors/manifest.json"}) or directories.difference(
        {"vectors"}
    ):
        raise ValueError("first replay miss overlay replay contains payload or unknown files")
    if "structured.json" not in files:
        raise ValueError("first replay miss overlay replay lacks structured.json")
    try:
        structured = json.loads((replay / "structured.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("first replay miss overlay structured replay is malformed") from exc
    if structured != {}:
        raise ValueError("first replay miss overlay structured replay is not empty")
    if "vectors" in directories and "vectors/manifest.json" not in files:
        raise ValueError("first replay miss overlay vectors directory lacks its empty manifest")
    if "vectors/manifest.json" in files:
        try:
            vector_manifest = json.loads(
                (replay / "vectors" / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("first replay miss overlay vector manifest is malformed") from exc
        if vector_manifest != {}:
            raise ValueError("first replay miss overlay vector manifest is not empty")


def _expected_empty_replay_miss(
    rows: list[dict[str, object]], replay_namespace_sha256: str, job_error: str
) -> dict[str, Any]:
    """Bind a safe terminal job error to exactly one durable provider call."""
    if not rows:
        raise RuntimeError("expected replay miss has no durable provider evidence")
    try:
        groups = _stable_logical_call_event_groups(rows, replay_namespace_sha256)
    except (KeyError, TypeError) as exc:
        raise RuntimeError("expected replay miss provider evidence is malformed") from exc
    if len(groups) != 1:
        raise RuntimeError("expected replay miss requires exactly one logical provider call")
    events = next(iter(groups.values()))
    if len(events) != 3:
        raise RuntimeError("expected replay miss provider chain has an unexpected topology")
    identity_keys = (
        "stage",
        "stage_attempt",
        "mode",
        "call_index",
        "subcall_ordinal",
        "kind",
        "request_sha256",
        "instruction_sha256",
        "input_sha256",
        "output_schema_sha256",
        "generation_parameters_sha256",
        "batch_note_ids_sha256",
    )
    first = events[0]
    event_ids = [row.get("id") for row in events]
    if not all(isinstance(value, int) for value in event_ids):
        raise RuntimeError("expected replay miss provider evidence is not append-only")
    ordered_event_ids = cast(list[int], event_ids)
    if ordered_event_ids != sorted(set(ordered_event_ids)):
        raise RuntimeError("expected replay miss provider evidence is not append-only")
    if any(not _has_safe_replay_identity(row, identity_keys) for row in events):
        raise RuntimeError("expected replay miss provider evidence lacks durable identity")
    if any(row.get(key) != first.get(key) for row in events for key in identity_keys):
        raise RuntimeError("expected replay miss provider chain changes identity")
    if [row.get("event") for row in events[:2]] != ["begun", "dispatched"]:
        raise RuntimeError("expected replay miss provider chain is not append-only")
    if any(
        row.get("response_text") is not None
        or row.get("response_sha256") is not None
        or row.get("event") in {"response_received", "accepted"}
        for row in events
    ):
        raise RuntimeError("expected replay miss provider evidence includes a response")
    terminal = events[-1]
    terminal_event = terminal.get("event")
    terminal_error = terminal.get("validation_error")
    if not isinstance(terminal_error, str):
        raise RuntimeError("expected replay miss provider terminal error is malformed")
    structured = _STRUCTURED_EMPTY_PACK_ERROR.fullmatch(job_error)
    if structured is not None:
        if terminal_event != "transport_failed" or terminal_error != job_error:
            raise RuntimeError("structured replay miss does not match durable provider evidence")
        if first.get("kind") not in {"primary", "repair"}:
            raise RuntimeError("structured replay miss has an unexpected provider call kind")
        expected_structured_topology = {
            "stage": CurationStage.CARD_LEDGER.value,
            "stage_attempt": 1,
            "mode": "canonical",
            "kind": "primary",
            "batch_index": 0,
            "batch_note_ids": [],
            "batch_note_ids_sha256": hashlib.sha256(b"[]").hexdigest(),
            "subcall_ordinal": 0,
        }
        if any(first.get(key) != value for key, value in expected_structured_topology.items()):
            raise RuntimeError("structured replay miss has an unexpected first-call topology")
        result: dict[str, Any] = {
            "kind": "structured_empty_pack",
            "key": structured.group(1),
        }
    elif job_error == _VECTOR_EMPTY_PACK_ERROR:
        raise RuntimeError("vector replay miss cannot satisfy the empty structured-pack smoke goal")
    else:
        raise RuntimeError("terminal job failure is not a recognized deterministic replay miss")
    result.update(
        {
            "stage": first["stage"],
            "stage_attempt": first["stage_attempt"],
            "call_index": first["call_index"],
            "subcall_ordinal": first["subcall_ordinal"],
            "request_sha256": first["request_sha256"],
            "provider_chain": [str(row["event"]) for row in events],
        }
    )
    return result


def _has_safe_replay_identity(row: dict[str, object], keys: tuple[str, ...]) -> bool:
    if not isinstance(row.get("id"), int) or cast(int, row["id"]) <= 0:
        return False
    if not isinstance(row.get("stage"), str) or not isinstance(row.get("mode"), str):
        return False
    if row.get("stage_attempt") is None or row.get("call_index") is None:
        return False
    if not isinstance(row.get("stage_attempt"), int) or not isinstance(row.get("call_index"), int):
        return False
    if cast(int, row["stage_attempt"]) < 1 or cast(int, row["call_index"]) < 1:
        return False
    if not isinstance(row.get("subcall_ordinal"), int) or cast(int, row["subcall_ordinal"]) < 0:
        return False
    for key in keys:
        if key.endswith("sha256") and not isinstance(row.get(key), str):
            return False
        if key.endswith("sha256") and not _is_sha256(cast(str, row[key])):
            return False
    return isinstance(row.get("kind"), str)


def _smoke_http_transcript(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep route/status evidence while excluding request and response plaintext."""
    redacted: list[dict[str, Any]] = []
    for item in transcript:
        method, path, status = item.get("method"), item.get("path"), item.get("status")
        if not isinstance(method, str) or not isinstance(path, str) or not isinstance(status, int):
            raise RuntimeError("expected replay miss HTTP transcript is malformed")
        redacted.append(
            {
                "method": method,
                "path": path,
                "status": status,
                "request_sha256": hashlib.sha256(
                    _canonical_json(item.get("request_body")).encode()
                ).hexdigest(),
                "response_sha256": hashlib.sha256(
                    _canonical_json(item.get("response_body")).encode()
                ).hexdigest(),
            }
        )
    return redacted


def _replay_supplement_entries(
    root: Path, expected_manifest_sha256: str | None
) -> dict[str, dict[str, object]]:
    if expected_manifest_sha256 is None or not _is_sha256(expected_manifest_sha256):
        raise ValueError("replay supplement requires an operator-supplied manifest SHA-256")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("replay supplement directory is unavailable")
    manifest_path = root / _REPLAY_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("replay supplement manifest is unavailable")
    if _sha256_file(manifest_path) != expected_manifest_sha256:
        raise ValueError("replay supplement manifest SHA-256 does not match operator identity")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("replay supplement manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "manifest_rule", "files"}
        or manifest.get("schema_version") != 1
        or manifest.get("manifest_rule") != "self-excluding"
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("replay supplement manifest is invalid")
    expected: dict[str, dict[str, object]] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise ValueError("replay supplement manifest has invalid file entries")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not _replay_relative_is_allowed(relative)
            or relative in expected
            or not isinstance(entry.get("bytes"), int)
            or entry["bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or not _is_sha256(entry["sha256"])
        ):
            raise ValueError("replay supplement manifest has unsafe file entries")
        expected[relative] = entry
    if "structured.json" not in expected:
        raise ValueError("replay supplement requires structured.json")
    observed: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("replay supplement contains a symbolic link")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == _REPLAY_MANIFEST_NAME:
            continue
        if not _replay_relative_is_allowed(relative):
            raise ValueError("replay supplement contains an unknown or sensitive file")
        entry = expected.get(relative)
        if entry is None:
            raise ValueError("replay supplement contains an unmanifested file")
        if path.stat().st_size != entry["bytes"] or _sha256_file(path) != entry["sha256"]:
            raise ValueError("replay supplement file integrity changed")
        observed.add(relative)
    if observed != set(expected):
        raise ValueError("replay supplement files are missing")
    return expected


def _replay_relative_is_allowed(relative: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        return False
    try:
        _reject_sensitive_path(relative)
    except CapsuleIntegrityError:
        return False
    return relative in {"structured.json", "capture-lineage.json"} or relative.startswith(
        "vectors/"
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _load_runtime_ledger(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"runtime evidence is missing: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"runtime evidence is malformed: {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"runtime evidence is malformed: {path.name}")
    return payload


def _validate_adapter_ledger(payload: dict[str, Any], nonce: str) -> None:
    if (
        set(payload) != {"schema_version", "run_nonce", "records"}
        or payload.get("schema_version") != 1
        or payload.get("run_nonce") != nonce
        or not isinstance(payload.get("records"), list)
    ):
        raise RuntimeError("read-only mutation evidence is stale or malformed")
    for ordinal, record in enumerate(payload["records"], 1):
        if (
            not isinstance(record, dict)
            or set(record) != {"action", "ordinal", "timestamp", "outcome"}
            or not isinstance(record.get("action"), str)
            or record.get("ordinal") != ordinal
            or not isinstance(record.get("timestamp"), str)
            or record.get("outcome") != "denied"
        ):
            raise RuntimeError("read-only mutation evidence has an invalid sequence")


def _validate_egress_ledger(
    payload: dict[str, Any], nonce: str, mode: Mode, *, require_clean_lifecycle: bool = True
) -> None:
    if (
        set(payload) != {"schema_version", "run_nonce", "mode", "records"}
        or payload.get("schema_version") != 1
        or payload.get("run_nonce") != nonce
        or payload.get("mode") != mode
        or not isinstance(payload.get("records"), list)
    ):
        raise RuntimeError("egress evidence is stale or malformed")
    records = payload["records"]
    markers: list[str] = []
    expected = {
        "kind",
        "mode",
        "host",
        "port",
        "resolved_address",
        "allowed",
        "ordinal",
        "timestamp",
    }
    for ordinal, record in enumerate(records, 1):
        if (
            not isinstance(record, dict)
            or set(record) != expected
            or record.get("mode") != mode
            or record.get("ordinal") != ordinal
            or not isinstance(record.get("timestamp"), str)
        ):
            raise RuntimeError("egress evidence has an invalid sequence")
        kind = record.get("kind")
        if kind in {"startup", "shutdown"}:
            if any(
                record.get(key) is not None
                for key in ("host", "port", "resolved_address", "allowed")
            ):
                raise RuntimeError("egress evidence marker is malformed")
            markers.append(kind)
        elif not (
            kind == "authorization"
            and isinstance(record.get("host"), str)
            and isinstance(record.get("port"), int)
            and (
                record.get("resolved_address") is None
                or isinstance(record.get("resolved_address"), str)
            )
            and isinstance(record.get("allowed"), bool)
        ):
            raise RuntimeError("egress evidence authorization is malformed")
    if not markers or markers[0] != "startup":
        raise RuntimeError("egress evidence lacks startup marker")
    if require_clean_lifecycle:
        if markers[-1] != "shutdown":
            raise RuntimeError("egress evidence lacks clean lifecycle markers")
        # A restarted process may legitimately follow one crash-validated
        # startup without a shutdown.  The final child still has to close its
        # own lifecycle, and repeated unaccounted starts remain fail-closed.
        if markers.count("startup") - markers.count("shutdown") not in {0, 1}:
            raise RuntimeError("egress evidence has incomplete lifecycle markers")


def _require_no_denied_egress_authorizations(
    records: list[object], phase: str
) -> list[dict[str, Any]]:
    """Lifecycle markers are expected; denied authorization attempts are not."""
    authorizations = [
        row for row in records if isinstance(row, dict) and row.get("kind") == "authorization"
    ]
    if any(row.get("allowed") is False for row in authorizations):
        raise RuntimeError(f"{phase} evidence records denied or forbidden egress authorization")
    return authorizations
