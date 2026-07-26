import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile, is_zipfile

from oms_hub.ingestion.domain import (
    ChunkSession,
    StagedUpload,
    UploadBatchRef,
    UploadKind,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PPTX_REQUIRED = {"[Content_Types].xml", "ppt/presentation.xml"}


class UploadRejected(ValueError):
    pass


class StagingService:
    def __init__(
        self,
        root: Path,
        max_file_bytes: int,
        max_batch_bytes: int,
        session_hours: int = 24,
    ):
        self.root = root
        self.max_file_bytes = max_file_bytes
        self.max_batch_bytes = max_batch_bytes
        self.session_hours = session_hours

    def begin_batch(self, kind: UploadKind) -> UploadBatchRef:
        batch = UploadBatchRef(str(uuid4()), kind)
        batch_root = self._batch_root(batch.id)
        batch_root.mkdir(parents=True, exist_ok=False)
        (batch_root / "kind").write_text(kind.value, encoding="ascii")
        return batch

    def stage_file(
        self,
        batch: UploadBatchRef,
        filename: str,
        stream: BinaryIO,
    ) -> StagedUpload:
        self._validate_filename(batch.kind, filename)
        item_id = str(uuid4())
        batch_root = self._batch_root(batch.id)
        if not batch_root.is_dir():
            raise UploadRejected("upload batch does not exist")
        temporary = batch_root / f"{item_id}.part"
        ready = batch_root / f"{item_id}.ready"
        existing_size = sum(
            path.stat().st_size
            for path in batch_root.glob("*.ready")
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise UploadRejected("file exceeds upload limit")
                    if existing_size + size > self.max_batch_bytes:
                        raise UploadRejected("batch exceeds upload limit")
                    digest.update(chunk)
                    output.write(chunk)
            self._validate_content(batch.kind, temporary)
            temporary.replace(ready)
            return StagedUpload(
                batch_id=batch.id,
                item_id=item_id,
                path=ready,
                sha256=digest.hexdigest(),
                size_bytes=size,
                original_filename=filename,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def begin_chunks(
        self,
        kind: UploadKind,
        filename: str,
        total_size: int,
        expected_sha256: str,
    ) -> ChunkSession:
        self._validate_filename(kind, filename)
        if total_size < 1 or total_size > self.max_file_bytes:
            raise UploadRejected("declared file size exceeds upload limit")
        if not _SHA256.fullmatch(expected_sha256):
            raise UploadRejected("invalid SHA-256 checksum")
        batch = self.begin_batch(kind)
        session = ChunkSession(
            id=str(uuid4()),
            batch_id=batch.id,
            item_id=str(uuid4()),
            kind=kind,
            filename=filename,
            total_size=total_size,
            expected_sha256=expected_sha256,
            received=0,
            expires_at=(
                datetime.now(UTC) + timedelta(hours=self.session_hours)
            ).isoformat(),
        )
        chunk_root = self._chunk_root()
        chunk_root.mkdir(parents=True, exist_ok=True)
        self._write_session(session)
        self._chunk_path(session.id).touch(exist_ok=False)
        return session

    def append_chunk(
        self,
        session_id: str,
        offset: int,
        stream: BinaryIO,
    ) -> int:
        session = self._load_session(session_id)
        self._require_active(session)
        if offset != session.received:
            raise UploadRejected("chunk offset does not match received bytes")
        received = session.received
        with self._chunk_path(session.id).open("ab") as output:
            while chunk := stream.read(1024 * 1024):
                received += len(chunk)
                if received > session.total_size:
                    raise UploadRejected("chunk exceeds declared file size")
                output.write(chunk)
        updated = ChunkSession(
            id=session.id,
            batch_id=session.batch_id,
            item_id=session.item_id,
            kind=session.kind,
            filename=session.filename,
            total_size=session.total_size,
            expected_sha256=session.expected_sha256,
            received=received,
            expires_at=session.expires_at,
        )
        self._write_session(updated)
        return received

    def finalize_chunks(self, session_id: str) -> StagedUpload:
        session = self._load_session(session_id)
        self._require_active(session)
        if session.received != session.total_size:
            raise UploadRejected("chunk upload is incomplete")
        source = self._chunk_path(session.id)
        digest = self._hash_file(source)
        if digest != session.expected_sha256:
            raise UploadRejected("chunk upload checksum mismatch")
        self._validate_content(session.kind, source)
        ready = self._batch_root(session.batch_id) / f"{session.item_id}.ready"
        source.replace(ready)
        self._session_path(session.id).unlink(missing_ok=True)
        return StagedUpload(
            batch_id=session.batch_id,
            item_id=session.item_id,
            path=ready,
            sha256=digest,
            size_bytes=session.total_size,
            original_filename=session.filename,
        )

    def _validate_filename(
        self,
        kind: UploadKind,
        filename: str,
    ) -> None:
        if (
            not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise UploadRejected("unsafe upload filename")
        expected = ".pptx" if kind is UploadKind.SLIDES else ".txt"
        if Path(filename).suffix.casefold() != expected:
            raise UploadRejected(f"{kind.value} uploads require {expected}")

    def _validate_content(self, kind: UploadKind, path: Path) -> None:
        if path.stat().st_size == 0:
            raise UploadRejected("uploaded file is empty")
        if kind is UploadKind.SLIDES:
            self._validate_pptx(path)
        else:
            self._validate_text(path)

    def _validate_pptx(self, path: Path) -> None:
        if not is_zipfile(path):
            raise UploadRejected("file is not a valid PowerPoint presentation")
        try:
            with ZipFile(path) as archive:
                if not _PPTX_REQUIRED.issubset(archive.namelist()):
                    raise UploadRejected(
                        "file is not a valid PowerPoint presentation"
                    )
        except BadZipFile as error:
            raise UploadRejected(
                "file is not a valid PowerPoint presentation"
            ) from error

    def _validate_text(self, path: Path) -> None:
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise UploadRejected("transcript contains binary data")
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = raw.decode("cp1252")
        if not decoded.strip():
            raise UploadRejected("transcript contains no text")
        printable = sum(
            character.isprintable() or character in "\r\n\t"
            for character in decoded
        )
        if printable / len(decoded) < 0.85:
            raise UploadRejected("transcript contains binary data")

    def _batch_root(self, batch_id: str) -> Path:
        return self._contained(self.root / "batches", batch_id)

    def _chunk_root(self) -> Path:
        return self.root / "chunks"

    def _session_path(self, session_id: str) -> Path:
        return self._contained(self._chunk_root(), f"{session_id}.json")

    def _chunk_path(self, session_id: str) -> Path:
        return self._contained(self._chunk_root(), f"{session_id}.part")

    def _write_session(self, session: ChunkSession) -> None:
        path = self._session_path(session.id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "id": session.id,
                    "batch_id": session.batch_id,
                    "item_id": session.item_id,
                    "kind": session.kind.value,
                    "filename": session.filename,
                    "total_size": session.total_size,
                    "expected_sha256": session.expected_sha256,
                    "received": session.received,
                    "expires_at": session.expires_at,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_session(self, session_id: str) -> ChunkSession:
        path = self._session_path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise UploadRejected("chunk upload session does not exist") from error
        return ChunkSession(
            id=str(raw["id"]),
            batch_id=str(raw["batch_id"]),
            item_id=str(raw["item_id"]),
            kind=UploadKind(raw["kind"]),
            filename=str(raw["filename"]),
            total_size=int(raw["total_size"]),
            expected_sha256=str(raw["expected_sha256"]),
            received=int(raw["received"]),
            expires_at=str(raw["expires_at"]),
        )

    def _require_active(self, session: ChunkSession) -> None:
        if datetime.fromisoformat(session.expires_at) <= datetime.now(UTC):
            raise UploadRejected("chunk upload session expired")

    def _contained(self, root: Path, name: str) -> Path:
        try:
            UUID(name.split(".", 1)[0])
        except ValueError as error:
            raise UploadRejected("invalid upload identifier") from error
        resolved_root = root.resolve()
        resolved = (root / name).resolve()
        if resolved_root not in resolved.parents:
            raise UploadRejected("upload path escaped staging root")
        return resolved

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(
                lambda: stream.read(1024 * 1024),
                b"",
            ):
                digest.update(chunk)
        return digest.hexdigest()
