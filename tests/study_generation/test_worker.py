import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from oms_hub.ingestion.domain import UploadKind
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    PromptSnapshot,
)
from oms_hub.study_generation.gemini_quiz import SharedQuiz
from oms_hub.study_generation.worker import GenerationWorker


class Repository:
    def __init__(self, job):
        self.job = job
        self.quiz = None

    def claim_next(self, now):
        job, self.job = self.job, None
        return job

    def advance(self, job_id, stage, **fields):
        self.job = replace(self.current, stage=stage, **fields)
        self.current = self.job
        return self.job

    def complete(self, job_id):
        self.current = replace(
            self.current,
            state=GenerationState.COMPLETE,
            stage=GenerationStage.COMPLETE,
        )

    def fail(self, job_id, error, paused=False):
        raise AssertionError(error)

    def record_quiz(self, lecture_id, job_id, url, docs_synced):
        self.quiz = url


class Gemini:
    def __init__(self):
        self.generate_calls = 0
        self.share_calls = 0

    def generate(self, job_id, content):
        self.generate_calls += 1
        raise AssertionError("generate must not repeat")

    def share(self, quiz):
        self.share_calls += 1
        return SharedQuiz("https://gemini.google.com/share/quiz-1")


def test_worker_resume_at_share_does_not_regenerate_quiz(tmp_path):
    pdf = tmp_path / "lecture.pdf"
    txt = tmp_path / "lecture.txt"
    pdf.write_bytes(b"pdf")
    txt.write_text("clean", encoding="utf-8")
    job = GenerationJob(
        "job-1",
        1,
        GenerationKind.QUIZ,
        GenerationState.RUNNING,
        GenerationStage.SHARE,
        1,
        prompt_sha256="a" * 64,
        pdf_revision_id=10,
        transcript_revision_id=11,
        notebook_id="nb-1",
        pdf_source_id="pdf-1",
        transcript_source_id="txt-1",
        notebook_answer="Quiz content",
        gemini_quiz_id="https://gemini.google.com/app/quiz-1",
    )
    repository = Repository(job)
    repository.current = job
    revisions = {
        10: SimpleNamespace(
            id=10,
            lecture_id=1,
            kind=UploadKind.SLIDES,
            current=True,
            canonical_derived_path=pdf,
            derived_sha256=hashlib.sha256(b"pdf").hexdigest(),
        ),
        11: SimpleNamespace(
            id=11,
            lecture_id=1,
            kind=UploadKind.TRANSCRIPTS,
            current=True,
            canonical_derived_path=txt,
            derived_sha256=hashlib.sha256(b"clean").hexdigest(),
        ),
    }
    docs = SimpleNamespace(
        ensure_course_document=lambda subject: "document",
        ensure_exam_tab=lambda document, exam: "tab",
        sync_quiz_link=lambda tab, lecture, url: None,
    )
    gemini = Gemini()
    worker = GenerationWorker(
        repository,
        SimpleNamespace(
            get_lecture=lambda lecture_id: SimpleNamespace(
                subject="Neuro",
                exam_number=1,
                lecture_number=1,
                topic="Seizures",
            )
        ),
        SimpleNamespace(get_study_revision=lambda revision_id: revisions[revision_id]),
        SimpleNamespace(
            inspect=lambda kind: PromptSnapshot(
                Path("prompt.md"),
                "Prompt",
                "a" * 64,
                "now",
            )
        ),
        SimpleNamespace(),
        SimpleNamespace(),
        gemini,
        docs,
    )

    assert worker.run_once()
    assert gemini.generate_calls == 0
    assert gemini.share_calls == 1
    assert repository.quiz == "https://gemini.google.com/share/quiz-1"
