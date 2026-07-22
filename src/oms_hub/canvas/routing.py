import os
import re
from pathlib import Path

from oms_hub.canvas.domain import CanonicalPaths, SourceKind
from oms_hub.config import Settings
from oms_hub.domain import LectureKey
from oms_hub.naming import artifact_names, sanitize_filename

_GENERIC_PQ = re.compile(
    r"^(practice\s*(questions?|qs)|questions?\s*set|review\s*questions?)$",
    re.IGNORECASE,
)


def _expanded(path: Path) -> Path:
    return Path(os.path.expandvars(str(path)))


def build_paths(
    settings: Settings,
    lecture: LectureKey,
    kind: SourceKind,
    original_filename: str,
    revision_id: int,
) -> CanonicalPaths:
    if settings.icloud_staging_root is None:
        raise ValueError("iCloud staging root has not been configured")
    study = _expanded(settings.study_root)
    cloud = _expanded(settings.icloud_staging_root) / "OMS II Goodnotes Inbox"
    revision = _expanded(settings.revision_root) / str(revision_id)
    subject = sanitize_filename(lecture.subject)
    names = artifact_names(lecture)
    suffix = Path(original_filename).suffix.casefold()
    revision_original = revision / sanitize_filename(Path(original_filename).name)
    revision_pdf = revision / "converted.pdf"
    if kind is SourceKind.LECTURE:
        local_dir = study / subject / f"Exam {lecture.exam_number}" / "Lectures"
        local_source = local_dir / Path(names.pptx).with_suffix(suffix).name
        local_pdf = local_dir / names.pdf
        icloud_pdf = cloud / subject / f"Exam {lecture.exam_number}" / names.pdf
    elif kind is SourceKind.PRACTICE_QUESTIONS:
        raw_stem = sanitize_filename(Path(original_filename).stem)
        descriptive = "Practice Questions" if _GENERIC_PQ.fullmatch(raw_stem) else raw_stem
        stem = f"{Path(names.pdf).stem} - {descriptive}"
        local_source = None
        local_pdf = (
            study
            / subject
            / f"Exam {lecture.exam_number}"
            / "Practice Questions"
            / f"{stem}.pdf"
        )
        icloud_pdf = (
            cloud
            / subject
            / f"Exam {lecture.exam_number}"
            / "Practice Questions"
            / f"{stem}.pdf"
        )
    else:
        raise ValueError(f"cannot build final paths for {kind.value}")
    return CanonicalPaths(
        revision_original,
        revision_pdf,
        local_source,
        local_pdf,
        icloud_pdf,
    )
