from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from oms_hub.llm.domain import (
    CleanResult,
    GeneratedText,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.openai import openai_output_schema, openai_style_model_ids
from oms_hub.llm.provider import (
    FIXED_TRANSCRIPT_CONSTRAINTS,
    estimated_cost,
    get_provider_json,
    invalid_response,
    post_provider_json,
    response_object,
    safe_request_id,
    token_count,
    transcript_input,
)
from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.domain import NativeQuiz, QuizQuestion
from oms_hub.transcripts.prompt import ApprovedPrompt

OPENROUTER_API_KEY_SECRET = "openrouter-api-key"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """OpenRouter adapter mirroring the OpenAI-format request/response shape."""

    name = ProviderName.OPENROUTER
    base_url = OPENROUTER_BASE_URL
    chat_url = f"{OPENROUTER_BASE_URL}/chat/completions"
    models_url = f"{OPENROUTER_BASE_URL}/models"

    def __init__(
        self,
        *,
        input_usd_per_million: float = 0,
        output_usd_per_million: float = 0,
        http: httpx.Client | None = None,
    ) -> None:
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        self.http = http or httpx.Client(timeout=300.0)

    def clean(
        self,
        raw_text: str,
        prompt: ApprovedPrompt,
        *,
        api_key: str,
        model: str,
    ) -> CleanResult:
        response = self._request(
            api_key,
            model,
            FIXED_TRANSCRIPT_CONSTRAINTS,
            transcript_input(raw_text, prompt),
            output_schema=None,
        )
        return self._clean_result(response, model)

    def test_connection(
        self,
        api_key: str,
        model: str,
    ) -> ProviderConnection:
        response = self._request(
            api_key,
            model,
            "Return only the requested text.",
            "Reply with exactly OK.",
            output_schema=None,
            max_tokens=16,
        )
        result = self._clean_result(response, model)
        return ProviderConnection(self.name, result.model, result.request_id)

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        api_key: str,
        model: str,
        output_schema: dict[str, object],
    ) -> GeneratedText:
        response = self._request(
            api_key,
            model,
            instruction,
            input_text,
            output_schema=output_schema,
        )
        return self._generated_text(response, model)

    def list_models(self, api_key: str) -> tuple[str, ...]:
        response = get_provider_json(
            self.http,
            self.models_url,
            provider=self.name,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        payload = response_object(response, self.name)
        return openai_style_model_ids(payload, self.name, response)

    def _request(
        self,
        api_key: str,
        model: str,
        instruction: str,
        content: str,
        *,
        output_schema: dict[str, object] | None,
        max_tokens: int | None = None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": content},
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": openai_output_schema(output_schema),
                },
            }
        return post_provider_json(
            self.http,
            self.chat_url,
            provider=self.name,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
        )

    def _clean_result(
        self,
        response: httpx.Response,
        requested_model: str,
    ) -> CleanResult:
        generated = self._generated_text(response, requested_model)
        return CleanResult(
            text=generated.text,
            provider=generated.provider,
            model=generated.model,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
        )

    def _generated_text(
        self,
        response: httpx.Response,
        requested_model: str,
    ) -> GeneratedText:
        payload = response_object(response, self.name)
        choices = payload.get("choices")
        usage = payload.get("usage")
        if not isinstance(choices, list) or not isinstance(usage, dict):
            raise invalid_response(self.name, response)
        text_parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            value = message.get("content")
            if isinstance(value, str):
                text_parts.append(value)
        cleaned = "".join(text_parts).strip()
        if not cleaned:
            raise invalid_response(self.name, response)
        input_tokens = token_count(
            usage.get("prompt_tokens"),
            self.name,
            response,
        )
        output_tokens = token_count(
            usage.get("completion_tokens"),
            self.name,
            response,
        )
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            request_id = safe_request_id(response) or ""
        returned_model = payload.get("model", requested_model)
        if not isinstance(returned_model, str) or not returned_model:
            raise invalid_response(self.name, response)
        return GeneratedText(
            text=cleaned,
            provider=self.name,
            model=returned_model,
            request_id=request_id[:200],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=estimated_cost(
                input_tokens,
                output_tokens,
                self.input_usd_per_million,
                self.output_usd_per_million,
            ),
        )


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
