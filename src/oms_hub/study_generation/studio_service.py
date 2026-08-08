import socket
from ipaddress import ip_address
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from oms_hub.document_processing.domain import SourceSnapshot
from oms_hub.document_processing.snapshots import URLSnapshotService
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.native_quiz import studio_quiz_prompt
from oms_hub.study_generation.practice_domain import (
    ImportSourceSelection,
    QuizContentKind,
    StudioSourcePurpose,
)
from oms_hub.study_generation.studio_domain import (
    StudioRun,
    StudioSource,
    StudioSourceType,
)
from oms_hub.study_generation.studio_repository import StudioRepository


class URLSnapshotter(Protocol):
    def fetch(self, source_id: str, title: str, url: str) -> SourceSnapshot: ...


class StudioService:
    def __init__(
        self,
        repository: StudioRepository,
        payload_root: Path,
        max_file_bytes: int,
        *,
        url_snapshot_service: URLSnapshotter | None = None,
    ):
        self.repository = repository
        self.payload_root = payload_root
        self.max_file_bytes = max_file_bytes
        self.url_snapshot_service = url_snapshot_service or URLSnapshotService(
            payload_root, max_file_bytes
        )

    def add_import_file(
        self,
        subject: str,
        exam_number: int,
        title: str,
        filename: str,
        payload: bytes,
    ) -> StudioSource:
        filename_path = Path(filename)
        if not title.strip():
            title = filename_path.stem or "Uploaded import source"
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        suffix = self._validated_suffix(filename_path)
        if not payload:
            raise ValueError("Studio file is empty")
        if len(payload) > self.max_file_bytes:
            raise ValueError("Studio file exceeds the upload limit")
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.FILE,
            title,
            original_filename=filename_path.name,
            purpose=StudioSourcePurpose.LOCAL_IMPORT,
        )
        path = self.payload_root / source.id / f"original{suffix}"
        try:
            digest = verified_atomic_write(payload, path)
            return self.repository.mark_import_ready(
                source.id,
                path,
                digest,
                media_type=self._media_type(filename_path),
            )
        except Exception:
            self.repository.fail_import_source(source.id)
            raise

    def add_import_text(
        self,
        subject: str,
        exam_number: int,
        title: str,
        text: str,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        payload = text.encode("utf-8")
        if not text.strip():
            raise ValueError("pasted text is empty")
        if len(payload) > self.max_file_bytes:
            raise ValueError("pasted text exceeds the upload limit")
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.TEXT,
            title,
            purpose=StudioSourcePurpose.LOCAL_IMPORT,
        )
        path = self.payload_root / source.id / "pasted.txt"
        try:
            digest = verified_atomic_write(payload, path)
            return self.repository.mark_import_ready(
                source.id,
                path,
                digest,
                media_type="text/plain",
            )
        except Exception:
            self.repository.fail_import_source(source.id)
            raise

    def add_import_url(
        self,
        subject: str,
        exam_number: int,
        title: str,
        url: str,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.URL,
            title,
            source_url=url.strip(),
            purpose=StudioSourcePurpose.LOCAL_IMPORT,
        )
        try:
            snapshot = self.url_snapshot_service.fetch(source.id, title, url)
            return self.repository.mark_import_ready(
                source.id,
                snapshot.path,
                snapshot.sha256,
                media_type=snapshot.media_type,
                final_url=snapshot.original_url,
            )
        except Exception:
            self.repository.fail_import_source(source.id)
            raise

    def queue_import_run(
        self,
        subject: str,
        exam_number: int,
        label: str,
        destination_subject: str,
        destination_exam_number: int,
        content_kind: QuizContentKind,
        sources: tuple[ImportSourceSelection, ...],
    ) -> StudioRun:
        subject, label = self._validate_scope_and_title(subject, exam_number, label)
        destination_subject, _ = self._validate_scope_and_title(
            destination_subject, destination_exam_number, label
        )
        return self.repository.queue_import_run(
            subject,
            exam_number,
            label,
            destination_subject,
            destination_exam_number,
            content_kind,
            sources,
        )

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

    def add_image_url(
        self,
        subject: str,
        exam_number: int,
        title: str,
        url: str,
    ) -> StudioSource:
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        current_url = url.strip()
        self._validate_public_url(current_url)
        suffix_by_type = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(15.0, connect=5.0),
                headers={"User-Agent": "Study Hub image source importer"},
            ) as client:
                for _ in range(4):
                    self._validate_public_url(current_url)
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("image URL redirect is missing its target")
                            current_url = str(httpx.URL(current_url).join(location))
                            continue
                        response.raise_for_status()
                        media_type = response.headers.get("content-type", "")
                        media_type = media_type.split(";", 1)[0].casefold().strip()
                        if (
                            media_type not in {"", "application/octet-stream"}
                            and media_type not in suffix_by_type
                        ):
                            raise ValueError("dropped URL did not return an image")
                        suffix = suffix_by_type.get(media_type)
                        if suffix is None:
                            suffix = Path(urlparse(current_url).path).suffix.casefold()
                        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                            raise ValueError("dropped URL must point to a PNG, JPEG, or WebP image")
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > self.max_file_bytes:
                            raise ValueError("dropped image exceeds the upload limit")
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.max_file_bytes:
                                raise ValueError("dropped image exceeds the upload limit")
                            chunks.append(chunk)
                        filename = (
                            Path(urlparse(current_url).path).name
                            or f"dropped-image{suffix}"
                        )
                        if Path(filename).suffix.casefold() not in {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                        }:
                            filename = f"dropped-image{suffix}"
                        return self.add_file(
                            subject,
                            exam_number,
                            title,
                            filename,
                            b"".join(chunks),
                        )
        except ValueError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise ValueError("the dropped image could not be downloaded") from error
        raise ValueError("the dropped image redirected too many times")

    def add_file(
        self,
        subject: str,
        exam_number: int,
        title: str,
        filename: str,
        payload: bytes,
    ) -> StudioSource:
        filename_path = Path(filename)
        if not title.strip():
            title = filename_path.stem or "Uploaded source"
        subject, title = self._validate_scope_and_title(subject, exam_number, title)
        suffix = filename_path.suffix.casefold()
        if suffix not in {
            ".pdf",
            ".pptx",
            ".txt",
            ".md",
            ".markdown",
            ".csv",
            ".json",
            ".xml",
            ".yaml",
            ".yml",
            ".docx",
            ".rtf",
            ".html",
            ".htm",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            raise ValueError(
                "Studio files must be PDF, PPTX, text, DOCX, HTML, or a supported image"
            )
        if not payload:
            raise ValueError("Studio file is empty")
        if len(payload) > self.max_file_bytes:
            raise ValueError("Studio file exceeds the upload limit")
        source = self.repository.create_source(
            subject,
            exam_number,
            StudioSourceType.FILE,
            title,
            original_filename=filename_path.name,
        )
        path = self.payload_root / source.id / f"original{suffix}"
        verified_atomic_write(payload, path)
        return self.repository.set_payload_path(source.id, path)

    @staticmethod
    def _validated_suffix(filename_path: Path) -> str:
        suffix = filename_path.suffix.casefold()
        if suffix not in {
            ".pdf",
            ".pptx",
            ".txt",
            ".md",
            ".markdown",
            ".csv",
            ".json",
            ".xml",
            ".yaml",
            ".yml",
            ".docx",
            ".rtf",
            ".html",
            ".htm",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            raise ValueError(
                "Studio files must be PDF, PPTX, text, DOCX, HTML, or a supported image"
            )
        return suffix

    @staticmethod
    def _media_type(filename_path: Path) -> str:
        return {
            ".csv": "text/csv",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".htm": "text/html",
            ".html": "text/html",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".json": "application/json",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".rtf": "application/rtf",
            ".txt": "text/plain",
            ".webp": "image/webp",
            ".xml": "application/xml",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
        }.get(filename_path.suffix.casefold(), "application/octet-stream")

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
            studio_quiz_prompt(prompt, subject),
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

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("image URL must be HTTP or HTTPS")
        try:
            addresses = {
                ip_address(result[4][0])
                for result in socket.getaddrinfo(parsed.hostname, None)
            }
        except OSError as error:
            raise ValueError("image host could not be resolved") from error
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("image URL must point to a public host")
