import socket
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse

import httpx

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
                        filename = Path(urlparse(current_url).path).name or f"dropped-image{suffix}"
                        if Path(filename).suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
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
