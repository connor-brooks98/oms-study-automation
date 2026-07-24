import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Protocol

from oms_hub.config import Settings
from oms_hub.panopto.browser_domain import BrowserRequestKind
from oms_hub.panopto.pipeline import TranscriptValidationError, validate_raw_caption
from oms_hub.panopto.repository import PanoptoRepository


class TranscriptIngestor(Protocol):
    def ingest_transcript(self, recording_id: int, payload: bytes) -> int: ...


class PanoptoDownloadIngestion:
    def __init__(
        self,
        repository: PanoptoRepository,
        pipeline: TranscriptIngestor,
        settings: Settings,
    ):
        self.repository = repository
        self.pipeline = pipeline
        self.settings = settings

    def complete_test_download(
        self,
        request_id: str,
        path: Path,
        language: str,
        now_utc: datetime,
    ) -> None:
        request = self.repository.get_browser_request(request_id)
        if request is None or request.kind is not BrowserRequestKind.CONNECTION_TEST:
            raise TranscriptValidationError("Connection test request does not match")
        try:
            managed = self._managed_path(path)
            self._validated_payload(managed, language)
            managed.unlink()
            self.repository.mark_acceptance_validated(now_utc)
            self.repository.complete_browser_request(request_id, now_utc)
            self.repository.heartbeat("connected", now_utc)
        except (OSError, TranscriptValidationError, ValueError) as error:
            self._handle_invalid(request_id, path, now_utc)
            if isinstance(error, TranscriptValidationError):
                raise
            raise TranscriptValidationError("Caption download could not be validated") from error

    def complete_recording_download(
        self,
        request_id: str,
        recording_id: int,
        path: Path,
        language: str,
        now_utc: datetime,
    ) -> int:
        request = self.repository.get_browser_request(request_id)
        if request is None or request.kind is not BrowserRequestKind.SCAN:
            raise TranscriptValidationError("Scan request does not match")
        try:
            self.repository.get_recording(recording_id)
            managed = self._managed_path(path)
            payload = self._validated_payload(managed, language)
            revision_id = self.pipeline.ingest_transcript(recording_id, payload)
            managed.unlink()
            return revision_id
        except (KeyError, OSError, TranscriptValidationError, ValueError) as error:
            self._handle_invalid(request_id, path, now_utc)
            if isinstance(error, TranscriptValidationError):
                raise
            raise TranscriptValidationError("Caption download could not be ingested") from error

    def _managed_path(self, path: Path) -> Path:
        inbox = self._expanded(self.settings.panopto_inbox)
        candidate = self._expanded(path)
        if not candidate.is_relative_to(inbox):
            raise TranscriptValidationError("Caption path is outside the managed inbox")
        if candidate.suffix.casefold() != ".txt":
            raise TranscriptValidationError("Caption download must be a .txt file")
        if not candidate.is_file():
            raise TranscriptValidationError("Caption download is missing")
        return candidate

    def _validated_payload(self, path: Path, language: str) -> bytes:
        if language != "English_USA":
            raise TranscriptValidationError(
                "English (United States) captions are required"
            )
        before = path.stat()
        if before.st_size > self.settings.panopto_max_caption_bytes:
            raise TranscriptValidationError("caption payload size is invalid")
        payload = path.read_bytes()
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(payload) != after.st_size
        ):
            raise TranscriptValidationError("Caption download is not stable")
        validate_raw_caption(payload, self.settings.panopto_max_caption_bytes)
        return payload

    def _handle_invalid(
        self,
        request_id: str,
        path: Path,
        now_utc: datetime,
    ) -> None:
        try:
            managed = self._managed_path_for_quarantine(path)
            if managed is not None:
                root = self._expanded(self.settings.panopto_quarantine_root)
                destination = (
                    root
                    / request_id
                    / f"{uuid.uuid4().hex}-{managed.name}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(managed, destination)
        finally:
            if self.repository.get_browser_request(request_id) is not None:
                self.repository.fail_browser_request(
                    request_id,
                    "invalid_caption_download",
                    now_utc,
                )
                self.repository.heartbeat(
                    "needs_review",
                    now_utc,
                    "invalid_caption_download",
                )

    def _managed_path_for_quarantine(self, path: Path) -> Path | None:
        inbox = self._expanded(self.settings.panopto_inbox)
        candidate = self._expanded(path)
        if candidate.is_relative_to(inbox) and candidate.is_file():
            return candidate
        return None

    @staticmethod
    def _expanded(path: Path) -> Path:
        return Path(os.path.expandvars(str(path))).resolve()
