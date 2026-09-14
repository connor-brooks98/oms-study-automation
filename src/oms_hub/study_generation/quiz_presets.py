"""Owner-scoped, local presets for the existing editable quiz instructions."""

import hashlib
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select, text

from oms_hub.db import Database
from oms_hub.models import QuizInstructionPresetModel

MAX_PRESETS = 30
MAX_NAME = 80
MAX_INSTRUCTIONS = 4000


class PresetConflict(ValueError):
    pass


@dataclass(frozen=True)
class QuizInstructionPreset:
    id: str
    name: str
    instructions: str
    updated_at: str


def validate_owner(owner: object) -> str:
    if not isinstance(owner, str) or not 1 <= len(owner) <= 320 or owner != owner.strip():
        raise ValueError("Private Study Hub access is required.")
    return owner  # Namespace identity is exact; never accept a client-selected owner.


def _content(name: str, instructions: str) -> tuple[str, str, str]:
    if not isinstance(name, str) or not isinstance(instructions, str):
        raise ValueError("Preset name and instructions must be text.")
    name = " ".join(name.split())
    if not 1 <= len(name) <= MAX_NAME or not name.isprintable():
        raise ValueError("Use a preset name of 1–80 printable characters.")
    if not instructions.strip() or len(instructions) > MAX_INSTRUCTIONS or "\x00" in instructions:
        raise ValueError("Use nonblank instructions of up to 4,000 characters.")
    key = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()
    return name, instructions.strip(), key


class QuizPresetRepository:
    def __init__(self, database: Database):
        self.database = database

    def list(self, owner: str) -> tuple[QuizInstructionPreset, ...]:
        owner = validate_owner(owner)
        with self.database.session() as session:
            rows = session.scalars(
                select(QuizInstructionPresetModel)
                .where(QuizInstructionPresetModel.owner_id == owner)
                .order_by(QuizInstructionPresetModel.name)
                .limit(MAX_PRESETS)
            ).all()
            return tuple(self._preset(row) for row in rows)

    def save(
        self, owner: str, name: str, instructions: str, *, preset_id: str | None = None
    ) -> QuizInstructionPreset:
        owner = validate_owner(owner)
        name, instructions, key = _content(name, instructions)
        with self.database.session() as session:
            # Serialize same-name upserts and the per-owner cap across local workers.
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            named = session.scalar(
                select(QuizInstructionPresetModel).where(
                    QuizInstructionPresetModel.owner_id == owner,
                    QuizInstructionPresetModel.name_key == key,
                )
            )
            if preset_id is not None:
                row = session.get(QuizInstructionPresetModel, preset_id)
                if row is None or row.owner_id != owner:
                    raise KeyError("Preset was not found.")
                if named is not None and named.id != row.id:
                    raise PresetConflict("Another preset already uses that name.")
            else:
                row = named
            if row is None:
                count = (
                    session.scalar(
                        select(func.count())
                        .select_from(QuizInstructionPresetModel)
                        .where(QuizInstructionPresetModel.owner_id == owner)
                    )
                    or 0
                )
                if count >= MAX_PRESETS:
                    raise PresetConflict(
                        "You can save up to 30 presets. Update or delete one first."
                    )
                row = QuizInstructionPresetModel(id=str(uuid4()), owner_id=owner)
                session.add(row)
            row.name, row.instructions, row.name_key = name, instructions, key
            session.flush()
            return self._preset(row)

    def delete(self, owner: str, preset_id: str) -> None:
        owner = validate_owner(owner)
        with self.database.session() as session:
            row = session.get(QuizInstructionPresetModel, preset_id)
            if row is None or row.owner_id != owner:
                raise KeyError("Preset was not found.")
            session.delete(row)

    @staticmethod
    def _preset(row: QuizInstructionPresetModel) -> QuizInstructionPreset:
        return QuizInstructionPreset(row.id, row.name, row.instructions, row.updated_at)
