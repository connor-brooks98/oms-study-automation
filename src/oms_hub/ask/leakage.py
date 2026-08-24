"""Deterministic, token-boundary-safe protection for pre-submit answers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from oms_hub.ask.models import GroundedAnswer


@dataclass(frozen=True, slots=True)
class LeakResult:
    """Non-sensitive outcome of a protected-answer comparison."""

    leaked: bool
    reason: str | None = None


_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_PUNCTUATED_INITIALS = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z])(?:[^\w\s]+([A-Za-z]))+(?![A-Za-z0-9])"
)
_OPTION_PREFIXES = {"answer", "choice", "option", "selection"}
_SAFE_REFUSAL = (
    "Submit the question first. I can still explain the underlying concept or point you to "
    "the relevant source."
)
_SAFE_REASON = "pre_submit_answer_protection"


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )


def _tokens(value: str) -> tuple[str, ...]:
    normalized = _normalized(value)
    return tuple(_WORD.findall(normalized))


def _punctuation_compacted_tokens(value: str) -> tuple[str, ...]:
    normalized = _normalized(value)

    def compact(match: re.Match[str]) -> str:
        return "".join(
            character
            for character in match.group(0)
            if character.isascii() and character.isalpha()
        )

    return tuple(_WORD.findall(_PUNCTUATED_INITIALS.sub(compact, normalized)))


def _strip_option_prefixes(tokens: tuple[str, ...]) -> tuple[str, ...]:
    stripped: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token in _OPTION_PREFIXES
            and index + 1 < len(tokens)
            and (
                tokens[index + 1].isdecimal()
                or (
                    len(tokens[index + 1]) == 1
                    and tokens[index + 1].isascii()
                    and tokens[index + 1].isalpha()
                )
            )
        ):
            index += 1
            continue
        stripped.append(token)
        index += 1
    return tuple(stripped)


def _variants(value: str) -> frozenset[tuple[str, ...]]:
    tokens = _tokens(value)
    if not tokens:
        return frozenset()
    stripped = _strip_option_prefixes(tokens)
    compacted = _punctuation_compacted_tokens(value)
    compacted_stripped = _strip_option_prefixes(compacted)
    return frozenset({tokens, stripped, compacted, compacted_stripped})


def _contains(tokens: tuple[str, ...], protected: tuple[str, ...]) -> bool:
    width = len(protected)
    return any(
        tokens[index : index + width] == protected
        for index in range(len(tokens) - width + 1)
    )


def detect_answer_leak(text: str, protected_answers: Sequence[str]) -> LeakResult:
    """Return whether text contains any supplied protected answer variant.

    Comparison is case-insensitive and token-based. Empty values are ignored, and no
    protected value is retained in the returned metadata.
    """

    if not isinstance(text, str):
        return LeakResult(False, "no_match")
    text_variants = _variants(text)
    if not text_variants:
        return LeakResult(False, "no_match")
    protected_values: Sequence[str]
    if isinstance(protected_answers, str):
        protected_values = (protected_answers,)
    elif not isinstance(protected_answers, Sequence):
        return LeakResult(False, "no_match")
    else:
        protected_values = protected_answers
    for protected in protected_values:
        if not isinstance(protected, str):
            continue
        for text_tokens in text_variants:
            for protected_tokens in _variants(protected):
                if _contains(text_tokens, protected_tokens):
                    return LeakResult(True, "protected_answer_match")
    return LeakResult(False, "no_match")


def safe_pre_submit_refusal() -> GroundedAnswer:
    """Build the deterministic response used when a pre-submit answer is blocked."""

    return GroundedAnswer(
        answer_markdown=_SAFE_REFUSAL,
        insufficient_evidence=False,
        safe_response_reason=_SAFE_REASON,
    )
