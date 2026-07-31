from pathlib import Path
from urllib.parse import urlparse

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.native_quiz import studio_quiz_prompt
from oms_hub.study_generation.studio_domain import (
    StudioRun,
    StudioSource,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository


class StudioService:
    def __init__(
        self,
        repository: StudioRepository,
        payload_root: Path,
        max_file_bytes: int,
    ):
        self.repository = repository
        self.payload_root = payload_root
        self.max_file_bytes = max_file_bytes

    def add_text(
        self,
        subject: str,
        exam_number: int,
        title: str,
        text: str,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        if not text.strip():
            raise ValueError("pasted text is empty")
        if len(text.encode("utf-8")) > self.max_file_bytes:
            raise ValueError("pasted text exceeds the upload limit")
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.TEXT,
            title,
        )
        path = self.payload_root / source.id / "pasted.txt"
        verified_atomic_write(text.encode("utf-8"), path)
        return self.repository.set_payload_path(source.id, path)

    def add_url(
        self,
        subject: str,
        exam_number: int,
        title: str,
        url: str,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source URL must be HTTP or HTTPS")
        return self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.URL,
            title,
            source_url=url,
        )

    def add_file(
        self,
        subject: str,
        exam_number: int,
        title: str,
        filename: str,
        payload: bytes,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".pdf", ".pptx"}:
            raise ValueError("Studio files must be PDF or PPTX")
        if not payload:
            raise ValueError("Studio file is empty")
        if len(payload) > self.max_file_bytes:
            raise ValueError("Studio file exceeds the upload limit")
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.FILE,
            title,
            original_filename=Path(filename).name,
        )
        path = self.payload_root / source.id / f"original{suffix}"
        verified_atomic_write(payload, path)
        return self.repository.set_payload_path(source.id, path)

    def queue_run(
        self,
        subject: str,
        exam_number: int,
        prompt: str,
        source_ids: list[str],
        label: str,
        destination_subject: str,
        destination_exam_number: int,
    ) -> StudioRun:
        subject, label = self._validate_scope_and_title(
            subject,
            exam_number,
            label,
        )
        destination_subject, _ = self._validate_scope_and_title(
            destination_subject,
            destination_exam_number,
            label,
        )
        if len(prompt) > 50_000:
            raise ValueError("Studio prompt is too long")
        return self.repository.queue_run(
            subject,
            exam_number,
            studio_quiz_prompt(prompt),
            source_ids,
            label,
            destination_subject,
            destination_exam_number,
        )

    @staticmethod
    def _validate_scope_and_title(
        subject: str,
        exam_number: int,
        title: str,
    ) -> tuple[str, str]:
        normalized_subject = " ".join(subject.split())
        normalized_title = " ".join(title.split())
        if not normalized_subject:
            raise ValueError("select a course")
        if exam_number < 1:
            raise ValueError("select an exam")
        if not normalized_title:
            raise ValueError("source label is required")
        return normalized_subject, normalized_title
