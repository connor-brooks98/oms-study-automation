import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from oms_hub.config import Settings
from oms_hub.domain import LectureKey
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.models import LectureModel, TranscriptJobModel, TranscriptRevisionModel
from oms_hub.naming import artifact_names, sanitize_filename
from oms_hub.panopto.domain import TranscriptAction
from oms_hub.panopto.openai_client import (
    CleanResult,
    OpenAIAuthenticationError,
    OpenAIRateLimitError,
    OpenAIResponseError,
    OpenAITransientError,
)
from oms_hub.panopto.prompt import (
    ApprovedPrompt,
    PromptInvalid,
    PromptLoader,
    PromptNotApproved,
)
from oms_hub.panopto.repository import PanoptoRepository
from oms_hub.repositories import CatalogRepository


class CaptionClient(Protocol):
    def download_captions(self, download_url: str, max_bytes: int) -> bytes: ...


class TranscriptCleaner(Protocol):
    def clean(self, raw_text: str, prompt: ApprovedPrompt) -> CleanResult: ...


class TranscriptValidationError(RuntimeError):
    pass


class TranscriptNeedsReview(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    requeued: int
    completed: int
    needs_review: int


def validate_raw_caption(payload: bytes, max_bytes: int) -> str:
    if not payload or len(payload) > max_bytes:
        raise TranscriptValidationError("caption payload size is invalid")
    prefix = payload[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b'{"error"')):
        raise TranscriptValidationError("caption response is not plain text")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TranscriptValidationError("caption response is not UTF-8") from error
    if not text.strip():
        raise TranscriptValidationError("caption response is empty")
    return text


