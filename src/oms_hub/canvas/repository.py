import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from oms_hub.canvas.domain import (
    CanvasAttachment,
    CatalogMatch,
    Classification,
    CourseMappingInput,
    DispositionContext,
    FailedRevisionReview,
    JobState,
    MetadataResult,
    RevisionState,
    ReviewState,
    SourceKind,
)
from oms_hub.db import Database
from oms_hub.models import (
    ArtifactModel,
    CanvasConnectionModel,
    CanvasCourseMappingModel,
    CanvasSourceItemModel,
    ProcessingJobModel,
    SourceRevisionModel,
)
from oms_hub.models import utc_now

APPROVED_SUBJECTS = {
    "Neuro",
    "MSK",
    "OPP",
    "EPC",
    "Heme/Lymph",
    "Cardio",
    "Renal",
    "Resp",
}


@dataclass(frozen=True, slots=True)
class ConnectionUpdate:
    state: str
    last_error: str | None = None


def remote_signature(value: CanvasAttachment) -> str:
    content = "\0".join(
        (value.course_id, value.file_id, value.modified_at, str(value.size))
    )
    return hashlib.sha256(content.encode()).hexdigest()


class CanvasRepository:
    def __init__(self, database: Database):
        self.database = database

    def connection(self, base_url: str = "https://lmunet.instructure.com") -> CanvasConnectionModel:
        with self.database.session() as session:
            value = session.scalar(
                select(CanvasConnectionModel).where(CanvasConnectionModel.base_url == base_url)
            )
            if value is None:
                value = CanvasConnectionModel(base_url=base_url)
                session.add(value)
                session.flush()
            return value

    def set_pairing(self, extension_id: str, fingerprint: str) -> None:
        with self.database.session() as session:
            value = self._connection_in_session(session)
            value.extension_id = extension_id
            value.credential_fingerprint = fingerprint
            value.paired_at = utc_now()
            value.state = "connected"

    def clear_pairing(self) -> None:
        with self.database.session() as session:
            value = self._connection_in_session(session)
            value.extension_id = None
            value.credential_fingerprint = None
            value.paired_at = None
            value.state = "unpaired"

    def heartbeat(
        self,
        state: str,
        error: str | None = None,
        *,
        scan_complete: bool = False,
        item_count: int = 0,
        new_count: int = 0,
    ) -> None:
        with self.database.session() as session:
            value = self._connection_in_session(session)
            value.state = state
            value.last_heartbeat = utc_now()
            value.last_error = error[:1000] if error else None
            if scan_complete:
                value.last_successful_scan = utc_now()
                value.last_scan_item_count = item_count
                value.last_scan_new_count = new_count

    def request_scan(self) -> None:
        with self.database.session() as session:
            self._connection_in_session(session).scan_requested_at = utc_now()

    def consume_scan_request(self) -> bool:
        with self.database.session() as session:
            value = self._connection_in_session(session)
            requested = value.scan_requested_at is not None
            value.scan_requested_at = None
            return requested

    def set_setup(
        self,
        *,
        study_root: str | None = None,
        icloud_staging_root: str | None = None,
        discovery_confirmed: bool | None = None,
        auto_process: bool | None = None,
    ) -> None:
        with self.database.session() as session:
            value = self._connection_in_session(session)
            if study_root is not None:
                value.study_root = study_root
            if icloud_staging_root is not None:
                value.icloud_staging_root = icloud_staging_root
            if discovery_confirmed is not None:
                value.discovery_confirmed = discovery_confirmed
            if auto_process is not None:
                value.auto_process = auto_process

    def set_course_candidates(self, values: list[dict[str, str]]) -> None:
        concise = [
            {
                "course_id": item["course_id"][:100],
                "course_name": item["course_name"][:300],
                "course_code": item.get("course_code", "")[:200],
            }
            for item in values[:100]
        ]
        with self.database.session() as session:
            self._connection_in_session(session).course_candidates_json = json.dumps(
                concise, sort_keys=True
            )

    def list_course_candidates(self) -> list[dict[str, str]]:
        raw = json.loads(self.connection().course_candidates_json)
        return [dict(item) for item in raw]

    def list_proposed_revisions(self) -> list[SourceRevisionModel]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(SourceRevisionModel)
                    .where(SourceRevisionModel.state == "proposed")
                    .order_by(SourceRevisionModel.discovered_at.desc())
                ).all()
            )

    @staticmethod
    def _connection_in_session(session: Session) -> CanvasConnectionModel:
        value = session.scalar(select(CanvasConnectionModel).limit(1))
        if value is None:
            value = CanvasConnectionModel(base_url="https://lmunet.instructure.com")
            session.add(value)
            session.flush()
        return value

    def replace_course_mappings(self, values: list[CourseMappingInput]) -> None:
        subjects = [item.subject for item in values]
        course_ids = [item.course_id for item in values]
        if not set(subjects) <= APPROVED_SUBJECTS:
            raise ValueError("course mappings must use approved subjects")
        if len(subjects) != len(set(subjects)) or len(course_ids) != len(set(course_ids)):
            raise ValueError("course IDs and subjects must be unique")
        with self.database.session() as session:
            session.execute(delete(CanvasCourseMappingModel))
            session.add_all(
                CanvasCourseMappingModel(
                    course_id=item.course_id,
                    course_name=item.course_name,
                    course_code=item.course_code,
                    subject=item.subject,
                    enabled=item.enabled,
                )
                for item in values
            )

    def list_course_mappings(self) -> list[CanvasCourseMappingModel]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(CanvasCourseMappingModel).order_by(CanvasCourseMappingModel.subject)
                ).all()
            )

    def ingest_metadata(
        self,
        value: CanvasAttachment,
        classification: Classification,
        match: CatalogMatch,
    ) -> MetadataResult:
        signature = remote_signature(value)
        with self.database.session() as session:
            mapping = session.scalar(
                select(CanvasCourseMappingModel).where(
                    CanvasCourseMappingModel.course_id == value.course_id,
                    CanvasCourseMappingModel.enabled.is_(True),
                )
            )
            if mapping is None:
                raise ValueError("Canvas course is not mapped or enabled")
            source = session.scalar(
                select(CanvasSourceItemModel).where(
                    CanvasSourceItemModel.course_id == value.course_id,
                    CanvasSourceItemModel.file_id == value.file_id,
                )
            )
            review = (
                classification.kind is SourceKind.REVIEW
                or (
                    classification.kind is not SourceKind.IGNORE
                    and match.lecture_id is None
                    and not (
                        classification.kind is SourceKind.PRACTICE_QUESTIONS
                        and match.subject is not None
                        and match.exam_number is not None
                    )
                )
            )
            context = {
                "module_id": value.module_id,
                "module_title": value.module_title,
                "item_id": value.item_id,
                "item_title": value.item_title,
                "item_type": value.item_type,
                "page_url": value.page_url,
                "page_title": value.page_title,
            }
            evidence = {
                "classification": classification.reason,
                "match": match.reason,
            }
            if source is None:
                source = CanvasSourceItemModel(
                    course_id=value.course_id,
                    file_id=value.file_id,
                    filename=value.filename,
                    source_url=value.download_url,
                    context_json=json.dumps(context, sort_keys=True),
                    source_kind=classification.kind.value,
                    lecture_id=match.lecture_id,
                    subject=match.subject,
                    exam_number=match.exam_number,
                    confidence=min(classification.confidence, match.confidence),
                    evidence_json=json.dumps(evidence, sort_keys=True),
                    review_state=(
                        ReviewState.NEEDS_REVIEW.value if review else ReviewState.NONE.value
                    ),
                )
                session.add(source)
                session.flush()
            else:
                existing_evidence = json.loads(source.evidence_json)
                manual_match = existing_evidence.get("manual_match") is True
                source.filename = value.filename
                source.source_url = value.download_url
                source.context_json = json.dumps(context, sort_keys=True)
                source.source_kind = classification.kind.value
                if not manual_match:
                    source.lecture_id = match.lecture_id
                    source.subject = match.subject
                    source.exam_number = match.exam_number
                    source.confidence = min(classification.confidence, match.confidence)
                    source.evidence_json = json.dumps(evidence, sort_keys=True)
                    source.review_state = (
                        ReviewState.NEEDS_REVIEW.value if review else ReviewState.NONE.value
                    )
            revision = session.scalar(
                select(SourceRevisionModel).where(
                    SourceRevisionModel.source_item_id == source.id,
                    SourceRevisionModel.remote_signature == signature,
                )
            )
            created = revision is None
            if revision is None:
                revision = SourceRevisionModel(
                    source_item_id=source.id,
                    remote_signature=signature,
                    modified_at=value.modified_at,
                    remote_size=value.size,
                    original_filename=value.filename,
                )
                session.add(revision)
                session.flush()
            return MetadataResult(
                source.id,
                revision.id,
                created,
                ReviewState(source.review_state),
            )

    def get_disposition_context(self, source_item_id: int) -> DispositionContext:
        with self.database.session() as session:
            source = session.get(CanvasSourceItemModel, source_item_id)
            if source is None:
                raise KeyError(source_item_id)
            revision = session.scalar(
                select(SourceRevisionModel)
                .where(SourceRevisionModel.source_item_id == source.id)
                .order_by(SourceRevisionModel.id.desc())
            )
            if revision is None:
                raise KeyError((source_item_id, "revision"))
            has_current = bool(
                session.scalar(
                    select(func.count(ArtifactModel.id)).where(
                        ArtifactModel.current.is_(True),
                        ArtifactModel.revision_id.in_(
                            select(SourceRevisionModel.id).where(
                                SourceRevisionModel.source_item_id == source.id
                            )
                        ),
                    )
                )
            )
            return DispositionContext(
                source.id,
                revision.id,
                SourceKind(source.source_kind),
                source.lecture_id,
                source.subject,
                source.exam_number,
                source.confidence,
                has_current,
            )

    def list_review_items(self) -> list[CanvasSourceItemModel]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(CanvasSourceItemModel)
                    .where(
                        CanvasSourceItemModel.review_state == ReviewState.NEEDS_REVIEW.value
                    )
                    .order_by(CanvasSourceItemModel.discovered_at.desc())
                ).all()
            )

    def list_failed_revisions(self) -> list[FailedRevisionReview]:
        with self.database.session() as session:
            rows = session.execute(
                select(
                    SourceRevisionModel.id,
                    CanvasSourceItemModel.filename,
                    CanvasSourceItemModel.subject,
                    ProcessingJobModel.error,
                )
                .join(
                    CanvasSourceItemModel,
                    SourceRevisionModel.source_item_id == CanvasSourceItemModel.id,
                )
                .join(
                    ProcessingJobModel,
                    ProcessingJobModel.revision_id == SourceRevisionModel.id,
                )
                .where(
                    SourceRevisionModel.state == RevisionState.FAILED.value,
                    ProcessingJobModel.state.in_(
                        (JobState.FAILED.value, JobState.NEEDS_REVIEW.value)
                    ),
                )
                .order_by(CanvasSourceItemModel.subject, CanvasSourceItemModel.filename)
            ).all()
            return [
                FailedRevisionReview(revision_id, filename, subject, error or "Processing failed")
                for revision_id, filename, subject, error in rows
            ]

    def count_revisions(self, source_item_id: int) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(SourceRevisionModel.id)).where(
                        SourceRevisionModel.source_item_id == source_item_id
                    )
                )
                or 0
            )

    def get_source(self, source_item_id: int) -> CanvasSourceItemModel:
        with self.database.session() as session:
            value = session.get(CanvasSourceItemModel, source_item_id)
            if value is None:
                raise KeyError(source_item_id)
            return value

    def pending_revision(self, source_item_id: int) -> SourceRevisionModel:
        with self.database.session() as session:
            value = session.scalar(
                select(SourceRevisionModel)
                .where(
                    SourceRevisionModel.source_item_id == source_item_id,
                    SourceRevisionModel.state == "discovered",
                )
                .order_by(SourceRevisionModel.id.desc())
            )
            if value is None:
                value = session.scalar(
                    select(SourceRevisionModel)
                    .where(SourceRevisionModel.source_item_id == source_item_id)
                    .order_by(SourceRevisionModel.id.desc())
                )
            if value is None:
                raise KeyError((source_item_id, "revision"))
            return value

    def get_revision(self, revision_id: int) -> SourceRevisionModel:
        with self.database.session() as session:
            value = session.get(SourceRevisionModel, revision_id)
            if value is None:
                raise KeyError(revision_id)
            return value

    def complete_ingestion(self, revision_id: int, sha256: str, stored_path: str) -> None:
        from oms_hub.models import ProcessingJobModel

        with self.database.session() as session:
            revision = session.get(SourceRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.sha256 and revision.sha256 != sha256:
                raise ValueError("revision checksum changed after ingestion")
            revision.sha256 = sha256
            revision.stored_path = stored_path
            revision.state = "downloaded"
            job = session.scalar(
                select(ProcessingJobModel).where(
                    ProcessingJobModel.revision_id == revision_id,
                    ProcessingJobModel.action == "convert",
                )
            )
            if job is None:
                session.add(ProcessingJobModel(revision_id=revision_id, action="convert"))

    def mark_revision_review(self, revision_id: int, message: str) -> None:
        from oms_hub.models import ProcessingJobModel

        with self.database.session() as session:
            revision = session.get(SourceRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            revision.state = "failed"
            source = session.get(CanvasSourceItemModel, revision.source_item_id)
            if source is not None:
                source.review_state = "needs_review"
                evidence = json.loads(source.evidence_json)
                evidence["processing"] = message[:1000]
                source.evidence_json = json.dumps(evidence, sort_keys=True)
            job = session.scalar(
                select(ProcessingJobModel).where(
                    ProcessingJobModel.revision_id == revision_id,
                    ProcessingJobModel.action == "convert",
                )
            )
            if job is not None:
                job.state = "needs_review"
                job.error = message[:1000]

    def count_jobs(self, revision_id: int, action: str) -> int:
        from oms_hub.models import ProcessingJobModel

        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(ProcessingJobModel.id)).where(
                        ProcessingJobModel.revision_id == revision_id,
                        ProcessingJobModel.action == action,
                    )
                )
                or 0
            )
