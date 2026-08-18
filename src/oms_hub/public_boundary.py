"""Pure classification for the anonymous public quiz boundary."""

from dataclasses import dataclass

_ASSETS = frozenset(
    {
        "player.js",
        "player.css",
        "library.js",
        "library.css",
        "tokens.css",
        "reset.css",
        "study-hub.css",
    }
)


@dataclass(frozen=True, slots=True)
class PublicPathPolicy:
    is_public: bool
    is_canonical: bool
    category: str | None


def classify_public_path(path: str) -> PublicPathPolicy:
    """Recognize only the public route surface, including slash redirects."""
    canonical = path
    is_canonical = True
    if path.endswith("/"):
        canonical = path[:-1]
        is_canonical = False
        if not canonical or canonical.endswith("/"):
            return PublicPathPolicy(False, False, None)
    if canonical in {"/public/quizzes", "/public/practice-questions"}:
        return PublicPathPolicy(True, is_canonical, "general")
    asset_prefix = "/public/quizzes/assets/"
    if canonical.startswith(asset_prefix) and canonical[len(asset_prefix) :] in _ASSETS:
        return PublicPathPolicy(True, is_canonical, "general")
    prefix = "/public/quizzes/"
    if not canonical.startswith(prefix):
        return PublicPathPolicy(False, is_canonical, None)
    parts = canonical[len(prefix) :].split("/")
    if not parts or not parts[0] or len(parts) > 3:
        return PublicPathPolicy(False, is_canonical, None)
    if len(parts) == 1 or (
        parts[1] in {"content", "answer", "flags"} and len(parts) == 2
    ):
        return PublicPathPolicy(True, is_canonical, "general")
    if parts[1] == "outline" and len(parts) == 2:
        return PublicPathPolicy(True, is_canonical, "outline")
    if parts[1] == "media" and len(parts) == 3 and parts[2]:
        return PublicPathPolicy(True, is_canonical, "general")
    return PublicPathPolicy(False, is_canonical, None)