class TranscriptPipeline:
    def __init__(
        self,
        repository: PanoptoRepository,
        catalog: CatalogRepository,
        prompt: PromptLoader,
        cleaner: TranscriptCleaner,
        settings: Settings,
        panopto: CaptionClient | None = None,
    ):
        self.repository = repository
        self.catalog = catalog
        self.panopto = panopto
        self.prompt = prompt
        self.cleaner = cleaner
        self.settings = settings

    def ingest_transcript(self, recording_id: int, payload: bytes) -> int:
        validate_raw_caption(payload, self.settings.panopto_max_caption_bytes)
        raw_sha256 = hashlib.sha256(payload).hexdigest()
        revision = self.repository.create_raw_revision(recording_id, raw_sha256, "")
        if revision.raw_path:
            existing = Path(revision.raw_path)
            if existing.is_file() and sha256_file(existing) == raw_sha256:
                return revision.id
            raise TranscriptValidationError(
                "Immutable raw transcript is missing or changed"
            )
        raw_path = self._revision_root(revision.id) / "raw.txt"
        self._write_immutable(raw_path, payload, raw_sha256)
        self.repository.finalize_download(revision.id, str(raw_path))
        return revision.id

    def ingest_captions(self, recording_id: int, download_url: str) -> int:
        if self.panopto is None:
            raise TranscriptValidationError("Panopto caption client is unavailable")
        payload = self.panopto.download_captions(
            download_url,
            self.settings.panopto_max_caption_bytes,
        )
        return self.ingest_transcript(recording_id, payload)

    def run_next(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        job = self.repository.claim_next_job(current)
        if job is None:
            return False
        try:
            if job.action == TranscriptAction.CLEAN.value:
                self._clean(job)
            elif job.action == TranscriptAction.FILE.value:
                self._file(job)
            else:
                raise TranscriptNeedsReview("Unsupported transcript job action")
        except (OpenAIRateLimitError, OpenAITransientError) as error:
            attempt = job.attempts + 1
            delay_minutes = min(15 * 2 ** (attempt - 1), 120)
            jitter_minutes = job.id % 3
            self.repository.mark_job_transient(
                job.id,
                str(error),
                current + timedelta(minutes=delay_minutes + jitter_minutes),
            )
        except (
            OpenAIAuthenticationError,
            OpenAIResponseError,
            PromptInvalid,
            PromptNotApproved,
            TranscriptNeedsReview,
            TranscriptValidationError,
            OSError,
            ValueError,
        ) as error:
            self.repository.mark_job_for_review(job.id, str(error))
        return True

    def recover_abandoned_jobs(self) -> RecoverySummary:
        requeued = completed = needs_review = 0
        for job in self.repository.running_jobs():
            revision = self.repository.get_revision(job.revision_id)
            if job.action == TranscriptAction.CLEAN.value:
                raw_path = Path(revision.raw_path)
                if (
                    raw_path.is_file()
                    and sha256_file(raw_path) == revision.raw_sha256
                ):
                    self.repository.requeue_job(job.id)
                    requeued += 1
                else:
                    self.repository.mark_job_for_review(
                        job.id,
                        "Immutable raw transcript is missing or changed",
                    )
                    needs_review += 1
                continue
            if job.action == TranscriptAction.FILE.value:
                try:
                    destination = self._destination_for_revision(revision)
                except (KeyError, ValueError, OSError) as error:
                    self.repository.mark_job_for_review(job.id, str(error))
                    needs_review += 1
                    continue
                if (
                    revision.cleaned_sha256
                    and destination.is_file()
                    and sha256_file(destination) == revision.cleaned_sha256
                ):
                    self.repository.complete_file(
                        job.id,
                        str(destination),
                        revision.cleaned_sha256,
                    )
                    completed += 1
                else:
                    self.repository.requeue_job(job.id)
                    requeued += 1
                continue
            self.repository.mark_job_for_review(job.id, "Unsupported transcript job action")
            needs_review += 1
        return RecoverySummary(requeued, completed, needs_review)

    def retry_job(self, job_id: int) -> None:
        self.repository.retry_job(job_id)

    def _clean(self, job: TranscriptJobModel) -> None:
        revision = self.repository.get_revision(job.revision_id)
        raw_path = Path(revision.raw_path)
        if not raw_path.is_file() or sha256_file(raw_path) != revision.raw_sha256:
            raise TranscriptNeedsReview("Immutable raw transcript is missing or changed")
        raw_text = raw_path.read_text(encoding="utf-8")
        approved_prompt = self.prompt.current()
        result = self.cleaner.clean(raw_text, approved_prompt)
        ratio = len(result.text) / len(raw_text)
        if not (
            self.settings.transcript_min_clean_ratio
            <= ratio
            <= self.settings.transcript_max_clean_ratio
        ):
            raise TranscriptNeedsReview(
                f"cleaned length ratio {ratio:.2f} is outside "
                f"{self.settings.transcript_min_clean_ratio:.2f}-"
                f"{self.settings.transcript_max_clean_ratio:.2f}"
            )
        cleaned_payload = result.text.encode("utf-8")
        cleaned_sha256 = hashlib.sha256(cleaned_payload).hexdigest()
        cleaned_path = self._revision_root(revision.id) / "cleaned.txt"
        self._write_immutable(cleaned_path, cleaned_payload, cleaned_sha256)
        self.repository.complete_clean(
            job.id,
            str(cleaned_path),
            cleaned_sha256,
            approved_prompt.sha256,
            model=result.model,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microusd=result.cost_microusd,
        )

    def _file(self, job: TranscriptJobModel) -> None:
        revision = self.repository.get_revision(job.revision_id)
        if not revision.cleaned_path or not revision.cleaned_sha256:
            raise TranscriptNeedsReview("Cleaned transcript artifact is missing")
        cleaned_path = Path(revision.cleaned_path)
        if (
            not cleaned_path.is_file()
            or sha256_file(cleaned_path) != revision.cleaned_sha256
        ):
            raise TranscriptNeedsReview("Immutable cleaned transcript is missing or changed")
        destination = self._destination_for_revision(revision)
        digest = verified_atomic_copy(cleaned_path, destination)
        self.repository.complete_file(job.id, str(destination), digest)

    def _destination_for_revision(
        self,
        revision: TranscriptRevisionModel,
    ) -> Path:
        recording = self.repository.get_recording(revision.recording_id)
        if recording.lecture_id is None:
            raise TranscriptNeedsReview("Transcript recording is not matched to a lecture")
        lecture = self.catalog.get_lecture(recording.lecture_id)
        if lecture is None:
            raise TranscriptNeedsReview("Matched lecture no longer exists")
        return self._destination(lecture)

    def _destination(self, lecture: LectureModel) -> Path:
        study_root = Path(os.path.expandvars(str(self.settings.study_root))).resolve()
        destination = (
            study_root
            / sanitize_filename(lecture.subject)
            / f"Exam {lecture.exam_number}"
            / "Transcripts"
            / artifact_names(
                LectureKey(
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                )
            ).transcript
        ).resolve()
        if not destination.is_relative_to(study_root):
            raise TranscriptNeedsReview("Transcript destination escapes the study root")
        return destination

    def _revision_root(self, revision_id: int) -> Path:
        root = Path(
            os.path.expandvars(str(self.settings.panopto_revision_root))
        ).resolve()
        destination = (root / str(revision_id)).resolve()
        if not destination.is_relative_to(root):
            raise TranscriptNeedsReview("Transcript revision path escapes its root")
        return destination

    @staticmethod
    def _write_immutable(
        destination: Path,
        payload: bytes,
        expected_sha256: str,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != expected_sha256:
                raise TranscriptNeedsReview("Immutable transcript artifact already changed")
            return
        temporary = destination.with_name(
            f".{destination.name}.partial-{uuid.uuid4().hex}"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if sha256_file(temporary) != expected_sha256:
                raise OSError("Transcript artifact checksum verification failed")
            os.replace(temporary, destination)
            if sha256_file(destination) != expected_sha256:
                raise OSError("Transcript artifact promotion verification failed")
        finally:
            temporary.unlink(missing_ok=True)
