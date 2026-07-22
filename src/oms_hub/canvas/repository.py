import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import delete, func, select

from oms_hub.canvas.domain import (
    CanvasAttachment,
    CatalogMatch,
    Classification,
    CourseMappingInput,
    DispositionContext,
    MetadataResult,
    ReviewState,
    SourceKind,
)
from oms_hub.db import Database
from oms_hub.models import (
    ArtifactModel,
    CanvasConnectionModel,
    CanvasCourseMappingModel,
    CanvasSourceItemModel,
    SourceRevisionModel,
)

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
                or (classification.kind is not SourceKind.IGNORE and match.lecture_id is None)
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
                source.filename = value.filename
                source.source_url = value.download_url
                source.context_json = json.dumps(context, sort_keys=True)
                source.source_kind = classification.kind.value
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
