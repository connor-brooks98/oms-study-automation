import json
from pathlib import Path

import pytest

from oms_hub.config import Settings
from oms_hub.study_generation.domain import PromptSnapshot
from oms_hub.study_generation.native_quiz import (
    QuizContractError,
    grade_answer,
    parse_native_quiz,
    public_quiz_content,
    quiz_origin,
    quiz_prompt,
    quiz_url,
    validate_native_quiz_url,
)


def _payload(**overrides):
    question = {
        "stem": "Which mechanism best explains the low reticulocyte count?",
        "choices": [
            "Viral lysis of erythroid precursor cells",
            "Immune-complex deposition",
            "Autoimmune destruction of mature erythrocytes",
            "Transformation of hematopoietic stem cells",
        ],
        "correct_index": 0,
        "rationale": "Parvovirus B19 temporarily suppresses erythropoiesis.",
    }
    question.update(overrides)
    return {"title": "Aplastic Crisis", "questions": [question]}


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(_payload()),
        f"```json\n{json.dumps(_payload())}\n```",
    ],
)
def test_notebook_json_is_validated_with_stable_public_ids(raw):
    quiz = parse_native_quiz(raw)

    assert quiz.title == "Aplastic Crisis"
    assert quiz.questions[0].id == "q1"
    assert [choice.id for choice in quiz.questions[0].choices] == [
        "c1",
        "c2",
        "c3",
        "c4",
    ]
    assert quiz.questions[0].correct_choice_id == "c1"


def test_quiz_prompt_preserves_obsidian_snapshot_and_appends_json_contract():
    original = PromptSnapshot(
        Path("Quiz Prompt.md"),
        "Create clinically useful questions.",
        "a" * 64,
        "2026-07-28T12:00:00Z",
    )

    enhanced = quiz_prompt(original)

    assert enhanced.path == original.path
    assert enhanced.sha256 == original.sha256
    assert enhanced.modified_at == original.modified_at
    assert enhanced.content.startswith(original.content)
    assert '"correct_index": 0' in enhanced.content
    assert "Return exactly one JSON object" in enhanced.content


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"choices": ["Same", " same "]}, "distinct"),
        ({"correct_index": 4}, "correct_index"),
        ({"rationale": "  "}, "rationale"),
        ({"stem": ""}, "stem"),
    ],
)
def test_invalid_notebook_quiz_is_rejected_before_publication(override, message):
    with pytest.raises(QuizContractError, match=message):
        parse_native_quiz(json.dumps(_payload(**override)))


def test_public_content_includes_image_dimensions_only_when_provided():
    payload = _payload(
        image_ref={
            "key": "img-1",
            "source_title": "Lecture slides",
            "locator": "Slide 4",
            "description": "Diagram of the nephron",
        },
    )
    quiz = parse_native_quiz(json.dumps(payload))

    with_dimensions = public_quiz_content(
        quiz,
        {"img-1": ("https://example.test/img-1.png", "Nephron diagram", 800, 600)},
    )
    assert with_dimensions["questions"][0]["image_url"] == (
        "https://example.test/img-1.png"
    )
    assert with_dimensions["questions"][0]["image_width"] == 800
    assert with_dimensions["questions"][0]["image_height"] == 600

    without_dimensions = public_quiz_content(
        quiz,
        {"img-1": ("https://example.test/img-1.png", "Nephron diagram", None, None)},
    )
    assert "image_width" not in without_dimensions["questions"][0]
    assert "image_height" not in without_dimensions["questions"][0]


def test_public_content_omits_answers_and_rationales():
    content = public_quiz_content(parse_native_quiz(json.dumps(_payload())))

    assert content == {
        "title": "Aplastic Crisis",
        "questions": [
            {
                "id": "q1",
                "stem": "Which mechanism best explains the low reticulocyte count?",
                "choices": [
                    {
                        "id": "c1",
                        "text": "Viral lysis of erythroid precursor cells",
                    },
                    {"id": "c2", "text": "Immune-complex deposition"},
                    {
                        "id": "c3",
                        "text": "Autoimmune destruction of mature erythrocytes",
                    },
                    {
                        "id": "c4",
                        "text": "Transformation of hematopoietic stem cells",
                    },
                ],
            }
        ],
    }
    assert "correct" not in repr(content)
    assert "rationale" not in repr(content)


def test_grading_returns_feedback_only_for_the_requested_question():
    quiz = parse_native_quiz(json.dumps(_payload()))

    incorrect = grade_answer(quiz, "q1", "c2")
    correct = grade_answer(quiz, "q1", "c1")

    assert incorrect.correct is False
    assert incorrect.correct_choice_id == "c1"
    assert incorrect.rationale == (
        "Parvovirus B19 temporarily suppresses erythropoiesis."
    )
    assert correct.correct is True


@pytest.mark.parametrize(
    ("question_id", "choice_id"),
    [("q2", "c1"), ("q1", "c9")],
)
def test_grading_rejects_unknown_question_or_choice(question_id, choice_id):
    quiz = parse_native_quiz(json.dumps(_payload()))

    with pytest.raises(KeyError):
        grade_answer(quiz, question_id, choice_id)


def test_public_quiz_url_uses_the_configured_exact_https_origin(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        public_hostname="study.example.com",
    )
    token = "a" * 64

    assert quiz_origin(settings) == "https://study.example.com"
    assert quiz_url(token, settings) == (
        f"https://study.example.com/public/quizzes/{token}"
    )
    assert validate_native_quiz_url(quiz_url(token, settings), settings) == (
        f"https://study.example.com/public/quizzes/{token}"
    )


def test_public_quiz_url_uses_local_dashboard_origin_without_public_host(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        dashboard_port=9123,
    )

    assert quiz_origin(settings) == "http://127.0.0.1:9123"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/public/quizzes/" + "a" * 64,
        "https://user@study.example.com/public/quizzes/" + "a" * 64,
        "https://study.example.com/public/quizzes/not-a-token",
        "https://study.example.com/public/quizzes/" + "a" * 64 + "?answer=1",
        "https://study.example.com/public/quizzes/" + "a" * 64 + "#key",
    ],
)
def test_public_quiz_url_rejects_untrusted_links(tmp_path, url):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        public_hostname="study.example.com",
    )

    with pytest.raises(QuizContractError, match="untrusted"):
        validate_native_quiz_url(url, settings)
