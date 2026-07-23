import json
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.models import (
    LectureStepModel,
    OpenAIUsageModel,
    PanoptoConnectionModel,
    PanoptoRecordingModel,
    TranscriptJobModel,
    TranscriptRevisionModel,
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
