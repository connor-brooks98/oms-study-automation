import json
from pathlib import Path

import pytest

from oms_hub.config import Settings
from oms_hub.study_generation.domain import (
    PromptSnapshot,
    QuizMatchingQuestion,
)
from oms_hub.study_generation.native_quiz import (
    QuizContractError,
    grade_answer,
    grade_matching_answer,
    parse_native_quiz,
    parse_notebook_quiz,
    public_quiz_content,
    quiz_origin,
    quiz_prompt,
    quiz_url,
    serialize_native_quiz,
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


def _matching_payload(**overrides: object) -> dict[str, object]:
    question: dict[str, object] = {
        "kind": "matching",
        "stem": "Match each description with its term.",
        "prompts": [
            {"label": "A", "text": "Description alpha", "correct_index": 1},
            {"label": "B", "text": "Description beta", "correct_index": 1},
        ],
        "choices": ["Term one", "Term two"],
        "rationale": "Source-marked matches: A -> Term two; B -> Term two.",
        "image_ref": None,
    }
    question.update(overrides)
    return {"title": "Matching set", "questions": [question]}


def test_matching_quiz_round_trips_with_stable_group_prompt_and_choice_ids() -> None:
    quiz = parse_native_quiz(json.dumps(_matching_payload()))
    question = quiz.questions[0]

    assert isinstance(question, QuizMatchingQuestion)
    assert question.id == "q1"
    assert tuple(prompt.id for prompt in question.prompts) == ("p1", "p2")
    assert tuple(choice.id for choice in question.choices) == ("c1", "c2")
    assert tuple(prompt.correct_choice_id for prompt in question.prompts) == ("c2", "c2")
    assert serialize_native_quiz(quiz) == json.dumps(
        _matching_payload(), ensure_ascii=False, separators=(",", ":")
    )


def test_legacy_multiple_choice_serialization_does_not_gain_a_kind_field() -> None:
    quiz = parse_native_quiz(json.dumps(_payload()))
    serialized = json.loads(serialize_native_quiz(quiz))

    assert serialized["questions"] == [
        {
            **_payload()["questions"][0],
            "image_ref": None,
        }
    ]
    assert "kind" not in serialized["questions"][0]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"prompts": [{"label": "A", "text": "One", "correct_index": 0}]}, "prompts"),
        (
            {
                "prompts": [
                    {"label": "A", "text": "One", "correct_index": 0},
                    {"label": "a", "text": "Two", "correct_index": 1},
                ]
            },
            "labels must be distinct",
        ),
        (
            {
                "prompts": [
                    {"label": "A", "text": "One", "correct_index": 0},
                    {"label": "B", "text": "Two"},
                ]
            },
            "correct_index",
        ),
        (
            {
                "prompts": [
                    {"label": "A", "text": "One", "correct_index": 0},
                    {"label": "B", "text": "Two", "correct_index": 2},
                ]
            },
            "available choice",
        ),
    ],
)
def test_invalid_matching_native_contract_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(QuizContractError, match=message):
        parse_native_quiz(json.dumps(_matching_payload(**overrides)))


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


def test_matching_public_content_withholds_every_mapping() -> None:
    content = public_quiz_content(parse_native_quiz(json.dumps(_matching_payload())))

    assert content["questions"] == [{
        "kind": "matching",
        "id": "q1",
        "stem": "Match each description with its term.",
        "prompts": [
            {"id": "p1", "label": "A", "text": "Description alpha"},
            {"id": "p2", "label": "B", "text": "Description beta"},
        ],
        "choices": [
            {"id": "c1", "text": "Term one"},
            {"id": "c2", "text": "Term two"},
        ],
    }]
    assert "correct" not in repr(content)


def test_matching_grading_is_all_or_nothing_with_row_feedback_and_choice_reuse() -> None:
    quiz = parse_native_quiz(json.dumps(_matching_payload()))

    correct = grade_matching_answer(quiz, "q1", {"p1": "c2", "p2": "c2"})
    wrong = grade_matching_answer(quiz, "q1", {"p1": "c1", "p2": "c2"})

    assert correct.correct is True
    assert correct.row_results == {"p1": True, "p2": True}
    assert wrong.correct is False
    assert wrong.correct_matches == {"p1": "c2", "p2": "c2"}
    assert wrong.row_results == {"p1": False, "p2": True}


@pytest.mark.parametrize(
    "matches",
    [
        {"p1": "c2"},
        {"p1": "c2", "p2": "c2", "p3": "c1"},
        {"p1": "c2", "p9": "c2"},
        {"p1": "c9", "p2": "c2"},
    ],
)
def test_matching_grading_rejects_partial_extra_unknown_or_invalid_maps(
    matches: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="matching answer"):
        grade_matching_answer(
            parse_native_quiz(json.dumps(_matching_payload())), "q1", matches
        )


def test_notebook_parser_rejects_matching_but_native_parser_accepts_it() -> None:
    raw = json.dumps(_matching_payload())
    assert len(parse_native_quiz(raw).questions) == 1
    with pytest.raises(QuizContractError, match="multiple-choice"):
        parse_notebook_quiz(raw)


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
