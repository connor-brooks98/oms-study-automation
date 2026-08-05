from dataclasses import dataclass

from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.models import StudyAISettingModel


@dataclass(frozen=True, slots=True)
class StudyAISettings:
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
        accuracy_gate_enabled: bool | None = None,
    ) -> StudyAISettings:
        with self.database.session() as session:
            model = session.get(StudyAISettingModel, 1)
            if model is None:
                model = StudyAISettingModel(id=1)
                session.add(model)
            if accuracy_gate_enabled is not None:
                model.accuracy_gate_enabled = bool(accuracy_gate_enabled)
            session.flush()
            return self._domain(model)

    def _ensure_defaults(self) -> None:
        with self.database.session() as session:
            existing = session.scalar(
                select(StudyAISettingModel.id).where(StudyAISettingModel.id == 1)
            )
            if existing is None:
                session.add(StudyAISettingModel(id=1))

    @staticmethod
    def _domain(model: StudyAISettingModel) -> StudyAISettings:
        return StudyAISettings(model.accuracy_gate_enabled)
