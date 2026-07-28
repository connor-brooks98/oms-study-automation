from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from oms_hub.study_generation.domain import (
    NativeQuiz,
    PromptSnapshot,
    QuizChoice,
    QuizFeedback,
    QuizQuestion,
)

if TYPE_CHECKING:
    from oms_hub.config import Settings

_Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]
_Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]

_QUIZ_OUTPUT_CONTRACT = """

STUDY HUB OUTPUT CONTRACT
Return exactly one JSON object. Do not add prose before or after it. Do not use
Markdown except that a single ```json code fence around the object is allowed.
Use this exact shape:
{
  "title": "Lecture quiz title",
  "questions": [
    {
      "stem": "Question text",
      "choices": ["Choice A", "Choice B", "Choice C", "Choice D"],
      "correct_index": 0,
      "rationale": "Why the correct answer is correct and the others are not."
    }
  ]
}
`correct_index` is zero-based. Include 1 to 100 questions, 2 to 8 distinct
choices per question, and a non-empty expert rationale for every question.
""".strip()


class QuizContractError(ValueError):
    pass


class _QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stem: _Text
    choices: Annotated[list[_Text], Field(min_length=2, max_length=8)]
    correct_index: int = Field(ge=0)
    rationale: _Text

    @field_validator("choices")
    @classmethod
    def choices_are_distinct(cls, choices: list[str]) -> list[str]:
        normalized = {choice.casefold() for choice in choices}
        if len(normalized) != len(choices):
            raise ValueError("choices must be distinct")
        return choices

    @field_validator("correct_index")
    @classmethod
    def correct_index_is_in_range(
        cls,
        correct_index: int,
        info: object,
    ) -> int:
        data = getattr(info, "data", {})
        choices = data.get("choices", [])
        if choices and correct_index >= len(choices):
            raise ValueError("correct_index must identify an available choice")
        return correct_index


class _QuizInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Title
    questions: Annotated[list[_QuestionInput], Field(min_length=1, max_length=100)]


def quiz_prompt(prompt: PromptSnapshot) -> PromptSnapshot:
    return replace(
        prompt,
        content=f"{prompt.content.rstrip()}\n\n{_QUIZ_OUTPUT_CONTRACT}",
    )


def parse_native_quiz(raw: str) -> NativeQuiz:
    text = raw.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced is not None:
        text = fenced.group(1)
    try:
        decoded = json.loads(text)
        validated = _QuizInput.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise QuizContractError(
            f"NotebookLM quiz JSON is invalid: {error}"
        ) from error
    return NativeQuiz(
        validated.title,
        tuple(
            QuizQuestion(
                f"q{question_index}",
                question.stem,
                tuple(
                    QuizChoice(f"c{choice_index}", choice)
                    for choice_index, choice in enumerate(
                        question.choices,
                        start=1,
                    )
                ),
                f"c{question.correct_index + 1}",
                question.rationale,
            )
            for question_index, question in enumerate(
                validated.questions,
                start=1,
            )
        ),
    )


def public_quiz_content(quiz: NativeQuiz) -> dict[str, object]:
    return {
        "title": quiz.title,
        "questions": [
            {
                "id": question.id,
                "stem": question.stem,
                "choices": [
                    {"id": choice.id, "text": choice.text}
                    for choice in question.choices
                ],
            }
            for question in quiz.questions
        ],
    }


def grade_answer(
    quiz: NativeQuiz,
    question_id: str,
    choice_id: str,
) -> QuizFeedback:
    question = next(
        (item for item in quiz.questions if item.id == question_id),
        None,
    )
    if question is None:
        raise KeyError(question_id)
    if choice_id not in {choice.id for choice in question.choices}:
        raise KeyError(choice_id)
    return QuizFeedback(
        correct=choice_id == question.correct_choice_id,
        correct_choice_id=question.correct_choice_id,
        rationale=question.rationale,
    )


def serialize_native_quiz(quiz: NativeQuiz) -> str:
    return json.dumps(
        {
            "title": quiz.title,
            "questions": [
                {
                    "stem": question.stem,
                    "choices": [
                        choice.text
                        for choice in question.choices
                    ],
                    "correct_index": next(
                        index
                        for index, choice in enumerate(question.choices)
                        if choice.id == question.correct_choice_id
                    ),
                    "rationale": question.rationale,
                }
                for question in quiz.questions
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def quiz_origin(settings: Settings) -> str:
    if settings.public_hostname:
        return f"https://{settings.public_hostname}"
    return f"http://127.0.0.1:{settings.dashboard_port}"


def quiz_url(token: str, settings: Settings) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise QuizContractError("native quiz token is invalid")
    return f"{quiz_origin(settings)}/public/quizzes/{token}"


def validate_native_quiz_url(url: str, settings: Settings) -> str:
    normalized = url.strip()
    parsed = urlsplit(normalized)
    expected = urlsplit(quiz_origin(settings))
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/public/quizzes/[0-9a-f]{64}", parsed.path)
    ):
        raise QuizContractError("Study Hub returned an untrusted quiz link")
    return normalized
