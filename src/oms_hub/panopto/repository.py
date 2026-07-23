import json
from datetime import datetime

from sqlalchemy import func, or_, select

from oms_hub.db import Database
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.models import (
    LectureStepModel,
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
