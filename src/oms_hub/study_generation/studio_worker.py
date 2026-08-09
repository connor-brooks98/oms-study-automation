from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from oms_hub.db import is_sqlite_busy
from oms_hub.files.office import OfficeConverter
from oms_hub.files.pdf import inspect_pdf
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.native_quiz import (
    QuizContractError,
    image_requirements,
    parse_native_quiz,
)
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
    NotebookSourceNotFoundError,
)
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import (
    StudioRunStage,
    StudioSource,
    StudioSourceOperation,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository

if TYPE_CHECKING:
    from oms_hub.study_generation.quiz_import_worker import QuizImportWorker


class NotebookConnection(Protocol):
    def invalidate(self, diagnostic: str) -> object: ...


class StudioWorker:
    def __init__(
        self,
        repository: StudioRepository,
        gateway: StoredNotebookLMGateway,
        converter: OfficeConverter,
        connection: NotebookConnection,
        publisher: GenerationRepository | None = None,
        image_service: StudioQuizImageService | None = None,
        import_worker: QuizImportWorker | None = None,
    ):
        self.repository = repository
        self.gateway = gateway
        self.converter = converter
        self.connection = connection
        self.publisher = publisher
        self.image_service = image_service
        self.import_worker = import_worker

    def recover_interrupted_jobs(self) -> int:
        return self.repository.recover_interrupted_jobs()

    def run_once(self) -> bool:
        claimed_operation = self.repository.claim_next_source_operation()
        if claimed_operation is not None:
            self._run_source_operation(*claimed_operation)
            return True
        source = self.repository.claim_next()
        if source is not None:
            claimed_operation = self.repository.claim_next_source_operation()
            if claimed_operation is None:
                self.repository.fail(
                    source.id,
                    DiagnosticSource.STUDY_HUB.value,
                    "durable source operation could not be claimed",
                    retry=False,
                )
                return True
            self._run_source_operation(*claimed_operation)
            return True
        run = self.repository.claim_next_run()
        if run is None:
            return False
        from oms_hub.study_generation.practice_domain import QuizWorkflowKind

        if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
            if self.import_worker is None:
                self.repository.fail_run(
                    run.id,
                    DiagnosticSource.STUDY_HUB.value,
                    "direct-import worker is not configured",
                )
            else:
                self.import_worker.run(run)
            return True
        try:
            self.repository.set_run_stage(run.id, StudioRunStage.CHAT)
            notebook_id, answer = self.gateway.ask_studio(
                run.subject,
                run.exam_number,
                run.prompt,
                [source.remote_source_id for source in run.sources],
            )
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            self.repository.record_run_attempt(
                run.id,
                run.attempts,
                error.source.value,
                None,
                str(error),
            )
            if error.retryable and run.attempts < 4:
                self.repository.retry_run(
                    run.id,
                    error.source.value,
                    str(error),
                    timedelta(seconds=min(30 * (2 ** (run.attempts - 1)), 300)),
                )
            else:
                self.repository.fail_run(run.id, error.source.value, str(error))
            return True
        except Exception as error:  # noqa: BLE001 - durable worker boundary
            self.repository.record_run_attempt(
                run.id,
                run.attempts,
                DiagnosticSource.STUDY_HUB.value,
                None,
                str(error),
            )
            if is_sqlite_busy(error) and run.attempts < 4:
                self.repository.retry_run(
                    run.id,
                    DiagnosticSource.STUDY_HUB.value,
                    str(error),
                    timedelta(seconds=min(30 * (2 ** (run.attempts - 1)), 300)),
                )
            else:
                self.repository.fail_run(
                    run.id,
                    DiagnosticSource.STUDY_HUB.value,
                    str(error),
                )
            return True

        self.repository.save_run_response(run.id, answer)
        self.repository.record_run_attempt(
            run.id,
            run.attempts,
            "notebook_chat",
            answer,
            None,
        )
        if self.publisher is None:
            self.repository.complete_run(run.id, notebook_id, answer)
            return True
        self.repository.set_run_stage(run.id, StudioRunStage.QUIZ_VALIDATE)
        try:
            quiz = replace(parse_native_quiz(answer), title=run.label)
        except QuizContractError as error:
            self.repository.mark_run_attempt_error(
                run.id,
                run.attempts,
                DiagnosticSource.CONTRACT.value,
                str(error),
            )
            if self.repository.contract_failure_count(run.id) < 2:
                self.repository.retry_run(
                    run.id,
                    DiagnosticSource.CONTRACT.value,
                    str(error),
                    timedelta(seconds=5),
                )
            else:
                self.repository.fail_run(
                    run.id,
                    DiagnosticSource.CONTRACT.value,
                    str(error),
                )
            return True
        if image_requirements(quiz):
            self.repository.await_image_review(
                run.id,
                notebook_id,
                answer,
                quiz,
            )
            if self.image_service is not None:
                review = self.repository.quiz_review(run.id)
                sources = tuple(
                    source
                    for snapshot in run.sources
                    if (source := self.repository.get(snapshot.source_id)) is not None
                )
                self.image_service.auto_bind_from_sources(
                    run.id,
                    review.requirements,
                    sources,
                )
            return True
        try:
            self.repository.set_run_stage(run.id, StudioRunStage.PUBLISH)
            self.publisher.publish_and_complete_studio_run(
                run.id,
                quiz,
                notebook_id,
                answer,
            )
        except Exception as error:  # noqa: BLE001 - durable publication boundary
            self.repository.mark_run_attempt_error(
                run.id,
                run.attempts,
                DiagnosticSource.VALIDATION.value,
                str(error),
            )
            if is_sqlite_busy(error) and run.attempts < 4:
                self.repository.retry_run(
                    run.id,
                    DiagnosticSource.VALIDATION.value,
                    str(error),
                    timedelta(seconds=min(30 * (2 ** (run.attempts - 1)), 300)),
                )
            else:
                self.repository.fail_run(
                    run.id,
                    DiagnosticSource.VALIDATION.value,
                    str(error),
                )
        return True

    def _run_source_operation(
        self,
        operation: StudioSourceOperation,
        source: StudioSource,
    ) -> None:
        if operation.operation_kind == "delete":
            self._run_delete_operation(operation)
            return
        if operation.state == "reconciling":
            self._reconcile_add_operation(operation)
            return

        remote_effect_started = False
        try:
            path, text, converted = self._prepare_source_payload(source)
            notebook_id, baseline = self.gateway.prepare_studio_source_add(
                source.subject,
                source.exam_number,
            )
            self.repository.record_attach_baseline(
                operation.id,
                notebook_id,
                set(baseline),
            )
            remote_effect_started = True
            remote_id = self.gateway.add_studio_source_to_notebook(
                notebook_id,
                source.source_type.value,
                source.title,
                path=path,
                text=text,
                url=source.source_url,
            )
            self.repository.complete_attach_operation(
                operation.id,
                remote_id,
                converted=converted,
                payload_path=path,
            )
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            if remote_effect_started:
                self.repository.mark_attach_reconciling(
                    operation.id,
                    error.source.value,
                    str(error),
                )
            else:
                self.repository.fail_attach_preparation(
                    operation.id,
                    error.source.value,
                    str(error),
                    retry=error.retryable,
                )
        except Exception as error:  # noqa: BLE001 - durable worker boundary
            if remote_effect_started:
                self.repository.mark_attach_reconciling(
                    operation.id,
                    DiagnosticSource.STUDY_HUB.value,
                    str(error),
                )
            else:
                self.repository.fail_attach_preparation(
                    operation.id,
                    DiagnosticSource.STUDY_HUB.value,
                    str(error),
                    retry=is_sqlite_busy(error),
                )

    def _reconcile_add_operation(self, operation: StudioSourceOperation) -> None:
        if not operation.notebook_id:
            self.repository.fail_attach_preparation(
                operation.id,
                DiagnosticSource.STUDY_HUB.value,
                "durable source operation is missing its notebook",
                retry=False,
            )
            return
        try:
            remote_ids = self.gateway.list_studio_source_ids(operation.notebook_id)
            self.repository.reconcile_attach_operation(operation.id, set(remote_ids))
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            self.repository.mark_attach_reconciling(
                operation.id,
                error.source.value,
                str(error),
            )
        except Exception as error:  # noqa: BLE001 - durable reconciliation boundary
            self.repository.mark_attach_reconciling(
                operation.id,
                DiagnosticSource.STUDY_HUB.value,
                str(error),
            )

    def _run_delete_operation(self, operation: StudioSourceOperation) -> None:
        if not operation.notebook_id or not operation.remote_source_id:
            self.repository.complete_delete_operation(operation.id)
            return
        try:
            self.gateway.delete_studio_source(
                operation.notebook_id,
                operation.remote_source_id,
            )
            self.repository.complete_delete_operation(operation.id)
        except NotebookSourceNotFoundError:
            self.repository.complete_delete_operation(operation.id)
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            self.repository.retry_delete_operation(
                operation.id,
                error.source.value,
                str(error),
            )
        except Exception as error:  # noqa: BLE001 - durable deletion boundary
            self.repository.retry_delete_operation(
                operation.id,
                DiagnosticSource.STUDY_HUB.value,
                str(error),
            )

    def _prepare_source_payload(
        self,
        source: StudioSource,
    ) -> tuple[Path | None, str | None, bool]:
        path = source.payload_path
        converted = False
        if source.source_type in {
            StudioSourceType.FILE,
            StudioSourceType.TEXT,
        } and (path is None or not path.is_file()):
            raise ValueError("stored Studio source payload is missing")
        if source.source_type is StudioSourceType.FILE and path is not None:
            if path.suffix.casefold() == ".pptx":
                converted_path = path.with_name("converted.pdf")
                self.converter.convert(path, converted_path)
                inspect_pdf(converted_path)
                path = converted_path
                converted = True
            elif path.suffix.casefold() == ".pdf":
                inspect_pdf(path)
        text = (
            path.read_text(encoding="utf-8")
            if source.source_type is StudioSourceType.TEXT and path
            else None
        )
        return path, text, converted
