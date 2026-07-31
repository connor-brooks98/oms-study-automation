import json
from pathlib import Path

import pytest

from oms_hub.config import Settings
from oms_hub.study_generation.domain import PromptSnapshot, QuizImageRef
from oms_hub.study_generation.native_quiz import (
    QuizContractError,
    grade_answer,
    image_requirements,
    parse_native_quiz,
    public_quiz_content,
    quiz_origin,
    quiz_prompt,
    quiz_url,
    serialize_native_quiz,
    studio_quiz_prompt,
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
    assert "image_ref" not in enhanced.content


def test_studio_prompt_requests_image_locations_without_changing_user_prompt():
    enhanced = studio_quiz_prompt("Preserve every question verbatim.")

    assert enhanced.startswith("Preserve every question verbatim.")
    assert '"image_ref": null' in enhanced
    assert "repeat the exact same key and metadata" in enhanced
    assert "Do not invent image contents" in enhanced


def test_shared_image_reference_parses_and_groups_once_in_question_order():
    image_ref = {
        "key": "image-1",
        "source_title": "Dr. Wang's website",
        "locator": "Image immediately before question 4",
        "description": "Reference image used for questions 4-7",
    }
    payload = _payload(image_ref=image_ref)
    payload["questions"].append(
        {
            **payload["questions"][0],
            "stem": "Which additional finding is visible?",
        }
    )

    quiz = parse_native_quiz(json.dumps(payload))

    expected = QuizImageRef(
        "image-1",
        "Dr. Wang's website",
        "Image immediately before question 4",
        "Reference image used for questions 4-7",
    )
    assert quiz.questions[0].image_ref == expected
    assert quiz.questions[1].image_ref == expected
    assert image_requirements(quiz) == (expected,)


def test_legacy_question_without_image_reference_remains_valid():
    quiz = parse_native_quiz(json.dumps(_payload()))

    assert quiz.questions[0].image_ref is None


def test_image_reference_survives_native_quiz_serialization():
    payload = _payload(
        image_ref={
            "key": "slide-12",
            "source_title": "Lecture slides",
            "locator": "Slide 12, upper-right panel",
            "description": "Chest radiograph",
        }
    )

    restored = parse_native_quiz(serialize_native_quiz(parse_native_quiz(json.dumps(payload))))

    assert restored.questions[0].image_ref == QuizImageRef(
        "slide-12",
        "Lecture slides",
        "Slide 12, upper-right panel",
        "Chest radiograph",
    )


def test_conflicting_metadata_for_a_shared_image_key_is_rejected():
    payload = _payload(
        image_ref={
            "key": "image-1",
            "source_title": "Professor website",
            "locator": "Before question 4",
            "description": "Gross pathology",
        }
    )
    payload["questions"].append(
        {
            **payload["questions"][0],
            "stem": "Second question",
            "image_ref": {
                "key": "image-1",
                "source_title": "Professor website",
                "locator": "Before question 8",
                "description": "Gross pathology",
            },
        }
    )

    with pytest.raises(QuizContractError, match="conflicting metadata"):
        parse_native_quiz(json.dumps(payload))


@pytest.mark.parametrize(
    "image_ref",
    [
        {
            "key": "Image 1",
            "source_title": "Slides",
            "locator": "Slide 1",
            "description": "Figure",
        },
        {
            "key": "image-1",
            "source_title": "",
            "locator": "Slide 1",
            "description": "Figure",
        },
        {
            "key": "image-1",
            "source_title": "Slides",
            "locator": "Slide 1",
            "description": "Figure",
            "url": "https://example.com/figure.png",
        },
    ],
)
def test_malformed_image_reference_is_rejected(image_ref):
    with pytest.raises(QuizContractError, match="image_ref"):
        parse_native_quiz(json.dumps(_payload(image_ref=image_ref)))


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
