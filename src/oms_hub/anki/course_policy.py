"""Immutable, hash-bound course policy contracts for card_centric_v3."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256


class PolicyEmphasisColor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rgb: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{6}$")
    theme_ref: str | None = Field(default=None, min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)

    @field_validator("rgb", mode="before")
    @classmethod
    def _uppercase_rgb(cls, value: object) -> str | None:
        return None if value is None else str(value).strip().upper()

    @field_validator("theme_ref", "label", mode="before")
    @classmethod
    def _trim_text(cls, value: object) -> str | None:
        return None if value is None else str(value).strip()

    @model_validator(mode="after")
    def _reference_is_present(self) -> "PolicyEmphasisColor":
        if self.rgb is None and self.theme_ref is None:
            raise ValueError("emphasis color needs rgb or theme_ref")
        return self


class CourseCurationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(gt=0)
    course_id: str | None = Field(default=None, min_length=1, max_length=200)
    course_label: str | None = Field(default=None, min_length=1, max_length=300)
    professor_label: str = Field(min_length=1, max_length=300)
    scope_instruction: str = Field(min_length=1, max_length=20_000)
    emphasis_mode: Literal["colored_text", "transcript_emphasis", "outline_depth", "combined"]
    emphasis_colors: tuple[PolicyEmphasisColor, ...] = ()
    missing_emphasis_fallback: Literal["block", "require_confirmation", "transcript_outline"]
    tag_scope_mode: Literal["hard_filter", "prior_boost", "disabled"]
    classification_strictness: Literal["strict", "balanced", "permissive"]
    generation_style_profile: str = Field(min_length=1, max_length=200)
    ordinary_cost_limit_microusd: int = Field(ge=0)
    hard_stop_cost_limit_microusd: int = Field(ge=0)
    policy_sha256: str = ""

    @field_validator(
        "policy_id",
        "course_id",
        "course_label",
        "professor_label",
        "scope_instruction",
        "generation_style_profile",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"policy_sha256"})

    @model_validator(mode="after")
    def _validate_identity_and_hash(self) -> "CourseCurationPolicy":
        if self.course_id is None and self.course_label is None:
            raise ValueError("course_id or course_label is required")
        if self.hard_stop_cost_limit_microusd < self.ordinary_cost_limit_microusd:
            raise ValueError("hard stop cost limit cannot be below ordinary cost limit")
        keys = tuple(
            (color.rgb, color.theme_ref, color.label.casefold()) for color in self.emphasis_colors
        )
        if keys != tuple(sorted(keys, key=str)) or len(keys) != len(set(keys)):
            raise ValueError("emphasis colors must be unique and deterministically ordered")
        if self.emphasis_mode in {"colored_text", "combined"} and not self.emphasis_colors:
            raise ValueError("colored emphasis modes require at least one emphasis color")
        if self.emphasis_mode in {"transcript_emphasis", "outline_depth"} and self.emphasis_colors:
            raise ValueError("non-colored emphasis modes cannot carry unused emphasis colors")
        expected = canonical_payload_sha256(self.canonical_payload())
        if self.policy_sha256 not in {"", expected}:
            raise ValueError("policy hash does not match its canonical payload")
        if not self.policy_sha256:
            object.__setattr__(self, "policy_sha256", expected)
        return self
