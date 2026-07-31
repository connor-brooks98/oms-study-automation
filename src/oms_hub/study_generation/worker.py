from datetime import UTC, datetime, timedelta
from typing import Any, cast

from oms_hub.db import is_sqlite_busy
from oms_hub.domain import LectureKey, StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import UploadKind
from oms_hub.llm.domain import DiagnosticSource
from oms_hub.study_generation.domain import (
    GenerationKind,
    GenerationStage,
    LectureSourceSet,
    NotebookAnswer,
    NotebookRef,
    PromptKind,
    RemoteSource,
    RevisionSource,
    SourceIsolationError,
    SourceKind,
)
from oms_hub.study_generation.native_quiz import (
    QuizContractError,
    parse_native_quiz,
    quiz_prompt,
)
from oms_hub.study_generation.notebook_errors import (
    NotebookAuthenticationError,
    NotebookGatewayError,
)


class GenerationWorker:
    def __init__(
        self,
        repository: Any,
        catalog: Any,
        ingestion: Any,
        prompts: Any,
        notebook: Any,
        outline: Any,
        publisher: Any,
        notebook_connection: Any | None = None,
    ):
        self.repository = repository
        self.catalog = catalog
        self.ingestion = ingestion
        self.prompts = prompts
        self.notebook = notebook
        self.outline = outline
        self.publisher = publisher
        self.notebook_connection = notebook_connection

    def recover_interrupted_jobs(self) -> int:
        return cast(int, self.repository.recover_interrupted())

    def run_once(self) -> bool:
        job = self.repository.claim_next(datetime.now(UTC))
        if job is None:
            return False
        progress_step = _progress_step(job.kind)
        self.catalog.set_step_status(
            job.lecture_id,
            progress_step,
            StepStatus.RUNNING,
            f"{job.kind.value.title()} generation is running",
        )
        try:
            self._run(job)
        except Exception as error:  # noqa: BLE001 - durable boundary sanitizes content
            if (
                self.notebook_connection is not None
                and isinstance(error, NotebookAuthenticationError)
            ):
                self.notebook_connection.invalidate(str(error))
            safe = _safe_error(error)
            retryable = _is_transient(error)
            if isinstance(error, QuizContractError):
                retryable = self.repository.contract_failure_count(job.id) < 2
            if retryable and job.attempts < 4:
                self.repository.retry(
                    job.id,
                    safe,
                    timedelta(seconds=min(30 * (2 ** max(job.attempts - 1, 0)), 300)),
                )
                self.catalog.set_step_status(
                    job.lecture_id,
                    progress_step,
                    StepStatus.QUEUED,
                    safe,
                )
            else:
                paused = _is_auth_error(error)
                self.repository.fail(
                    job.id,
                    safe,
                    paused=paused,
                )
                self.catalog.set_step_status(
                    job.lecture_id,
                    progress_step,
                    (
                        StepStatus.NEEDS_REVIEW
                        if paused
                        else StepStatus.FAILED
                    ),
                    safe,
                )
        return True

    def _run(self, job: Any) -> None:
        lecture = self.catalog.get_lecture(job.lecture_id)
        if lecture is None:
            raise RuntimeError("lecture was removed")
        if (
            job.kind is GenerationKind.QUIZ
            and job.quiz_url
            and job.stage in {
                GenerationStage.CATALOG,
                GenerationStage.DOCS,
            }
        ):
            self._complete_quiz(job, job.quiz_url)
            return
        prompt_kind = (
            PromptKind.OUTLINE
            if job.kind is GenerationKind.OUTLINE
            else PromptKind.QUIZ
        )
        prompt = self.prompts.inspect(prompt_kind)
        if prompt.sha256 != job.prompt_sha256:
            raise RuntimeError(
                "The Obsidian prompt changed after this job was queued; retry generation"
            )
        pdf_revision = self.ingestion.get_study_revision(job.pdf_revision_id)
        transcript_revision = self.ingestion.get_study_revision(
            job.transcript_revision_id
        )
        pdf = _revision_source(
            pdf_revision,
            job.lecture_id,
            UploadKind.SLIDES,
            SourceKind.LECTURE_PDF,
        )
        transcript = _revision_source(
            transcript_revision,
            job.lecture_id,
            UploadKind.TRANSCRIPTS,
            SourceKind.CLEANED_TRANSCRIPT,
        )

        if job.notebook_answer:
            if not (
                job.notebook_id
                and job.pdf_source_id
                and job.transcript_source_id
            ):
                raise SourceIsolationError(
                    "saved NotebookLM answer is missing its source bindings"
                )
            notebook_ref = NotebookRef(
                job.notebook_id,
                _notebook_title(lecture),
            )
            sources = _stored_sources(job, pdf, transcript)
            answer = NotebookAnswer(job.notebook_answer)
        else:
            notebook_prompt = (
                prompt
                if job.kind is GenerationKind.OUTLINE
                else quiz_prompt(prompt)
            )
            generate = getattr(self.notebook, "generate", None)
            if callable(generate):
                generated = generate(
                    lecture.subject,
                    lecture.exam_number,
                    job.lecture_id,
                    pdf,
                    transcript,
                    notebook_prompt,
                )
                notebook_ref = generated.notebook
                sources = generated.sources
                answer = generated.answer
            else:
                notebook_ref = (
                    NotebookRef(job.notebook_id, _notebook_title(lecture))
                    if job.notebook_id
                    else self.notebook.ensure_notebook(
                        lecture.subject,
                        lecture.exam_number,
                    )
                )
                sources = (
                    _stored_sources(job, pdf, transcript)
                    if job.pdf_source_id and job.transcript_source_id
                    else self.notebook.ensure_sources(
                        notebook_ref,
                        job.lecture_id,
                        pdf,
                        transcript,
                    )
                )
                answer = self.notebook.ask(
                    notebook_ref,
                    sources,
                    notebook_prompt,
                )
            next_stage = (
                GenerationStage.PDF
                if job.kind is GenerationKind.OUTLINE
                else GenerationStage.QUIZ_VALIDATE
            )
            job = self.repository.advance(
                job.id,
                next_stage,
                notebook_id=notebook_ref.id,
                pdf_source_id=sources.pdf.remote_id,
                transcript_source_id=sources.transcript.remote_id,
                notebook_answer=answer.text,
            )
        lecture_key = LectureKey(
            lecture.subject,
            lecture.exam_number,
            lecture.lecture_number,
            lecture.topic,
        )
        if job.kind is GenerationKind.OUTLINE:
            self.outline.file(job, lecture_key, answer)
            self.repository.complete(job.id)
            self.catalog.set_step_status(
                job.lecture_id,
                V2StepName.SUMMARY_FILED,
                StepStatus.COMPLETE,
                "Lecture summary PDF is ready",
            )
            return
        if job.quiz_url:
            quiz_url = job.quiz_url
        else:
            try:
                quiz = parse_native_quiz(answer.text)
            except QuizContractError as error:
                self.repository.record_attempt(
                    job.id,
                    job.attempts,
                    DiagnosticSource.CONTRACT.value,
                    answer.text,
                    str(error),
                )
                if self.repository.contract_failure_count(job.id) < 2:
                    self.repository.advance(
                        job.id,
                        GenerationStage.NOTEBOOK_PROMPT,
                        notebook_answer=None,
                    )
                raise
            job = self.repository.advance(
                job.id,
                GenerationStage.PUBLISH,
            )
            quiz_url = self.publisher.publish(
                job.lecture_id,
                job.id,
                quiz,
            )
            job = self.repository.advance(
                job.id,
                GenerationStage.CATALOG,
                quiz_url=quiz_url,
            )
        self._complete_quiz(job, quiz_url)

    def _complete_quiz(self, job: Any, quiz_url: str) -> None:
        self.repository.record_quiz(
            job.lecture_id,
            job.id,
            quiz_url,
        )
        self.repository.complete(job.id)
        self.catalog.set_step_status(
            job.lecture_id,
            V2StepName.QUIZ_PUBLISHED,
            StepStatus.COMPLETE,
            "Lecture quiz is published",
        )


