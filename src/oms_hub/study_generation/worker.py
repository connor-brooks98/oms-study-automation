from datetime import UTC, datetime, timedelta
from typing import Any, cast

from oms_hub.domain import LectureKey, StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import UploadKind
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
from oms_hub.study_generation.gemini_quiz import GeminiQuizRef


class GenerationWorker:
    def __init__(
        self,
        repository: Any,
        catalog: Any,
        ingestion: Any,
        prompts: Any,
        notebook: Any,
        outline: Any,
        gemini: Any,
        docs: Any,
    ):
        self.repository = repository
        self.catalog = catalog
        self.ingestion = ingestion
        self.prompts = prompts
        self.notebook = notebook
        self.outline = outline
        self.gemini = gemini
        self.docs = docs

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
            safe = _safe_error(error)
            if _is_transient(error) and job.attempts < 4:
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

        notebook_ref = (
            NotebookRef(job.notebook_id, _notebook_title(lecture))
            if job.notebook_id
            else self.notebook.ensure_notebook(
                lecture.subject,
                lecture.exam_number,
            )
        )
        if not job.notebook_id:
            job = self.repository.advance(
                job.id,
                GenerationStage.SOURCES,
                notebook_id=notebook_ref.id,
            )
        if job.pdf_source_id and job.transcript_source_id:
            sources = LectureSourceSet(
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
        else:
            sources = self.notebook.ensure_sources(
                notebook_ref,
                job.lecture_id,
                pdf,
                transcript,
            )
            job = self.repository.advance(
                job.id,
                GenerationStage.NOTEBOOK_PROMPT,
                pdf_source_id=sources.pdf.remote_id,
                transcript_source_id=sources.transcript.remote_id,
            )
        if job.notebook_answer:
            answer = NotebookAnswer(job.notebook_answer)
        else:
            answer = self.notebook.ask(notebook_ref, sources, prompt)
            next_stage = (
                GenerationStage.PDF
                if job.kind is GenerationKind.OUTLINE
                else GenerationStage.GEMINI
            )
            job = self.repository.advance(
                job.id,
                next_stage,
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
        quiz_ref = (
            GeminiQuizRef(job.gemini_quiz_id)
            if job.gemini_quiz_id
            else self.gemini.generate(job.id, answer.text)
        )
        if not job.gemini_quiz_id:
            job = self.repository.advance(
                job.id,
                GenerationStage.SHARE,
                gemini_quiz_id=quiz_ref.id,
            )
        if job.quiz_url:
            quiz_url = job.quiz_url
        else:
            shared = self.gemini.share(quiz_ref)
            quiz_url = shared.url
            job = self.repository.advance(
                job.id,
                GenerationStage.DOCS,
                quiz_url=quiz_url,
            )
        document = self.docs.ensure_course_document(lecture.subject)
        tab = self.docs.ensure_exam_tab(document, lecture.exam_number)
        self.docs.sync_quiz_link(tab, lecture.lecture_number, quiz_url)
        self.repository.record_quiz(
            job.lecture_id,
            job.id,
            quiz_url,
            docs_synced=True,
        )
        self.repository.complete(job.id)
        self.catalog.set_step_status(
            job.lecture_id,
            V2StepName.QUIZ_PUBLISHED,
            StepStatus.COMPLETE,
            "Lecture quiz is published and linked",
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


def _notebook_title(lecture: Any) -> str:
    return f"{lecture.subject} · Exam {lecture.exam_number}"


def _safe_error(error: Exception) -> str:
    allowed = (
        SourceIsolationError,
        FileNotFoundError,
        RuntimeError,
        ValueError,
    )
    if isinstance(error, allowed):
        return str(error)[:500]
    return "Generation stopped because an external service failed"


def _is_auth_error(error: Exception) -> bool:
    message = str(error).casefold()
    return any(
        phrase in message
        for phrase in (
            "authentication",
            "connect google",
            "not connected",
            "oauth",
            "sign-in",
            "sign in",
        )
    )


def _is_transient(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    message = str(error).casefold()
    return any(
        phrase in message
        for phrase in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "rate limit",
            "connection reset",
        )
    )


def _progress_step(kind: GenerationKind) -> V2StepName:
    return (
        V2StepName.SUMMARY_FILED
        if kind is GenerationKind.OUTLINE
        else V2StepName.QUIZ_PUBLISHED
    )
