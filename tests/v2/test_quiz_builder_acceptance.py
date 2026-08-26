"""End-to-end contracts for the direct practice-question import workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.llm.domain import GeneratedText, LLMTask, ProviderName
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.notebook import NotebookQuestionResult, NotebookQuestionStatus
from oms_hub.study_generation.practice_answers import PracticeAnswerResolver
from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedQuestion,
    SegmentCitation,
)
from oms_hub.study_generation.practice_domain import AnswerProvenance, ImportSourceRole
from oms_hub.study_generation.practice_extraction import ExtractionResult, SourceDocument
from oms_hub.study_generation.quiz_import_worker import QuizImportWorker


class FixtureParser:
    name = "fixture"
    version = "1"

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        del asset_root
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="text",
            parser_name=self.name,
            parser_version=self.version,
            segments=(
                ParsedSegment(
                    "block-1",
                    SegmentKind.PARAGRAPH,
                    "Question source text",
                    DocumentLocator("block 1"),
                ),
            ),
            assets=(),
            warnings=(),
        )


class FixtureExtractor:
    def __init__(self, *, supplied_answer: bool) -> None:
        self.supplied_answer = supplied_answer

    def extract(self, documents: tuple[SourceDocument, ...]) -> ExtractionResult:
        question_source = next(item for item in documents if item.role == "questions")
        answer_source = next(
            (item for item in documents if item.role == "answer_key"),
            question_source,
        )
        question = ExtractedQuestion(
            original_identifier="1",
            stem="Which answer is correct?",
            choices=("Yes", "No"),
            source_segments=(
                SegmentCitation(
                    source_id=question_source.document.source_id,
                    segment_key="block-1",
                ),
            ),
            confidence=0.9,
        )
        answers = (
            ExtractedAnswer(
                original_identifier="1",
                correct_index=0,
                rationale="The supplied answer key says Yes.",
                source_segments=(
                    SegmentCitation(
                        source_id=answer_source.document.source_id,
                        segment_key="block-1",
                    ),
                ),
            ),
        ) if self.supplied_answer else ()
        return ExtractionResult(
            questions=(question,),
            answers=answers,
            question_source_refs=((),),
            provider_metadata=(),
            diagnostics=(),
        )


class FailIfCalledNotebook:
    def attach_studio_source(self, *args: object, **kwargs: object) -> tuple[str, str]:
        del args, kwargs
        raise AssertionError("supplied answers must not call NotebookLM")

    def answer_studio_question(self, *args: object) -> NotebookQuestionResult:
        del args
        raise AssertionError("supplied answers must not call NotebookLM")


class NoSupportNotebook:
    def __init__(self) -> None:
        self.attach_calls = 0
        self.answer_calls = 0
        self.no_support_results = 0
        self.remote_ids: set[str] = set()

    def prepare_studio_source_add(
        self, *args: object, **kwargs: object
    ) -> tuple[str, frozenset[str]]:
        del args, kwargs
        return "notebook-1", frozenset(self.remote_ids)

    def add_studio_source_to_notebook(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        self.attach_calls += 1
        self.remote_ids.add("support-1")
        return "support-1"

    def list_studio_source_ids(self, *args: object, **kwargs: object) -> frozenset[str]:
        del args, kwargs
        return frozenset(self.remote_ids)

    def answer_studio_question(self, *args: object) -> NotebookQuestionResult:
        del args
        self.answer_calls += 1
        self.no_support_results += 1
        return NotebookQuestionResult(NotebookQuestionStatus.NO_SUPPORT, None, "No support.", ())


class FailIfCalledFallback:
    def generate_text_for_task(self, *args: object, **kwargs: object) -> GeneratedText:
        del args, kwargs
        raise AssertionError("supplied answers must not call the AI fallback")


class GeneratedFallback:
    def generate_text_for_task(
        self,
        task: LLMTask,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
    ) -> GeneratedText:
        del task, instruction, input_text, output_schema
        return GeneratedText(
            text=json.dumps(
                {
                    "correct_index": 1,
                    "rationale": "Generated only after NotebookLM reported no support.",
                    "evidence": ["No notebook support was available."],
                    "uncertainty_note": "A reviewer must verify this generated answer.",
                }
            ),
            provider=ProviderName.GEMINI,
            model="acceptance-answer-model",
            request_id="acceptance-answer-1",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        )


def acceptance_app(
    tmp_path: Path,
    *,
    notebook: FailIfCalledNotebook | NoSupportNotebook,
    fallback: FailIfCalledFallback | GeneratedFallback,
    supplied_answer: bool,
) -> object:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(LectureInput("Neuro", 1, 1, "Seizures", "", None))
    worker = QuizImportWorker(
        app.state.studio_repository,
        FixtureParser(),
        FixtureExtractor(supplied_answer=supplied_answer),
        PracticeAnswerResolver(notebook, fallback),
        notebook,
        tmp_path / "import-assets",
    )
    app.state.quiz_import_worker = worker
    app.state.studio_worker.import_worker = worker
    return app


def _csrf_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/studio")
    assert response.status_code == 200
    token = client.cookies.get("study_hub_csrf")
    assert token is not None
    return {"X-CSRF-Token": token}


def _add_text_source(client: TestClient, headers: dict[str, str], title: str) -> str:
    response = client.post(
        "/studio/import/sources/text",
        data={"subject": "Neuro", "exam_number": "1", "title": title, "text": title},
        headers=headers,
    )
    assert response.status_code == 202
    return str(response.json()["id"])


def _queue_import(
    client: TestClient,
    headers: dict[str, str],
    *,
    answer_mode: Literal["supplied", "generated"],
) -> str:
    questions = _add_text_source(client, headers, "Questions")
    sources: list[dict[str, object]] = [{"source_id": questions, "role": "questions"}]
    if answer_mode == "supplied":
        answers = _add_text_source(client, headers, "Answer key")
        sources.append({"source_id": answers, "role": "answer_key"})
    else:
        support = _add_text_source(client, headers, "Supporting reference")
        sources.append(
            {
                "source_id": support,
                "role": ImportSourceRole.SUPPORTING_REFERENCE.value,
                "attach_to_notebook": True,
            }
        )
    response = client.post(
        "/studio/import/runs",
        json={
            "subject": "Neuro",
            "exam_number": 1,
            "label": "Acceptance import",
            "destination_subject": "Neuro",
            "destination_exam_number": 1,
            "content_kind": "practice_questions",
            "sources": sources,
        },
        headers=headers,
    )
    assert response.status_code == 202
    return str(response.json()["id"])


def _drain_studio_worker(app: object) -> None:
    assert app.state.studio_worker.run_once() is True
    assert app.state.studio_worker.run_once() is False


def test_direct_import_with_supplied_answers_never_calls_notebook(tmp_path: Path) -> None:
    app = acceptance_app(
        tmp_path,
        notebook=FailIfCalledNotebook(),
        fallback=FailIfCalledFallback(),
        supplied_answer=True,
    )
    client = TestClient(app)
    run_id = _queue_import(client, _csrf_headers(client), answer_mode="supplied")

    _drain_studio_worker(app)

    review = app.state.practice_review.review(run_id)
    assert app.state.practice_review.blockers(run_id) == ()
    assert len(review) == 1
    assert review[0].draft.stem == "Which answer is correct?"
    assert review[0].answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE


def test_generated_answer_cannot_publish_before_question_verification(tmp_path: Path) -> None:
    notebook = NoSupportNotebook()
    app = acceptance_app(
        tmp_path,
        notebook=notebook,
        fallback=GeneratedFallback(),
        supplied_answer=False,
    )
    client = TestClient(app)
    headers = _csrf_headers(client)
    run_id = _queue_import(client, headers, answer_mode="generated")

    _drain_studio_worker(app)

    blocked = client.post(f"/studio/runs/{run_id}/publication", headers=headers)
    review = client.get(f"/studio/runs/{run_id}/review/data")
    question_id = review.json()["questions"][0]["id"]
    verified = client.post(
        f"/studio/runs/{run_id}/questions/{question_id}/verify-answer",
        headers=headers,
    )
    published = client.post(f"/studio/runs/{run_id}/publication", headers=headers)
    assert blocked.status_code == 409
    assert verified.status_code == 200
    assert published.status_code == 200
    assert notebook.attach_calls == 1
    assert notebook.answer_calls == 1
    assert notebook.no_support_results == 1
