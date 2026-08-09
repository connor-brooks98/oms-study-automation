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
    UploadManifest,
    UploadManifestSlot,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PPTX_REQUIRED = {"[Content_Types].xml", "ppt/presentation.xml"}


class UploadRejected(ValueError):
    pass


def decode_utf8_transcript(raw: bytes) -> str:
    """The single admission decoder for staged, matched, and processed text."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UploadRejected("transcript is not UTF-8") from error


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

    def begin_manifest(
        self,
        kind: UploadKind,
        slots: list[UploadManifestSlot],
        lecture_id: int | None = None,
    ) -> UploadManifest:
        """Create only request-scoped staging metadata, never queue rows."""
        if not slots:
            raise UploadRejected("at least one file is required")
        total = 0
        seen: set[str] = set()
        for slot in slots:
            if slot.id in seen:
                raise UploadRejected("manifest slot identifiers must be unique")
            seen.add(slot.id)
            self._validate_filename(kind, slot.filename)
            if slot.size_bytes < 1 or slot.size_bytes > self.max_file_bytes:
                raise UploadRejected("declared file size exceeds upload limit")
            total += slot.size_bytes
            if total > self.max_batch_bytes:
                raise UploadRejected("batch exceeds upload limit")
            if not _SHA256.fullmatch(slot.sha256):
                raise UploadRejected("invalid SHA-256 checksum")
        manifest = UploadManifest(str(uuid4()), kind, lecture_id, tuple(slots))
        root = self._manifest_root(manifest.id)
        root.mkdir(parents=True, exist_ok=False)
        self._write_manifest(manifest)
        return manifest

    def get_manifest(self, manifest_id: str) -> UploadManifest:
        try:
            raw = json.loads(self._manifest_path(manifest_id).read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as error:
            raise UploadRejected("upload manifest does not exist") from error
        try:
            slots = tuple(
                UploadManifestSlot(
                    id=str(slot["id"]),
                    filename=str(slot["filename"]),
                    size_bytes=int(slot["size_bytes"]),
                    sha256=str(slot["sha256"]),
                )
                for slot in raw["slots"]
            )
            return UploadManifest(
                id=str(raw["id"]),
                kind=UploadKind(raw["kind"]),
                lecture_id=(int(raw["lecture_id"]) if raw["lecture_id"] is not None else None),
                slots=slots,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UploadRejected("upload manifest is invalid") from error

    def stage_manifest_file(
        self,
        manifest_id: str,
        slot_id: str,
        stream: BinaryIO,
    ) -> None:
        manifest = self.get_manifest(manifest_id)
        slot = self._manifest_slot(manifest, slot_id)
        target = self._manifest_file_path(manifest_id, slot_id)
        self._write_checked_file(
            manifest.kind,
            slot.filename,
            stream,
            target,
            expected_size=slot.size_bytes,
            expected_sha256=slot.sha256,
        )

    def manifest_uploads(self, manifest_id: str) -> list[StagedUpload]:
        manifest = self.get_manifest(manifest_id)
        uploads: list[StagedUpload] = []
        errors: list[dict[str, str]] = []
        for slot in manifest.slots:
            path = self._manifest_file_path(manifest.id, slot.id)
            try:
                if not path.is_file():
                    raise UploadRejected("file transfer is incomplete")
                self._validate_content(manifest.kind, path)
                if path.stat().st_size != slot.size_bytes:
                    raise UploadRejected("file size does not match manifest")
                if self._hash_file(path) != slot.sha256:
                    raise UploadRejected("file checksum does not match manifest")
            except UploadRejected as error:
                errors.append(
                    {
                        "slot_id": slot.id,
                        "filename": slot.filename,
                        "code": "validation_failed",
                        "detail": str(error),
                    }
                )
                continue
            uploads.append(
                StagedUpload(
                    batch_id=manifest.id,
                    item_id=slot.id,
                    path=path,
                    sha256=slot.sha256,
                    size_bytes=slot.size_bytes,
                    original_filename=slot.filename,
                )
            )
        if errors:
            self.discard_manifest(manifest_id)
            raise UploadRejected(json.dumps({"errors": errors}))
        return uploads

    def promote_manifest(
        self,
        manifest_id: str,
        batch_id: str,
    ) -> list[StagedUpload]:
        """Move validated temporary files into batch-owned ready names.

        Callers must revert with ``revert_promoted_manifest`` if their single
        database finalization transaction fails.
        """
        uploads = self.manifest_uploads(manifest_id)
        kind = self.get_manifest(manifest_id).kind
        target_root = self._batch_root(batch_id)
        target_root.mkdir(parents=True, exist_ok=False)
        (target_root / "kind").write_text(kind.value, encoding="ascii")
        target_root = self._batch_root(batch_id)
        moved: list[StagedUpload] = []
        try:
            for upload in uploads:
                target = target_root / f"{upload.item_id}.ready"
                upload.path.replace(target)
                moved.append(
                    StagedUpload(
                        batch_id=batch_id,
                        item_id=upload.item_id,
                        path=target,
                        sha256=upload.sha256,
                        size_bytes=upload.size_bytes,
                        original_filename=upload.original_filename,
                    )
                )
        except Exception:
            self.revert_promoted_manifest(manifest_id, batch_id, moved)
            raise
        return moved

    def revert_promoted_manifest(
        self,
        manifest_id: str,
        batch_id: str,
        uploads: list[StagedUpload],
    ) -> None:
        for upload in uploads:
            if upload.path.exists():
                upload.path.replace(self._manifest_file_path(manifest_id, upload.item_id))
        batch_root = self._batch_root(batch_id)
        for child in batch_root.glob("*"):
            child.unlink(missing_ok=True)
        batch_root.rmdir()

    def discard_manifest(self, manifest_id: str) -> None:
        root = self._manifest_root(manifest_id)
        if not root.exists():
            return
        for path in root.glob("*"):
            path.unlink(missing_ok=True)
        root.rmdir()
        chunk_root = self._chunk_root()
        if chunk_root.exists():
            for session_path in chunk_root.glob("*.json"):
                try:
                    session = self._load_session(session_path.stem)
                except UploadRejected:
                    continue
                if session.manifest_owned and session.batch_id == manifest_id:
                    self._chunk_path(session.id).unlink(missing_ok=True)
                    session_path.unlink(missing_ok=True)

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

    def _write_checked_file(
        self,
        kind: UploadKind,
        filename: str,
        stream: BinaryIO,
        target: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        self._validate_filename(kind, filename)
        temporary = target.with_suffix(".writing")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > expected_size or size > self.max_file_bytes:
                        raise UploadRejected("file exceeds declared upload size")
                    digest.update(chunk)
                    output.write(chunk)
            if size != expected_size:
                raise UploadRejected("file size does not match manifest")
            if digest.hexdigest() != expected_sha256:
                raise UploadRejected("file checksum does not match manifest")
            self._validate_content(kind, temporary)
            temporary.replace(target)
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
            manifest_owned=False,
        )
        chunk_root = self._chunk_root()
        chunk_root.mkdir(parents=True, exist_ok=True)
        self._write_session(session)
        self._chunk_path(session.id).touch(exist_ok=False)
        return session

    def begin_manifest_chunks(
        self,
        manifest_id: str,
        slot_id: str,
    ) -> ChunkSession:
        manifest = self.get_manifest(manifest_id)
        slot = self._manifest_slot(manifest, slot_id)
        session = ChunkSession(
            id=str(uuid4()),
            batch_id=manifest.id,
            item_id=slot.id,
            kind=manifest.kind,
            filename=slot.filename,
            total_size=slot.size_bytes,
            expected_sha256=slot.sha256,
            received=0,
            expires_at=(datetime.now(UTC) + timedelta(hours=self.session_hours)).isoformat(),
            manifest_owned=True,
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
            manifest_owned=session.manifest_owned,
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
        if session.manifest_owned:
            self._manifest_slot(self.get_manifest(session.batch_id), session.item_id)
            ready = self._manifest_file_path(session.batch_id, session.item_id)
        else:
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

    def discard_file(self, path: Path) -> None:
        resolved_root = self.root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise UploadRejected("upload path escaped staging root")
        if not resolved.is_file():
            raise UploadRejected("staged upload file is missing")
        resolved.unlink()

    def collect_expired(self, now: datetime | None = None) -> int:
        """Idempotently collect only abandoned chunk/manifest request state.

        Accepted ready files require repository reference checks and are
        deliberately handled by the repository-facing cleanup hook.
        """
        current = now or datetime.now(UTC)
        removed = 0
        chunk_root = self._chunk_root()
        if chunk_root.exists():
            for session_path in chunk_root.glob("*.json"):
                try:
                    session = self._load_session(session_path.stem)
                    expired = datetime.fromisoformat(session.expires_at) <= current
                except UploadRejected:
                    expired = True
                if expired:
                    self._chunk_path(session_path.stem).unlink(missing_ok=True)
                    session_path.unlink(missing_ok=True)
                    removed += 1
        manifest_root = self.root / "manifests"
        if manifest_root.exists():
            cutoff = current - timedelta(hours=self.session_hours)
            for root in manifest_root.iterdir():
                if not root.is_dir():
                    continue
                try:
                    created = datetime.fromtimestamp(root.stat().st_mtime, UTC)
                except OSError:
                    continue
                if created <= cutoff:
                    self.discard_manifest(root.name)
                    removed += 1
        return removed

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
        decoded = decode_utf8_transcript(raw)
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

    def _manifest_root(self, manifest_id: str) -> Path:
        return self._contained(self.root / "manifests", manifest_id)

    def _manifest_path(self, manifest_id: str) -> Path:
        return self._manifest_root(manifest_id) / "manifest.json"

    def _manifest_file_path(self, manifest_id: str, slot_id: str) -> Path:
        self._contained(self._manifest_root(manifest_id), slot_id)
        return self._manifest_root(manifest_id) / f"{slot_id}.part"

    def _manifest_slot(
        self, manifest: UploadManifest, slot_id: str
    ) -> UploadManifestSlot:
        try:
            UUID(slot_id)
        except ValueError as error:
            raise UploadRejected("invalid manifest slot identifier") from error
        for slot in manifest.slots:
            if slot.id == slot_id:
                return slot
        raise UploadRejected("manifest slot does not exist")

    def _write_manifest(self, manifest: UploadManifest) -> None:
        path = self._manifest_path(manifest.id)
        path.write_text(
            json.dumps(
                {
                    "id": manifest.id,
                    "kind": manifest.kind.value,
                    "lecture_id": manifest.lecture_id,
                    "slots": [
                        {
                            "id": slot.id,
                            "filename": slot.filename,
                            "size_bytes": slot.size_bytes,
                            "sha256": slot.sha256,
                        }
                        for slot in manifest.slots
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

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
                    "manifest_owned": session.manifest_owned,
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
            manifest_owned=bool(raw.get("manifest_owned", False)),
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
