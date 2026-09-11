from typing import Any, Literal, cast

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
from oms_hub.study_generation.notebook_auth import NOTEBOOK_CHECK_UNVERIFIED
from oms_hub.study_generation.notebook_errors import NotebookGatewayError


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
        *, backend: str = "notebooklm", model: str = "",
    ):
        self.catalog = catalog
        self.ingestion = ingestion
        self.jobs = jobs
        self.prompts = prompts
        self.notebook_connection = notebook_connection
        self.backend, self.model = backend, model

    def queue_outline(self, lecture_id: int) -> GenerationJob:
        return self._queue(lecture_id, GenerationKind.OUTLINE)

    def queue_quiz(self, lecture_id: int) -> GenerationJob:
        if self.backend == "codex_subscription":
            raise GenerationPrerequisiteError(
                "Use Create lecture quiz and enter learning objectives.")
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
        if self.backend != "codex_subscription":
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
            except NotebookGatewayError as error:
                raise GenerationPrerequisiteError(str(error)) from error
            except Exception as error:
                raise GenerationPrerequisiteError(
                    NOTEBOOK_CHECK_UNVERIFIED
                ) from error
            if notebook_status.state != "connected":
                raise GenerationPrerequisiteError(
                    "Connect Gemini Notebook in Settings before generating"
                    if notebook_status.state in {"disconnected", "failed"}
                    else NOTEBOOK_CHECK_UNVERIFIED
                )
        prompt_kind = (
            PromptKind.OUTLINE
            if kind is GenerationKind.OUTLINE
            else PromptKind.QUIZ
        )
        prompt = self.prompts.inspect(prompt_kind)
        if self.backend == "codex_subscription":
            if not self.model.strip():
                raise GenerationPrerequisiteError(
                    "Select a GPT model in Settings before generating.")
            try:
                job = self.jobs.queue(lecture_id, kind,
                                      backend=self.backend, codex_model=self.model)
            except ValueError as error:
                raise GenerationPrerequisiteError(str(error)) from error
            if job.backend != self.backend:
                raise GenerationPrerequisiteError("An existing outline operation is still active.")
        else:
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


class GptLectureService:
    """Queue local lecture snapshots without consulting an optional notebook account."""

    def __init__(self, catalog: Any, ingestion: Any, studio: Any, router: Any,
                 renderer: Any, work_root: Any, *, owner_id: str, model: str):
        self.catalog, self.ingestion, self.studio = catalog, ingestion, studio
        self.router, self.renderer, self.work_root = router, renderer, work_root
        self.owner_id, self.model = owner_id, model

    def queue(self, lecture_id: int, *, owner_id: str, label: str,
              objectives: tuple[tuple[str, str], ...], require_images: bool = True) -> Any:
        import mimetypes
        from uuid import uuid4

        from oms_hub.document_processing.domain import SourceSnapshot
        from oms_hub.study_generation.gpt_lecture import (
            LectureInputs,
            LectureSourceBinding,
            parse_lecture_sources,
        )

        self._authorize(owner_id)
        lecture = self.catalog.get_lecture(lecture_id)
        if lecture is None:
            raise KeyError(lecture_id)
        if not 1 <= len(objectives) <= 500 or sum(len(t) for _, t in objectives) > 100_000:
            raise ValueError("provide 1-500 bounded learning objectives")
        revisions = {r.kind: r for r in self.ingestion.list_current_revisions(lecture_id)}
        bindings = []
        for kind, role in ((UploadKind.SLIDES, "slides"),
                           (UploadKind.TRANSCRIPTS, "cleaned_transcript")):
            revision = revisions.get(kind)
            problem = revision_readiness_problem(revision)
            if revision is None or problem or revision.state != "current":
                raise GenerationPrerequisiteError(f"{role}: {problem or 'not approved'}")
            path, digest = ((revision.immutable_source_path, revision.source_sha256)
                if kind is UploadKind.SLIDES else
                (revision.immutable_derived_path, revision.derived_sha256))
            snapshot = SourceSnapshot(str(uuid4()), f"{lecture.topic} — {role}", path,
                mimetypes.guess_type(path.name)[0] or "application/octet-stream", digest)
            bindings.append(LectureSourceBinding(lecture.id, lecture.subject, lecture.exam_number,
                revision.id, cast(Literal["slides", "cleaned_transcript"], role),
                snapshot, True, True))
        run_id = str(uuid4())
        inputs = LectureInputs(lecture.id, lecture.subject, lecture.exam_number,
            bindings[0].revision_id, bindings[1].revision_id,
            bindings[0].snapshot.id, bindings[1].snapshot.id, objectives, (),
            bindings=tuple(bindings), image_required=require_images)
        inputs = parse_lecture_sources(inputs, self.router, self.work_root / run_id,
            renderer=self.renderer)
        return self.studio.queue_gpt_lecture(inputs, run_id=run_id, owner_id=owner_id,
            label=label, model=self.model)

    def load_inputs(self, run: Any) -> Any:
        import json

        from oms_hub.study_generation.gpt_lecture import lecture_inputs_from_manifest

        artifact = self.studio.run_artifact(run.id, "gpt:manifest")
        settings = self.studio.run_artifact(run.id, "gpt:settings")
        if run.backend != "codex_subscription" or artifact is None or settings is None:
            raise ValueError("GPT lecture manifest is missing")
        self._authorize(json.loads(settings.payload_json)["owner_id"])
        inputs = lecture_inputs_from_manifest(json.loads(artifact.payload_json))
        lecture = self.catalog.get_lecture(inputs.lecture_id)
        if lecture is None or (lecture.subject, lecture.exam_number) != (
            inputs.subject, inputs.exam_number
        ):
            raise ValueError("lecture scope changed")
        current = {r.id: r for r in self.ingestion.list_current_revisions(inputs.lecture_id)}
        for binding in inputs.bindings:
            revision = current.get(binding.revision_id)
            if revision is None or revision.state != "current":
                raise ValueError("lecture source is no longer current and approved")
            path, digest = ((revision.immutable_source_path, revision.source_sha256)
                if binding.role == "slides" else
                (revision.immutable_derived_path, revision.derived_sha256))
            if (path, digest) != (binding.snapshot.path, binding.snapshot.sha256):
                raise ValueError("lecture source binding changed")
        return inputs

    def _authorize(self, owner_id: str) -> None:
        if not owner_id or owner_id != self.owner_id:
            raise PermissionError("lecture owner mismatch")
