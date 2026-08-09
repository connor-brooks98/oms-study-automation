import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.files.atomic import sha256_file
from oms_hub.models import (
    PublishedQuizMediaModel,
    PublishedQuizModel,
    StudioImportRunSourceModel,
    StudioQuestionReviewModel,
    StudioQuizImageOverrideModel,
    StudioQuizImageRequirementModel,
    StudioRunArtifactModel,
    StudioRunAttemptModel,
    StudioRunModel,
    StudioRunSourceModel,
    StudioSourceModel,
    StudioSourceOperationModel,
)
from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.native_quiz import (
    image_requirements,
    parse_native_quiz,
    serialize_native_quiz,
)
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    ImportSourceSelection,
    QuestionDraft,
    QuizContentKind,
    QuizWorkflowKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.studio_domain import (
    StudioImportRunSource,
    StudioQuizImageRequirement,
    StudioQuizReview,
    StudioRun,
    StudioRunArtifact,
    StudioRunAttempt,
    StudioRunSource,
    StudioRunStage,
    StudioRunState,
    StudioSource,
    StudioSourceOperation,
    StudioSourceState,
    StudioSourceType,
    StudioStoredImage,
)

_ACTIVE_LABEL_INDEX = "ix_studio_runs_active_label"


def _is_active_label_conflict(error: IntegrityError) -> bool:
    """Return True only when ``error`` is the active-label uniqueness index.

    SQLite's own IntegrityError message doesn't include the index name for
    a UNIQUE violation (it lists the participating columns instead), so we
    match on those columns; other dialects that do surface the index name
    (e.g. Postgres) are matched directly. Anything else -- notably a
    foreign-key violation, which shares the same exception type -- is left
    for the caller to re-raise unchanged.
    """
    message = str(error.orig)
    if _ACTIVE_LABEL_INDEX in message:
        return True
    return (
        "UNIQUE constraint failed" in message
        and "destination_subject_key" in message
        and "destination_exam_number" in message
        and "label_key" in message
    )


