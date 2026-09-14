import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.domain import StepStatus
from oms_hub.ingestion.domain import UploadKind
from oms_hub.study_generation.domain import (
    GenerationJob,
    GenerationKind,
    GenerationStage,
    GenerationState,
    NotebookAnswer,
    PromptSnapshot,
)
from oms_hub.study_generation.notebook import NotebookAuthenticationError
from oms_hub.study_generation.worker import GenerationWorker

QUIZ_JSON = json.dumps(
    {
        "title": "Seizure Practice",
        "questions": [
            {
                "stem": "Which finding is expected?",
                "choices": ["First", "Second", "Third", "Fourth"],
                "correct_index": 1,
                "rationale": "Second is the best answer.",
            }
        ],
    }
)
MATCHING_QUIZ_JSON = json.dumps({
    "title": "Matching set",
    "questions": [{
        "kind": "matching",
        "stem": "Match each description with its term.",
        "prompts": [
            {"label": "A", "text": "Alpha", "correct_index": 1},
            {"label": "B", "text": "Beta", "correct_index": 0},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
        "image_ref": None,
    }],
})
QUIZ_URL = "https://study.example.com/public/quizzes/" + "a" * 64


class Repository:
    def __init__(self, job):
        self.current = job
        self.claimable = job
        self.quiz = None
        self.advances = []

    def claim_next(self, now):
        del now
        job, self.claimable = self.claimable, None
        return job

    def advance(self, job_id, stage, **fields):
        assert job_id == self.current.id
        self.current = replace(self.current, stage=stage, **fields)
        self.advances.append((stage, fields))
        return self.current

    def complete(self, job_id):
        assert job_id == self.current.id
        self.current = replace(
            self.current,
            state=GenerationState.COMPLETE,
            stage=GenerationStage.COMPLETE,
        )
        return self.current

    def fail(self, job_id, error, paused=False):
        raise AssertionError((job_id, error, paused))

    def retry(self, job_id, error, delay):
        assert job_id == self.current.id
        self.retried = (job_id, error, delay)

    def record_quiz(self, lecture_id, job_id, url):
        self.quiz = (lecture_id, job_id, url)


class Publisher:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def publish(self, lecture_id, job_id, quiz):
        if self.fail:
            raise AssertionError("native quiz must not be republished")
        self.calls.append((lecture_id, job_id, quiz))
        return QUIZ_URL


class NotebookConnection:
    def __init__(self):
        self.invalidations = []

    def invalidate(self, message):
        self.invalidations.append(message)


class Notebook:
    def __init__(self):
        self.prompt = None

    def ask(self, notebook, sources, prompt):
        del notebook, sources
        self.prompt = prompt
        return NotebookAnswer(QUIZ_JSON)


class ExpiredNotebook(Notebook):
    def ask(self, notebook, sources, prompt):
        del notebook, sources, prompt
        raise NotebookAuthenticationError(
            "NotebookLM login expired; reconnect Google in Settings."
        )


def _job(**overrides):
    values = {
        "id": "job-1",
        "lecture_id": 1,
        "kind": GenerationKind.QUIZ,
        "state": GenerationState.RUNNING,
        "stage": GenerationStage.NOTEBOOK_PROMPT,
        "attempts": 1,
        "prompt_sha256": "a" * 64,
        "pdf_revision_id": 10,
        "transcript_revision_id": 11,
        "notebook_id": "nb-1",
        "pdf_source_id": "pdf-1",
        "transcript_source_id": "txt-1",
    }
    values.update(overrides)
    return GenerationJob(**values)


def _worker(tmp_path, job, publisher, notebook=None):
    pdf = tmp_path / "lecture.pdf"
    txt = tmp_path / "lecture.txt"
    pdf.write_bytes(b"pdf")
    txt.write_text("clean", encoding="utf-8")
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
    progress = []
    catalog = SimpleNamespace(
        get_lecture=lambda lecture_id: SimpleNamespace(
            subject="Neuro",
            exam_number=1,
            lecture_number=2,
            topic="Seizures",
        ),
        set_step_status=lambda lecture_id, name, status, detail=None: (
            progress.append((lecture_id, name, status, detail))
        ),
    )
    repository = Repository(job)
    connection = NotebookConnection()
    selected_notebook = notebook or SimpleNamespace()
    worker = GenerationWorker(
        repository,
        catalog,
        SimpleNamespace(
            get_study_revision=lambda revision_id: revisions[revision_id]
        ),
        SimpleNamespace(
            inspect=lambda kind: PromptSnapshot(
                Path("Quiz Prompt.md"),
                "Create a rigorous lecture quiz.",
                "a" * 64,
                "now",
            )
        ),
        selected_notebook,
        SimpleNamespace(),
        publisher,
        connection,
    )
    return worker, repository, connection, progress


@pytest.mark.parametrize("kind", [GenerationKind.QUIZ, GenerationKind.OUTLINE])
@pytest.mark.parametrize("stage", [GenerationStage.NOTEBOOK_PROMPT, GenerationStage.PDF,
                                  GenerationStage.QUIZ_VALIDATE, GenerationStage.DOCS])
def test_legacy_claims_pause_without_inference_or_losing_saved_answer(tmp_path, kind, stage):
    job = _job(kind=kind, stage=stage, notebook_answer=QUIZ_JSON, attempts=99)
    publisher = Publisher(fail=True)
    worker, repository, connection, progress = _worker(tmp_path, job, publisher)
    stopped = []
    repository.fail = lambda job_id, error, paused=False: stopped.append((job_id, error, paused))
    worker._run = lambda _: pytest.fail("legacy generation must not start")
    assert worker.run_once()
    assert stopped[0][0] == job.id and stopped[0][2] is True
    assert "upload-only" in stopped[0][1]
    assert repository.current.notebook_answer == QUIZ_JSON
    assert repository.current.stage is stage and not repository.advances
    assert not publisher.calls and not connection.invalidations
    assert progress[-1][2] is StepStatus.NEEDS_REVIEW
    assert not worker.run_once()
