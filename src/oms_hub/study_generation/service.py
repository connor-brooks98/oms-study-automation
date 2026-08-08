from typing import Any, cast

from oms_hub.domain import StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import UploadKind
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    PromptKind,
)


class GenerationPrerequisiteError(RuntimeError):
    pass


class GenerationService:
    def __init__(
        self,
        catalog: Any,
        ingestion: Any,
        jobs: Any,
        prompts: Any,
        notebook_connection: Any,
    ):
        self.catalog = catalog
        self.ingestion = ingestion
        self.jobs = jobs
        self.prompts = prompts
        self.notebook_connection = notebook_connection

    def queue_outline(self, lecture_id: int) -> GenerationJob:
        return self._queue(lecture_id, GenerationKind.OUTLINE)

    def queue_quiz(self, lecture_id: int) -> GenerationJob:
        return self._queue(lecture_id, GenerationKind.QUIZ)

    def _queue(
        self,
        lecture_id: int,
        kind: GenerationKind,
    ) -> GenerationJob:
        if self.catalog.get_lecture(lecture_id) is None:
            raise KeyError(lecture_id)
        revisions = {
            revision.kind: revision
            for revision in self.ingestion.list_current_revisions(lecture_id)
        }
        pdf = revisions.get(UploadKind.SLIDES)
        transcript = revisions.get(UploadKind.TRANSCRIPTS)
        problems = [
            f"lecture PDF {problem}"
            for problem in [revision_readiness_problem(pdf)]
            if problem is not None
        ]
        problems.extend(
            f"cleaned transcript {problem}"
            for problem in [revision_readiness_problem(transcript)]
            if problem is not None
        )
        if problems:
            raise GenerationPrerequisiteError(
                "Current lecture PDF and cleaned transcript are required: "
                + "; ".join(problems)
            )
        live_check = getattr(
            self.notebook_connection,
            "require_live",
            None,
        )
        try:
            notebook_status = (
                live_check()
                if live_check is not None
                else self.notebook_connection.status()
            )
        except Exception as error:
            raise GenerationPrerequisiteError(
                "Reconnect Gemini Notebook in Settings before generating"
            ) from error
        if notebook_status.state != "connected":
            raise GenerationPrerequisiteError(
                "Connect Gemini Notebook in Settings before generating"
            )
        prompt_kind = (
            PromptKind.OUTLINE
            if kind is GenerationKind.OUTLINE
            else PromptKind.QUIZ
        )
        prompt = self.prompts.inspect(prompt_kind)
        job = self.jobs.queue(lecture_id, kind)
        if job.state is GenerationState.PAUSED:
            job = self.jobs.requeue(job.id)
        if job.pdf_revision_id is not None:
            self.catalog.set_step_status(
                lecture_id,
                _progress_step(kind),
                (
                    StepStatus.RUNNING
                    if job.state is GenerationState.RUNNING
                    else StepStatus.QUEUED
                ),
                f"{kind.value.title()} generation queued",
            )
            return cast(GenerationJob, job)
        assert pdf is not None and transcript is not None
        queued = cast(
            GenerationJob,
            self.jobs.advance(
                job.id,
                GenerationStage.VALIDATE,
                prompt_path=str(prompt.path),
                prompt_sha256=prompt.sha256,
                pdf_revision_id=pdf.id,
                transcript_revision_id=transcript.id,
            ),
        )
        self.catalog.set_step_status(
            lecture_id,
            _progress_step(kind),
            StepStatus.QUEUED,
            f"{kind.value.title()} generation queued",
        )
        return queued


def revision_readiness_problem(revision: Any | None) -> str | None:
    if revision is None:
        return "is not uploaded"
    if not revision.current:
        return "is not current"
    if revision.canonical_derived_path is None:
        return "has no filed artifact"
    if revision.derived_sha256 is None:
        return "has no recorded checksum"
    try:
        if not revision.canonical_derived_path.is_file():
            return "file is missing"
        if sha256_file(revision.canonical_derived_path) != revision.derived_sha256:
            return "file checksum does not match"
    except OSError:
        return "file is not readable"
    return None


def _progress_step(kind: GenerationKind) -> V2StepName:
    return (
        V2StepName.SUMMARY_FILED
        if kind is GenerationKind.OUTLINE
        else V2StepName.QUIZ_PUBLISHED
    )