def _revision_source(
    revision: Any,
    lecture_id: int,
    upload_kind: UploadKind,
    source_kind: SourceKind,
) -> RevisionSource:
    if (
        revision.lecture_id != lecture_id
        or revision.kind is not upload_kind
        or not revision.current
        or revision.canonical_derived_path is None
        or revision.derived_sha256 is None
        or not revision.canonical_derived_path.is_file()
        or sha256_file(revision.canonical_derived_path)
        != revision.derived_sha256
    ):
        raise SourceIsolationError("queued lecture sources are no longer current")
    return RevisionSource(
        lecture_id,
        revision.id,
        revision.canonical_derived_path,
        revision.derived_sha256,
        source_kind,
    )


def _stored_sources(
    job: Any,
    pdf: RevisionSource,
    transcript: RevisionSource,
) -> LectureSourceSet:
    if not job.pdf_source_id or not job.transcript_source_id:
        raise SourceIsolationError("saved NotebookLM source bindings are incomplete")
    return LectureSourceSet(
        job.lecture_id,
        RemoteSource(
            job.pdf_source_id,
            job.lecture_id,
            pdf.revision_id,
            pdf.sha256,
            SourceKind.LECTURE_PDF,
            True,
        ),
        RemoteSource(
            job.transcript_source_id,
            job.lecture_id,
            transcript.revision_id,
            transcript.sha256,
            SourceKind.CLEANED_TRANSCRIPT,
            True,
        ),
    )


def _notebook_title(lecture: Any) -> str:
    return f"{lecture.subject} · Exam {lecture.exam_number}"


def _safe_error(error: Exception) -> str:
    allowed = (
        NotebookGatewayError,
        SourceIsolationError,
        FileNotFoundError,
        RuntimeError,
        ValueError,
    )
    if isinstance(error, allowed):
        return str(error)[:500]
    return "Generation stopped because an external service failed"


def _is_auth_error(error: Exception) -> bool:
    return bool(
        isinstance(error, NotebookGatewayError)
        and error.source is DiagnosticSource.AUTHENTICATION
    )


def _is_transient(error: Exception) -> bool:
    if is_sqlite_busy(error):
        return True
    if isinstance(error, NotebookGatewayError):
        return error.retryable
    if isinstance(error, QuizContractError):
        return True
    return isinstance(error, (TimeoutError, ConnectionError))


def _progress_step(kind: GenerationKind) -> V2StepName:
    return (
        V2StepName.SUMMARY_FILED
        if kind is GenerationKind.OUTLINE
        else V2StepName.QUIZ_PUBLISHED
    )
