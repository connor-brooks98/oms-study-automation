from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    CleanResult,
    DiagnosticSource,
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    LLMTask,
    ProviderCapabilities,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.openai import openai_output_schema, openai_style_model_ids
from oms_hub.llm.provider import (
    FIXED_TRANSCRIPT_CONSTRAINTS,
    LLMProvider,
    estimated_cost,
    get_provider_json,
    invalid_response,
    post_provider_json,
    prompt_with_cacheable_prefix,
    require_supported_generation_options,
    response_object,
    safe_request_id,
    token_count,
    transcript_input,
)
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.domain import (
    NativeQuiz,
    QuizMatchingQuestion,
    QuizQuestionValue,
)
from oms_hub.transcripts.prompt import ApprovedPrompt

if TYPE_CHECKING:
    from oms_hub.llm.service import LLMService

OPENROUTER_API_KEY_SECRET = "openrouter-api-key"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_OPENROUTER_ERROR_SOURCES = {
    "authentication": DiagnosticSource.AUTHENTICATION,
    "permission_denied": DiagnosticSource.AUTHENTICATION,
    "model_not_found": DiagnosticSource.MODEL,
    "invalid_request": DiagnosticSource.REQUEST,
    "invalid_prompt": DiagnosticSource.REQUEST,
    "context_length_exceeded": DiagnosticSource.REQUEST,
    "max_tokens_exceeded": DiagnosticSource.REQUEST,
    "token_limit_exceeded": DiagnosticSource.REQUEST,
    "string_too_long": DiagnosticSource.REQUEST,
    "rate_limit_exceeded": DiagnosticSource.QUOTA,
    "insufficient_credits": DiagnosticSource.QUOTA,
}

_OPENROUTER_ERROR_MESSAGES = {
    DiagnosticSource.AUTHENTICATION: "Openrouter rejected the credential",
    DiagnosticSource.MODEL: "Openrouter rejected the selected model",
    DiagnosticSource.REQUEST: "Openrouter rejected the request",
    DiagnosticSource.QUOTA: "Openrouter reported a quota or rate limit",
    DiagnosticSource.SERVICE: "Openrouter could not complete the generation",
}


class OpenRouterProvider:
    """OpenRouter adapter mirroring the OpenAI-format request/response shape."""

    name = ProviderName.OPENROUTER
    capabilities = ProviderCapabilities()
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
            reasoning_effort="none",
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
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText:
        response = self._request(
            api_key,
            model,
            instruction,
            input_text,
            output_schema=output_schema,
            max_tokens=options.max_tokens,
            options=options,
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

    def capabilities_for_model(self, model: str) -> ProviderCapabilities:
        return self.capabilities

    def _request(
        self,
        api_key: str,
        model: str,
        instruction: str,
        content: str,
        *,
        output_schema: dict[str, object] | None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> httpx.Response:
        require_supported_generation_options(self.name, self.capabilities, options)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": prompt_with_cacheable_prefix(content, options),
                },
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}
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
        self._raise_for_embedded_error(payload, response)
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
            finish_reasons = {
                choice.get("finish_reason")
                for choice in choices
                if isinstance(choice, dict)
            }
            if "length" in finish_reasons:
                raise LLMRequestError(
                    "Openrouter exhausted the output token limit",
                    source=DiagnosticSource.REQUEST,
                    http_status=response.status_code,
                    provider_request_id=self._request_id(payload, response),
                )
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
        request_id = self._request_id(payload, response) or ""
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

    def _raise_for_embedded_error(
        self,
        payload: dict[str, Any],
        response: httpx.Response,
    ) -> None:
        error = payload.get("error")
        if not isinstance(error, dict):
            choices = payload.get("choices")
            if isinstance(choices, list):
                error = next(
                    (
                        choice.get("error")
                        for choice in choices
                        if isinstance(choice, dict)
                        and isinstance(choice.get("error"), dict)
                    ),
                    None,
                )
        if not isinstance(error, dict):
            return

        metadata = error.get("metadata")
        error_type = error.get("error_type") or error.get("type")
        if isinstance(metadata, dict):
            error_type = metadata.get("error_type", error_type)
        normalized_type = error_type if isinstance(error_type, str) else ""
        source = _OPENROUTER_ERROR_SOURCES.get(
            normalized_type.lower(),
            DiagnosticSource.SERVICE,
        )
        raise LLMRequestError(
            _OPENROUTER_ERROR_MESSAGES[source],
            source=source,
            http_status=response.status_code,
            provider_request_id=self._request_id(payload, response),
        )

    @staticmethod
    def _request_id(
        payload: dict[str, Any],
        response: httpx.Response,
    ) -> str | None:
        request_id = payload.get("id")
        if isinstance(request_id, str) and request_id:
            return request_id[:200]
        return safe_request_id(response)


class AccuracyGateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AccuracyAssessment:
    approved: bool
    issues: tuple[str, ...]


_ACCURACY_REVIEW_INSTRUCTION = (
    "You are a cautious medical education fact checker. "
    "Assess only factual medical accuracy and whether the "
    "answer and rationale support one unambiguous best choice. "
    "Return JSON only: {\"approved\": true|false, "
    "\"issues\": [\"short issue\"]}. Do not rewrite the question."
)

_ACCURACY_REVIEW_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "issues"],
    "additionalProperties": False,
}


class MedicalAccuracyGate:
    """Fail-closed medical review that is opt-in and isolated from Anki.

    Resolves its provider, model, and credential from the ``accuracy_review``
    task assignment on every call, so the reviewing provider can be changed
    from settings without redeploying.
    """

    def __init__(
        self,
        settings: StudyAISettingsRepository,
        service: LLMService,
    ) -> None:
        self.settings = settings
        self.service = service

    def validate(self, quiz: NativeQuiz) -> None:
        configuration = self.settings.get()
        if not configuration.accuracy_gate_enabled:
            return
        provider, model, api_key = self._resolve()
        failures: list[str] = []
        for question in quiz.questions:
            assessment = self._assess(question, provider, model, api_key)
            if not assessment.approved:
                details = "; ".join(assessment.issues) or "reviewer did not approve the question"
                failures.append(f"{question.id}: {details}")
        if failures:
            raise AccuracyGateError(
                "Medical accuracy review blocked publication: " + " | ".join(failures[:8])
            )

    def assess(self, question: QuizQuestionValue) -> AccuracyAssessment:
        provider, model, api_key = self._resolve()
        return self._assess(question, provider, model, api_key)

    def test_connection(self) -> None:
        provider, model, api_key = self._resolve()
        try:
            provider.test_connection(api_key, model)
        except LLMRequestError as error:
            raise AccuracyGateError(
                "Medical accuracy review connection test failed"
            ) from error

    def _resolve(self) -> tuple[LLMProvider, str, str]:
        try:
            return self.service.for_task(LLMTask.ACCURACY_REVIEW)
        except LLMRequestError as error:
            raise AccuracyGateError(
                "Medical accuracy review is enabled but its provider "
                "credential is not configured"
            ) from error

    def _assess(
        self,
        question: QuizQuestionValue,
        provider: LLMProvider,
        model: str,
        api_key: str,
    ) -> AccuracyAssessment:
        try:
            generated = provider.generate_text(
                _ACCURACY_REVIEW_INSTRUCTION,
                _question_text(question),
                api_key=api_key,
                model=model,
                output_schema=_ACCURACY_REVIEW_OUTPUT_SCHEMA,
            )
        except LLMRequestError as error:
            raise AccuracyGateError("Medical accuracy review failed") from error
        try:
            result = _parse_json_object(generated.text)
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
            raise AccuracyGateError(
                "Medical accuracy review returned an invalid assessment"
            ) from error
        return AccuracyAssessment(approved, normalized_issues)


def _question_text(question: QuizQuestionValue) -> str:
    if isinstance(question, QuizMatchingQuestion):
        choice_by_id = {choice.id: choice.text for choice in question.choices}
        matches = "\n".join(
            f"{prompt.id} ({prompt.label}): {prompt.text} -> "
            f"{prompt.correct_choice_id}: {choice_by_id[prompt.correct_choice_id]}"
            for prompt in question.prompts
        )
        choices = "\n".join(f"{choice.id}. {choice.text}" for choice in question.choices)
        return (
            f"Question:\n{question.stem}\n\nChoices:\n{choices}\n\n"
            f"Matching prompts and proposed matches:\n{matches}\n"
            f"Rationale: {question.rationale}"
        )
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
