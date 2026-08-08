import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from oms_hub.models import LectureModel
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.tracker_import import (
    TrackerImportResult,
    TrackerParseIssue,
    parse_tracker,
)


@dataclass(frozen=True, slots=True)
class TrackerPreview:
    id: str
    state: str
    source_sha256: str
    source_name: str
    detected: tuple[LectureInput, ...]
    new: tuple[LectureInput, ...]
    changed: tuple[LectureInput, ...]
    missing: tuple[LectureInput, ...]
    issues: tuple[TrackerParseIssue, ...]
    unchanged: int

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.state,
            "source_sha256": self.source_sha256,
            "source_name": self.source_name,
            "detected": [asdict(item) for item in self.detected],
            "new": [asdict(item) for item in self.new],
            "changed": [asdict(item) for item in self.changed],
            "missing": [asdict(item) for item in self.missing],
            "issues": [asdict(item) for item in self.issues],
            "unchanged": self.unchanged,
        }


def _key(value: LectureInput) -> tuple[str, int, int]:
    return value.subject, value.exam_number, value.lecture_number


def _model_input(value: LectureModel) -> LectureInput:
    return LectureInput(
        subject=value.subject,
        exam_number=value.exam_number,
        lecture_number=value.lecture_number,
        topic=value.topic,
        lecturer=value.lecturer,
        exam_date=value.exam_date,
    )


class TrackerPreviewService:
    def __init__(self, repository: CatalogRepository, preview_root: Path):
        self.repository = repository
        self.preview_root = preview_root

    def preview(
        self,
        path: Path,
        source_name: str | None = None,
    ) -> TrackerPreview:
        parsed = parse_tracker(path)
        current = {
            _key(value): value
            for value in (
                _model_input(lecture)
                for lecture in self.repository.list_lectures()
            )
        }
        proposed = {_key(value): value for value in parsed.lectures}
        new = tuple(
            proposed[key] for key in sorted(proposed.keys() - current.keys())
        )
        missing = tuple(
            current[key] for key in sorted(current.keys() - proposed.keys())
        )
        changed = tuple(
            proposed[key]
            for key in sorted(proposed.keys() & current.keys())
            if proposed[key] != current[key]
        )
        unchanged = (
            len(proposed.keys() & current.keys()) - len(changed)
        )
        state = (
            "identical"
            if self.repository.has_import_hash(parsed.source_sha256)
            else "ready"
        )
        preview = TrackerPreview(
            id=str(uuid4()),
            state=state,
            source_sha256=parsed.source_sha256,
            source_name=source_name or parsed.source_name,
            detected=parsed.lectures,
            new=new,
            changed=changed,
            missing=missing,
            issues=parsed.issues,
            unchanged=unchanged,
        )
        self._store(preview, path)
        return preview

    def apply(self, preview_id: str) -> TrackerImportResult:
        preview_dir = self._preview_dir(preview_id)
        preview = self._load(preview_dir / "preview.json")
        if preview.state == "identical":
            raise ValueError("tracker workbook has already been imported")
        workbook = preview_dir / "workbook.xlsx"
        digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
        if digest != preview.source_sha256:
            raise ValueError("tracker preview workbook checksum changed")
        parsed = parse_tracker(workbook)
        issues = [
            (
                issue.sheet,
                issue.row_number,
                issue.message,
                issue.raw_values,
            )
            for issue in parsed.issues
        ]
        issues.extend(
            (
                "__catalog__",
                0,
                "lecture missing from latest tracker",
                json.dumps(asdict(lecture), sort_keys=True),
            )
            for lecture in preview.missing
        )
        imported, updated = self.repository.commit_tracker_import(
            list(parsed.lectures),
            issues,
            parsed.source_sha256,
            preview.source_name,
        )
        return TrackerImportResult(
            imported=imported,
            updated=updated,
            issues=len(issues),
            source_sha256=parsed.source_sha256,
        )

    def _store(self, preview: TrackerPreview, path: Path) -> None:
        preview_dir = self._preview_dir(preview.id)
        preview_dir.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(path, preview_dir / "workbook.xlsx")
        temporary = preview_dir / "preview.json.part"
        temporary.write_text(
            json.dumps(preview.public_dict(), sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(preview_dir / "preview.json")

    def _load(self, path: Path) -> TrackerPreview:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TrackerPreview(
            id=str(raw["id"]),
            state=str(raw["state"]),
            source_sha256=str(raw["source_sha256"]),
            source_name=str(raw["source_name"]),
            detected=tuple(
                LectureInput(**item)
                for item in raw.get(
                    "detected",
                    [*raw["new"], *raw["changed"]],
                )
            ),
            new=tuple(LectureInput(**item) for item in raw["new"]),
            changed=tuple(LectureInput(**item) for item in raw["changed"]),
            missing=tuple(LectureInput(**item) for item in raw["missing"]),
            issues=tuple(
                TrackerParseIssue(**item) for item in raw["issues"]
            ),
            unchanged=int(raw["unchanged"]),
        )

    def _preview_dir(self, preview_id: str) -> Path:
        normalized = str(UUID(preview_id))
        if normalized != preview_id:
            raise ValueError("invalid tracker preview ID")
        root = self.preview_root.resolve()
        resolved = (root / normalized).resolve()
        if root not in resolved.parents:
            raise ValueError("tracker preview path escaped root")
        return resolved
