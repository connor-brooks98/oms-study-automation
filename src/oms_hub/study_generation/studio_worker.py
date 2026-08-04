from dataclasses import replace
from datetime import timedelta
from typing import Protocol

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
)
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_domain import (
    StudioRunStage,
    StudioSource,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository


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
    ):
        self.repository = repository
        self.gateway = gateway
        self.converter = converter
        self.connection = connection
        self.publisher = publisher
        self.image_service = image_service

    def recover_interrupted_jobs(self) -> int:
        return self.repository.recover_interrupted_jobs()

    def run_once(self) -> bool:
        source = self.repository.claim_next()
        if source is not None:
            self._run_source(source)
            return True
        run = self.repository.claim_next_run()
        if run is None:
            return False
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
            published = self.publisher.publish_studio_quiz(
                run.id,
                quiz,
            )
            self.repository.complete_published_run(
                run.id,
                notebook_id,
                answer,
                published.token,
            )
        except Exception as error:  # noqa: BLE001 - durable publication boundary
            self.repository.mark_run_attempt_error(
                run.id,
                run.attempts,
                DiagnosticSource.VALIDATION.value,
                str(error),
            )
            self.repository.fail_run(
                run.id,
                DiagnosticSource.VALIDATION.value,
                str(error),
            )
        return True

    def _run_source(self, source: StudioSource) -> None:
        try:
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
            notebook_id, remote_id = self.gateway.attach_studio_source(
                source.subject,
                source.exam_number,
                source.source_type.value,
                source.title,
                path=path,
                text=text,
                url=source.source_url,
            )
            self.repository.complete(
                source.id,
                notebook_id,
                remote_id,
                converted=converted,
                payload_path=path,
            )
        except NotebookGatewayError as error:
            if isinstance(error, NotebookAuthenticationError):
                self.connection.invalidate(str(error))
            self.repository.fail(
                source.id,
                error.source.value,
                str(error),
                retry=error.retryable,
            )
        except Exception as error:  # noqa: BLE001 - durable worker boundary
            self.repository.fail(
                source.id,
                "study_hub",
                str(error),
                retry=False,
            )
