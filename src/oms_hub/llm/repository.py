from sqlalchemy import select
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.llm.domain import LLMTask, ProviderName, ProviderPreference, TaskAssignment
from oms_hub.models import LLMProviderSettingModel, LLMTaskAssignmentModel

DEFAULT_MODELS = {
    ProviderName.OPENAI: "gpt-5.2",
    ProviderName.GEMINI: "gemini-3.6-flash",
    ProviderName.ANTHROPIC: "claude-sonnet-5",
    ProviderName.OPENROUTER: "openai/gpt-4o-mini",
}


class LLMSettingsRepository:
    def __init__(
        self,
        database: Database,
        *,
        default_openai_model: str,
    ) -> None:
        self.database = database
        self.default_models = {
            **DEFAULT_MODELS,
            ProviderName.OPENAI: self._validated_model(default_openai_model),
        }
        self._ensure_defaults()

    def list(self) -> tuple[ProviderPreference, ...]:
        with self.database.session() as session:
            stored = {
                row.provider: row
                for row in session.scalars(
                    select(LLMProviderSettingModel)
                ).all()
            }
            return tuple(
                self._preference(stored[provider.value])
                for provider in ProviderName
            )

    def get(self, provider: ProviderName) -> ProviderPreference:
        with self.database.session() as session:
            stored = session.get(LLMProviderSettingModel, provider.value)
            if stored is None:
                raise KeyError(provider.value)
            return self._preference(stored)

    def set_model(
        self,
        provider: ProviderName,
        model: str,
    ) -> ProviderPreference:
        normalized = self._validated_model(model)
        with self.database.session() as session:
            stored = session.get(LLMProviderSettingModel, provider.value)
            if stored is None:
                raise KeyError(provider.value)
            stored.model = normalized
            session.flush()
            return self._preference(stored)

    def assignment(self, task: LLMTask) -> TaskAssignment:
        with self.database.session() as session:
            stored = session.get(LLMTaskAssignmentModel, task.value)
            if stored is None:
                provider = self._default_task_provider(task, session)
                stored = LLMTaskAssignmentModel(
                    task=task.value,
                    provider=provider.value,
                    model=self.default_models[provider],
                )
                session.add(stored)
                session.flush()
            return self._assignment(stored)

    def set_assignment(
        self,
        task: LLMTask,
        provider: ProviderName,
        model: str,
    ) -> TaskAssignment:
        normalized = self._validated_model(model)
        with self.database.session() as session:
            stored = session.get(LLMTaskAssignmentModel, task.value)
            if stored is None:
                stored = LLMTaskAssignmentModel(task=task.value)
                session.add(stored)
            stored.provider = provider.value
            stored.model = normalized
            session.flush()
            return self._assignment(stored)

    def record_test(
        self,
        provider: ProviderName,
        *,
        state: str,
        tested_at: str,
        diagnostic_source: str | None = None,
        diagnostic_message: str | None = None,
        http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> ProviderPreference:
        with self.database.session() as session:
            stored = session.get(LLMProviderSettingModel, provider.value)
            if stored is None:
                raise KeyError(provider.value)
            stored.last_test_state = state
            stored.last_tested_at = tested_at
            stored.diagnostic_source = diagnostic_source
            stored.diagnostic_message = diagnostic_message
            stored.http_status = http_status
            stored.provider_request_id = provider_request_id
            session.flush()
            return self._preference(stored)

    def clear_test(
        self,
        provider: ProviderName,
    ) -> ProviderPreference:
        with self.database.session() as session:
            stored = session.get(LLMProviderSettingModel, provider.value)
            if stored is None:
                raise KeyError(provider.value)
            stored.last_test_state = None
            stored.last_tested_at = None
            stored.diagnostic_source = None
            stored.diagnostic_message = None
            stored.http_status = None
            stored.provider_request_id = None
            session.flush()
            return self._preference(stored)

    def _ensure_defaults(self) -> None:
        with self.database.session() as session:
            existing = {
                row.provider: row
                for row in session.scalars(
                    select(LLMProviderSettingModel)
                ).all()
            }
            has_active = any(row.active for row in existing.values())
            for provider in ProviderName:
                if provider.value in existing:
                    continue
                session.add(
                    LLMProviderSettingModel(
                        provider=provider.value,
                        model=self.default_models[provider],
                        active=provider is ProviderName.OPENAI and not has_active,
                    )
                )

    @staticmethod
    def _default_task_provider(task: LLMTask, session: Session) -> ProviderName:
        if task in {
            LLMTask.QUIZ_EXTRACTION,
            LLMTask.QUIZ_ANSWER_GENERATION,
        }:
            return ProviderName.OPENAI
        stored = session.scalar(
            select(LLMProviderSettingModel).where(
                LLMProviderSettingModel.active.is_(True)
            )
        )
        if stored is None:
            return ProviderName.ANTHROPIC
        return ProviderName(stored.provider)

    @staticmethod
    def _assignment(stored: LLMTaskAssignmentModel) -> TaskAssignment:
        return TaskAssignment(
            task=LLMTask(stored.task),
            provider=ProviderName(stored.provider),
            model=stored.model,
        )

    @staticmethod
    def _validated_model(model: str) -> str:
        normalized = model.strip()
        if not normalized:
            raise ValueError("model cannot be empty")
        if len(normalized) > 200:
            raise ValueError("model is too long")
        return normalized

    @staticmethod
    def _preference(stored: LLMProviderSettingModel) -> ProviderPreference:
        return ProviderPreference(
            provider=ProviderName(stored.provider),
            model=stored.model,
            active=stored.active,
            last_test_state=stored.last_test_state,
            last_tested_at=stored.last_tested_at,
            diagnostic_source=stored.diagnostic_source,
            diagnostic_message=stored.diagnostic_message,
            http_status=stored.http_status,
            provider_request_id=stored.provider_request_id,
        )
