from dataclasses import dataclass

from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.models import StudyAISettingModel


@dataclass(frozen=True, slots=True)
class StudyAISettings:
    openrouter_model: str
    accuracy_gate_enabled: bool


class StudyAISettingsRepository:
    def __init__(self, database: Database):
        self.database = database
        self._ensure_defaults()

    def get(self) -> StudyAISettings:
        with self.database.session() as session:
            model = session.get(StudyAISettingModel, 1)
            if model is None:
                raise RuntimeError("Study AI settings are unavailable")
            return self._domain(model)

    def save(
        self,
        *,
        openrouter_model: str | None = None,
        accuracy_gate_enabled: bool | None = None,
    ) -> StudyAISettings:
        with self.database.session() as session:
            model = session.get(StudyAISettingModel, 1)
            if model is None:
                model = StudyAISettingModel(id=1)
                session.add(model)
            if openrouter_model is not None:
                normalized = " ".join(openrouter_model.split())
                if not normalized:
                    raise ValueError("OpenRouter model cannot be empty")
                if len(normalized) > 200:
                    raise ValueError("OpenRouter model is too long")
                model.openrouter_model = normalized
            if accuracy_gate_enabled is not None:
                model.accuracy_gate_enabled = bool(accuracy_gate_enabled)
            session.flush()
            return self._domain(model)

    def _ensure_defaults(self) -> None:
        with self.database.session() as session:
            if session.scalar(select(StudyAISettingModel.id).where(StudyAISettingModel.id == 1)) is None:
                session.add(StudyAISettingModel(id=1))

    @staticmethod
    def _domain(model: StudyAISettingModel) -> StudyAISettings:
        return StudyAISettings(model.openrouter_model, model.accuracy_gate_enabled)
