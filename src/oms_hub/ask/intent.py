"""Deterministic pre-submit Ask intent classification."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum


class AskIntent(StrEnum):
    """The supported pre-submit Ask intents."""

    CONCEPT_HINT = "concept_hint"
    DEFINITION = "definition"
    MECHANISM = "mechanism"
    SOURCE_EXCERPT = "source_excerpt"
    COMPARE_CONCEPTS = "compare_concepts"
    REQUEST_ANSWER = "request_answer"
    REQUEST_OPTION_ELIMINATION = "request_option_elimination"
    OTHER = "other"


_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CONTRACTION = re.compile(r"\bwhat['’]s\b", re.IGNORECASE)


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    normalized = _CONTRACTION.sub("what is", normalized)
    return tuple(_WORD.findall(normalized))


def _contains(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(tokens[index : index + width] == phrase for index in range(len(tokens) - width + 1))


def _contains_any(tokens: tuple[str, ...], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(_contains(tokens, phrase) for phrase in phrases)


_OPTION_WORDS = {"answer", "choice", "choices", "option", "options"}
_GENERIC_STRATEGY_WORDS = {
    "distractor",
    "distractors",
    "strategy",
}
_GENERIC_TEST_WORDS = {"exam", "exams", "general", "test", "tests"}
_GENERIC_ELIMINATION_WORDS = {
    "choices",
    "choice",
    "eliminate",
    "elimination",
    "options",
    "option",
    "rule",
    "wrong",
}
_QUESTION_SCOPE_WORDS = {
    "case",
    "patient",
    "question",
    "scenario",
    "specific",
    "stem",
    "this",
}
_ELIMINATION_WORDS = {
    "cross",
    "discard",
    "eliminate",
    "exclude",
    "eliminated",
    "narrow",
    "remove",
    "rule",
    "ruled",
    "wrong",
}
_ANSWER_SUFFIXES_THAT_ARE_NOT_REVEALS = {
    "format",
    "length",
    "number",
    "sheet",
    "style",
    "type",
}
_DIRECT_PHRASES = (
    ("what", "is", "the", "answer"),
    ("what", "is", "answer"),
    ("what", "is", "your", "answer"),
    ("what", "is", "the", "correct", "answer"),
    ("what", "is", "the", "correct", "option"),
    ("what", "is", "correct", "option"),
    ("what", "is", "the", "correct", "choice"),
    ("which", "answer", "is", "correct"),
    ("which", "answer", "is", "right"),
    ("which", "choice", "is", "correct"),
    ("which", "choice", "is", "right"),
    ("which", "option", "is", "correct"),
    ("which", "option", "is", "right"),
    ("which", "one", "is", "correct"),
    ("which", "one", "is", "right"),
    ("which", "option", "should", "i", "choose"),
    ("which", "choice", "should", "i", "choose"),
    ("which", "one", "should", "i", "choose"),
    ("what", "option", "should", "i", "choose"),
    ("what", "choice", "should", "i", "choose"),
    ("give", "me", "the", "answer"),
    ("give", "me", "an", "answer"),
    ("tell", "me", "the", "answer"),
    ("provide", "the", "answer"),
    ("reveal", "the", "answer"),
    ("show", "me", "the", "answer"),
    ("show", "me", "the", "answer", "key"),
    ("give", "me", "the", "answer", "key"),
    ("what", "is", "the", "diagnosis"),
    ("what", "diagnosis", "is", "this"),
    ("which", "diagnosis", "is", "this"),
    ("which", "diagnosis", "this", "is"),
    ("what", "condition", "is", "this"),
    ("which", "condition", "is", "this"),
    ("tell", "me", "what", "diagnosis"),
    ("tell", "me", "which", "diagnosis"),
    ("what", "option", "do", "i", "pick"),
    ("what", "choice", "do", "i", "pick"),
    ("which", "option", "do", "i", "pick"),
    ("which", "choice", "do", "i", "pick"),
    ("which", "one", "do", "i", "pick"),
    ("tell", "me", "the", "diagnosis"),
    ("which", "diagnosis", "fits"),
    ("diagnose", "this"),
    ("is", "my", "answer", "correct"),
    ("is", "my", "answer", "right"),
    ("am", "i", "right"),
    ("confirm", "my", "answer"),
)
_OPTION_LABEL_PATTERNS = (
    ("is", "it"),
    ("could", "it", "be"),
    ("could", "the", "answer", "be"),
    ("is", "option"),
    ("is", "choice"),
)


def _requests_an_option_label(tokens: tuple[str, ...]) -> bool:
    for phrase in _OPTION_LABEL_PATTERNS:
        width = len(phrase)
        for index in range(len(tokens) - width + 1):
            if tokens[index : index + width] != phrase:
                continue
            label_index = index + width
            if label_index >= len(tokens):
                continue
            label = tokens[label_index]
            if _is_option_label(label):
                return True
    for index, label in enumerate(tokens):
        if not _is_option_label(label):
            continue
        following = tokens[index + 1 : index + 3]
        preceding = tokens[max(0, index - 2) : index]
        if following[:1] in (("correct",), ("right",)) and preceding[-1:] in (("is",), ("be",)):
            return True
        if following[:2] == ("the", "correct") and preceding[-1:] == ("is",):
            return True
        if following == ("be", "correct") and preceding[-1:] in (("could",), ("should",)):
            return True
        if following == ("is", "correct"):
            return True
    return False


def _is_option_label(value: str) -> bool:
    return len(value) == 1 and (
        (value.isascii() and value.isalpha()) or value.isdecimal()
    )


def _is_generic_strategy_query(tokens: tuple[str, ...]) -> bool:
    if _QUESTION_SCOPE_WORDS.intersection(tokens):
        return False
    has_strategy_term = bool(_GENERIC_STRATEGY_WORDS.intersection(tokens))
    has_generic_test_context = bool(_GENERIC_TEST_WORDS.intersection(tokens))
    has_elimination_term = bool(_GENERIC_ELIMINATION_WORDS.intersection(tokens))
    return has_strategy_term or (has_generic_test_context and has_elimination_term)


def _requests_option_elimination(tokens: tuple[str, ...]) -> bool:
    if not _OPTION_WORDS.intersection(tokens):
        return False
    return bool(_ELIMINATION_WORDS.intersection(tokens))


def _requests_direct_answer(tokens: tuple[str, ...]) -> bool:
    for phrase in _DIRECT_PHRASES:
        width = len(phrase)
        for index in range(len(tokens) - width + 1):
            if tokens[index : index + width] != phrase:
                continue
            if phrase[-1] == "answer":
                suffix = tokens[index + width :]
                if suffix and suffix[0] in _ANSWER_SUFFIXES_THAT_ARE_NOT_REVEALS:
                    continue
            return True
    if _contains(tokens, ("rule", "out")):
        return True
    return _requests_an_option_label(tokens)


_DEFINITION_PHRASES = (
    ("what", "is"),
    ("what", "does", "this", "mean"),
    ("what", "does", "that", "mean"),
    ("what", "is", "the", "meaning", "of"),
    ("definition", "of"),
    ("define",),
)
_MECHANISM_PHRASES = (
    ("how", "does"),
    ("how", "do"),
    ("how", "is"),
    ("why", "does"),
    ("why", "do"),
    ("why", "is"),
    ("mechanism", "of"),
    ("mechanism",),
    ("pathophysiology", "of"),
    ("pathophysiology",),
    ("what", "is", "the", "mechanism"),
    ("what", "is", "mechanism"),
    ("what", "causes"),
)
_SOURCE_PHRASES = (
    ("source",),
    ("excerpt",),
    ("citation",),
    ("cite",),
    ("slide",),
    ("where", "in", "the", "lecture"),
    ("point", "me", "to"),
)
_COMPARE_PHRASES = (
    ("compare",),
    ("contrast",),
    ("difference", "between"),
    ("differentiate",),
    ("distinguish",),
)
_HINT_PHRASES = (
    ("hint",),
    ("clue",),
    ("help", "me", "understand"),
    ("underlying", "concept"),
    ("what", "should", "i", "focus", "on"),
    ("walk", "me", "through"),
    ("explain",),
)


def classify_pre_submit_intent(query: str) -> AskIntent:
    """Classify a pre-submit query with local phrase and token rules only."""

    if not isinstance(query, str):
        return AskIntent.OTHER
    tokens = _tokens(query)
    if not tokens:
        return AskIntent.OTHER

    if _is_generic_strategy_query(tokens):
        return AskIntent.CONCEPT_HINT

    # Policy-sensitive requests are checked before benign educational intents.
    if _requests_option_elimination(tokens):
        return AskIntent.REQUEST_OPTION_ELIMINATION
    if _requests_direct_answer(tokens):
        return AskIntent.REQUEST_ANSWER
    if _contains_any(tokens, _MECHANISM_PHRASES):
        return AskIntent.MECHANISM
    if _contains_any(tokens, _SOURCE_PHRASES):
        return AskIntent.SOURCE_EXCERPT
    if _contains_any(tokens, _COMPARE_PHRASES) or "versus" in tokens or "vs" in tokens:
        return AskIntent.COMPARE_CONCEPTS
    if _contains_any(tokens, _DEFINITION_PHRASES):
        return AskIntent.DEFINITION
    if _contains_any(tokens, _HINT_PHRASES):
        return AskIntent.CONCEPT_HINT
    return AskIntent.OTHER
