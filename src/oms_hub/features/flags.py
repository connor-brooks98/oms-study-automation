from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class FeatureFlag(StrEnum):
    SOURCE_TRUST_V1 = "source_trust_v1"
    GEMINI_FILE_SEARCH_V1 = "gemini_file_search_v1"
    ASK_STUDYHUB_V1 = "ask_studyhub_v1"
    ASK_QUIZ_CONTEXT_V1 = "ask_quiz_context_v1"
    BOARD_QUESTION_V1 = "board_question_v1"
    ADAPTIVE_PRACTICE_V1 = "adaptive_practice_v1"
    PRACTICE_MODES_V1 = "practice_modes_v1"
    ERROR_NOTEBOOK_V1 = "error_notebook_v1"
    TIMED_BLOCKS_V1 = "timed_blocks_v1"
    ANKI_LEARNING_LOOP_V1 = "anki_learning_loop_v1"
    BOARD_RUNWAY_V1 = "board_runway_v1"
    JOURNAL_EVIDENCE_V1 = "journal_evidence_v1"
    LEGACY_NOTEBOOKLM_GENERATION = "legacy_notebooklm_generation"


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    values: Mapping[FeatureFlag, bool]

    @classmethod
    def from_mapping(cls, values: Mapping[str, bool]) -> FeatureFlags:
        unknown = sorted(set(values).difference(flag.value for flag in FeatureFlag))
        if unknown:
            raise ValueError(f"unknown feature flags: {unknown}")
        invalid = sorted(name for name, value in values.items() if not isinstance(value, bool))
        if invalid:
            raise ValueError(f"feature flags must be bool values: {invalid}")
        mapped = {flag: values.get(flag.value, False) for flag in FeatureFlag}
        return cls(values=MappingProxyType(mapped))

    def is_enabled(self, flag: FeatureFlag) -> bool:
        return self.values.get(flag, False)
