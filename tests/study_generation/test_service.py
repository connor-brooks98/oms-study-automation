from dataclasses import dataclass
from pathlib import Path

import pytest

from oms_hub.ingestion.domain import UploadKind
from oms_hub.study_generation.domain import (
    GenerationKind,
    GenerationState,
    PromptSnapshot,
)
from oms_hub.study_generation.service import (
    GenerationPrerequisiteError,
    GenerationService,
)


@dataclass
class Lecture:
    id: int = 4


@dataclass
class Revision:
    id: int
    kind: UploadKind
    canonical_derived_path: Path | None
    derived_sha256: str | None
    current: bool = True


class Catalog:
    def get_lecture(self, lecture_id):
        return Lecture(lecture_id)


class Ingestion:
    def __init__(self, revisions):
        self.revisions = revisions

    def list_current_revisions(self, lecture_id):
        return self.revisions


class Prompts:
    def inspect(self, kind):
        return PromptSnapshot(Path("prompt.md"), "Prompt", "a" * 64, "now")


class Google:
    def status(self):
        return type("Status", (), {"state": "connected"})()


class Jobs:
    def __init__(self):
        self.advanced = None

    def queue(self, lecture_id, kind):
        return type(
            "Job",
            (),
            {
                "id": "job-1",
                "pdf_revision_id": None,
                "kind": kind,
                "state": GenerationState.QUEUED,
            },
        )()

    def advance(self, job_id, stage, **fields):
        self.advanced = fields
        return type(
            "Job",
            (),
            {"id": job_id, "kind": GenerationKind.OUTLINE, **fields},
        )()


def test_outline_queue_requires_current_pdf_and_cleaned_transcript(tmp_path):
    service = GenerationService(
        Catalog(),
        Ingestion([]),
        Jobs(),
        Prompts(),
        Google(),
    )

    with pytest.raises(GenerationPrerequisiteError) as error:
        service.queue_outline(4)

    assert str(error.value) == "Current lecture PDF and cleaned transcript are required"


def test_queue_snapshots_current_revision_ids_and_prompt(tmp_path):
    pdf = tmp_path / "lecture.pdf"
    transcript = tmp_path / "transcript.txt"
    pdf.write_bytes(b"pdf")
    transcript.write_text("clean", encoding="utf-8")
    import hashlib

    jobs = Jobs()
    service = GenerationService(
        Catalog(),
        Ingestion(
            [
                Revision(
                    10,
                    UploadKind.SLIDES,
                    pdf,
                    hashlib.sha256(b"pdf").hexdigest(),
                ),
                Revision(
                    11,
                    UploadKind.TRANSCRIPTS,
                    transcript,
                    hashlib.sha256(b"clean").hexdigest(),
                ),
            ]
        ),
        jobs,
        Prompts(),
        Google(),
    )

    service.queue_outline(4)

    assert jobs.advanced["pdf_revision_id"] == 10
    assert jobs.advanced["transcript_revision_id"] == 11
    assert jobs.advanced["prompt_sha256"] == "a" * 64
