from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from oms_hub.db import is_sqlite_busy
from oms_hub.files.atomic import sha256_file
from oms_hub.files.office import OfficeConverter
from oms_hub.files.pdf import inspect_pdf
from oms_hub.files.trusted_paths import is_indirection
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.notebook import NOTEBOOKLM_UPLOAD_ONLY, StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
    NotebookScopeBusyError,
    NotebookSourceNotFoundError,
)
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import (
    StudioSource,
    StudioSourceOperation,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import (
    NotebookMutationBusy,
    StudioRepository,
)

if TYPE_CHECKING:
    from oms_hub.study_generation.gpt_lecture import GptLectureWorker
    from oms_hub.study_generation.quiz_import_worker import QuizImportWorker

LOGGER = logging.getLogger(__name__)


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
        gpt_worker: GptLectureWorker | None = None,
    ):
        self.repository = repository
        self.gateway = gateway
        self.converter = converter
        self.connection = connection
        self.publisher = publisher
        self.image_service = image_service
        self.import_worker = import_worker
        self.gpt_worker = gpt_worker

    def recover_interrupted_jobs(self) -> int:
        recovered = 0
        if self.publisher is not None:
            recovered += self.publisher.recover_owned_studio_publications()
        recovered += self.repository.recover_interrupted_jobs()
        return recovered

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

        if run.workflow_kind is QuizWorkflowKind.LECTURE_GENERATION:
            if self.gpt_worker is None:
                from oms_hub.llm.codex_session import SessionError

                self.repository.stop_gpt_run(run.id, SessionError("capability_unverified"))
            else:
                self.gpt_worker.run(run)
            return True
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
        self.repository.fail_run(
            run.id, DiagnosticSource.VALIDATION.value, NOTEBOOKLM_UPLOAD_ONLY, paused=True
        )
        return True

    def _run_source_operation(
        self,
        operation: StudioSourceOperation,
        source: StudioSource,
    ) -> None:
        if operation.operation_kind == "delete":
            self._run_delete_operation(operation, source)
            return
        if operation.state == "reconciling":
            self._reconcile_add_operation(operation, source)
            return

        remote_effect_started = False
        try:
            with self._notebook_scope(operation, source):
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
                self._verify_pinned_payload(source)
            # Exiting the durable mutation scope is part of the remote effect's
            # success contract. A lost lease must remain reconcilable rather
            # than committing a silently trusted local completion.
            self.repository.complete_attach_operation(
                operation.id,
                remote_id,
                converted=converted,
                payload_path=path,
            )
        except NotebookMutationBusy:
            self.repository.defer_attach_for_notebook(operation.id)
        except NotebookScopeBusyError:
            self.repository.defer_source_operation_for_scope(operation.id)
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

    def _reconcile_add_operation(
        self,
        operation: StudioSourceOperation,
        source: StudioSource,
    ) -> None:
        if not operation.notebook_id:
            self.repository.fail_attach_preparation(
                operation.id,
                DiagnosticSource.STUDY_HUB.value,
                "durable source operation is missing its notebook",
                retry=False,
            )
            return
        try:
            with self._notebook_scope(operation, source):
                self._verify_pinned_payload(source)
                remote_ids = self.gateway.list_studio_source_ids(
                    operation.notebook_id, baseline_ids=operation.baseline_remote_ids
                )
                self.repository.reconcile_attach_operation(operation.id, set(remote_ids))
        except NotebookScopeBusyError:
            self.repository.defer_source_operation_for_scope(operation.id)
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            self.repository.mark_attach_reconciling(
                operation.id,
                error.source.value,
                str(error),
                during_reconciliation=True,
            )
        except Exception as error:  # noqa: BLE001 - durable reconciliation boundary
            self.repository.mark_attach_reconciling(
                operation.id,
                DiagnosticSource.STUDY_HUB.value,
                str(error),
                during_reconciliation=True,
            )

    def _run_delete_operation(
        self,
        operation: StudioSourceOperation,
        source: StudioSource,
    ) -> None:
        if not operation.notebook_id or not operation.remote_source_id:
            self.repository.complete_delete_operation(operation.id)
            return
        try:
            with self._notebook_scope(operation, source):
                self.gateway.delete_studio_source(
                    operation.notebook_id,
                    operation.remote_source_id,
                )
                self.repository.complete_delete_operation(operation.id)
        except NotebookSourceNotFoundError:
            self.repository.complete_delete_operation(operation.id)
        except NotebookScopeBusyError:
            self.repository.defer_source_operation_for_scope(operation.id)
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

    def _notebook_scope(
        self,
        operation: StudioSourceOperation,
        source: StudioSource,
    ) -> AbstractContextManager[None]:
        scope = getattr(self.gateway, "mutation_scope", None)
        if not callable(scope):
            return nullcontext()
        return cast(
            AbstractContextManager[None],
            scope(
                source.subject,
                source.exam_number,
                "studio",
                operation.id,
            ),
        )

    def _prepare_source_payload(
        self,
        source: StudioSource,
    ) -> tuple[Path | None, str | None, bool]:
        path = source.payload_path
        self._verify_pinned_payload(source)
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

    @staticmethod
    def _verify_pinned_payload(source: StudioSource) -> None:
        if source.snapshot_sha256 is not None and (
            source.payload_path is None or not source.payload_path.is_file()
            or any(is_indirection(part)
                   for part in (source.payload_path, *source.payload_path.parents))
            or sha256_file(source.payload_path) != source.snapshot_sha256
        ):
            raise ValueError("queued source no longer matches its pinned artifact")
