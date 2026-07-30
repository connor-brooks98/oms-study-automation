import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from oms_hub.anki.domain import TagPatch

TagClassification = Literal[
    "pipeline_owned",
    "approved_optional",
    "source_managed",
    "unknown",
]
_CONTROL_OR_SPACE = re.compile(r"[\s\x00-\x1f\x7f]")


class TagPolicyError(ValueError):
    """A requested Anki tag edit is outside the approved policy."""


class StaleTagPatch(TagPolicyError):
    """The note's tags changed after the review patch was created."""


@dataclass(frozen=True, slots=True)
class ValidatedTagPatch:
    note_id: int
    add_tags: tuple[str, ...]
    remove_tags: tuple[str, ...]
    resulting_tags: tuple[str, ...]
    expected_tag_hash: str
    policy_version: str


class TagPolicy:
    def __init__(
        self,
        *,
        pipeline_owned_roots: tuple[str, ...],
        approved_optional_roots: tuple[str, ...],
        source_managed_roots: tuple[str, ...],
        version: str,
    ) -> None:
        if not version.strip():
            raise ValueError("tag policy version cannot be blank")
        self.pipeline_owned_roots = _normalized_roots(
            pipeline_owned_roots
        )
        self.approved_optional_roots = _normalized_roots(
            approved_optional_roots
        )
        self.source_managed_roots = _normalized_roots(
            source_managed_roots
        )
        self.version = version.strip()

    def classify(self, tag: str) -> TagClassification:
        normalized = normalize_tag(tag).casefold()
        if _matches_any(normalized, self.pipeline_owned_roots):
            return "pipeline_owned"
        if _matches_any(normalized, self.approved_optional_roots):
            return "approved_optional"
        if _matches_any(normalized, self.source_managed_roots):
            return "source_managed"
        return "unknown"

    def validate_tag_patch(
        self,
        current_tags: Iterable[str],
        patch: TagPatch,
    ) -> ValidatedTagPatch:
        if patch.note_id <= 0:
            raise TagPolicyError("tag patch note ID is invalid")
        if patch.tag_policy_version != self.version:
            raise TagPolicyError("tag patch policy version is stale")
        current = _unique_tags(current_tags)
        before = _unique_tags(patch.before)
        after = _unique_tags(patch.after)
        additions = _unique_tags(patch.add_tags)
        removals = _unique_tags(patch.remove_tags)
        if (
            tag_hash(current) != patch.expected_tag_hash
            or _canonical_set(current) != _canonical_set(before)
        ):
            raise StaleTagPatch(
                "current Anki tags do not match the reviewed patch"
            )
        add_keys = _canonical_set(additions)
        remove_keys = _canonical_set(removals)
        if add_keys & remove_keys:
            raise TagPolicyError(
                "the same tag cannot be added and removed"
            )
        expected = (
            _canonical_set(before) - remove_keys
        ) | add_keys
        if expected != _canonical_set(after):
            raise TagPolicyError(
                "tag patch does not contain an exact before/after diff"
            )
        if not additions and not removals:
            raise TagPolicyError("tag patch makes no changes")
        for tag in (*additions, *removals):
            classification = self.classify(tag)
            if classification not in {
                "pipeline_owned",
                "approved_optional",
            }:
                action = (
                    "add" if tag.casefold() in add_keys else "remove"
                )
                raise TagPolicyError(
                    f"cannot {action} {classification} tag"
                )
        return ValidatedTagPatch(
            note_id=patch.note_id,
            add_tags=additions,
            remove_tags=removals,
            resulting_tags=after,
            expected_tag_hash=patch.expected_tag_hash,
            policy_version=self.version,
        )

    def validate_initial_tags(
        self,
        tags: Iterable[str],
    ) -> tuple[str, ...]:
        normalized = _unique_tags(tags)
        for tag in normalized:
            if self.classify(tag) not in {
                "pipeline_owned",
                "approved_optional",
            }:
                raise TagPolicyError(
                    "generated note contains a non-editable initial tag"
                )
        return tuple(sorted(normalized, key=str.casefold))


def normalize_tag(tag: str) -> str:
    normalized = tag.strip()
    if (
        not normalized
        or len(normalized) > 500
        or _CONTROL_OR_SPACE.search(normalized)
        or normalized.startswith("::")
        or normalized.endswith("::")
        or "::::" in normalized
        or any(not part for part in normalized.split("::"))
    ):
        raise TagPolicyError("tag is not safe for Anki")
    return normalized


def tag_hash(tags: Iterable[str]) -> str:
    canonical = "\n".join(
        sorted(_canonical_set(_unique_tags(tags)))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unique_tags(tags: Iterable[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for value in tags:
        normalized = normalize_tag(value)
        key = normalized.casefold()
        if key in seen:
            raise TagPolicyError(
                "tag lists cannot contain case-insensitive duplicates"
            )
        seen.add(key)
        values.append(normalized)
    return tuple(values)


def _canonical_set(tags: Iterable[str]) -> set[str]:
    return {normalize_tag(tag).casefold() for tag in tags}


def _normalized_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {normalize_tag(root).casefold() for root in roots},
            key=lambda root: (-len(root), root),
        )
    )


def _matches_any(tag: str, roots: tuple[str, ...]) -> bool:
    return any(
        tag == root
        or tag.startswith(f"{root}::")
        or (
            (root.startswith("#") or root.endswith("_"))
            and tag.startswith(root)
        )
        for root in roots
    )
