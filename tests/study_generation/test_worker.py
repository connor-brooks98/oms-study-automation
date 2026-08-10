import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.artifact_writes import ArtifactWriteClaimLost, ArtifactWriteContended
from oms_hub.domain import StepStatus, V2StepName
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


def test_worker_validates_and_publishes_notebook_quiz_natively(tmp_path):
    publisher = Publisher()
    worker, repository, connection, progress = _worker(
        tmp_path,
        _job(
            stage=GenerationStage.QUIZ_VALIDATE,
            notebook_answer=QUIZ_JSON,
        ),
        publisher,
    )

    assert worker.run_once()

    assert len(publisher.calls) == 1
    assert publisher.calls[0][:2] == (1, "job-1")
    assert publisher.calls[0][2].title == "Seizure Practice"
    assert repository.quiz == (1, "job-1", QUIZ_URL)
    assert connection.invalidations == []
    assert repository.current.state is GenerationState.COMPLETE
    assert progress[0][:3] == (
        1,
        V2StepName.QUIZ_PUBLISHED,
        StepStatus.RUNNING,
    )
    assert progress[-1][:3] == (
        1,
        V2StepName.QUIZ_PUBLISHED,
        StepStatus.COMPLETE,
    )


@pytest.mark.parametrize("error", [ArtifactWriteContended("held"), ArtifactWriteClaimLost("lost")])
def test_claim_failures_are_deferred_after_generation_retry_limit(tmp_path, error):
    publisher = Publisher()
    worker, repository, _, progress = _worker(
        tmp_path, _job(attempts=99), publisher
    )
    worker._run = lambda job: (_ for _ in ()).throw(error)
    assert worker.run_once() is True
    assert repository.current.state is not GenerationState.FAILED
    assert repository.retried[0] == "job-1"
    assert progress[-1][2] is StepStatus.QUEUED


def test_worker_appends_machine_contract_to_editable_obsidian_prompt(tmp_path):
    notebook = Notebook()
    publisher = Publisher()
    worker, repository, _, _ = _worker(
        tmp_path,
        _job(),
        publisher,
        notebook,
    )

    assert worker.run_once()

    assert notebook.prompt.path == Path("Quiz Prompt.md")
    assert notebook.prompt.sha256 == "a" * 64
    assert notebook.prompt.content.startswith(
        "Create a rigorous lecture quiz."
    )
    assert "Return exactly one JSON object" in notebook.prompt.content
    assert [stage for stage, _ in repository.advances] == [
        GenerationStage.QUIZ_VALIDATE,
        GenerationStage.PUBLISH,
        GenerationStage.CATALOG,
    ]


def test_worker_resume_at_docs_does_not_republish_quiz(tmp_path):
    worker, repository, connection, _ = _worker(
        tmp_path,
        _job(
            stage=GenerationStage.DOCS,
            notebook_answer=QUIZ_JSON,
            quiz_url=QUIZ_URL,
        ),
        Publisher(fail=True),
    )
    worker.prompts = SimpleNamespace(
        inspect=lambda kind: (_ for _ in ()).throw(
            AssertionError(f"legacy recovery must not reload {kind}")
        )
    )

    assert worker.run_once()

    assert connection.invalidations == []
    assert repository.current.state is GenerationState.COMPLETE


def test_worker_pauses_and_invalidates_expired_notebook_login(tmp_path):
    worker, repository, _, progress = _worker(
        tmp_path,
        _job(),
        Publisher(),
        ExpiredNotebook(),
    )
    failures = []
    repository.fail = lambda job_id, error, paused=False: failures.append(
        (job_id, error, paused)
    )
    connection = worker.notebook_connection

    assert worker.run_once()

    assert failures == [
        (
            "job-1",
            "NotebookLM login expired; reconnect Google in Settings.",
            True,
        )
    ]
    assert connection.invalidations == [
        "NotebookLM login expired; reconnect Google in Settings."
    ]
    assert progress[-1][:3] == (
        1,
        V2StepName.QUIZ_PUBLISHED,
        StepStatus.NEEDS_REVIEW,
    )
