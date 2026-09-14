from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from threading import RLock
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from oms_hub.document_processing.lecture_intake import parse_lecture_source
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.ingestion.domain import (
    ChunkSession,
    MatchDecision,
    StoredUploadItem,
    UploadEvidence,
    UploadKind,
    UploadState,
)
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.repositories import CatalogRepository

_MAX_EVIDENCE_MEMBER = 1024 * 1024


class IngestionService:
    def __init__(
        self,
        repository: IngestionRepository,
        catalog: CatalogRepository,
        matcher: UploadMatcher,
        staging: StagingService,
    ):
        self.repository = repository
        self.catalog = catalog
        self.matcher = matcher
        self.staging = staging
        self._decision_lock = RLock()

    def match_item(self, item_id: str) -> StoredUploadItem:
        item = self.repository.require_item(item_id)
        if item.manual_assignment:
            return item
        decision = self.matcher.match(
            self._evidence(item),
            self.catalog.list_lectures(),
        )
        self.repository.apply_match(item_id, decision)
        if decision.lecture_id is not None:
            self._complete_match_steps(
                decision.lecture_id,
                item.kind,
            )
        return self.repository.require_item(item_id)

    def decide_staged(
        self, kind: UploadKind, path: Path, filename: str
    ) -> MatchDecision:
        """Compute a match without creating item rows or catalog progress."""
        title, opening = self._source_text(path, filename)
        return self.matcher.match(
            UploadEvidence(filename=filename, embedded_title=title, opening_text=opening),
            self.catalog.list_lectures(),
        )

    def assign(self, item_id: str, lecture_id: int) -> None:
        if self.catalog.get_lecture(lecture_id) is None:
            raise KeyError(lecture_id)
        item = self.repository.require_item(item_id)
        self.repository.set_manual_assignment(item_id, lecture_id)
        self._complete_match_steps(lecture_id, item.kind)

    def confirm_processing(self, item_id: str) -> StoredUploadItem:
        with self._decision_lock:
            return self.repository.confirm_processing(item_id)

    def discard_item(self, item_id: str) -> StoredUploadItem:
        with self._decision_lock:
            item = self.repository.require_item(item_id)
            if item.state is UploadState.DISCARDED:
                return item
            if item.state is not UploadState.AWAITING_CONFIRMATION:
                raise ValueError("upload is not awaiting confirmation")
            self.staging.discard_file(item.staged_path)
            return self.repository.mark_discarded(item_id)

    def collect_staging(
        self,
        now: datetime | None = None,
        *,
        terminal_retention: timedelta = timedelta(hours=24),
    ) -> int:
        """Safe idempotent hook for startup/periodic runtime ownership."""
        current = now or datetime.now(UTC)
        expired_chunks: list[ChunkSession] = []
        removed = self.staging.collect_expired(current, expired_chunks=expired_chunks)
        for chunk in expired_chunks:
            if not chunk.manifest_owned and self.repository.get_batch(chunk.batch_id) is None:
                removed += int(self.staging.discard_unreferenced_batch(chunk.batch_id))
        for manifest_id, batch_id in self.staging.expired_pending_manifest_finalizations(
            current
        ):
            committed = self.repository.get_batch(batch_id) is not None
            if not committed:
                removed += int(self.staging.discard_unreferenced_batch(batch_id))
            removed += int(
                self.staging.reconcile_pending_manifest_finalization(
                    manifest_id, batch_id, committed=committed
                )
            )
        for path in self.repository.terminal_staging_paths_before(
            current - terminal_retention
        ):
            try:
                self.staging.discard_file(path)
            except UploadRejected:
                # Missing files and paths outside staging are not cleanup
                # candidates; a future collection remains safe/idempotent.
                continue
            removed += 1
        return removed

    def _complete_match_steps(
        self,
        lecture_id: int,
        kind: UploadKind,
    ) -> None:
        received, matched = (
            (
                V2StepName.SLIDES_RECEIVED,
                V2StepName.SLIDES_MATCHED,
            )
            if kind is UploadKind.SLIDES
            else (
                V2StepName.TRANSCRIPT_RECEIVED,
                V2StepName.TRANSCRIPT_MATCHED,
            )
        )
        self.catalog.set_step_status(
            lecture_id,
            received,
            StepStatus.COMPLETE,
            "Manual file received",
        )
        self.catalog.set_step_status(
            lecture_id,
            matched,
            StepStatus.COMPLETE,
            "Lecture match stored",
        )

    def _evidence(self, item: StoredUploadItem) -> UploadEvidence:
        title, opening = self._source_text(item.staged_path, item.original_filename)
        return UploadEvidence(
            filename=item.original_filename,
            embedded_title=title,
            opening_text=opening,
        )

    def _source_text(self, path: Path, filename: str) -> tuple[str, str]:
        suffix = Path(filename).suffix.casefold()
        if suffix == ".pptx":
            return self._pptx_text(path)
        if suffix in {".txt", ".md"}:
            return self._transcript_text(path)
        # Staging filenames deliberately carry no source extension. Give the
        # existing parsers a disposable, format-correct snapshot, never rename
        # or modify the staged original. Missing OCR falls back to filename
        # matching and review, rather than silently inventing text evidence.
        try:
            with TemporaryDirectory(prefix="oms-lecture-match-") as directory:
                root = Path(directory)
                source = root / ("source" + suffix)
                copyfile(path, source)
                parsed = parse_lecture_source(source, root / "assets")
                texts = [
                    segment.text.strip() for segment in parsed.segments if segment.text.strip()
                ]
                return (texts[0][:500] if texts else "", " ".join(texts)[:2000])
        except Exception:  # noqa: BLE001 - absent parser evidence requires manual matching
            return "", ""

    def _pptx_text(self, path: Path) -> tuple[str, str]:
        title = ""
        opening = ""
        try:
            with ZipFile(path) as archive:
                title = self._xml_text(
                    archive,
                    "docProps/core.xml",
                )
                opening = self._xml_text(
                    archive,
                    "ppt/slides/slide1.xml",
                )
        except BadZipFile:
            return "", ""
        return title[:500], opening[:2000]

    def _xml_text(self, archive: ZipFile, name: str) -> str:
        try:
            info = archive.getinfo(name)
        except KeyError:
            return ""
        if info.file_size > _MAX_EVIDENCE_MEMBER:
            return ""
        try:
            root = ElementTree.fromstring(archive.read(info))
        except ElementTree.ParseError:
            return ""
        return " ".join(
            text.strip()
            for text in root.itertext()
            if text.strip()
        )

    def _transcript_text(self, path: Path) -> tuple[str, str]:
        from oms_hub.ingestion.staging import decode_utf8_transcript

        # Validate the complete stream before taking display evidence.  A raw
        # byte slice can split a valid UTF-8 code point at its boundary.
        text = decode_utf8_transcript(path.read_bytes())
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else ""
        return title[:500], " ".join(lines[:20])[:2000]