class StudioRepository:
    def __init__(self, database: Database):
        self.database = database

    def create_source(
        self,
        subject: str,
        exam_number: int,
        source_type: StudioSourceType,
        title: str,
        *,
        payload_path: Path | None = None,
        source_url: str | None = None,
        original_filename: str | None = None,
        purpose: StudioSourcePurpose = StudioSourcePurpose.NOTEBOOK,
        import_role: ImportSourceRole | None = None,
        import_attach_to_notebook: bool = False,
    ) -> StudioSource:
        if purpose is not StudioSourcePurpose.LOCAL_IMPORT and (
            import_role is not None or import_attach_to_notebook
        ):
            raise ValueError("only local import sources may have import defaults")
        if import_attach_to_notebook and import_role not in {
            ImportSourceRole.SUPPORTING_REFERENCE,
            ImportSourceRole.COMBINED,
        }:
            raise ValueError(
                "only Supporting Reference or Combined sources may attach to NotebookLM"
            )
        with self.database.session() as session:
            model = StudioSourceModel(
                id=str(uuid4()),
                subject=subject,
                subject_key=normalize_subject(subject),
                exam_number=exam_number,
                source_type=source_type.value,
                title=title,
                payload_path=str(payload_path) if payload_path else None,
                source_url=source_url,
                original_filename=original_filename,
                purpose=purpose.value,
                import_role=import_role.value if import_role is not None else None,
                import_attach_to_notebook=import_attach_to_notebook,
            )
            session.add(model)
            session.flush()
            return self._domain(model)

    def get(self, source_id: str) -> StudioSource | None:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            return None if model is None else self._domain(model)

    def list_sources(
        self,
        subject_key: str | None = None,
        exam_number: int | None = None,
    ) -> list[StudioSource]:
        with self.database.session() as session:
            statement = (
                select(StudioSourceModel)
                .where(StudioSourceModel.state != StudioSourceState.DELETED.value)
                .order_by(StudioSourceModel.created_at)
            )
            if subject_key is not None:
                statement = statement.where(
                    StudioSourceModel.subject_key == normalize_subject(subject_key)
                )
            if exam_number is not None:
                statement = statement.where(StudioSourceModel.exam_number == exam_number)
            return [self._domain(item) for item in session.scalars(statement).all()]

    def set_payload_path(self, source_id: str, payload_path: Path) -> StudioSource:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.payload_path = str(payload_path)
            session.flush()
            return self._domain(model)

    def mark_import_ready(
        self,
        source_id: str,
        payload_path: Path,
        snapshot_sha256: str,
        *,
        media_type: str,
        final_url: str | None = None,
    ) -> StudioSource:
        if not payload_path.is_file() or sha256_file(payload_path) != snapshot_sha256:
            raise ValueError("local import snapshot could not be verified")
        with self.database.session() as session:
            result = session.execute(
                update(StudioSourceModel)
                .where(
                    StudioSourceModel.id == source_id,
                    StudioSourceModel.purpose == StudioSourcePurpose.LOCAL_IMPORT.value,
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                    StudioSourceModel.payload_path.is_(None),
                    StudioSourceModel.snapshot_sha256.is_(None),
                    StudioSourceModel.media_type.is_(None),
                    StudioSourceModel.final_url.is_(None),
                )
                .values(
                    payload_path=str(payload_path),
                    snapshot_sha256=snapshot_sha256,
                    media_type=media_type,
                    final_url=final_url,
                    state=StudioSourceState.READY.value,
                    next_attempt_at=None,
                    diagnostic_source=None,
                    error=None,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise ValueError("local import source is no longer pending")
            session.flush()
            model = session.get(StudioSourceModel, source_id)
            assert model is not None
            return self._domain(model)

    def fail_import_source(self, source_id: str) -> bool:
        """Fail a newly-created import source without reviving a terminal row."""
        with self.database.session() as session:
            result = session.execute(
                update(StudioSourceModel)
                .where(
                    StudioSourceModel.id == source_id,
                    StudioSourceModel.purpose == StudioSourcePurpose.LOCAL_IMPORT.value,
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                )
                .values(
                    state=StudioSourceState.FAILED.value,
                    diagnostic_source="source_processing",
                    error="local import source processing failed",
                    next_attempt_at=None,
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    def claim_next(self, now: datetime | None = None) -> StudioSource | None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.scalar(
                select(StudioSourceModel)
                .where(
                    StudioSourceModel.purpose == StudioSourcePurpose.NOTEBOOK.value,
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                    or_(
                        StudioSourceModel.next_attempt_at.is_(None),
                        StudioSourceModel.next_attempt_at <= now.isoformat(),
                    ),
                )
                .order_by(StudioSourceModel.created_at, StudioSourceModel.id)
                .limit(1)
            )
            if model is None:
                return None
            result = session.execute(
                update(StudioSourceModel)
                .where(
                    StudioSourceModel.id == model.id,
                    StudioSourceModel.state == StudioSourceState.PENDING.value,
                )
                .values(
                    state=StudioSourceState.ATTACHING.value,
                    attempts=StudioSourceModel.attempts + 1,
                    error=None,
                    next_attempt_at=None,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                return None
            session.add(
                StudioSourceOperationModel(
                    id=str(uuid4()),
                    source_id=model.id,
                    operation_kind="add",
                    state="queued",
                )
            )
            session.flush()
            session.refresh(model)
            return self._domain(model)

    def claim_next_source_operation(self) -> tuple[StudioSourceOperation, StudioSource] | None:
        """Claim one durable external mutation; normal Studio worker owns execution."""
        with self.database.session() as session:
            operation = session.scalar(
                select(StudioSourceOperationModel)
                .where(
                    StudioSourceOperationModel.state.in_(
                        {"queued", "reconciling", "deleting"}
                    )
                )
                .order_by(StudioSourceOperationModel.created_at, StudioSourceOperationModel.id)
                .limit(1)
            )
            if operation is None:
                return None
            source = session.get(StudioSourceModel, operation.source_id)
            if source is None:
                operation.state = "failed"
                operation.error = "Studio source was removed before operation execution"
                return None
            if operation.operation_kind == "delete":
                operation.state = "deleting"
                source.state = StudioSourceState.DELETING.value
            elif operation.state == "queued":
                # Baseline acquisition follows; no remote add can happen while queued.
                source.state = StudioSourceState.ATTACHING.value
            operation.attempts += 1
            session.flush()
            return self._operation_domain(operation), self._domain(source)

    def record_attach_baseline(
        self, operation_id: str, notebook_id: str, baseline_remote_ids: set[str]
    ) -> None:
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "add":
                raise KeyError(operation_id)
            operation.notebook_id = notebook_id
            operation.baseline_remote_ids_json = json.dumps(sorted(baseline_remote_ids))
            operation.state = "executing"
            operation.error = None

    def mark_attach_reconciling(
        self,
        operation_id: str,
        diagnostic_source: str,
        error: str,
    ) -> None:
        """Preserve an ambiguous add for list-and-delta reconciliation."""
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "add":
                raise KeyError(operation_id)
            operation.state = "reconciling"
            operation.diagnostic_source = diagnostic_source
            operation.error = error[:1000]

    def fail_attach_preparation(
        self,
        operation_id: str,
        diagnostic_source: str,
        error: str,
        *,
        retry: bool,
    ) -> None:
        """Handle a failure known to occur before the remote add was attempted."""
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "add":
                raise KeyError(operation_id)
            source = session.get(StudioSourceModel, operation.source_id)
            if source is None:
                raise KeyError(operation.source_id)
            operation.diagnostic_source = diagnostic_source
            operation.error = error[:1000]
            source.diagnostic_source = diagnostic_source
            source.error = error[:1000]
            if retry and operation.attempts < 3:
                operation.state = "queued"
                source.state = StudioSourceState.ATTACHING.value
            else:
                operation.state = "failed"
                source.state = StudioSourceState.FAILED.value

    def complete_attach_operation(
        self, operation_id: str, remote_source_id: str, *, converted: bool = False,
        payload_path: Path | None = None,
    ) -> StudioSource:
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "add" or not operation.notebook_id:
                raise KeyError(operation_id)
            source = session.get(StudioSourceModel, operation.source_id)
            if source is None:
                raise KeyError(operation.source_id)
            source.state = StudioSourceState.ATTACHED.value
            source.remote_notebook_id = operation.notebook_id
            source.remote_source_id = remote_source_id
            source.converted_from_pptx = converted
            if payload_path is not None:
                source.payload_path = str(payload_path)
            source.next_attempt_at = None
            operation.remote_source_id = remote_source_id
            operation.state = "completed"
            operation.error = None
            session.flush()
            return self._domain(source)

    def reconcile_attach_operation(self, operation_id: str, remote_ids: set[str]) -> str:
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "add":
                raise KeyError(operation_id)
            source = session.get(StudioSourceModel, operation.source_id)
            if source is None:
                raise KeyError(operation.source_id)
            baseline = set(json.loads(operation.baseline_remote_ids_json))
            delta = remote_ids - baseline
            if len(delta) == 1:
                remote_id = next(iter(delta))
                source.state = StudioSourceState.ATTACHED.value
                source.remote_notebook_id = operation.notebook_id
                source.remote_source_id = remote_id
                source.next_attempt_at = None
                operation.remote_source_id = remote_id
                operation.state = "completed"
                operation.error = None
                return "adopted"
            if not delta:
                if operation.attempts < 3:
                    source.state = StudioSourceState.ATTACHING.value
                    operation.state = "queued"
                    operation.notebook_id = None
                    operation.baseline_remote_ids_json = "[]"
                    operation.error = None
                    return "retry"
                source.state = StudioSourceState.FAILED.value
                source.diagnostic_source = operation.diagnostic_source
                source.error = "NotebookLM source add did not produce a remote source"
                operation.state = "failed"
                operation.error = source.error
                return "failed"
            source.state = StudioSourceState.NEEDS_REVIEW.value
            source.next_attempt_at = None
            operation.state = "needs_review"
            operation.error = "ambiguous remote source delta; manual reconciliation is required"
            return "needs_review"

    def retry_delete_operation(
        self,
        operation_id: str,
        diagnostic_source: str,
        error: str,
    ) -> None:
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "delete":
                raise KeyError(operation_id)
            operation.state = "deleting"
            operation.diagnostic_source = diagnostic_source
            operation.error = error[:1000]

    def queue_source_delete(self, source_id: str) -> StudioSource:
        with self.database.session() as session:
            source = session.get(StudioSourceModel, source_id)
            if source is None:
                raise KeyError(source_id)
            if source.state == StudioSourceState.DELETED.value:
                return self._domain(source)
            if not source.remote_notebook_id or not source.remote_source_id:
                source.state = StudioSourceState.DELETED.value
                return self._domain(source)
            existing = session.scalar(
                select(StudioSourceOperationModel).where(
                    StudioSourceOperationModel.state.in_(
                        {"queued", "executing", "reconciling", "deleting"}
                    ),
                    or_(
                        StudioSourceOperationModel.source_id == source_id,
                        StudioSourceOperationModel.notebook_id
                        == source.remote_notebook_id,
                    ),
                )
            )
            if existing is not None:
                raise ValueError("Notebook already has a pending source mutation")
            source.state = StudioSourceState.DELETING.value
            session.add(StudioSourceOperationModel(
                id=str(uuid4()), source_id=source_id, operation_kind="delete",
                state="deleting", notebook_id=source.remote_notebook_id,
                remote_source_id=source.remote_source_id,
            ))
            try:
                session.flush()
            except IntegrityError as error:
                raise ValueError("Notebook already has a pending source mutation") from error
            return self._domain(source)

    def complete_delete_operation(self, operation_id: str) -> StudioSource:
        with self.database.session() as session:
            operation = session.get(StudioSourceOperationModel, operation_id)
            if operation is None or operation.operation_kind != "delete":
                raise KeyError(operation_id)
            source = session.get(StudioSourceModel, operation.source_id)
            if source is None:
                raise KeyError(operation.source_id)
            source.state = StudioSourceState.DELETED.value
            source.next_attempt_at = None
            source.remote_source_id = None
            operation.state = "completed"
            operation.error = None
            session.flush()
            return self._domain(source)

    def complete(
        self,
        source_id: str,
        notebook_id: str,
        remote_source_id: str,
        *,
        converted: bool = False,
        payload_path: Path | None = None,
    ) -> None:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = StudioSourceState.ATTACHED.value
            model.next_attempt_at = None
            model.remote_notebook_id = notebook_id
            model.remote_source_id = remote_source_id
            model.converted_from_pptx = converted
            if payload_path is not None:
                model.payload_path = str(payload_path)

    def fail(
        self,
        source_id: str,
        source: str,
        error: str,
        *,
        retry: bool,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = (
                StudioSourceState.PENDING.value
                if retry and model.attempts < 3
                else StudioSourceState.FAILED.value
            )
            model.diagnostic_source = source
            model.error = error[:1000]
            model.next_attempt_at = (
                (now + timedelta(seconds=min(30, 5 * (2 ** (model.attempts - 1))))).isoformat()
                if model.state == StudioSourceState.PENDING.value
                else None
            )

    def recover_interrupted_jobs(self) -> int:
        with self.database.session() as session:
            operations = session.scalars(
                select(StudioSourceOperationModel).where(
                    StudioSourceOperationModel.state.in_({"executing", "deleting"})
                )
            ).all()
            for operation in operations:
                operation.state = "reconciling" if operation.operation_kind == "add" else "deleting"
            source_models = session.scalars(
                select(StudioSourceModel).where(
                    StudioSourceModel.state == StudioSourceState.ATTACHING.value
                )
            ).all()
            operation_source_ids = {operation.source_id for operation in operations}
            for source_model in source_models:
                if source_model.id not in operation_source_ids:
                    source_model.state = StudioSourceState.NEEDS_REVIEW.value
                    source_model.error = "interrupted legacy source attach requires review"
            run_models = session.scalars(
                select(StudioRunModel).where(StudioRunModel.state == StudioRunState.RUNNING.value)
            ).all()
            for run_model in run_models:
                run_model.state = StudioRunState.QUEUED.value
                run_model.error = "requeued after an interrupted Hub process"
                run_model.next_attempt_at = None
            return len(source_models) + len(operations) + len(run_models)

    def queue_run(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
        label: str,
        destination_subject: str,
        destination_exam_number: int,
        *,
        supersedes_run_id: str | None = None,
    ) -> StudioRun:
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("selected Studio sources contain duplicates")
        subject_key = normalize_subject(subject)
        destination_key = normalize_subject(destination_subject)
        label_key = normalize_subject(label)
        with self.database.session() as session:
            sources = list(
                session.scalars(
                    select(StudioSourceModel).where(StudioSourceModel.id.in_(source_ids))
                ).all()
            )
            by_id = {source.id: source for source in sources}
            if len(by_id) != len(source_ids):
                raise ValueError("a selected Studio source no longer exists")
            ordered = [by_id[source_id] for source_id in source_ids]
            if any(
                source.subject_key != subject_key or source.exam_number != exam_number
                for source in ordered
            ):
                raise ValueError("selected Studio sources belong to another course or exam")
            if any(
                source.state != StudioSourceState.ATTACHED.value or not source.remote_source_id
                for source in ordered
            ):
                raise ValueError("all selected Studio sources must be attached")
            if (
                supersedes_run_id is not None
                and session.get(StudioRunModel, supersedes_run_id) is None
            ):
                raise ValueError("the Studio run being replaced no longer exists")
            if supersedes_run_id is None:
                active_run = session.scalar(
                    select(StudioRunModel).where(
                        StudioRunModel.destination_subject_key == destination_key,
                        StudioRunModel.destination_exam_number == destination_exam_number,
                        StudioRunModel.label_key == label_key,
                        StudioRunModel.state.in_(
                            {
                                StudioRunState.QUEUED.value,
                                StudioRunState.RUNNING.value,
                                StudioRunState.RETRYING.value,
                            }
                        ),
                    )
                )
                published = session.scalar(
                    select(PublishedQuizModel).where(
                        PublishedQuizModel.studio_run_id.is_not(None),
                        PublishedQuizModel.destination_subject_key == destination_key,
                        PublishedQuizModel.destination_exam_number == destination_exam_number,
                        PublishedQuizModel.label_key == label_key,
                        PublishedQuizModel.active.is_(True),
                    )
                )
                if active_run is not None or published is not None:
                    raise ValueError("this quiz label is already in use for the destination exam")
            model = StudioRunModel(
                id=str(uuid4()),
                subject=subject,
                subject_key=subject_key,
                exam_number=exam_number,
                destination_subject=destination_subject,
                destination_subject_key=destination_key,
                destination_exam_number=destination_exam_number,
                label=label,
                label_key=label_key,
                prompt=prompt,
                state=StudioRunState.QUEUED.value,
                stage=StudioRunStage.VALIDATE.value,
                supersedes_run_id=supersedes_run_id,
            )
            session.add(model)
            try:
                session.flush()
            except IntegrityError as error:
                if _is_active_label_conflict(error):
                    raise ValueError(
                        "this quiz label is already in use for the destination exam"
                    ) from error
                raise
            for position, source in enumerate(ordered):
                assert source.remote_source_id is not None
                session.add(
                    StudioRunSourceModel(
                        run_id=model.id,
                        source_id=source.id,
                        remote_source_id=source.remote_source_id,
                        source_title=source.title,
                        position=position,
                    )
                )
            session.flush()
            return self._run_domain(session, model)

    def queue_import_run(
        self,
        subject: str,
        exam_number: int,
        label: str,
        destination_subject: str,
        destination_exam_number: int,
        content_kind: QuizContentKind,
        sources: Sequence[ImportSourceSelection],
        *,
        supersedes_run_id: str | None = None,
    ) -> StudioRun:
        if not sources:
            raise ValueError("select at least one import source")
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError("selected import sources contain duplicates")
        if not any(
            source.role in {ImportSourceRole.QUESTIONS, ImportSourceRole.COMBINED}
            for source in sources
        ):
            raise ValueError("select a Questions or Combined source")
        if any(
            source.attach_to_notebook
            and source.role
            not in {ImportSourceRole.SUPPORTING_REFERENCE, ImportSourceRole.COMBINED}
            for source in sources
        ):
            raise ValueError(
                "only Supporting Reference or Combined sources may attach to NotebookLM"
            )
        subject_key = normalize_subject(subject)
        destination_key = normalize_subject(destination_subject)
        label_key = normalize_subject(label)
        with self.database.session() as session:
            stored_sources = list(
                session.scalars(
                    select(StudioSourceModel).where(
                        StudioSourceModel.id.in_([source.source_id for source in sources])
                    )
                ).all()
            )
            source_by_id = {source.id: source for source in stored_sources}
            if len(source_by_id) != len(sources):
                raise ValueError("a selected import source no longer exists")
            ordered_sources = [source_by_id[source.source_id] for source in sources]
            if any(
                source.subject_key != subject_key or source.exam_number != exam_number
                for source in ordered_sources
            ):
                raise ValueError("selected import sources belong to another course or exam")
            if any(
                source.purpose != StudioSourcePurpose.LOCAL_IMPORT.value
                or source.state != StudioSourceState.READY.value
                for source in ordered_sources
            ):
                raise ValueError("all selected import sources must be ready local sources")
            active_run = session.scalar(
                select(StudioRunModel).where(
                    StudioRunModel.destination_subject_key == destination_key,
                    StudioRunModel.destination_exam_number == destination_exam_number,
                    StudioRunModel.label_key == label_key,
                    StudioRunModel.state.in_(
                        {
                            StudioRunState.QUEUED.value,
                            StudioRunState.RUNNING.value,
                            StudioRunState.RETRYING.value,
                        }
                    ),
                )
            )
            published = session.scalar(
                select(PublishedQuizModel).where(
                    PublishedQuizModel.studio_run_id.is_not(None),
                    PublishedQuizModel.destination_subject_key == destination_key,
                    PublishedQuizModel.destination_exam_number == destination_exam_number,
                    PublishedQuizModel.label_key == label_key,
                    PublishedQuizModel.active.is_(True),
                )
            )
            if active_run is not None or (
                published is not None and published.studio_run_id != supersedes_run_id
            ):
                raise ValueError("this quiz label is already in use for the destination exam")
            model = StudioRunModel(
                id=str(uuid4()),
                subject=subject,
                subject_key=subject_key,
                exam_number=exam_number,
                destination_subject=destination_subject,
                destination_subject_key=destination_key,
                destination_exam_number=destination_exam_number,
                label=label,
                label_key=label_key,
                prompt="",
                workflow_kind=QuizWorkflowKind.DIRECT_IMPORT.value,
                content_kind=content_kind.value,
                supersedes_run_id=supersedes_run_id,
            )
            session.add(model)
            try:
                session.flush()
            except IntegrityError as error:
                if _is_active_label_conflict(error):
                    raise ValueError(
                        "this quiz label is already in use for the destination exam"
                    ) from error
                raise
            for position, source in enumerate(sources):
                session.add(
                    StudioImportRunSourceModel(
                        run_id=model.id,
                        source_id=source.source_id,
                        source_role=source.role.value,
                        attach_to_notebook=source.attach_to_notebook,
                        position=position,
                    )
                )
            session.flush()
            return self._run_domain(session, model)

    def import_sources(self, run_id: str) -> tuple[StudioImportRunSource, ...]:
        with self.database.session() as session:
            models = session.scalars(
                select(StudioImportRunSourceModel)
                .where(StudioImportRunSourceModel.run_id == run_id)
                .order_by(StudioImportRunSourceModel.position)
            ).all()
            return tuple(
                StudioImportRunSource(
                    model.source_id,
                    ImportSourceRole(model.source_role),
                    model.attach_to_notebook,
                    model.remote_notebook_id,
                    model.remote_source_id,
                    model.position,
                )
                for model in models
            )

    def save_run_artifact(
        self,
        run_id: str,
        artifact_key: str,
        signature_sha256: str,
        payload_json: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
    ) -> None:
        with self.database.session() as session:
            stored = session.scalar(
                select(StudioRunArtifactModel).where(
                    StudioRunArtifactModel.run_id == run_id,
                    StudioRunArtifactModel.artifact_key == artifact_key,
                )
            )
            if stored is None:
                stored = StudioRunArtifactModel(run_id=run_id, artifact_key=artifact_key)
                session.add(stored)
            stored.signature_sha256 = signature_sha256
            stored.payload_json = payload_json
            stored.provider = provider
            stored.model = model
            stored.request_id = request_id

    def run_artifact(self, run_id: str, artifact_key: str) -> StudioRunArtifact | None:
        """Return one durable direct-import stage artifact, if it exists."""
        with self.database.session() as session:
            stored = session.scalar(
                select(StudioRunArtifactModel).where(
                    StudioRunArtifactModel.run_id == run_id,
                    StudioRunArtifactModel.artifact_key == artifact_key,
                )
            )
            if stored is None:
                return None
            return StudioRunArtifact(
                stored.artifact_key,
                stored.signature_sha256,
                stored.payload_json,
                stored.provider,
                stored.model,
                stored.request_id,
            )

    def invalidate_import_artifacts_after(
        self, run_id: str, artifact_prefixes: Sequence[str]
    ) -> None:
        """Discard derived import outputs while retaining immutable source snapshots.

        The caller supplies only downstream stage prefixes.  This deliberately never
        touches source rows or published quizzes, which are outside an import retry's
        ownership boundary.
        """
        if not artifact_prefixes:
            return
        with self.database.session() as session:
            predicates = [
                StudioRunArtifactModel.artifact_key.startswith(prefix)
                for prefix in artifact_prefixes
            ]
            session.execute(
                delete(StudioRunArtifactModel).where(
                    StudioRunArtifactModel.run_id == run_id,
                    or_(*predicates),
                )
            )
            session.execute(
                delete(StudioQuestionReviewModel).where(StudioQuestionReviewModel.run_id == run_id)
            )

    def save_import_source_binding(
        self,
        run_id: str,
        source_id: str,
        notebook_id: str,
        remote_source_id: str,
    ) -> None:
        """Persist an attached supporting-source binding after validating its scope."""
        if not notebook_id.strip() or not remote_source_id.strip():
            raise ValueError("NotebookLM binding identifiers must not be blank")
        with self.database.session() as session:
            binding = session.scalar(
                select(StudioImportRunSourceModel).where(
                    StudioImportRunSourceModel.run_id == run_id,
                    StudioImportRunSourceModel.source_id == source_id,
                )
            )
            run = session.get(StudioRunModel, run_id)
            source = session.get(StudioSourceModel, source_id)
            if binding is None or run is None or source is None:
                raise KeyError((run_id, source_id))
            if (
                not binding.attach_to_notebook
                or binding.source_role
                not in {
                    ImportSourceRole.SUPPORTING_REFERENCE.value,
                    ImportSourceRole.COMBINED.value,
                }
                or source.subject_key != run.subject_key
                or source.exam_number != run.exam_number
            ):
                raise ValueError("import source is not eligible for this NotebookLM scope")
            binding.remote_notebook_id = notebook_id
            binding.remote_source_id = remote_source_id

    def await_import_review(self, run_id: str, drafts: Sequence[QuestionDraft]) -> StudioRun:
        """Persist direct-import provenance and stop before any publication path."""
        self.save_question_reviews(run_id, drafts)
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.AWAITING_REVIEW.value
            model.stage = StudioRunStage.REVIEW.value
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def save_question_reviews(self, run_id: str, drafts: Sequence[QuestionDraft]) -> None:
        if len({draft.question_id for draft in drafts}) != len(drafts):
            raise ValueError("question drafts contain duplicate question identifiers")
        with self.database.session() as session:
            session.execute(
                delete(StudioQuestionReviewModel).where(StudioQuestionReviewModel.run_id == run_id)
            )
            session.add_all(
                StudioQuestionReviewModel(
                    run_id=run_id,
                    question_id=draft.question_id,
                    answer_provenance=(
                        draft.answer_provenance.value
                        if draft.answer_provenance is not None
                        else None
                    ),
                    verification_required=draft.verification_required,
                    verified_at=draft.verified_at,
                    source_refs_json=json.dumps(
                        [asdict(source_ref) for source_ref in draft.source_refs]
                    ),
                    extraction_confidence=draft.extraction_confidence,
                    diagnostics_json=json.dumps(
                        [asdict(diagnostic) for diagnostic in draft.diagnostics]
                    ),
                    original_identifier=draft.original_identifier,
                )
                for draft in drafts
            )

    def get_run(self, run_id: str) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            return self._run_domain(session, model)

    def list_runs(
        self,
        subject_key: str | None = None,
        exam_number: int | None = None,
        limit: int = 50,
    ) -> list[StudioRun]:
        with self.database.session() as session:
            statement = select(StudioRunModel).order_by(
                StudioRunModel.created_at.desc(), StudioRunModel.id.desc()
            ).where(StudioRunModel.history_hidden_at.is_(None))
            if subject_key is not None:
                statement = statement.where(
                    StudioRunModel.subject_key == normalize_subject(subject_key)
                )
            if exam_number is not None:
                statement = statement.where(StudioRunModel.exam_number == exam_number)
            statement = statement.limit(limit)
            models = session.scalars(statement).all()
            run_ids = [model.id for model in models]
            sources_by_run: dict[str, list[StudioRunSourceModel]] = {}
            if run_ids:
                snapshots = session.scalars(
                    select(StudioRunSourceModel)
                    .where(StudioRunSourceModel.run_id.in_(run_ids))
                    .order_by(StudioRunSourceModel.run_id, StudioRunSourceModel.position)
                ).all()
                for snapshot in snapshots:
                    sources_by_run.setdefault(snapshot.run_id, []).append(snapshot)
            return [
                self._run_domain(session, model, sources_by_run.get(model.id, ()))
                for model in models
            ]

    def claim_next_run(self, now: datetime | None = None) -> StudioRun | None:
        now = now or datetime.now(UTC)
        with self.database.session() as session:
            model = session.scalar(
                select(StudioRunModel)
                .where(
                    StudioRunModel.state.in_(
                        {
                            StudioRunState.QUEUED.value,
                            StudioRunState.RETRYING.value,
                        }
                    ),
                    or_(
                        StudioRunModel.next_attempt_at.is_(None),
                        StudioRunModel.next_attempt_at <= now.isoformat(),
                    ),
                )
                .order_by(StudioRunModel.created_at, StudioRunModel.id)
                .limit(1)
            )
            if model is None:
                return None
            claimed = session.execute(
                update(StudioRunModel)
                .where(
                    StudioRunModel.id == model.id,
                    StudioRunModel.state.in_(
                        {
                            StudioRunState.QUEUED.value,
                            StudioRunState.RETRYING.value,
                        }
                    ),
                )
                .values(
                    state=StudioRunState.RUNNING.value,
                    stage=(
                        StudioRunStage.PARSE.value
                        if model.workflow_kind == QuizWorkflowKind.DIRECT_IMPORT.value
                        else StudioRunStage.NOTEBOOK.value
                    ),
                    attempts=StudioRunModel.attempts + 1,
                    error=None,
                    next_attempt_at=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(model)
            return self._run_domain(session, model)

    def set_run_stage(self, run_id: str, stage: StudioRunStage) -> None:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.stage = stage.value

    def list_run_attempts(self, run_id: str) -> tuple[StudioRunAttempt, ...]:
        with self.database.session() as session:
            models = session.scalars(
                select(StudioRunAttemptModel)
                .where(StudioRunAttemptModel.run_id == run_id)
                .order_by(StudioRunAttemptModel.attempt_number)
            ).all()
            return tuple(
                StudioRunAttempt(
                    model.attempt_number,
                    model.diagnostic_source,
                    model.raw_response,
                    model.error,
                    model.created_at,
                )
                for model in models
            )

    def complete_run(self, run_id: str, notebook_id: str, raw_response: str) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.COMPLETE.value
            model.stage = StudioRunStage.COMPLETE.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def complete_published_run(
        self,
        run_id: str,
        notebook_id: str,
        raw_response: str,
        published_token: str,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.COMPLETE.value
            model.stage = StudioRunStage.COMPLETE.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.published_token = published_token
            model.error = None
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def save_run_response(self, run_id: str, raw_response: str) -> None:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.raw_response = raw_response

    def await_image_review(
        self,
        run_id: str,
        notebook_id: str,
        raw_response: str,
        quiz: NativeQuiz,
    ) -> StudioRun:
        requirements = image_requirements(quiz)
        if not requirements:
            raise ValueError("quiz does not require image review")
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            session.execute(
                delete(StudioQuizImageOverrideModel).where(
                    StudioQuizImageOverrideModel.run_id == run_id
                )
            )
            stale_asset_paths = {
                path
                for path in session.scalars(
                    select(StudioQuizImageRequirementModel.asset_path).where(
                        StudioQuizImageRequirementModel.run_id == run_id,
                        StudioQuizImageRequirementModel.asset_path.is_not(None),
                    )
                ).all()
                if path is not None
            }
            session.execute(
                delete(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id
                )
            )
            for requirement in requirements:
                session.add(
                    StudioQuizImageRequirementModel(
                        run_id=run_id,
                        image_key=requirement.key,
                        source_title=requirement.source_title,
                        locator=requirement.locator,
                        description=requirement.description,
                    )
                )
            model.state = StudioRunState.AWAITING_IMAGES.value
            model.stage = StudioRunStage.IMAGE_REVIEW.value
            model.notebook_id = notebook_id
            model.raw_response = raw_response
            model.draft_payload_json = serialize_native_quiz(quiz)
            model.error = None
            model.next_attempt_at = None
            session.flush()
            result = self._run_domain(session, model)
        self._unlink_orphaned_assets(stale_asset_paths)
        return result

    def _unlink_orphaned_assets(self, asset_paths: set[str]) -> None:
        if not asset_paths:
            return
        with self.database.session() as session:
            referenced = set(
                session.scalars(
                    select(PublishedQuizMediaModel.path).where(
                        PublishedQuizMediaModel.path.in_(asset_paths)
                    )
                ).all()
            )
        for asset_path in asset_paths - referenced:
            Path(asset_path).unlink(missing_ok=True)

    def quiz_review(self, run_id: str) -> StudioQuizReview:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            return self._review_domain(session, model)

    def bind_image(
        self,
        run_id: str,
        image_key: str,
        image: StudioStoredImage,
    ) -> StudioQuizReview:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != StudioRunState.AWAITING_IMAGES.value:
                raise ValueError("Studio run is not awaiting images")
            model = session.scalar(
                select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id,
                    StudioQuizImageRequirementModel.image_key == image_key,
                )
            )
            if model is None:
                raise KeyError(image_key)
            model.asset_path = str(image.path)
            model.asset_sha256 = image.sha256
            model.media_type = image.media_type
            model.width = image.width
            model.height = image.height
            model.original_filename = image.original_filename
            session.flush()
            return self._review_domain(session, run)

    def bind_import_review_image(
        self,
        run_id: str,
        image_key: str,
        source_title: str,
        locator: str,
        description: str,
        image: StudioStoredImage,
    ) -> None:
        """Persist sanitized candidate media for a direct-import review run."""
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if (
                run.workflow_kind != QuizWorkflowKind.DIRECT_IMPORT.value
                or run.state != StudioRunState.AWAITING_REVIEW.value
            ):
                raise ValueError("imported quiz is not awaiting question review")
            requirement = session.scalar(
                select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id,
                    StudioQuizImageRequirementModel.image_key == image_key,
                )
            )
            if requirement is None:
                requirement = StudioQuizImageRequirementModel(
                    run_id=run_id,
                    image_key=image_key,
                    source_title=source_title,
                    locator=locator,
                    description=description,
                )
                session.add(requirement)
            requirement.asset_path = str(image.path)
            requirement.asset_sha256 = image.sha256
            requirement.media_type = image.media_type
            requirement.width = image.width
            requirement.height = image.height
            requirement.original_filename = image.original_filename

    def import_review_image(self, run_id: str, image_key: str) -> StudioStoredImage:
        """Return one verified, sanitized direct-import image for private preview."""
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if (
                run is None
                or run.workflow_kind != QuizWorkflowKind.DIRECT_IMPORT.value
                or run.state != StudioRunState.AWAITING_REVIEW.value
            ):
                raise KeyError(run_id)
            requirement = session.scalar(
                select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id,
                    StudioQuizImageRequirementModel.image_key == image_key,
                )
            )
            if not (
                requirement is not None
                and requirement.asset_path
                and requirement.asset_sha256
                and requirement.media_type
                and requirement.width is not None
                and requirement.height is not None
                and requirement.original_filename
            ):
                raise KeyError(image_key)
            path = Path(requirement.asset_path)
            try:
                if not path.is_file() or sha256_file(path) != requirement.asset_sha256:
                    raise KeyError(image_key)
            except OSError as error:
                raise KeyError(image_key) from error
            return StudioStoredImage(
                path,
                requirement.asset_sha256,
                requirement.media_type,
                requirement.width,
                requirement.height,
                requirement.original_filename,
            )

    def set_image_override(
        self,
        run_id: str,
        question_id: str,
        enabled: bool,
    ) -> StudioQuizReview:
        with self.database.session() as session:
            run = session.get(StudioRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != StudioRunState.AWAITING_IMAGES.value:
                raise ValueError("Studio run is not awaiting images")
            if not run.draft_payload_json:
                raise ValueError("Studio quiz draft is missing")
            quiz = parse_native_quiz(run.draft_payload_json)
            question = next(
                (item for item in quiz.questions if item.id == question_id),
                None,
            )
            if question is None or question.image_ref is None:
                raise ValueError("question does not have an image requirement")
            existing = session.scalar(
                select(StudioQuizImageOverrideModel).where(
                    StudioQuizImageOverrideModel.run_id == run_id,
                    StudioQuizImageOverrideModel.question_id == question_id,
                )
            )
            if enabled and existing is None:
                session.add(
                    StudioQuizImageOverrideModel(
                        run_id=run_id,
                        question_id=question_id,
                        image_key=question.image_ref.key,
                    )
                )
            elif not enabled and existing is not None:
                session.delete(existing)
            session.flush()
            return self._review_domain(session, run)

    def resolved_quiz(self, run_id: str) -> NativeQuiz:
        review = self.quiz_review(run_id)
        if review.unresolved_keys:
            raise ValueError("quiz images are still required: " + ", ".join(review.unresolved_keys))
        return replace(
            review.quiz,
            questions=tuple(
                replace(question, image_ref=None)
                if question.id in review.overridden_question_ids
                else question
                for question in review.quiz.questions
            ),
        )

    def mark_run_attempt_error(
        self,
        run_id: str,
        attempt_number: int,
        diagnostic_source: str,
        error: str,
    ) -> None:
        with self.database.session() as session:
            model = session.scalar(
                select(StudioRunAttemptModel).where(
                    StudioRunAttemptModel.run_id == run_id,
                    StudioRunAttemptModel.attempt_number == attempt_number,
                )
            )
            if model is None:
                raise KeyError((run_id, attempt_number))
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]

    def contract_failure_count(self, run_id: str) -> int:
        with self.database.session() as session:
            return len(
                session.scalars(
                    select(StudioRunAttemptModel.id).where(
                        StudioRunAttemptModel.run_id == run_id,
                        StudioRunAttemptModel.diagnostic_source == "contract",
                    )
                ).all()
            )

    def rerun(self, run_id: str) -> StudioRun:
        previous = self.get_run(run_id)
        if previous.state not in {
            StudioRunState.AWAITING_IMAGES,
            StudioRunState.AWAITING_REVIEW,
            StudioRunState.COMPLETE,
            StudioRunState.FAILED,
        }:
            raise ValueError("only finished or review-ready Studio runs can be re-run")
        if previous.workflow_kind == QuizWorkflowKind.DIRECT_IMPORT:
            bindings = self.import_sources(previous.id)
            return self.queue_import_run(
                previous.subject,
                previous.exam_number,
                previous.label,
                previous.destination_subject,
                previous.destination_exam_number,
                previous.content_kind,
                tuple(
                    ImportSourceSelection(
                        binding.source_id,
                        binding.role,
                        binding.attach_to_notebook,
                    )
                    for binding in bindings
                ),
                supersedes_run_id=previous.id,
            )
        return self.queue_run(
            previous.subject,
            previous.exam_number,
            previous.prompt,
            [source.source_id for source in previous.sources],
            previous.label,
            previous.destination_subject,
            previous.destination_exam_number,
            supersedes_run_id=previous.id,
        )

    def hide_run(self, run_id: str) -> None:
        """Remove a terminal run from the Studio history without deleting its data.

        Artifacts and the ``PublishedQuizModel`` relationship stay in place so
        an already-published quiz remains reachable and a future successor can
        still replace it through ``supersedes_run_id``.
        """
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            if model.state not in {
                StudioRunState.AWAITING_IMAGES.value,
                StudioRunState.AWAITING_REVIEW.value,
                StudioRunState.COMPLETE.value,
                StudioRunState.FAILED.value,
            }:
                raise ValueError("only finished or review-ready Studio runs can be removed")
            model.history_hidden_at = datetime.now(UTC).isoformat()

    def mark_source_deleted(self, source_id: str) -> StudioSource:
        with self.database.session() as session:
            model = session.get(StudioSourceModel, source_id)
            if model is None:
                raise KeyError(source_id)
            model.state = StudioSourceState.DELETED.value
            session.flush()
            return self._domain(model)

    def retry_run(
        self,
        run_id: str,
        diagnostic_source: str,
        error: str,
        delay: timedelta,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.RETRYING.value
            # The caller sets a concrete stage before doing work; retaining it makes
            # direct-import retries explainable without changing NotebookLM behavior.
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]
            model.next_attempt_at = (datetime.now(UTC) + delay).isoformat()
            session.flush()
            return self._run_domain(session, model)

    def fail_run(
        self,
        run_id: str,
        diagnostic_source: str,
        error: str,
    ) -> StudioRun:
        with self.database.session() as session:
            model = session.get(StudioRunModel, run_id)
            if model is None:
                raise KeyError(run_id)
            model.state = StudioRunState.FAILED.value
            model.diagnostic_source = diagnostic_source
            model.error = error[:1000]
            model.next_attempt_at = None
            session.flush()
            return self._run_domain(session, model)

    def record_run_attempt(
        self,
        run_id: str,
        attempt_number: int,
        diagnostic_source: str,
        raw_response: str | None,
        error: str | None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                StudioRunAttemptModel(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    diagnostic_source=diagnostic_source,
                    raw_response=raw_response,
                    error=error[:1000] if error else None,
                )
            )

    @staticmethod
    def _domain(model: StudioSourceModel) -> StudioSource:
        return StudioSource(
            model.id,
            model.subject,
            model.subject_key,
            model.exam_number,
            StudioSourceType(model.source_type),
            model.title,
            model.original_filename,
            Path(model.payload_path) if model.payload_path else None,
            model.source_url,
            StudioSourceState(model.state),
            model.attempts,
            model.next_attempt_at,
            model.diagnostic_source,
            model.error,
            model.remote_notebook_id,
            model.remote_source_id,
            model.converted_from_pptx,
            StudioSourcePurpose(model.purpose),
            model.snapshot_sha256,
            model.media_type,
            model.final_url,
            ImportSourceRole(model.import_role) if model.import_role else None,
            model.import_attach_to_notebook,
        )

    @staticmethod
    def _operation_domain(model: StudioSourceOperationModel) -> StudioSourceOperation:
        return StudioSourceOperation(
            model.id,
            model.source_id,
            model.operation_kind,
            model.state,
            model.notebook_id,
            model.remote_source_id,
            frozenset(json.loads(model.baseline_remote_ids_json)),
            model.attempts,
            model.diagnostic_source,
            model.error,
        )

    @staticmethod
    def _run_domain(
        session: Session,
        model: StudioRunModel,
        sources: Sequence[StudioRunSourceModel] | None = None,
    ) -> StudioRun:
        snapshots = (
            sources
            if sources is not None
            else session.scalars(
                select(StudioRunSourceModel)
                .where(StudioRunSourceModel.run_id == model.id)
                .order_by(StudioRunSourceModel.position)
            ).all()
        )
        return StudioRun(
            model.id,
            model.subject,
            model.subject_key,
            model.exam_number,
            model.destination_subject,
            model.destination_subject_key,
            model.destination_exam_number,
            model.label,
            model.prompt,
            StudioRunState(model.state),
            StudioRunStage(model.stage),
            model.attempts,
            model.next_attempt_at,
            model.diagnostic_source,
            model.error,
            model.notebook_id,
            model.raw_response,
            model.draft_payload_json,
            model.published_token,
            model.supersedes_run_id,
            tuple(
                StudioRunSource(
                    snapshot.source_id,
                    snapshot.remote_source_id,
                    snapshot.source_title,
                )
                for snapshot in snapshots
            ),
            QuizWorkflowKind(model.workflow_kind),
            QuizContentKind(model.content_kind),
        )

    @classmethod
    def _review_domain(
        cls,
        session: Session,
        model: StudioRunModel,
    ) -> StudioQuizReview:
        if not model.draft_payload_json:
            raise ValueError("Studio quiz draft is missing")
        quiz = parse_native_quiz(model.draft_payload_json)
        requirement_models = session.scalars(
            select(StudioQuizImageRequirementModel)
            .where(StudioQuizImageRequirementModel.run_id == model.id)
            .order_by(StudioQuizImageRequirementModel.id)
        ).all()
        overridden = frozenset(
            session.scalars(
                select(StudioQuizImageOverrideModel.question_id).where(
                    StudioQuizImageOverrideModel.run_id == model.id
                )
            ).all()
        )
        question_ids_by_key: dict[str, list[str]] = {}
        for question in quiz.questions:
            if question.image_ref is not None:
                question_ids_by_key.setdefault(question.image_ref.key, []).append(question.id)
        requirements = tuple(
            StudioQuizImageRequirement(
                requirement.image_key,
                requirement.source_title,
                requirement.locator,
                requirement.description,
                tuple(question_ids_by_key.get(requirement.image_key, ())),
                (
                    StudioStoredImage(
                        Path(requirement.asset_path),
                        requirement.asset_sha256,
                        requirement.media_type,
                        requirement.width,
                        requirement.height,
                        requirement.original_filename,
                    )
                    if requirement.asset_path is not None
                    and requirement.asset_sha256 is not None
                    and requirement.media_type is not None
                    and requirement.width is not None
                    and requirement.height is not None
                    and requirement.original_filename is not None
                    else None
                ),
            )
            for requirement in requirement_models
        )
        return StudioQuizReview(
            cls._run_domain(session, model),
            quiz,
            requirements,
            overridden,
        )


def normalize_subject(subject: str) -> str:
    return " ".join(subject.casefold().split())
