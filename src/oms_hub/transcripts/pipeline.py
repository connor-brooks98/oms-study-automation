import json
from pathlib import Path

from oms_hub.artifact_writes import (
    ArtifactWriteClaim,
    ArtifactWriteClaimLost,
    ArtifactWriteContended,
    ArtifactWriteCoordinator,
)
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey, StepStatus, V2StepName
from oms_hub.files.atomic import (
    sha256_file,
    verified_atomic_copy,
    verified_atomic_write,
)
from oms_hub.ingestion.domain import (
    StudyRevision,
    UploadKind,
    UploadState,
)
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import CleanResult
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import (
    build_transcript_destination,
    expanded_path,
)
from oms_hub.transcripts.cleaner import TranscriptCleaner
from oms_hub.transcripts.prompt import ApprovedPrompt, PromptLoader


class TranscriptValidationError(RuntimeError):
    pass


def validate_transcript_bytes(payload: bytes, max_bytes: int) -> str:
    if not payload or len(payload) > max_bytes:
        raise TranscriptValidationError("transcript size is invalid")
    prefix = payload[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html")):
        raise TranscriptValidationError("transcript is an HTML response")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TranscriptValidationError("transcript is not UTF-8") from error
    if not text.strip():
        raise TranscriptValidationError("transcript is empty")
    if _is_data_envelope(text):
        raise TranscriptValidationError("transcript is not plain text")
    return text


class TranscriptPipeline:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        prompt: PromptLoader,
        cleaner: TranscriptCleaner,
        artifact_writes: ArtifactWriteCoordinator | None = None,
    ):
        self.settings = settings
        self.prompt = prompt
        self.cleaner = cleaner
        self.repository = IngestionRepository(database)
        self.catalog = CatalogRepository(database)
        # See SlidePipeline: this seam is only for deterministic interleaving
        # tests; production still creates the standard coordinator.
        self.writes = artifact_writes or ArtifactWriteCoordinator(database, settings)

    def process(self, item_id: str) -> StudyRevision:
        item = self.repository.require_item(item_id)
        if item.kind is not UploadKind.TRANSCRIPTS:
            raise ValueError("upload item is not a transcript")
        if item.lecture_id is None:
            raise ValueError("upload item has not been matched to a lecture")
        lecture = self.catalog.get_lecture(item.lecture_id)
        if lecture is None:
            raise ValueError("matched catalog lecture does not exist")

        revision = self.repository.begin_revision(
            item_id,
            expanded_path(self.settings.data_dir) / "artifacts" / "v2" / "transcripts",
        )
        cleaned_path = revision.immutable_derived_path
        if cleaned_path is None:
            raise ValueError("transcript revision has no cleaned destination")
        try:
            self._set_steps(
                revision.lecture_id,
                StepStatus.RUNNING,
                "Validating and cleaning the uploaded transcript",
            )
            payload = item.staged_path.read_bytes()
            raw_text = validate_transcript_bytes(
                payload,
                self.settings.max_upload_file_bytes,
            )
            if sha256_file(item.staged_path) != revision.source_sha256:
                raise TranscriptValidationError("staged transcript checksum mismatch")
            source_sha256 = verified_atomic_write(
                payload,
                revision.immutable_source_path,
            )
            if source_sha256 != revision.source_sha256:
                raise TranscriptValidationError("preserved transcript checksum mismatch")
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.TRANSCRIPT_VALIDATED,
                StepStatus.COMPLETE,
                "Raw transcript preserved and validated",
            )

            approved_prompt = self.prompt.current()
            cleaned_sha256, model_result = self._ensure_cleaned(
                raw_text,
                cleaned_path,
                revision,
                approved_prompt,
            )
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.TRANSCRIPT_CLEANED,
                StepStatus.COMPLETE,
                "Cleaned transcript preserved and validated",
            )
            destination = build_transcript_destination(
                self.settings,
                LectureKey(
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                ),
            )
            revision = self.repository.update_transcript_revision(
                revision.id,
                derived_sha256=cleaned_sha256,
                prompt_sha256=approved_prompt.sha256,
                canonical_derived_path=destination,
            )
            if model_result is not None:
                self.repository.record_study_usage(
                    revision.id,
                    provider=model_result.provider.value,
                    model=model_result.model,
                    request_id=model_result.request_id,
                    input_tokens=model_result.input_tokens,
                    output_tokens=model_result.output_tokens,
                    cost_microusd=model_result.cost_microusd,
                )
            with self.writes.claim(revision.lecture_id, "transcript-filing") as claim:
                claim.assert_owned()
                if self.repository.has_other_current_revision(
                    revision.lecture_id,
                    UploadKind.TRANSCRIPTS,
                    revision.id,
                ):
                    self.catalog.set_step_status(
                        revision.lecture_id,
                        V2StepName.TRANSCRIPT_FILED,
                        StepStatus.NEEDS_REVIEW,
                        "A cleaned transcript replacement is ready for approval",
                    )
                    return self.repository.finish_revision(
                        item_id,
                        revision.id,
                        UploadState.NEEDS_REVIEW,
                        current=False,
                        error="transcript replacement awaits approval",
                    )
                backup = None
                if destination.exists():
                    backup = destination.with_name(f".{destination.name}.oms-backup-{revision.id}")
                    if backup.exists():
                        raise TranscriptValidationError(
                            "prior transcript filing recovery backup requires operator review"
                        )
                    claim.assert_owned()
                    verified_atomic_copy(destination, backup)
                copied = False
                try:
                    copied_sha256 = verified_atomic_copy(
                        cleaned_path,
                        destination,
                    )
                    copied = True
                    if copied_sha256 != cleaned_sha256:
                        raise TranscriptValidationError("filed transcript checksum mismatch")
                    claim.assert_owned()
                    if self.repository.has_other_current_revision(
                        revision.lecture_id,
                        UploadKind.TRANSCRIPTS,
                        revision.id,
                    ):
                        backup = self._restore_filed_destination(
                            claim, backup, destination
                        )
                        copied = False
                        self.catalog.set_step_status(
                            revision.lecture_id,
                            V2StepName.TRANSCRIPT_FILED,
                            StepStatus.NEEDS_REVIEW,
                            "A cleaned transcript replacement is ready for approval",
                        )
                        return self.repository.finish_revision(
                            item_id,
                            revision.id,
                            UploadState.NEEDS_REVIEW,
                            current=False,
                            error="transcript replacement awaits approval",
                        )
                    self.catalog.set_step_status(
                        revision.lecture_id,
                        V2StepName.TRANSCRIPT_FILED,
                        StepStatus.COMPLETE,
                        "Cleaned transcript filed on the NUC",
                    )
                    claim.assert_owned()
                    return self.repository.finish_revision(
                        item_id,
                        revision.id,
                        UploadState.COMPLETE,
                        current=True,
                    )
                except Exception:
                    if copied:
                        backup = self._restore_filed_destination(
                            claim, backup, destination
                        )
                    raise
                finally:
                    try:
                        claim.assert_owned()
                    except ArtifactWriteClaimLost:
                        pass
                    else:
                        if backup is not None:
                            backup.unlink(missing_ok=True)
        except Exception as error:
            if isinstance(error, (ArtifactWriteContended, ArtifactWriteClaimLost)):
                raise
            self._set_steps(
                revision.lecture_id,
                StepStatus.NEEDS_REVIEW,
                str(error),
            )
            self.repository.finish_revision(
                item_id,
                revision.id,
                UploadState.NEEDS_REVIEW,
                current=False,
                error=str(error),
                revision_state="failed",
            )
            raise

    @staticmethod
    def _restore_filed_destination(
        claim: ArtifactWriteClaim, backup: Path | None, destination: Path
    ) -> None:
        """Restore only while this writer still owns the canonical destination."""
        claim.assert_owned()
        if backup is not None:
            verified_atomic_copy(backup, destination)
            backup.unlink(missing_ok=True)
        else:
            destination.unlink(missing_ok=True)
        return None

    def _ensure_cleaned(
        self,
        raw_text: str,
        cleaned_path: Path,
        revision: StudyRevision,
        approved_prompt: ApprovedPrompt,
    ) -> tuple[str, CleanResult | None]:
        if (
            cleaned_path.is_file()
            and revision.derived_sha256
            and revision.prompt_sha256 == approved_prompt.sha256
            and sha256_file(cleaned_path) == revision.derived_sha256
        ):
            cleaned_text = cleaned_path.read_text(encoding="utf-8")
            self._validate_cleaned(raw_text, cleaned_text)
            return revision.derived_sha256, None
        if cleaned_path.exists():
            raise TranscriptValidationError(
                "immutable cleaned transcript does not match this prompt"
            )
        result = self.cleaner.clean(raw_text, approved_prompt)
        self._validate_cleaned(raw_text, result.text)
        try:
            cleaned_payload = result.text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise TranscriptValidationError("cleaned transcript is not valid UTF-8") from error
        cleaned_sha256 = verified_atomic_write(
            cleaned_payload,
            cleaned_path,
        )
        return cleaned_sha256, result

    def _validate_cleaned(self, raw_text: str, cleaned: str) -> None:
        if not cleaned.strip():
            raise TranscriptValidationError("cleaned transcript is empty")
        if cleaned.lstrip().lower().startswith(("<html", "<!doctype")):
            raise TranscriptValidationError("cleaned transcript contains an HTML response")
        if _is_data_envelope(cleaned):
            raise TranscriptValidationError("cleaned transcript contains a data or error envelope")
        try:
            cleaned.encode("utf-8")
        except UnicodeEncodeError as error:
            raise TranscriptValidationError("cleaned transcript is not valid UTF-8") from error
        ratio = len(cleaned) / len(raw_text)
        if not (
            self.settings.transcript_min_clean_ratio
            <= ratio
            <= self.settings.transcript_max_clean_ratio
        ):
            raise TranscriptValidationError(
                f"cleaned length ratio {ratio:.2f} is outside "
                f"{self.settings.transcript_min_clean_ratio:.2f}-"
                f"{self.settings.transcript_max_clean_ratio:.2f}"
            )

    def _set_steps(
        self,
        lecture_id: int,
        status: StepStatus,
        detail: str,
    ) -> None:
        for step in (
            V2StepName.TRANSCRIPT_VALIDATED,
            V2StepName.TRANSCRIPT_CLEANED,
            V2StepName.TRANSCRIPT_FILED,
        ):
            self.catalog.set_step_status(
                lecture_id,
                step,
                status,
                detail,
            )


def _is_data_envelope(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(value, (dict, list))
