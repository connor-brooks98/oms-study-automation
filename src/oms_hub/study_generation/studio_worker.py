from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from oms_hub.files.office import OfficeConverter
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.processes import ProcessHeld, process_operation
from oms_hub.study_generation.notebook import NOTEBOOKLM_UPLOAD_ONLY, StoredNotebookLMGateway
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_repository import (
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
        recovered = self.repository.retire_abandoned_notebook_sources()
        if self.publisher is not None:
            recovered += self.publisher.recover_owned_studio_publications()
        recovered += self.repository.recover_interrupted_jobs()
        return recovered

    def run_once(self) -> bool:
        run = self.repository.claim_next_run()
        if run is None:
            return False
        from oms_hub.study_generation.practice_domain import QuizWorkflowKind

        try:
            with process_operation(self.repository.database, "studio", run.id):
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
        except ProcessHeld:
            if run.workflow_kind is QuizWorkflowKind.LECTURE_GENERATION:
                from oms_hub.llm.codex_session import SessionError

                self.repository.stop_gpt_run(run.id, SessionError("interrupted"))
            return True
