from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
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
    QuizImageRef,
    QuizQuestion,
)

if TYPE_CHECKING:
    from oms_hub.config import Settings
    from oms_hub.study_generation.repository import GenerationRepository

_Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]
_Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
_ImageKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    ),
]
_ImageSource = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
_ImageMetadata = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
_Dimension = Annotated[
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
      "rationale": "Why the correct answer is correct and the others are not.",
      "area": "Optional clinical area",
      "learning_objective": "Optional learning objective",
      "topic": "Optional topic",
      "image_ref": null
    }
  ]
}
`correct_index` is zero-based. Include 1 to 100 questions, 2 to 8 distinct
choices per question, and a non-empty expert rationale for every question. If
a question genuinely depends on a source diagram, image, graph, or table, add
an image_ref with the source title, page or slide locator, and a short
description; otherwise set image_ref to null. Do not invent image URLs.
""".strip()

_STUDIO_QUIZ_OUTPUT_CONTRACT = """

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
      "rationale": "Why the correct answer is correct and the others are not.",
      "area": "Optional clinical area",
      "learning_objective": "Optional learning objective",
      "topic": "Optional topic",
      "image_ref": null
    }
  ]
}
`correct_index` is zero-based. Include 1 to 100 questions, 2 to 8 distinct
choices per question, and a non-empty expert rationale for every question.

For every question, set `image_ref` to null when it can be answered without a
specific source image. If the question depends on a particular diagram,
photograph, scan, graph, table, or other source image, use this shape instead:
{
  "key": "image-1",
  "source_title": "Name of the source containing the image",
  "locator": "Where the user can find the image in that source",
  "description": "Short accessible description of the required image"
}
Use lowercase image keys containing only letters, numbers, and hyphens. When multiple
questions use the same source image, repeat the exact same key and metadata on every
question. Do not invent image contents, image URLs, or source locations.
Identify the source and locator so the user can upload the image.
""".strip()


class QuizContractError(ValueError):
    pass


class NativeQuizPublisher:
    def __init__(
        self,
        repository: GenerationRepository,
        settings: Settings,
    ):
        self.repository = repository
        self.settings = settings

    def publish(
        self,
        lecture_id: int,
        job_id: str,
        quiz: NativeQuiz,
    ) -> str:
        published = self.repository.publish_quiz(
            lecture_id,
            job_id,
            quiz,
        )
        return quiz_url(published.token, self.settings)


class _ImageRefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: _ImageKey
    source_title: _ImageSource
    locator: _ImageMetadata
    description: _ImageMetadata


class _QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stem: _Text
    choices: Annotated[list[_Text], Field(min_length=2, max_length=8)]
    correct_index: int = Field(ge=0)
    rationale: _Text
    area: _Dimension | None = None
    learning_objective: _Dimension | None = Field(
        default=None,
        validation_alias=AliasChoices("learning_objective", "objective"),
    )
    topic: _Dimension | None = None
    image_ref: _ImageRefInput | None = None

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


def quiz_prompt(
    prompt: PromptSnapshot,
    subject: str | None = None,
) -> PromptSnapshot:
    guidance = subject_quiz_guidance(subject)
    return replace(
        prompt,
        content=f"{prompt.content.rstrip()}\n\n{guidance}\n\n{_QUIZ_OUTPUT_CONTRACT}",
    )


def studio_quiz_prompt(prompt: str, subject: str | None = None) -> str:
    normalized = prompt.strip()
    if not normalized:
        raise ValueError("Studio prompt is empty")
    return f"{normalized}\n\n{subject_quiz_guidance(subject)}\n\n{_STUDIO_QUIZ_OUTPUT_CONTRACT}"


def is_omm_subject(subject: str | None) -> bool:
    normalized = " ".join((subject or "").casefold().replace("&", " ").split())
    return (
        "omm" in normalized.split()
        or any(
            marker in normalized
            for marker in (
                "osteopathic manipulative",
                "osteopathic medicine",
                "manipulative medicine",
            )
        )
    )


def subject_quiz_guidance(subject: str | None) -> str:
    if is_omm_subject(subject):
        return (
            "SUBJECT SCOPE: This is an OMM subject. Include OMM concepts when "
            "supported by the provided sources, including relevant anatomy and mechanics."
        )
    return (
        "SUBJECT SCOPE: This is not an OMM subject. Do not ask OMM questions "
        "or unrelated thoracic spine segmental-level questions; stay within "
        "the subject and the provided sources."
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
        raise QuizContractError(f"NotebookLM quiz JSON is invalid: {error}") from error
    images_by_key: dict[str, _ImageRefInput] = {}
    for question in validated.questions:
        image_ref = question.image_ref
        if image_ref is None:
            continue
        existing = images_by_key.setdefault(image_ref.key, image_ref)
        if existing != image_ref:
            raise QuizContractError(
                f"NotebookLM quiz JSON has conflicting metadata for image key {image_ref.key}"
            )
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
                (
                    QuizImageRef(
                        question.image_ref.key,
                        question.image_ref.source_title,
                        question.image_ref.locator,
                        question.image_ref.description,
                    )
                    if question.image_ref is not None
                    else None
                ),
                area=question.area,
                learning_objective=question.learning_objective,
                topic=question.topic,
            )
            for question_index, question in enumerate(
                validated.questions,
                start=1,
            )
        ),
    )


def image_requirements(quiz: NativeQuiz) -> tuple[QuizImageRef, ...]:
    by_key: dict[str, QuizImageRef] = {}
    for question in quiz.questions:
        if question.image_ref is not None:
            by_key.setdefault(question.image_ref.key, question.image_ref)
    return tuple(by_key.values())


def public_quiz_content(
    quiz: NativeQuiz,
    image_urls: Mapping[str, tuple[str, str, int | None, int | None]] | None = None,
) -> dict[str, object]:
    questions: list[dict[str, object]] = []
    for question in quiz.questions:
        item: dict[str, object] = {
            "id": question.id,
            "stem": question.stem,
            "choices": [
                {"id": choice.id, "text": choice.text}
                for choice in question.choices
            ],
        }
        if question.image_ref is not None and image_urls is not None:
            media = image_urls.get(question.image_ref.key)
            if media is not None:
                image_url, image_alt, image_width, image_height = media
                item["image_url"] = image_url
                item["image_alt"] = image_alt
                if image_width is not None and image_height is not None:
                    item["image_width"] = image_width
                    item["image_height"] = image_height
        for field_name in ("area", "learning_objective", "topic"):
            value = getattr(question, field_name)
            if value is not None:
                item[field_name] = value
        questions.append(item)
    return {"title": quiz.title, "questions": questions}


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
                    "choices": [choice.text for choice in question.choices],
                    "correct_index": next(
                        index
                        for index, choice in enumerate(question.choices)
                        if choice.id == question.correct_choice_id
                    ),
                    "rationale": question.rationale,
                    **(
                        {"area": question.area}
                        if question.area is not None
                        else {}
                    ),
                    **(
                        {"learning_objective": question.learning_objective}
                        if question.learning_objective is not None
                        else {}
                    ),
                    **(
                        {"topic": question.topic}
                        if question.topic is not None
                        else {}
                    ),
                    "image_ref": (
                        {
                            "key": question.image_ref.key,
                            "source_title": question.image_ref.source_title,
                            "locator": question.image_ref.locator,
                            "description": question.image_ref.description,
                        }
                        if question.image_ref is not None
                        else None
                    ),
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
