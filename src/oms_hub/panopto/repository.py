import json
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.models import (
    LectureModel,
    LectureStepModel,
    OpenAIUsageModel,
    PanoptoBrowserCommandModel,
    PanoptoBrowserRequestModel,
    PanoptoConnectionModel,
    PanoptoRecordingModel,
    PanoptoRecordingSourceModel,
    TranscriptJobModel,
    TranscriptRevisionModel,
)
from oms_hub.panopto.browser_domain import (
    BrowserCommand,
    BrowserCommandKind,
    BrowserRequest,
    BrowserRequestKind,
)
from oms_hub.panopto.domain import (
    PanoptoSession,
    RecordingDisposition,
    RecordingMatch,
    TranscriptAction,
    TranscriptJobState,
)


class PanoptoRepository:
    def __init__(
        self,
        database: Database,
        tenant_url: str = "https://lmunet.hosted.panopto.com",
    ):
        self.database = database
        self.tenant_url = tenant_url.rstrip("/")

    def connection(self) -> PanoptoConnectionModel:
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
                db_session.flush()
            return connection

    def set_enabled(self, enabled: bool) -> None:
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.enabled = enabled
            connection.state = "enabled" if enabled else "paused"

    def queue_browser_command(
        self,
        kind: BrowserCommandKind,
        payload: dict[str, object],
        now_utc: datetime,
        *,
        retry_running: bool = False,
    ) -> str:
        with self.database.session() as db_session:
            existing = db_session.scalar(
                select(PanoptoBrowserCommandModel)
                .where(
                    PanoptoBrowserCommandModel.kind == kind.value,
                    PanoptoBrowserCommandModel.state.in_(("pending", "running")),
                )
                .order_by(PanoptoBrowserCommandModel.created_at)
            )
            if existing is not None:
                if retry_running and existing.state == "running":
                    existing.state = "pending"
                    existing.payload_json = json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    existing.created_at = now_utc.isoformat()
                    existing.claimed_at = None
                    existing.completed_at = None
                    existing.error_code = None
                return existing.id
            command_id = str(uuid.uuid4())
            db_session.add(
                PanoptoBrowserCommandModel(
                    id=command_id,
                    kind=kind.value,
                    payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    created_at=now_utc.isoformat(),
                )
            )
            return command_id

    def create_browser_request(
        self,
        kind: BrowserRequestKind,
        payload: dict[str, object],
        now_utc: datetime,
    ) -> str:
        request_id = str(uuid.uuid4())
        with self.database.session() as db_session:
            db_session.add(
                PanoptoBrowserRequestModel(
                    id=request_id,
                    kind=kind.value,
                    payload_json=json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    requested_at=now_utc.isoformat(),
                )
            )
        return request_id

    def next_browser_request(self, now_utc: datetime) -> BrowserRequest | None:
        with self.database.session() as db_session:
            request = db_session.scalar(
                select(PanoptoBrowserRequestModel)
                .where(
                    PanoptoBrowserRequestModel.state.in_(
                        (
                            "requested",
                            "running",
                            "awaiting_login",
                            "waiting_for_captions",
                        )
                    ),
                    or_(
                        PanoptoBrowserRequestModel.next_eligible_at.is_(None),
                        PanoptoBrowserRequestModel.next_eligible_at
                        <= now_utc.isoformat(),
                    ),
                )
                .order_by(PanoptoBrowserRequestModel.requested_at)
            )
            if request is None:
                return None
            payload = json.loads(request.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("Panopto browser request payload is invalid")
            return BrowserRequest(
                request.id,
                BrowserRequestKind(request.kind),
                request.state,
                payload,
                request.progress,
                request.error_code,
            )

    def get_browser_request(self, request_id: str) -> BrowserRequest | None:
        with self.database.session() as db_session:
            request = db_session.get(PanoptoBrowserRequestModel, request_id)
            if request is None:
                return None
            payload = json.loads(request.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("Panopto browser request payload is invalid")
            return BrowserRequest(
                request.id,
                BrowserRequestKind(request.kind),
                request.state,
                payload,
                request.progress,
                request.error_code,
            )

    def latest_browser_request(self) -> BrowserRequest | None:
        with self.database.session() as db_session:
            request = db_session.scalar(
                select(PanoptoBrowserRequestModel).order_by(
                    PanoptoBrowserRequestModel.requested_at.desc()
                )
            )
            if request is None:
                return None
            payload = json.loads(request.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("Panopto browser request payload is invalid")
            return BrowserRequest(
                request.id,
                BrowserRequestKind(request.kind),
                request.state,
                payload,
                request.progress,
                request.error_code,
            )

    def update_browser_request(
        self,
        request_id: str,
        state: str,
        progress: str,
        now_utc: datetime,
        error_code: str | None = None,
    ) -> None:
        with self.database.session() as db_session:
            request = db_session.get(PanoptoBrowserRequestModel, request_id)
            if request is None:
                raise KeyError(request_id)
            request.state = state[:40]
            request.progress = progress[:80]
            request.error_code = error_code[:80] if error_code else None
            request.next_eligible_at = None
            if request.started_at is None:
                request.started_at = now_utc.isoformat()

    def wait_browser_request(
        self,
        request_id: str,
        reason_code: str,
        next_eligible_at: datetime,
        now_utc: datetime,
    ) -> None:
        with self.database.session() as db_session:
            request = db_session.get(PanoptoBrowserRequestModel, request_id)
            if request is None:
                raise KeyError(request_id)
            request.state = "waiting_for_captions"
            request.progress = "captions_pending"
            request.error_code = reason_code[:80]
            request.next_eligible_at = next_eligible_at.isoformat()
            if request.started_at is None:
                request.started_at = now_utc.isoformat()

    def complete_browser_request(
        self,
        request_id: str,
        now_utc: datetime,
    ) -> None:
        with self.database.session() as db_session:
            request = db_session.get(PanoptoBrowserRequestModel, request_id)
            if request is None:
                raise KeyError(request_id)
            request.state = "complete"
            request.progress = "complete"
            request.completed_at = now_utc.isoformat()
            request.next_eligible_at = None
            request.error_code = None

    def fail_browser_request(
        self,
        request_id: str,
        reason_code: str,
        now_utc: datetime,
    ) -> None:
        with self.database.session() as db_session:
            request = db_session.get(PanoptoBrowserRequestModel, request_id)
            if request is None:
                raise KeyError(request_id)
            request.state = "failed"
            request.progress = "failed"
            request.completed_at = now_utc.isoformat()
            request.next_eligible_at = None
            request.error_code = reason_code[:80]

    def supersede_legacy_browser_commands(self, now_utc: datetime) -> int:
        count = 0
        with self.database.session() as db_session:
            commands = db_session.scalars(
                select(PanoptoBrowserCommandModel).where(
                    PanoptoBrowserCommandModel.state.in_(("pending", "running"))
                )
            ).all()
            for command in commands:
                command.state = "failed"
                command.completed_at = now_utc.isoformat()
                command.error_code = "superseded_command_model"
                count += 1
        return count

    def claim_browser_command(self, now_utc: datetime) -> BrowserCommand | None:
        with self.database.session() as db_session:
            command = db_session.scalar(
                select(PanoptoBrowserCommandModel)
                .where(PanoptoBrowserCommandModel.state == "pending")
                .order_by(PanoptoBrowserCommandModel.created_at)
            )
            if command is None:
                return None
            payload = json.loads(command.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("Panopto browser command payload is invalid")
            command.state = "running"
            command.claimed_at = now_utc.isoformat()
            return BrowserCommand(
                command.id,
                BrowserCommandKind(command.kind),
                payload,
            )

    def get_running_browser_command(self, command_id: str) -> BrowserCommand | None:
        with self.database.session() as db_session:
            command = db_session.get(PanoptoBrowserCommandModel, command_id)
            if command is None or command.state != "running":
                return None
            payload = json.loads(command.payload_json)
            if not isinstance(payload, dict):
                raise TypeError("Panopto browser command payload is invalid")
            return BrowserCommand(
                command.id,
                BrowserCommandKind(command.kind),
                payload,
            )

    def complete_browser_command(self, command_id: str, now_utc: datetime) -> None:
        with self.database.session() as db_session:
            command = db_session.get(PanoptoBrowserCommandModel, command_id)
            if command is None:
                raise KeyError(command_id)
            command.state = "complete"
            command.completed_at = now_utc.isoformat()
            command.error_code = None

    def fail_browser_command(
        self,
        command_id: str,
        now_utc: datetime,
        error_code: str,
    ) -> None:
        with self.database.session() as db_session:
            command = db_session.get(PanoptoBrowserCommandModel, command_id)
            if command is None:
                raise KeyError(command_id)
            command.state = "failed"
            command.completed_at = now_utc.isoformat()
            command.error_code = error_code[:80]

    def recover_stale_browser_commands(
        self,
        now_utc: datetime,
        timeout_seconds: int = 300,
    ) -> int:
        cutoff = now_utc - timedelta(seconds=timeout_seconds)
        recovered = 0
        with self.database.session() as db_session:
            commands = db_session.scalars(
                select(PanoptoBrowserCommandModel).where(
                    PanoptoBrowserCommandModel.state == "running"
                )
            ).all()
            for command in commands:
                if not command.claimed_at:
                    continue
                claimed = datetime.fromisoformat(command.claimed_at)
                if claimed <= cutoff:
                    command.state = "pending"
                    command.claimed_at = None
                    recovered += 1
        return recovered

    def heartbeat(
        self,
        state: str,
        now_utc: datetime,
        error: str | None = None,
    ) -> None:
        allowed = {
            "companion_unavailable",
            "panopto_login_required",
            "connected",
            "scanning",
            "waiting_for_transcript",
            "needs_review",
            "error",
        }
        if state not in allowed:
            raise ValueError("Panopto browser state is invalid")
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.state = state
            connection.last_error = error[:1000] if error else None
            connection.scan_requested_at = now_utc.isoformat()

    def set_recording_source(self, recording_id: int, viewer_url: str) -> None:
        parsed = urlparse(viewer_url)
        session_values = parse_qs(parsed.query).get("id", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname != "lmunet.hosted.panopto.com"
            or parsed.path != "/Panopto/Pages/Viewer.aspx"
            or len(session_values) != 1
        ):
            raise ValueError("Viewer URL must use the LMU Panopto viewer")
        with self.database.session() as db_session:
            source = db_session.scalar(
                select(PanoptoRecordingSourceModel).where(
                    PanoptoRecordingSourceModel.recording_id == recording_id
                )
            )
            if source is None:
                source = PanoptoRecordingSourceModel(
                    recording_id=recording_id,
                    viewer_url=viewer_url,
                )
                db_session.add(source)
            else:
                source.viewer_url = viewer_url

    def get_recording_source(self, recording_id: int) -> str | None:
        with self.database.session() as db_session:
            source = db_session.scalar(
                select(PanoptoRecordingSourceModel).where(
                    PanoptoRecordingSourceModel.recording_id == recording_id
                )
            )
            return source.viewer_url if source is not None else None

    def request_scan(self, now_utc: datetime | None = None) -> None:
        requested_at = now_utc or datetime.now().astimezone()
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.scan_requested_at = requested_at.isoformat()

    def mark_poll_success(self, now_utc: datetime) -> None:
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.last_successful_poll = now_utc.isoformat()
            connection.last_error = None

    def approve_prompt(self, sha256: str, prompt_path: str | None = None) -> None:
        if len(sha256) != 64:
            raise ValueError("Prompt SHA-256 is invalid")
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.approved_prompt_sha256 = sha256
            if prompt_path is not None:
                connection.prompt_path = prompt_path

    def mark_acceptance_validated(self, now_utc: datetime | None = None) -> None:
        validated_at = now_utc or datetime.now().astimezone()
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.acceptance_validated_at = validated_at.isoformat()

    def reset_acceptance(self) -> None:
        with self.database.session() as db_session:
            connection = db_session.scalar(
                select(PanoptoConnectionModel).where(
                    PanoptoConnectionModel.tenant_url == self.tenant_url
                )
            )
            if connection is None:
                connection = PanoptoConnectionModel(tenant_url=self.tenant_url)
                db_session.add(connection)
            connection.acceptance_validated_at = None
            connection.enabled = False
            connection.state = "paused"

    def upsert_recording(
        self,
        panopto_session: PanoptoSession,
        match: RecordingMatch,
    ) -> RecordingDisposition:
        with self.database.session() as db_session:
            recording = db_session.scalar(
                select(PanoptoRecordingModel).where(
                    PanoptoRecordingModel.session_id == panopto_session.session_id
                )
            )
            created = recording is None
            review_state = "needs_review" if match.needs_review else "none"
            if recording is None:
                recording = PanoptoRecordingModel(
                    session_id=panopto_session.session_id,
                    name=panopto_session.name,
                    created_utc=panopto_session.created_utc.isoformat(),
                    duration_seconds=panopto_session.duration_seconds,
                    folder_name=panopto_session.folder_name,
                    content_language=panopto_session.content_language,
                    lecture_id=match.lecture_id,
                    confidence=match.confidence,
                    evidence_json=json.dumps(match.evidence),
                    review_state=review_state,
                )
                db_session.add(recording)
                db_session.flush()
            else:
                recording.name = panopto_session.name
                recording.created_utc = panopto_session.created_utc.isoformat()
                recording.duration_seconds = panopto_session.duration_seconds
                recording.folder_name = panopto_session.folder_name
                recording.content_language = panopto_session.content_language
                recording.lecture_id = match.lecture_id
                recording.confidence = match.confidence
                recording.evidence_json = json.dumps(match.evidence)
                recording.review_state = review_state

            if match.lecture_id is not None and not match.needs_review:
                step = db_session.scalar(
                    select(LectureStepModel).where(
                        LectureStepModel.lecture_id == match.lecture_id,
                        LectureStepModel.name
                        == LectureStepName.PANOPTO_RECORDING_FOUND.value,
                    )
                )
                if step is not None:
                    step.status = StepStatus.COMPLETE.value
                    step.detail = f"Panopto session {panopto_session.session_id}"

            return RecordingDisposition(recording.id, created, match.needs_review)

    def create_raw_revision(
        self,
        recording_id: int,
        raw_sha256: str,
        raw_path: str,
    ) -> TranscriptRevisionModel:
        with self.database.session() as db_session:
            revision = db_session.scalar(
                select(TranscriptRevisionModel).where(
                    TranscriptRevisionModel.recording_id == recording_id,
                    TranscriptRevisionModel.raw_sha256 == raw_sha256,
                )
            )
            if revision is None:
                revision = TranscriptRevisionModel(
                    recording_id=recording_id,
                    raw_sha256=raw_sha256,
                    raw_path=raw_path,
                )
                db_session.add(revision)
                db_session.flush()
            return revision

    def get_recording(self, recording_id: int) -> PanoptoRecordingModel:
        with self.database.session() as db_session:
            recording = db_session.get(PanoptoRecordingModel, recording_id)
            if recording is None:
                raise KeyError(recording_id)
            return recording

    def list_review_recordings(self) -> list[PanoptoRecordingModel]:
        with self.database.session() as db_session:
            return list(
                db_session.scalars(
                    select(PanoptoRecordingModel)
                    .where(PanoptoRecordingModel.review_state == "needs_review")
                    .order_by(
                        PanoptoRecordingModel.discovered_at.desc(),
                        PanoptoRecordingModel.id.desc(),
                    )
                ).all()
            )

    def list_review_jobs(self) -> list[TranscriptJobModel]:
        with self.database.session() as db_session:
            return list(
                db_session.scalars(
                    select(TranscriptJobModel)
                    .where(
                        TranscriptJobModel.state.in_(
                            (
                                TranscriptJobState.NEEDS_REVIEW.value,
                                TranscriptJobState.FAILED.value,
                            )
                        )
                    )
                    .order_by(
                        TranscriptJobModel.updated_at.desc(),
                        TranscriptJobModel.id.desc(),
                    )
                ).all()
            )

    def pending_review_count(self) -> int:
        with self.database.session() as db_session:
            recording_count = db_session.scalar(
                select(func.count(PanoptoRecordingModel.id)).where(
                    PanoptoRecordingModel.review_state == "needs_review"
                )
            )
            job_count = db_session.scalar(
                select(func.count(TranscriptJobModel.id)).where(
                    TranscriptJobModel.state.in_(
                        (
                            TranscriptJobState.NEEDS_REVIEW.value,
                            TranscriptJobState.FAILED.value,
                        )
                    )
                )
            )
            return int(recording_count or 0) + int(job_count or 0)

    def usage_totals(self) -> tuple[int, int, int]:
        with self.database.session() as db_session:
            row = db_session.execute(
                select(
                    func.coalesce(func.sum(OpenAIUsageModel.input_tokens), 0),
                    func.coalesce(func.sum(OpenAIUsageModel.output_tokens), 0),
                    func.coalesce(func.sum(OpenAIUsageModel.cost_microusd), 0),
                )
            ).one()
            return int(row[0]), int(row[1]), int(row[2])

    def remap_recording(self, recording_id: int, lecture_id: int) -> None:
        with self.database.session() as db_session:
            recording = db_session.get(PanoptoRecordingModel, recording_id)
            lecture = db_session.get(LectureModel, lecture_id)
            if recording is None or lecture is None:
                raise KeyError((recording_id, lecture_id))
            recording.lecture_id = lecture_id
            recording.confidence = 1.0
            recording.review_state = "none"
            recording.evidence_json = json.dumps(("manually remapped",))
            self._set_step(
                db_session,
                lecture_id,
                LectureStepName.PANOPTO_RECORDING_FOUND,
                StepStatus.COMPLETE,
                f"Panopto session {recording.session_id} manually matched",
            )

    def get_revision(self, revision_id: int) -> TranscriptRevisionModel:
        with self.database.session() as db_session:
            revision = db_session.get(TranscriptRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            return revision

    def get_job(
        self,
        revision_id: int,
        action: TranscriptAction,
    ) -> TranscriptJobModel | None:
        with self.database.session() as db_session:
            return db_session.scalar(
                select(TranscriptJobModel).where(
                    TranscriptJobModel.revision_id == revision_id,
                    TranscriptJobModel.action == action.value,
                )
            )

    def finalize_download(self, revision_id: int, raw_path: str) -> None:
        with self.database.session() as db_session:
            revision = db_session.get(TranscriptRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            revision.raw_path = raw_path
            revision.state = "downloaded"
            recording = db_session.get(PanoptoRecordingModel, revision.recording_id)
            if recording is None:
                raise ValueError("Panopto recording is missing")
            if recording.lecture_id is not None:
                self._set_step(
                    db_session,
                    recording.lecture_id,
                    LectureStepName.TRANSCRIPT_DOWNLOADED,
                    StepStatus.COMPLETE,
                    f"Immutable raw transcript revision {revision.id}",
                )
            job = db_session.scalar(
                select(TranscriptJobModel).where(
                    TranscriptJobModel.revision_id == revision.id,
                    TranscriptJobModel.action == TranscriptAction.CLEAN.value,
                )
            )
            if job is None:
                db_session.add(
                    TranscriptJobModel(
                        revision_id=revision.id,
                        action=TranscriptAction.CLEAN.value,
                        state=TranscriptJobState.QUEUED.value,
                    )
                )

    def complete_clean(
        self,
        job_id: int,
        cleaned_path: str,
        cleaned_sha256: str,
        prompt_sha256: str,
        *,
        model: str,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
    ) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            revision = db_session.get(TranscriptRevisionModel, job.revision_id)
            if revision is None:
                raise ValueError("Transcript revision is missing")
            revision.cleaned_path = cleaned_path
            revision.cleaned_sha256 = cleaned_sha256
            revision.prompt_sha256 = prompt_sha256
            revision.state = "cleaned"
            job.state = TranscriptJobState.COMPLETE.value
            job.error = None
            job.next_attempt_at = None
            usage = db_session.scalar(
                select(OpenAIUsageModel).where(
                    OpenAIUsageModel.revision_id == revision.id
                )
            )
            if usage is None:
                usage = OpenAIUsageModel(
                    revision_id=revision.id,
                    model=model,
                    request_id=request_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_microusd=cost_microusd,
                )
                db_session.add(usage)
            recording = db_session.get(PanoptoRecordingModel, revision.recording_id)
            if recording is None:
                raise ValueError("Panopto recording is missing")
            if recording.lecture_id is not None:
                self._set_step(
                    db_session,
                    recording.lecture_id,
                    LectureStepName.TRANSCRIPT_CLEANED,
                    StepStatus.COMPLETE,
                    f"Cleaned transcript revision {revision.id}",
                )
            file_job = db_session.scalar(
                select(TranscriptJobModel).where(
                    TranscriptJobModel.revision_id == revision.id,
                    TranscriptJobModel.action == TranscriptAction.FILE.value,
                )
            )
            if file_job is None:
                db_session.add(
                    TranscriptJobModel(
                        revision_id=revision.id,
                        action=TranscriptAction.FILE.value,
                        state=TranscriptJobState.QUEUED.value,
                    )
                )

    def complete_file(
        self,
        job_id: int,
        canonical_path: str,
        canonical_sha256: str,
    ) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            revision = db_session.get(TranscriptRevisionModel, job.revision_id)
            if revision is None:
                raise ValueError("Transcript revision is missing")
            if revision.cleaned_sha256 != canonical_sha256:
                raise ValueError("Canonical transcript checksum does not match cleaned revision")
            recording = db_session.get(PanoptoRecordingModel, revision.recording_id)
            if recording is None or recording.lecture_id is None:
                raise ValueError("Transcript has no matched lecture")
            prior_revisions = list(
                db_session.scalars(
                    select(TranscriptRevisionModel)
                    .join(
                        PanoptoRecordingModel,
                        TranscriptRevisionModel.recording_id == PanoptoRecordingModel.id,
                    )
                    .where(PanoptoRecordingModel.lecture_id == recording.lecture_id)
                ).all()
            )
            for stored_revision in prior_revisions:
                stored_revision.current = stored_revision.id == revision.id
            revision.canonical_path = canonical_path
            revision.state = "filed"
            job.state = TranscriptJobState.COMPLETE.value
            job.error = None
            job.next_attempt_at = None
            self._set_step(
                db_session,
                recording.lecture_id,
                LectureStepName.TRANSCRIPT_FILED,
                StepStatus.COMPLETE,
                f"Filed transcript: {canonical_path}",
            )

    def mark_job_for_review(self, job_id: int, error: str) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            job.state = TranscriptJobState.NEEDS_REVIEW.value
            job.error = error[:1000]
            job.next_attempt_at = None

    def mark_job_transient(
        self,
        job_id: int,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            job.attempts += 1
            job.error = error[:1000]
            if job.attempts >= 3:
                job.state = TranscriptJobState.FAILED.value
                job.next_attempt_at = None
            else:
                job.state = TranscriptJobState.WAITING.value
                job.next_attempt_at = next_attempt_at.isoformat()

    def running_jobs(self) -> list[TranscriptJobModel]:
        with self.database.session() as db_session:
            return list(
                db_session.scalars(
                    select(TranscriptJobModel).where(
                        TranscriptJobModel.state == TranscriptJobState.RUNNING.value
                    )
                ).all()
            )

    def requeue_job(self, job_id: int) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            job.state = TranscriptJobState.QUEUED.value
            job.error = None
            job.next_attempt_at = None

    def retry_job(self, job_id: int) -> None:
        with self.database.session() as db_session:
            job = db_session.get(TranscriptJobModel, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state not in {
                TranscriptJobState.FAILED.value,
                TranscriptJobState.NEEDS_REVIEW.value,
            }:
                raise ValueError("Transcript job is not eligible for retry")
            job.state = TranscriptJobState.QUEUED.value
            job.attempts = 0
            job.error = None
            job.next_attempt_at = None

    def queue_job(self, revision_id: int, action: TranscriptAction) -> None:
        with self.database.session() as db_session:
            job = db_session.scalar(
                select(TranscriptJobModel).where(
                    TranscriptJobModel.revision_id == revision_id,
                    TranscriptJobModel.action == action.value,
                )
            )
            if job is None:
                db_session.add(
                    TranscriptJobModel(
                        revision_id=revision_id,
                        action=action.value,
                        state=TranscriptJobState.QUEUED.value,
                    )
                )

    def claim_next_job(self, now_utc: datetime) -> TranscriptJobModel | None:
        with self.database.session() as db_session:
            job = db_session.scalar(
                select(TranscriptJobModel)
                .where(
                    TranscriptJobModel.state.in_(
                        (
                            TranscriptJobState.QUEUED.value,
                            TranscriptJobState.WAITING.value,
                        )
                    ),
                    or_(
                        TranscriptJobModel.next_attempt_at.is_(None),
                        TranscriptJobModel.next_attempt_at <= now_utc.isoformat(),
                    ),
                )
                .order_by(TranscriptJobModel.created_at, TranscriptJobModel.id)
                .limit(1)
            )
            if job is None:
                return None
            job.state = TranscriptJobState.RUNNING.value
            job.error = None
            db_session.flush()
            return job

    def job_count(self, revision_id: int, action: TranscriptAction) -> int:
        with self.database.session() as db_session:
            return int(
                db_session.scalar(
                    select(func.count(TranscriptJobModel.id)).where(
                        TranscriptJobModel.revision_id == revision_id,
                        TranscriptJobModel.action == action.value,
                    )
                )
                or 0
            )

    @staticmethod
    def _set_step(
        db_session: Session,
        lecture_id: int,
        name: LectureStepName,
        status: StepStatus,
        detail: str | None,
    ) -> None:
        step = db_session.scalar(
            select(LectureStepModel).where(
                LectureStepModel.lecture_id == lecture_id,
                LectureStepModel.name == name.value,
            )
        )
        if step is None:
            raise KeyError((lecture_id, name.value))
        step.status = status.value
        step.detail = detail
