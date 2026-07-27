from typing import Any, cast

from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import UploadKind
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
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
        google: Any,
    ):
        self.catalog = catalog
        self.ingestion = ingestion
        self.jobs = jobs
        self.prompts = prompts
        self.google = google

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
        if not _ready_revision(pdf) or not _ready_revision(transcript):
            raise GenerationPrerequisiteError(
                "Current lecture PDF and cleaned transcript are required"
            )
        if self.google.status().state != "connected":
            raise GenerationPrerequisiteError(
                "Connect Google in Settings before generating"
            )
        prompt_kind = (
            PromptKind.OUTLINE
            if kind is GenerationKind.OUTLINE
            else PromptKind.QUIZ
        )
        prompt = self.prompts.inspect(prompt_kind)
        job = self.jobs.queue(lecture_id, kind)
        if job.pdf_revision_id is not None:
            return cast(GenerationJob, job)
        assert pdf is not None and transcript is not None
        return cast(
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


def _ready_revision(revision: Any | None) -> bool:
    if (
        revision is None
        or not revision.current
        or revision.canonical_derived_path is None
        or revision.derived_sha256 is None
        or not revision.canonical_derived_path.is_file()
    ):
        return False
    return bool(
        sha256_file(revision.canonical_derived_path) == revision.derived_sha256
    )
