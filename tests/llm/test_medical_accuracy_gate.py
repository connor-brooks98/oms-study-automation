# ruff: noqa: E501
from dataclasses import dataclass, field

import pytest

from oms_hub.db import Database
from oms_hub.llm.domain import (
    GeneratedText,
    LLMTask,
    ProviderConnection,
    ProviderName,
)
from oms_hub.llm.openrouter import AccuracyGateError, MedicalAccuracyGate
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.llm.service import LLMService
from oms_hub.study_generation.ai_settings import StudyAISettingsRepository
from oms_hub.study_generation.domain import (
    NativeQuiz,
    QuizChoice,
    QuizMatchingPrompt,
    QuizMatchingQuestion,
    QuizQuestion,
)


class MemorySecrets:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@dataclass
class RecordingProvider:
    name: ProviderName
    response_text: str = '{"approved": true, "issues": []}'
    calls: list[tuple[str, str]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)

    def clean(self, raw_text, prompt, *, api_key, model):
        raise NotImplementedError

    def test_connection(self, api_key, model):
        self.calls.append((model, api_key))
        return ProviderConnection(self.name, model, f"{self.name.value}-test")

    def generate_text(self, instruction, input_text, *, api_key, model, output_schema):
        self.calls.append((model, api_key))
        self.inputs.append(input_text)
        return GeneratedText(
            text=self.response_text,
            provider=self.name,
            model=model,
            request_id=f"{self.name.value}-request",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=0,
        )

    def list_models(self, api_key):
        raise NotImplementedError


def _question(question_id: str = "q1") -> QuizQuestion:
    return QuizQuestion(
        id=question_id,
        stem="What causes iron deficiency anemia?",
        choices=(
            QuizChoice(id="a", text="Iron deficiency"),
            QuizChoice(id="b", text="B12 deficiency"),
        ),
        correct_choice_id="a",
        rationale="Low ferritin confirms iron deficiency.",
    )


def _quiz(*question_ids: str) -> NativeQuiz:
    return NativeQuiz(
        title="Heme quiz",
        questions=tuple(_question(question_id) for question_id in question_ids),
    )


def _matching_quiz() -> NativeQuiz:
    return NativeQuiz(
        "Matching quiz",
        (
            QuizMatchingQuestion(
                "q1",
                "Match each description with its term.",
                (
                    QuizMatchingPrompt("p1", "A", "Description alpha", "c2"),
                    QuizMatchingPrompt("p2", "B", "Description beta", "c1"),
                ),
                (QuizChoice("c1", "Term one"), QuizChoice("c2", "Term two")),
                "Source-marked matches: A -> Term two; B -> Term one.",
            ),
        ),
    )


def test_matching_accuracy_request_contains_one_group_and_every_mapping(tmp_path) -> None:
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(LLMTask.ACCURACY_REVIEW, ProviderName.GEMINI, "gemini-review-model")
    gate.validate(_matching_quiz())
    inputs = providers[ProviderName.GEMINI].inputs
    assert len(inputs) == 1
    assert inputs[0].count("Question:\n") == 1
    assert "Matching prompts and proposed matches:" in inputs[0]
    assert "p1 (A): Description alpha -> c2: Term two" in inputs[0]
    assert "p2 (B): Description beta -> c1: Term one" in inputs[0]


def prepared_gate(tmp_path, *, gate_enabled: bool = True):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    llm_settings = LLMSettingsRepository(database, default_openai_model="gpt-5.2")
    study_ai_settings = StudyAISettingsRepository(database)
    study_ai_settings.save(accuracy_gate_enabled=gate_enabled)
    secrets = MemorySecrets(
        {
            "openai-api-key": "openai-secret",
            "gemini-api-key": "gemini-secret",
            "anthropic-api-key": "anthropic-secret",
            "openrouter-api-key": "openrouter-secret",
        }
    )
    providers = {name: RecordingProvider(name) for name in ProviderName}
    service = LLMService(llm_settings, secrets, providers)
    gate = MedicalAccuracyGate(study_ai_settings, service)
    return gate, llm_settings, secrets, providers


def test_validate_uses_the_accuracy_review_assignments_provider_and_model(tmp_path):
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )

    gate.validate(_quiz("q1", "q2"))

    assert providers[ProviderName.GEMINI].calls == [
        ("gemini-review-model", "gemini-secret"),
        ("gemini-review-model", "gemini-secret"),
    ]
    assert providers[ProviderName.OPENROUTER].calls == []


def test_validate_is_a_noop_when_the_gate_is_disabled(tmp_path):
    gate, llm_settings, _, providers = prepared_gate(tmp_path, gate_enabled=False)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )

    gate.validate(_quiz("q1"))

    assert providers[ProviderName.GEMINI].calls == []


def test_validate_pauses_with_accuracy_gate_error_when_credential_is_missing(tmp_path):
    gate, llm_settings, secrets, _ = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )
    secrets.delete("gemini-api-key")

    with pytest.raises(AccuracyGateError):
        gate.validate(_quiz("q1"))


def test_validate_raises_on_malformed_reviewer_output(tmp_path):
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )
    providers[ProviderName.GEMINI].response_text = "not json"

    with pytest.raises(AccuracyGateError):
        gate.validate(_quiz("q1"))


def test_validate_blocks_publication_when_reviewer_disapproves(tmp_path):
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.GEMINI,
        "gemini-review-model",
    )
    providers[
        ProviderName.GEMINI
    ].response_text = '{"approved": false, "issues": ["stem contradicts the rationale"]}'

    with pytest.raises(AccuracyGateError) as raised:
        gate.validate(_quiz("q1"))

    assert "stem contradicts the rationale" in str(raised.value)


def test_test_connection_uses_the_accuracy_review_assignment(tmp_path):
    gate, llm_settings, _, providers = prepared_gate(tmp_path)
    llm_settings.set_assignment(
        LLMTask.ACCURACY_REVIEW,
        ProviderName.ANTHROPIC,
        "claude-sonnet-5",
    )

    gate.test_connection()

    assert providers[ProviderName.ANTHROPIC].calls == [
        ("claude-sonnet-5", "anthropic-secret"),
    ]
