import os
import time
from dataclasses import dataclass
from pathlib import Path

from oms_hub.canvas.repository import CanvasRepository
from oms_hub.config import Settings
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.office_security import OfficeSecurityError, office_file_is_encrypted
from oms_hub.naming import sanitize_filename


@dataclass(frozen=True, slots=True)
class IngestedRevision:
    revision_id: int
    sha256: str
    stored_path: Path


def _expanded(path: Path) -> Path:
    return Path(os.path.expandvars(str(path)))


def _validate_magic(path: Path) -> None:
    suffix = path.suffix.casefold()
    header = path.read_bytes()[:8]
    if suffix in {".pptx", ".docx"} and not header.startswith(b"PK"):
        raise ValueError("OOXML file does not have ZIP signature")
    if suffix in {".ppt", ".doc"} and header != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise ValueError("legacy Office file does not have OLE signature")
    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise ValueError("PDF signature is invalid")
    if suffix not in {".pptx", ".docx", ".ppt", ".doc", ".pdf"}:
        raise ValueError("downloaded file type is not supported")


class IngestionService:
    def __init__(
        self,
        repository: CanvasRepository,
        settings: Settings,
        stability_wait_seconds: float = 0.5,
    ):
        self.repository = repository
        self.settings = settings
        self.stability_wait_seconds = stability_wait_seconds

    def _stable(self, path: Path) -> None:
        previous: tuple[int, int] | None = None
        for _ in range(10):
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime_ns)
            if previous == current:
                return
            previous = current
            time.sleep(self.stability_wait_seconds)
        raise ValueError("download did not stabilize within the allowed time")

    def complete_download(
        self,
        source_item_id: int,
        download_id: int,
        path: Path,
    ) -> IngestedRevision:
        del download_id
        inbox = _expanded(self.settings.canvas_inbox).resolve()
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(inbox):
            raise ValueError("download path is outside the managed Canvas inbox")
        revision = self.repository.pending_revision(source_item_id)
        if revision.sha256 and revision.stored_path:
            return IngestedRevision(revision.id, revision.sha256, Path(revision.stored_path))
        try:
            self._stable(resolved)
            if resolved.stat().st_size > self.settings.max_ingest_bytes:
                raise ValueError("download exceeds the configured size limit")
            if resolved.stat().st_size != revision.remote_size:
                raise ValueError("download size does not match Canvas metadata")
            if resolved.suffix.casefold() != Path(revision.original_filename).suffix.casefold():
                raise ValueError("download extension does not match Canvas metadata")
            _validate_magic(resolved)
            if resolved.suffix.casefold() in {".ppt", ".pptx", ".doc", ".docx"}:
                if office_file_is_encrypted(resolved):
                    raise ValueError("encrypted Office files require manual review")
            digest = sha256_file(resolved)
            destination = (
                _expanded(self.settings.revision_root)
                / str(revision.id)
                / sanitize_filename(revision.original_filename)
            )
            verified_atomic_copy(resolved, destination)
            self.repository.complete_ingestion(revision.id, digest, str(destination))
            return IngestedRevision(revision.id, digest, destination)
        except (OSError, ValueError, OfficeSecurityError) as error:
            self.repository.mark_revision_review(revision.id, str(error))
            raise
