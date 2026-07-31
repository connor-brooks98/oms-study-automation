from pathlib import Path
from threading import RLock
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from oms_hub.domain import StepStatus, V2StepName
from oms_hub.ingestion.domain import (
    StoredUploadItem,
    UploadEvidence,
    UploadKind,
    UploadState,
)
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.staging import StagingService
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
        if item.kind is UploadKind.SLIDES:
            title, opening = self._pptx_text(item.staged_path)
        else:
            title, opening = self._transcript_text(item.staged_path)
        return UploadEvidence(
            filename=item.original_filename,
            embedded_title=title,
            opening_text=opening,
        )

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
        except (ElementTree.ParseError, DefusedXmlException):
            return ""
        return " ".join(
            text.strip()
            for text in root.itertext()
            if text.strip()
        )

    def _transcript_text(self, path: Path) -> tuple[str, str]:
        raw = path.read_bytes()[:8192]
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else ""
        return title[:500], " ".join(lines[:20])[:2000]
