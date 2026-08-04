from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.domain import NativeQuiz, QuizQuestion

OPENROUTER_API_KEY_SECRET = "openrouter-api-key"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class AccuracyGateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AccuracyAssessment:
    approved: bool
    issues: tuple[str, ...]


class MedicalAccuracyGate:
    """Fail-closed medical review that is opt-in and isolated from Anki."""

    def __init__(
        self,
        settings: StudyAISettingsRepository,
        secrets: SecretStore,
        *,
        endpoint: str = OPENROUTER_ENDPOINT,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.endpoint = endpoint
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0))
        )

    def validate(self, quiz: NativeQuiz) -> None:
        configuration = self.settings.get()
        if not configuration.accuracy_gate_enabled:
            return
        api_key = (self.secrets.get(OPENROUTER_API_KEY_SECRET) or "").strip()
        if not api_key:
            raise AccuracyGateError(
                "Medical accuracy review is enabled but an OpenRouter API key is not configured"
            )
        failures: list[str] = []
        for question in quiz.questions:
            assessment = self.assess(question, configuration.openrouter_model, api_key)
            if not assessment.approved:
                details = "; ".join(assessment.issues) or "reviewer did not approve the question"
                failures.append(f"{question.id}: {details}")
        if failures:
            raise AccuracyGateError(
                "Medical accuracy review blocked publication: " + " | ".join(failures[:8])
            )

    def assess(
        self,
        question: QuizQuestion,
        model: str,
        api_key: str | None = None,
    ) -> AccuracyAssessment:
        key = (api_key or self.secrets.get(OPENROUTER_API_KEY_SECRET) or "").strip()
        if not key:
            raise AccuracyGateError("OpenRouter API key is not configured")
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 500,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a cautious medical education fact checker. "
                        "Assess only factual medical accuracy and whether the "
                        "answer and rationale support one unambiguous best choice. "
                        "Return JSON only: {\"approved\": true|false, "
                        "\"issues\": [\"short issue\"]}. Do not rewrite the question."
                    ),
                },
                {"role": "user", "content": _question_text(question)},
            ],
        }
        try:
            with self.client_factory() as client:
                response = client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://study-hub.local",
                        "X-Title": "OMS Study Hub medical accuracy review",
                    },
                    json=payload,
                )
                response.raise_for_status()
                decoded = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AccuracyGateError("OpenRouter medical accuracy review failed") from error
        try:
            content = decoded["choices"][0]["message"]["content"]
            result = _parse_json_object(content)
            approved = result.get("approved")
            issues = result.get("issues", [])
            if not isinstance(approved, bool) or not isinstance(issues, list):
                raise TypeError("invalid accuracy response")
            normalized_issues = tuple(
                " ".join(str(issue).split())[:500]
                for issue in issues
                if str(issue).strip()
            )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise AccuracyGateError("OpenRouter returned an invalid accuracy assessment") from error
        return AccuracyAssessment(approved, normalized_issues)

    def test_connection(self) -> None:
        settings = self.settings.get()
        key = (self.secrets.get(OPENROUTER_API_KEY_SECRET) or "").strip()
        if not key:
            raise AccuracyGateError("OpenRouter API key is not configured")
        payload = {
            "model": settings.openrouter_model,
            "temperature": 0,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "Return the word READY."}],
        }
        try:
            with self.client_factory() as client:
                response = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise AccuracyGateError("OpenRouter connection test failed") from error


def _question_text(question: QuizQuestion) -> str:
    choices = "\n".join(f"{choice.id}. {choice.text}" for choice in question.choices)
    return (
        f"Question:\n{question.stem}\n\nChoices:\n{choices}\n\n"
        f"Proposed answer: {question.correct_choice_id}\n"
        f"Rationale: {question.rationale}"
    )


def _parse_json_object(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise TypeError("accuracy content is not text")
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        text = fenced.group(1)
    decoded = json.loads(text)
    if not isinstance(decoded, dict):
        raise TypeError("accuracy response is not an object")
    return decoded
