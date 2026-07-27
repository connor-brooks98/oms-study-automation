import os
from dataclasses import dataclass
from pathlib import Path

from oms_hub.config import Settings
from oms_hub.domain import LectureKey
from oms_hub.naming import artifact_names, sanitize_filename


@dataclass(frozen=True, slots=True)
class SlideDestinations:
    source: Path
    pdf: Path
    icloud_pdf: Path


def expanded_path(path: Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def build_slide_destinations(
    settings: Settings,
    lecture: LectureKey,
) -> SlideDestinations:
    if settings.icloud_staging_root is None:
        raise ValueError("iCloud staging root has not been configured")
    subject = sanitize_filename(lecture.subject)
    names = artifact_names(lecture)
    local_root = expanded_path(settings.study_root)
    cloud_root = (
        expanded_path(settings.icloud_staging_root)
        / "OMS II Goodnotes Inbox"
    )
    lecture_dir = (
        local_root
        / subject
        / f"Exam {lecture.exam_number}"
        / "Lectures"
    )
    cloud_dir = cloud_root / subject / f"Exam {lecture.exam_number}"
    destinations = SlideDestinations(
        source=lecture_dir / names.pptx,
        pdf=lecture_dir / names.pdf,
        icloud_pdf=cloud_dir / names.pdf,
    )
    _require_within(destinations.source, local_root)
    _require_within(destinations.pdf, local_root)
    _require_within(destinations.icloud_pdf, cloud_root)
    return destinations


def build_transcript_destination(
    settings: Settings,
    lecture: LectureKey,
) -> Path:
    study_root = expanded_path(settings.study_root)
    destination = (
        study_root
        / sanitize_filename(lecture.subject)
        / f"Exam {lecture.exam_number}"
        / "Transcripts"
        / artifact_names(lecture).transcript
    )
    _require_within(destination, study_root)
    return destination


def _require_within(path: Path, root: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact destination escapes its configured root")
